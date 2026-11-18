#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""民事一审立案一键runner（配置驱动，中文案件目录）。

目录约定见 references/workspace-layout.md。spec 默认位于
`<案件目录>/05案件信息/spec.json`，会话文件落在 `<案件目录>/06运行记录/`。

    python filing_runner.py <案件目录>/05案件信息/spec.json                 # dry-run
    python filing_runner.py .../spec.json --apply                          # 写完停在提交前
    python filing_runner.py .../spec.json --apply --mediation no \
        --defendant-id-photo skip --submit --yes                            # 提交

两项决策缺一不可：
  --mediation yes|no               是否同意先行调解
  --defendant-id-photo upload|skip 是否上传被告身份证照片（身份信息照填，照片单独确认）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from court_client import (  # noqa: E402
    CASE_TYPES,
    DEFAULT_UPLOAD_BATCH,
    DEFAULT_UPLOAD_COOLDOWN,
    DEFAULT_UPLOAD_INTERVAL,
    DEFAULT_UPLOAD_JITTER,
    ROLE_CODES,
    CourtClient,
    rate_state_file_for,
    session_file_for,
    workspace_root,
)
from timing import StepTimer  # noqa: E402

SLOT_ALIAS = {
    "起诉状": "11800016-2",
    "执行申请书": "11800016-2",
    "当事人身份证明": "11800016-1",
    "申请人身份材料": "11800016-1",
    "代理人手续": "11800016-9",
    "授权委托书及代理人身份证明": "11800016-9",
    "执行依据文书": "11800016-8",
    "证据": "11800016-4",
    "送达地址确认书": "11800016-254",
    "账户确认书": "11800016-36",
    "收款账户确认书": "11800016-36",
    "其他材料": "11800016-3",
}
EXTRA_COPY = {"账户确认书": "其他材料"}
PARTY_ROLE = ROLE_CODES["sp"]  # 兼容旧引用；实际按 case_type 走 roles_of()


def die(msg: str) -> None:
    print(f"[ERROR] {msg}")
    raise SystemExit(1)


def roles_of(case_type: str) -> Dict[str, str]:
    """诉讼地位编码：一审 030109 族，执行 100225 族。"""
    return ROLE_CODES.get(case_type) or ROLE_CODES["sp"]


def role_label(case_type: str, role: str) -> str:
    if case_type == "zx":
        return "申请执行人" if role == "plaintiff" else "被执行人"
    return "原告" if role == "plaintiff" else "被告"


