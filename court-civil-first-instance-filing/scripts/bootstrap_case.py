#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""初始化案件工作目录（幂等、中文目录）。

工作空间根 = 用户在 WorkBuddy 里选的那个文件夹。只在其内建目录树，不跨盘另开。

    <根>/
      案件库/<YYYYMMDD>-<fyid>-<原告>诉<被告>-<案由>/
        01原始包/  02签字件/  03图片件/  04未签稿/  05案件信息/  06运行记录/
        案件说明.md
      通用件/
      .workbuddy/

用法：
    python bootstrap_case.py --root "D:/court-cases" \
        --fyid 0000 --court "某某市某某区人民法院" --region 000000 \
        --cause "民间借贷纠纷" --plaintiff "张三" --defendant "李四"
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

CASE_LIB = "案件库"
SHARED = "通用件"

SUBDIRS = {
    "01原始包": "客户交付的压缩包与解压结果（只读，不作上传）",
    "02签字件": "已签字/盖章的 PDF —— 可直接上传",
    "03图片件": "身份证、借条、截图等 jpg/png",
    "04未签稿": "Word 文档 + 尚未签字的 PDF",
    "05案件信息": "case.json / spec.json / draft.json / 提交结果",
    "06运行记录": "会话文件、验证码截图、运行日志（敏感，勿外发）",
}

README_TEMPLATE = """# {plaintiff} 诉 {defendant} {cause}

- 受理法院：{court}（fyid={fyid}，region={region}）
- 案由：{cause}
- 案件目录：`{case_dir}`
- 状态：已初始化

## 目录

| 目录 | 用途 |
| --- | --- |
| `01原始包/` | 客户原始压缩包 + `解压/` 展开结果，只读 |
| `02签字件/` | 已签字盖章的 PDF，直接上传用 |
| `03图片件/` | 身份证、借条、截图 |
| `04未签稿/` | Word 文档与未签字 PDF |
| `05案件信息/` | spec / 草稿 id / 提交结果 / 决策记录 |
| `06运行记录/` | 会话、验证码、日志（勿外发） |

## 进度

- [ ] 材料整理与签字核对
- [ ] 当事人/代理人/案由/标的额
- [ ] 材料上传（账户确认书需同时进「其他材料」）
- [ ] 诉请与事实理由
- [ ] 必问答疑：是否同意先行调解
- [ ] 必问答疑：是否上传被告身份证照片
- [ ] 预检通过并提交
"""

SPEC_TEMPLATE: Dict[str, Any] = {
    "base_url": "https://zxfw.court.gov.cn",
    "case_type": "sp",
    "user_type": "lsyh",
    "court": {"fyid": "", "region": ""},
    "case": {"cause": "", "amount": "", "ajmc": ""},
    "plaintiff": {"name": "", "gender": "", "birth": "", "age": 0,
                  "id_card": "", "phone": "", "address": ""},
    "defendant": {"name": "", "gender": "", "birth": "", "age": 0,
                  "id_card": "", "phone": "", "address": ""},
    "agent": {"enabled": True, "firm": ""},
    "decisions": {"mediation": None, "defendant_id_photo": None},
    "claims": {"ssqq": "", "ssly": ""},
    "materials": [
        {"slot": "起诉状", "files": []},
        {"slot": "当事人身份证明", "files": []},
        {"slot": "代理人手续", "files": []},
        {"slot": "证据", "files": []},
        {"slot": "送达地址确认书", "files": []},
        {"slot": "账户确认书", "files": []},
    ],
}

# 强制执行申请：无案由/标的额，改为执行依据 + 原审案件；材料槽位与一审不同
EXEC_SPEC_TEMPLATE: Dict[str, Any] = {
    "base_url": "https://zxfw.court.gov.cn",
    "case_type": "zx",
    "user_type": "gryh",
    "court": {"fyid": "", "region": ""},
    "case": {"cause": "", "amount": "", "ajmc": ""},
    "plaintiff": {"name": "", "gender": "", "birth": "", "age": 0,
                  "id_card": "", "phone": "", "address": ""},
    "defendant": {"name": "", "gender": "", "birth": "", "age": 0,
                  "id_card": "", "phone": "", "address": ""},
    "agent": {"enabled": False, "firm": ""},
    "decisions": {"mediation": None, "defendant_id_photo": None},
    "claims": {"ssqq": "", "ssly": ""},
    "zxyj": {
        "lb": "民商", "ah": "", "jbjg": "", "jbjgMc": "",
        "ysfyid": "", "ysfymc": "", "ysajbh": "", "ysajAjbs": "", "ysajah": "",
        "bd_zl": ["1501_100279-1"], "bd_je": "",
    },
    "materials": [
        {"slot": "执行申请书", "files": []},
        {"slot": "执行依据文书", "files": []},
        {"slot": "申请人身份材料", "files": []},
        {"slot": "授权委托书及代理人身份证明", "files": []},
        {"slot": "送达地址确认书", "files": []},
        {"slot": "收款账户确认书", "files": []},
    ],
}


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", text or "").strip() or "未命名"


def split_names(text: str) -> List[str]:
    """按 、，, 拆分多当事人姓名（多原告/多被告）。"""
    parts = re.split(r"[、，,]", text or "")
    return [p.strip() for p in parts if p.strip()]


def build_case_dir(root: Path, fyid: str, plaintiff: str, defendant: str, cause: str, date: str,
                   case_type: str = "sp") -> Path:
    verb = "申请执行" if case_type == "zx" else "诉"
    tail = f"-{safe_name(cause)}" if str(cause or "").strip() else ""
    name = f"{date}-{fyid}-{safe_name(plaintiff)}{verb}{safe_name(defendant)}{tail}"
    return root / CASE_LIB / name


def bootstrap(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).expanduser().resolve()
    date = args.date or datetime.now().strftime("%Y%m%d")
    plaintiffs = split_names(args.plaintiff)
    defendants = split_names(args.defendant)
    if not plaintiffs:
        plaintiffs = [args.plaintiff]
    if not defendants:
        defendants = [args.defendant]
    case_dir = build_case_dir(root, args.fyid, "、".join(plaintiffs), "、".join(defendants),
                              args.cause, date,
                              case_type=str(getattr(args, "case_type", "") or "sp"))

    created: List[str] = []
    (root / CASE_LIB).mkdir(parents=True, exist_ok=True)
    (root / SHARED).mkdir(parents=True, exist_ok=True)

    for sub, note in SUBDIRS.items():
        target = case_dir / sub
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(f"{sub}/")
        keep = target / ".说明.txt"
        if not keep.exists():
            keep.write_text(note + "\n", encoding="utf-8")

    state = case_dir / "05案件信息"
    case_json = state / "case.json"
    now = datetime.now().isoformat(timespec="seconds")
    if case_json.exists():
        case = json.loads(case_json.read_text(encoding="utf-8"))
        case["updated_at"] = now
    else:
        case = {
            "created_at": now,
            "court": {"fyid": args.fyid, "name": args.court, "region": args.region},
            "cause": args.cause,
            "plaintiff": "、".join(plaintiffs),
            "defendant": "、".join(defendants),
            "filing_id": "",
            "status": "initialized",
            "decisions": {"mediation": None, "defendant_id_photo": None},
            "paths": {
                "source": "01原始包", "signed": "02签字件", "images": "03图片件",
                "drafts": "04未签稿", "state": "05案件信息", "runtime": "06运行记录",
            },
        }
        case_json.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append("05案件信息/case.json")

    spec_path = state / "spec.json"
    case_type = str(getattr(args, "case_type", "") or "sp")
    template = EXEC_SPEC_TEMPLATE if case_type == "zx" else SPEC_TEMPLATE
    if not spec_path.exists():
        spec = json.loads(json.dumps(template))
        spec["court"] = {"fyid": args.fyid, "region": args.region}
        spec["case"]["cause"] = args.cause
        spec["plaintiff"]["name"] = plaintiffs[0]
        spec["defendant"]["name"] = defendants[0]
        if len(plaintiffs) > 1:
            spec["plaintiffs"] = [dict(template["plaintiff"], name=n) for n in plaintiffs]
        if len(defendants) > 1:
            spec["defendants"] = [dict(template["defendant"], name=n) for n in defendants]
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append("05案件信息/spec.json")

    readme = case_dir / f"{plaintiffs[0]}-案件说明.md"
    if not readme.exists():
        readme.write_text(README_TEMPLATE.format(
            plaintiff="、".join(plaintiffs), defendant="、".join(defendants), cause=args.cause,
            court=args.court, fyid=args.fyid, region=args.region, case_dir=case_dir,
        ), encoding="utf-8")
        created.append("案件说明.md")

    return {
        "ok": True,
        "case_dir": str(case_dir),
        "spec_file": str(spec_path),
        "session_file": str(case_dir / "06运行记录" / ".court_session.json"),
        "draft_file": str(state / "draft.json"),
        "created": created,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化案件工作目录（中文骨架）")
    ap.add_argument("--root", required=True, help="工作空间根（用户在 WorkBuddy 选择的文件夹）")
    ap.add_argument("--fyid", required=True)
    ap.add_argument("--court", required=True)
    ap.add_argument("--region", default="")
    ap.add_argument("--cause", default="", help="案由（民事一审必填；执行案件可留空）")
    ap.add_argument("--plaintiff", required=True, help="原告 / 申请执行人")
    ap.add_argument("--defendant", required=True, help="被告 / 被执行人")
    ap.add_argument("--case-type", choices=["sp", "zx"], default="sp",
                    help="sp=民事一审（默认）/ zx=强制执行申请")
    ap.add_argument("--date", default="", help="目录日期前缀，默认今天 YYYYMMDD")
    args = ap.parse_args()
    if args.case_type == "sp" and not args.cause.strip():
        ap.error("民事一审必须给 --cause")
    print(json.dumps(bootstrap(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