def plaintiffs_of(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """原告列表：优先 `plaintiffs`（多原告），回退单数 `plaintiff`（历史 spec）。"""
    parties = spec.get("plaintiffs")
    if isinstance(parties, list) and parties:
        return [p for p in parties if p]
    return [spec["plaintiff"]] if spec.get("plaintiff") else []


def defendants_of(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """被告列表：优先 `defendants`（多被告），回退单数 `defendant`（历史 spec）。"""
    parties = spec.get("defendants")
    if isinstance(parties, list) and parties:
        return [p for p in parties if p]
    return [spec["defendant"]] if spec.get("defendant") else []


def party_names(spec: Dict[str, Any], role: str) -> str:
    parties = plaintiffs_of(spec) if role == "plaintiff" else defendants_of(spec)
    return "、".join(str(p.get("name") or "") for p in parties if p.get("name"))


def load_spec(path: Path) -> Dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    for key in ("court", "case", "materials"):
        if key not in spec:
            die(f"spec 缺少必填段：{key}")
    case_type = str(spec.get("case_type") or "sp")
    if case_type not in CASE_TYPES:
        die(f"未知 case_type：{case_type}（可选 {'/'.join(CASE_TYPES)}）")
    if case_type == "sp" and not str(spec["case"].get("cause") or "").strip():
        die("spec.case.cause 不能为空（民事一审必须有案由）")
    if case_type == "zx":
        zxyj = spec.get("zxyj") or {}
        if not str(zxyj.get("ah") or "").strip():
            die("执行案件必须在 spec.zxyj.ah 填执行依据文书文号")
        if not str(zxyj.get("ysajbh") or "").strip():
            die("执行案件必须在 spec.zxyj.ysajbh 填原审案件 id（可用 list_ysaj 查）")
    if not plaintiffs_of(spec):
        die("spec 缺少原告（plaintiffs 列表 或 单数 plaintiff）")
    if not defendants_of(spec):
        die("spec 缺少被告（defendants 列表 或 单数 defendant）")
    if not spec["materials"]:
        die("materials 为空")
    claims = spec.get("claims") or {}
    if not claims.get("ssqq") or not claims.get("ssly"):
        die("claims.ssqq / claims.ssly 不能为空（否则提交时报“起诉状不能为空”）")
    # 材料路径统一转绝对路径（历史 spec 里写成 `案件库\...` 相对路径，换目录就跑不起来）
    root = workspace_root(path.parent.parent.parent)
    for item in spec["materials"]:
        resolved = []
        for raw in item.get("files", []):
            candidate = Path(raw)
            if not candidate.is_absolute():
                for base in (root, Path.cwd()):
                    if (base / candidate).exists():
                        candidate = base / candidate
                        break
                else:
                    candidate = root / candidate
            if not candidate.exists():
                die(f"材料文件不存在：{raw}")
            resolved.append(str(candidate))
        item["files"] = resolved
    return spec


def save_spec(path: Path, spec: Dict[str, Any]) -> None:
    """回写 spec（目前只写 filing_id，保证复跑不会重复建档）。"""
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_back_filing_id(spec_path: Path, spec: Dict[str, Any], filing_id: str) -> None:
    if str(spec.get("filing_id") or "") == str(filing_id):
        return
    previous = str(spec.get("filing_id") or "")
    spec["filing_id"] = filing_id
    save_spec(spec_path, spec)
    print(f"已把 filing_id 写回 spec：{filing_id}（复跑会复用，不会重复建档）")
    _bind_history(spec_path.parent.parent, filing_id, previous)


def _bind_history(case_dir: Path, filing_id: str, previous: str = "") -> None:
    """把新 filing_id 绑到案件上，旧单封存进历史链（见 bind_filing.py）。

    重新立案会拿到**新的** layy id，如果只是覆盖 case.json 顶层字段，
    上一单的驳回意见和案号就永久丢了 —— 所以换单必须走绑定链。
    """
    try:
        from bind_filing import bind_new, load

        case = load(case_dir)
        if not case:
            return
        old = str(case.get("filing_id") or "") or previous
        if old and old != filing_id:
            result = bind_new(case_dir, filing_id, f"重新提交立案（原单 {old}）", case)
            if result.get("superseded"):
                print(f"立案单已换绑：{result['superseded']} → {filing_id}（旧单转入历史）")
        else:
            bind_new(case_dir, filing_id, "首次建档", case)
    except Exception as exc:  # 绑定失败不该拦住立案主流程
        print(f"[WARN] 立案单绑定链未更新：{exc}")


STAGE_ORDER = {"create": 0, "fill": 1, "upload": 2, "all": 3}


def stage_at_least(stage: str, target: str) -> bool:
    return STAGE_ORDER.get(stage, 3) >= STAGE_ORDER[target]


def _refresh_note(case_dir: Path, spec_path: Path, filing_id: str,
                  client: CourtClient | None = None, fyid: str = "") -> None:
    """把 filing_id / 排期数写进 case.json，再刷新 案件说明.md。"""
    case_json = spec_path.parent / "case.json"
    if case_json.exists():
        case = json.loads(case_json.read_text(encoding="utf-8"))
        if str(case.get("filing_id") or "") != str(filing_id):
            # 走绑定链，别直接盖掉旧单（重新立案会换 id）
            _bind_history(case_dir, filing_id, str(case.get("filing_id") or ""))
            case = json.loads(case_json.read_text(encoding="utf-8"))
        case["filing_id"] = filing_id
        if client and fyid:
            try:
                case["court_queue"] = client.get_court_queue(fyid)
                case["court_queue_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        status = _remote_status(client, filing_id) if client else ""
        if status:
            case["status"] = status
        case_json.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from write_case_note import update as update_case_note
        update_case_note(case_dir)
    except Exception as exc:
        print(f"[WARN] 案件说明.md 未更新：{exc}")


def _remote_status(client: CourtClient, filing_id: str) -> str:
    try:
        return client.get_filing_status(filing_id)["status_text"]
    except Exception:
        return ""


def record_decisions(spec_path: Path, decisions: Dict[str, str]) -> None:
    """把用户决策写进 05案件信息/case.json，留痕可追溯。"""
    case_json = spec_path.parent / "case.json"
    if not case_json.exists():
        return
    case = json.loads(case_json.read_text(encoding="utf-8"))
    stamped = {k: {"value": v, "at": datetime.now().isoformat(timespec="seconds")}
               for k, v in decisions.items() if v}
    case.setdefault("decisions", {}).update(stamped)
    case_json.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")


def party_payload(src: Dict[str, Any], role: str, filing_id: str, fyid: str, ajlx: str,
                  case_type: str = "sp") -> Dict[str, Any]:
    """按角色构造单个当事人 payload；`src` 是该当事人的信息 dict（支持多原告/多被告逐个存）。

    `src.entity=true`（或 `type="法人"`）走法人分支：字段用 dwmc/dwzsd/zzhm/fddbrxm，
    证件类型是「统一社会信用代码证」而不是居民身份证。
    """
    ssdw = roles_of(case_type)[role]
    is_entity = bool(src.get("entity")) or str(src.get("type") or "") in {"法人", "单位"}
    if is_entity:
        return {
            "layyid": filing_id, "fyId": fyid, "ajlx": ajlx,
            "ssdw": ssdw,
            "dsrlx": "1501_000011-2", "cdsrlx": "法人",
            "dwmc": src["name"],
            "dwzsd": src.get("address", ""),
            "gj": "1501_GB0006-156", "cgj": "中国",
            "zzlx": "1501_000015-7", "czzlx": "统一社会信用代码证",
            "zzhm": src.get("id_card", ""),
            "fddbrxm": src.get("legal_rep", ""),
            "fddbrsjhm": src.get("legal_rep_phone") or src.get("phone", ""),
            "sjhm": src.get("phone", ""),
            "sfsqr": "1501_000010-1" if role == "plaintiff" else "1501_000010-2",
        }
    body = {
        "layyid": filing_id, "fyId": fyid, "ajlx": ajlx,
        "ssdw": ssdw,
        "dsrlx": "1501_000011-1", "cdsrlx": "自然人",
        "xm": src["name"],
        "sjhm": src.get("phone", ""),
        "dz": src.get("address", ""),
        "gj": "1501_GB0006-156", "cgj": "中国",
        "sfsqr": "1501_000010-1" if role == "plaintiff" else "1501_000010-2",
    }
    # 执行案件：申请执行人的「住所地（户籍所在地）」hjszd 是必填（实测页面标 *），
    # 只写 dz 会显示"待补全"，且提交被拒（RMFY109905）。
    if case_type == "zx":
        body["ssdwMc"] = "申请执行人" if role == "plaintiff" else "被执行人"
        body["ssdwType"] = "yg" if role == "plaintiff" else "bg"
        body["dsrType"] = "zrr"
        if role == "plaintiff":
            body["hjszd"] = src.get("domicile") or src.get("address", "")
    if src.get("id_card"):
        body.update({
            "zjlx": "1501_000015-1", "czjlx": "居民身份证", "zjhm": src["id_card"],
            "xb": "1501_GB0001-1" if src.get("gender") == "男" else "1501_GB0001-2",
            "cxb": src.get("gender", ""),
            "csrq": src.get("birth", ""), "ccsrq": src.get("birth", ""),
            "nl": src.get("age"),
            "mz": "1501_GB0002-01", "cmz": src.get("ethnicity", "汉族"),
        })
    return body


def run(spec_path: Path, apply: bool, submit: bool, mediation: str, id_photo: str,
        stage: str = "all", rate: float = 1.0, skip_preflight: bool = False,
        upload_interval: float | None = None, upload_jitter: float | None = None,
        upload_batch: int | None = None, upload_cooldown: float | None = None,
        case_type: str = "", user_type: str = "") -> None:
    spec = load_spec(spec_path)
    case_dir = spec_path.parent.parent
    session_file = session_file_for(case_dir)
    case_type = case_type or str(spec.get("case_type") or "sp")
    user_type = user_type or str(spec.get("user_type") or "lsyh")
    is_zx = case_type == "zx"

    fyid = str(spec["court"]["fyid"])
    region = str(spec["court"].get("region", ""))
    decisions = {"mediation": mediation, "defendant_id_photo": id_photo}

    # ---- 离线门禁：材料没过就不许碰法院（不建草稿、不上传）----
    if not skip_preflight:
        from preflight_materials import check as preflight_check

        gate = preflight_check(case_dir, case_type=case_type)
        if not gate["ok"]:
            print("材料门禁未通过，未创建任何草稿：")
            for item in gate["errors"]:
                print(f"  [!!] {item}")
            if apply:
                raise SystemExit(2)
            print("  （dry-run 继续展示计划）")
        else:
            print(f"材料门禁通过：{gate['counts']['files']} 份 / {gate['counts']['slots']} 槽")
        for item in gate.get("warnings") or []:
            print(f"  [警告] {item}")

    if not apply:
        preset = spec.get("decisions") or {}
        print("[dry-run] 计划：")
        print(f"  案件目录：{case_dir}")
        print(f"  阶段：{stage}  限速：{rate:g} 请求/秒（全局）")
        interval = upload_interval if upload_interval is not None else DEFAULT_UPLOAD_INTERVAL
        jitter = upload_jitter if upload_jitter is not None else DEFAULT_UPLOAD_JITTER
        batch = upload_batch if upload_batch is not None else DEFAULT_UPLOAD_BATCH
        cooldown = upload_cooldown if upload_cooldown is not None else DEFAULT_UPLOAD_COOLDOWN
        print(f"  上传节奏：每份 {interval:g}s±{jitter:g}s，每 {batch} 份停 {cooldown:g}s（跨进程串行）")
        print(f"  案件类型：{case_type}（{'强制执行申请' if is_zx else '民事一审'}）"
              f"  登录账号类型：{user_type}")
        if is_zx:
            zxyj = spec.get("zxyj") or {}
            print(f"  法院 fyid={fyid} region={region} 执行依据={zxyj.get('lb')} {zxyj.get('ah')}")
            print(f"  原审：{zxyj.get('ysfymc')} {zxyj.get('ysajah')}")
        else:
            print(f"  法院 fyid={fyid} region={region} 案由={spec['case']['cause']} "
                  f"标的额={spec['case'].get('amount')}")
        print(f"  申请执行人={party_names(spec, 'plaintiff')} 被执行人={party_names(spec, 'defendant')}"
              if is_zx else f"  原告={party_names(spec, 'plaintiff')} 被告={party_names(spec, 'defendant')}")
        print(f"  已有 filing_id={spec.get('filing_id') or '（无，将新建草稿）'}")
        if is_zx:
            print("  决策·先行调解=不适用（执行案件无先行调解）")
        else:
            print(f"  决策·先行调解={mediation or preset.get('mediation') or '未定'}")
        print(f"  决策·被告身份证照片={id_photo or preset.get('defendant_id_photo') or '未定'}")
        for item in spec["materials"]:
            print(f"  材料[{item.get('slot')}] <- {[Path(f).name for f in item.get('files', [])]}")
        print("  加 --apply 才会真正调用法院接口")
        return

    c = CourtClient(base_url=spec.get("base_url", "https://zxfw.court.gov.cn"),
                    username=os.getenv("COURT_USERNAME", ""),
                    password=os.getenv("COURT_PASSWORD", ""),
                    user_type=user_type,
                    session_file=str(session_file),
                    rate=rate, rate_state_file=str(rate_state_file_for(case_dir)),
                    interactive=False,
                    # 上传走单独慢通道：串行 + 间隔 + 抖动 + 批间长停
                    upload_interval=upload_interval,
                    upload_jitter=upload_jitter,
                    upload_batch=upload_batch,
                    upload_cooldown=upload_cooldown,
                    # --submit 本身已要求 --yes，这里就不要再卡一次 input()（无 stdin 会抛 EOFError）
                    confirm_hook=(lambda _prompt: True) if submit else None)
    c.set_cache_dir(case_dir / "05案件信息" / "cache")
    c.ensure_login()
    info = c.get_user_info()
    scope = info.get("cLszyzh") and f"（执业证 {info.get('cLszyzh')}）" or ""
    print(f"登录用户：{info.get('cName')}{scope} 账号类型={user_type}")

    timer = StepTimer(case_dir)
    started_at = time.time()

    def _finish(reason: str) -> None:
        elapsed = time.time() - started_at
        timer.record("total", elapsed, stage=stage, reason=reason, requests=c.request_count)
        print(f"── 本次 {elapsed:.1f}s / {c.request_count} 次请求（{reason}）")

    filing_id = spec.get("filing_id") or ""
    ajlx = ""
    if filing_id:
        ajlx = str(c.get_filing_detail(filing_id).get("data", {}).get("layy", {}).get("ajlx") or "")
        print(f"复用草稿：{filing_id}（不重复建档）")
    else:
        if is_zx:
            ajlx = str((spec.get("case") or {}).get("ajlx") or CASE_TYPES["zx"]["ajlx"])
        else:
            entry = c.get_civil_entry(region) if region else {"ajlx": spec["case"].get("ajlx", "")}
            ajlx = str(entry.get("ajlx") or "")
            if not ajlx:
                die("未能从类型树拿到民事一审 ajlx，请在 spec 里显式给 case.ajlx")
        res = c.create_filing(fyid, region_code=region, case_type=case_type,
                              profile={"ajlx": ajlx})
        data = res.get("data")
        filing_id = data if isinstance(data, str) else (data or {}).get("id", "")
        print(f"创建草稿：{filing_id}（{case_type}）")
        _write_back_filing_id(spec_path, spec, filing_id)

    if stage == "create":
        _refresh_note(case_dir, spec_path, filing_id, c, fyid)
        print(f"已停在「仅建草稿」。filing_id={filing_id}")
        _finish("create")
        return

    if stage == "submit":
        # 材料已传完、只想走“回读 + 提交”时用这个，避免重复上传
        _finalize(c, case_dir, spec_path, filing_id, fyid, mediation, id_photo, submit, _finish,
                  case_type=case_type)
        return

    if is_zx:
        _save_exec_basis(c, spec, filing_id, fyid)
    else:
        c.save_cause(filing_id, spec["case"]["cause"])
        print(f"案由：{spec['case']['cause']}")

    # 重跑幂等：法院禁止“有代理人关联时删当事人”，所以先删旧代理人
    fresh = c.get_filing_detail(filing_id, refresh=True).get("data") or {}
    for old_agent in fresh.get("dlrs") or []:
        if old_agent.get("id"):
            c.delete_agent(filing_id, old_agent["id"])
            print(f"已移除旧代理人记录：{old_agent.get('xm') or old_agent['id']}")
    c.invalidate_parties(filing_id)
    for p in c.list_parties(filing_id, ajlx):
        c.delete_party(filing_id, p["id"])
    for p in plaintiffs_of(spec):
        c.save_party(party_payload(p, "plaintiff", filing_id, fyid, ajlx, case_type))
        print(f"当事人：{p['name']} 已保存（{role_label(case_type, 'plaintiff')}）")
    for p in defendants_of(spec):
        c.save_party(party_payload(p, "defendant", filing_id, fyid, ajlx, case_type))
        print(f"当事人：{p['name']} 已保存（{role_label(case_type, 'defendant')}）")
    c.sync_dsr_mc(filing_id, ajlx, case_type=case_type)
    print(f"参与人展示串：{role_label(case_type, 'plaintiff')}/{role_label(case_type, 'defendant')}前缀已同步")

    agent = spec.get("agent") or {}
    # 执行案件本人申请默认不建代理人；律师代为申请时才开
    agent_default = False if is_zx else True
    if agent.get("enabled", agent_default):
        plaintiff_id = next((p["id"] for p in c.list_parties(filing_id, ajlx)
                             if p.get("ssdw") == roles_of(case_type)["plaintiff"]), "")
        c.save_agent({
            "layyid": filing_id, "fyId": fyid, "ajlx": ajlx,
            "dlrlx": "1501_000013-1", "cdlrlx": "执业律师",
            "xm": agent.get("name") or info.get("cName", ""),
            "zjlx": "1501_000015-1",
            "zjhm": agent.get("id_card") or info.get("cZjhm", ""),
            "zyzh": agent.get("license") or info.get("cLszyzh", ""),
            "zyjg": agent.get("firm", ""),
            "sjhm": agent.get("phone") or info.get("cPhone", ""),
            "bdlrid": plaintiff_id, "bdlrMc": (plaintiffs_of(spec) or [{}])[0].get("name", ""),
            "gj": "1501_GB0006-156", "cgj": "中国", "sfsqr": "1501_000010-2",
        })
        print("代理人已保存")

    if is_zx:
        # 执行案件没有案由/标的额，只有执行请求与执行理由
        print("案由/标的额：不适用（执行案件）")
    else:
        c.patch_layy({"id": filing_id, "laay": spec["case"]["cause"],
                      "laayMz": (c.find_cause(spec["case"]["cause"]) or {}).get("id", ""),
                      "sqbdje": str(spec["case"].get("amount", "")),
                      "ajmc": spec["case"].get("ajmc", "")})
        print(f"标的额：{spec['case'].get('amount')}")

    claims = spec["claims"]
    if is_zx:
        # 执行请求/理由已在 _save_exec_basis 写进 zxyjs[0].zxqq / .zxyjzw，不走一审 ssqq 接口
        print("执行请求与执行理由：已随执行依据保存（zxyjs[0].zxqq / zxyjzw）")
    else:
        c.save_claims({"layyid": filing_id, "ssqq": claims["ssqq"], "ssly": claims["ssly"]})
        print("诉请与事实理由已保存")

    if stage == "fill":
        _refresh_note(case_dir, spec_path, filing_id, c, fyid)
        print(f"已停在「字段填完、未上传材料」。filing_id={filing_id}")
        _finish("fill")
        return

    slots = {s["cllx"]: s for s in c.get_filing_detail(filing_id)["data"]["sscls"]}
    for item in spec["materials"]:
        cllx = item.get("cllx") or SLOT_ALIAS.get(item.get("slot", ""), "")
        if cllx not in slots:
            die(f"未知材料槽：{item.get('slot')}，请显式给 cllx")
        files = [Path(f) for f in item.get("files", [])]
        if id_photo == "skip":
            kept = [f for f in files if "被告身份证" not in f.name]
            if len(kept) != len(files):
                print("  已按决策跳过被告身份证照片")
            files = kept
        if not files:
            continue
        _upload(c, filing_id, fyid, slots[cllx], files, item.get("slot", ""))
        extra = EXTRA_COPY.get(item.get("slot", ""))
        if extra and slots.get(SLOT_ALIAS[extra]):
            _upload(c, filing_id, fyid, slots[SLOT_ALIAS[extra]], files, extra, prefix="其他材料-")
            print(f"  账户确认书已额外复制到「{extra}」")

    if stage == "upload":
        _refresh_note(case_dir, spec_path, filing_id, c, fyid)
        print(f"已停在「材料传完、未预检」。filing_id={filing_id}")
        _finish("upload")
        return

    _finalize(c, case_dir, spec_path, filing_id, fyid, mediation, id_photo, submit, _finish)



def _save_exec_basis(c: CourtClient, spec: Dict[str, Any], filing_id: str, fyid: str) -> None:
    """执行立案必填：原审信息 + 执行依据/执行标的（实测两个 PATCH）。

    执行请求与执行理由挂在 `zxyjs[0].zxqq` / `zxyjs[0].zxyjzw` 上，**不是**一审判的 `ssqq`/`ssly`。
    只写 ssqq 会导致提交报 `RMFY109905 提交网上立案信息失败`。
    """
    zxyj = dict(spec.get("zxyj") or {})
    claims = spec.get("claims") or {}
    zxyj.setdefault("zxqq", claims.get("ssqq", ""))
    zxyj.setdefault("zxyjzw", claims.get("ssly", ""))
    c.save_ysxx(filing_id, fyid, {
        "ysfyid": zxyj.get("ysfyid") or zxyj.get("jbjg"),
        "ysfymc": zxyj.get("ysfymc") or zxyj.get("jbjgMc"),
        "ysajbh": zxyj.get("ysajbh"),
        "ysajAjbs": zxyj.get("ysajAjbs"),
        "ysajah": zxyj.get("ysajah") or zxyj.get("ah"),
    })
    print(f"原审信息：{zxyj.get('ysajah') or zxyj.get('ah')} 已保存")
    c.save_zxyj(filing_id, fyid, zxyj)
    print(f"执行依据：{zxyj.get('lb')} / {zxyj.get('ah')} 已保存")
    bd_zl = zxyj.get("bd_zl") or ""
    if isinstance(bd_zl, (list, tuple)):
        bd_zl = ",".join(str(x) for x in bd_zl)
    if bd_zl:
        c.patch_layy({"id": filing_id, "sqzxbdzl": bd_zl})
        print(f"执行标的类型：{bd_zl}（金额 {zxyj.get('bd_je') or '-'}）")


def _finalize(c: CourtClient, case_dir: Path, spec_path: Path, filing_id: str, fyid: str,
              mediation: str, id_photo: str, submit: bool, finish,
              case_type: str = "sp") -> None:
    """提交前回读 + 两项闸门 + （可选）提交。抽出来是为了 `--stage submit` 能直接用，
    不必重跑填字段和上传（重跑上传会在法院端重复挂同一批材料）。

    执行案件没有先行调解，跳过调解闸门，只保留被告身份证照片那一问。
    """
    is_zx = case_type == "zx"
    decisions = {"mediation": mediation, "defendant_id_photo": id_photo}
    if is_zx:
        print("=== 先行调解：不适用（执行案件无先行调解）===")
    else:
        notice = c.get_mediation_notice(fyid)
        print("=== 先行调解（须用户决定）===")
        print(f"法院排期在前的案件：{notice['queue']} 件")
        print(notice["prompt"])
        if notice["title"]:
            print(f"[{notice['title']}]")
        if notice["text"]:
            print(notice["text"][:600])

        if mediation not in ("yes", "no"):
            if notice["need_mediation"]:
                die("必须由用户决定是否同意先行调解：--mediation yes|no")
            mediation = "no"
        decisions["mediation"] = mediation
        print(f"先行调解：{'同意' if mediation == 'yes' else '不同意'}（提交时随 layy 一并写入）")

    print("=== 被告/被执行人身份证照片（须用户决定）===")
    print("身份信息（证件号）已照填；照片是否上传需单独确认。")
    if id_photo not in ("upload", "skip"):
        if is_zx:
            id_photo = "skip"
            print("执行案件未指定，默认按「不上传」处理（如需上传请显式给 --defendant-id-photo upload）")
        else:
            die("必须由用户决定是否上传被告身份证照片：--defendant-id-photo upload|skip")
    print(f"被告身份证照片：{'上传' if id_photo == 'upload' else '不上传'}")
    decisions["defendant_id_photo"] = id_photo

    # 一次性回读法院真值 + 自查（confirm_snapshot 内部已含 precheck，不再单独跑一遍）
    snap = c.confirm_snapshot(filing_id, decisions, case_type=case_type)
    problems = snap.get("problems") or []
    if problems:
        die("提交前自查未通过：" + "；".join(problems))
    print("=== 法院端实际存储（回读，非本地值）===")
    if is_zx:
        print(f"  执行依据：{snap['cause'].get('zxyjAh')} {'OK' if snap['cause'].get('display_ok') else '[!!] 缺失'}")
    else:
        print(f"  案由：{snap['cause']['laay']} / 节点 {snap['cause']['laayMz']}"
              f" {'OK' if snap['cause']['display_ok'] and snap['cause']['id_ok'] else '[!!] 字段写错'}")
        print(f"  标的额：{snap['amount']}    案名：{snap['ajmc']}")
    print(f"  参与人：{snap['dsrMc']['value']}")
    print(f"  材料：{sum(len(m['files']) for m in snap['materials'])} 份，"
          f"缺项 {sum(1 for m in snap['materials'] if not m['ok'])}")
    record_decisions(spec_path, decisions)
    print("提交前自查通过，决策已写入 case.json")

    if not submit:
        print(f"已停在提交前。filing_id={filing_id}")
        _refresh_note(case_dir, spec_path, filing_id, c, fyid)
        finish("before-submit")
        return
    c.submit(filing_id, decisions, skip_precheck=True, case_type=case_type)
    after = c.get_filing_detail(filing_id, refresh=True)["data"]["layy"]
    print(f"已提交：filing_id={filing_id} zt={after.get('zt')} tjsj={after.get('tjsj')}")
    _refresh_note(case_dir, spec_path, filing_id, c, fyid)
    finish("submitted")


def _upload(c: CourtClient, filing_id: str, fyid: str, slot: Dict[str, Any],
            files: List[Path], slot_name: str, prefix: str = "") -> None:
    for idx, path in enumerate(files, start=1):
        shown = prefix + path.name
        c.upload_material(
            filing_id, fyid,
            {"court_material_code": slot["cllx"], "court_material_id": slot["id"],
             "court_material_name": slot_name, "court_material_order": idx},
            str(path), shown,
            "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg",
        )
        print(f"  材料 {slot_name}: {shown}")


def main() -> int:
    ap = argparse.ArgumentParser(description="立案 runner（民事一审 sp / 强制执行 zx）")
    ap.add_argument("spec", help="spec.json（通常在 <案件目录>/05案件信息/）")
    ap.add_argument("--apply", action="store_true", help="真正调用法院接口（默认 dry-run）")
    ap.add_argument("--submit", action="store_true", help="写完并最终提交")
    ap.add_argument("--yes", action="store_true", help="提交确认（配合 --submit）")
    ap.add_argument("--case-type", choices=list(CASE_TYPES), default="",
                    help="sp=民事一审（默认） / zx=强制执行申请；不填读 spec.case_type")
    ap.add_argument("--user-type", default="",
                    help="登录账号类型：lsyh=律师（默认） / gryh=个人 / fryh=法人；不填读 spec.user_type")
    ap.add_argument("--mediation", choices=["yes", "no"], default="",
                    help="是否同意先行调解（用户决定）")
    ap.add_argument("--defendant-id-photo", choices=["upload", "skip"], default="",
                    help="是否上传被告身份证照片（用户决定）")
    ap.add_argument("--stage", choices=["create", "fill", "upload", "submit", "all"], default="all",
                    help="执行到哪一步就停：create 只建档 / fill 填字段 / upload 传材料 / submit 只回读+提交 / all 全流程")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="全局请求速率（次/秒，跨进程共享）默认 1.0；调高等于放宽法院风控红线")
    ap.add_argument("--upload-interval", type=float, default=None,
                    help=f"材料上传间隔秒数，默认 {DEFAULT_UPLOAD_INTERVAL}；调小更易撞 OSS 403")
    ap.add_argument("--upload-jitter", type=float, default=None,
                    help=f"上传间隔随机抖动上限秒，默认 {DEFAULT_UPLOAD_JITTER}")
    ap.add_argument("--upload-batch", type=int, default=None,
                    help=f"每传 N 份材料长停一次，默认 {DEFAULT_UPLOAD_BATCH}；0 关闭")
    ap.add_argument("--upload-cooldown", type=float, default=None,
                    help=f"批间长停秒数，默认 {DEFAULT_UPLOAD_COOLDOWN}")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="跳过离线材料门禁（不建议：门禁就是用来避免建档后返工的）")
    args = ap.parse_args()

    if args.submit and not args.yes:
        die("--submit 需要同时给 --yes（提交不可逆）")
    run(Path(args.spec).resolve(), args.apply, args.submit, args.mediation, args.defendant_id_photo,
        stage=args.stage, rate=args.rate, skip_preflight=args.skip_preflight,
        upload_interval=args.upload_interval, upload_jitter=args.upload_jitter,
        upload_batch=args.upload_batch, upload_cooldown=args.upload_cooldown,
        case_type=args.case_type, user_type=args.user_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
