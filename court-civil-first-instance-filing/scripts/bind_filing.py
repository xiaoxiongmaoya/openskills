#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一个案件目录 ↔ 多个立案单（filing_id）的绑定链。

为什么需要它：法院驳回 / 要求补正后，常见做法是**重新提交一份立案申请**
（尤其「系统无法删除已上传材料」时只能重新立案）。重新提交会在法院端生成
**新的 layy id**，也就是新的 filing_id。而 case.json 顶层只有一个 filing_id 字段，
新 id 一写进去，旧单（连同它的驳回意见、案号、审核人）就彻底丢了 ——
过几天再查，就不知道这案子到底被退过几次、每次为什么退。

这里把关系显式存下来：

    case.json
      ├── filing_id            ← 永远指向「当前有效」那一单
      └── filings: [           ← 历史链，只追加，不覆盖
            {filing_id, first_seen_at, submitted_at, status, status_code,
             review_at, reviewer, review_opinion, case_no,
             superseded_by, superseded_at, note}
          ]

规则：
- 顶层 `filing_id` 只能由本模块改写，保证它一定出现在 `filings` 里。
- 换单时旧记录不删，补 `superseded_by` / `superseded_at`，形成可追溯链。
- 每次查询状态都按 filing_id 回填对应记录，历史单的状态也会被更新。

用法：
    python bind_filing.py --case-dir "<案件目录>" --show
    python bind_filing.py --case-dir "<案件目录>" --new-filing-id <id> --reason "驳回后重新立案"
    python bind_filing.py --case-dir "<案件目录>" --from-spec      # 用 spec.json 当前 id 绑定
    python bind_filing.py --case-dir "<案件目录>" --check          # 只补漏，不改链
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ACTIVE_HINT = ("当前", "历史")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def case_json_of(case_dir: Path) -> Path:
    return Path(case_dir) / "05案件信息" / "case.json"


def load(case_dir: Path) -> Dict[str, Any]:
    path = case_json_of(case_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save(case_dir: Path, case: Dict[str, Any]) -> Path:
    path = case_json_of(case_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def filings(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    """历史链（就地初始化，返回值可直接改）。"""
    chain = case.get("filings")
    if not isinstance(chain, list):
        chain = []
        case["filings"] = chain
    return chain


def _find(chain: List[Dict[str, Any]], filing_id: str) -> Optional[Dict[str, Any]]:
    for item in chain:
        if str(item.get("filing_id") or "") == str(filing_id):
            return item
    return None


def ensure(case: Dict[str, Any]) -> Dict[str, Any]:
    """保证顶层 filing_id 一定在链里；返回当前那一条记录。"""
    chain = filings(case)
    current = str(case.get("filing_id") or "").strip()
    if not current:
        return {}
    record = _find(chain, current)
    if record is None:
        record = {
            "filing_id": current,
            "first_seen_at": _now(),
            "submitted_at": case.get("submitted_at"),
            "status": case.get("status") or "",
            "status_code": case.get("status_code"),
            "review_at": case.get("review_at"),
            "reviewer": case.get("reviewer"),
            "review_opinion": case.get("review_opinion"),
            "case_no": case.get("case_no"),
            "note": "首次登记",
        }
        chain.append(record)
    return record


def _snapshot_from_top(case: Dict[str, Any]) -> Dict[str, Any]:
    """把 case.json 顶层的当前状态抄成一条记录（换单时用来封存旧单）。"""
    return {
        "filing_id": str(case.get("filing_id") or ""),
        "submitted_at": case.get("submitted_at"),
        "status": case.get("status") or "",
        "status_code": case.get("status_code"),
        "review_at": case.get("review_at"),
        "reviewer": case.get("reviewer"),
        "review_opinion": case.get("review_opinion"),
        "case_no": case.get("case_no"),
        "review_records": case.get("review_records") or [],
    }


def bind_new(case_dir: Path, new_filing_id: str, reason: str = "",
             case: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把 `new_filing_id` 设为当前单，旧单封存进历史链。

    幂等：新 id 与当前 id 相同则只补链，不产生 superseded 记录。
    """
    case = case if case is not None else load(case_dir)
    if not case:
        return {"ok": False, "message": "case.json 不存在，无法绑定"}
    new_filing_id = str(new_filing_id or "").strip()
    if not new_filing_id:
        return {"ok": False, "message": "新 filing_id 为空"}

    chain = filings(case)
    old_id = str(case.get("filing_id") or "").strip()
    superseded: Optional[str] = None

    if old_id and old_id != new_filing_id:
        record = _find(chain, old_id)
        if record is None:
            record = {"filing_id": old_id, "first_seen_at": _now(), **_snapshot_from_top(case)}
            chain.append(record)
        else:
            for key, value in _snapshot_from_top(case).items():
                if value not in (None, "", []):
                    record[key] = value
        record["superseded_by"] = new_filing_id
        record["superseded_at"] = _now()
        if reason:
            record["note"] = reason
        superseded = old_id

    case["filing_id"] = new_filing_id
    if superseded:
        # 顶层字段描述的是「当前单」，换单后旧单的结论必须清掉，
        # 否则会看到「当前单是 NEW222，状态却还是旧单的审核未通过」这种自相矛盾的记录。
        case["status"] = "待查询"
        case["status_code"] = None
        case["submitted_at"] = None
        case["case_no"] = None
        case["review_opinion"] = None
        case["review_result"] = None
        case["review_at"] = None
        case["reviewer"] = None
        case["reject_reason"] = None
        case["review_records"] = []
        case["last_checked_at"] = None
    current = _find(chain, new_filing_id)
    if current is None:
        chain.append({
            "filing_id": new_filing_id,
            "first_seen_at": _now(),
            "status": "",
            "note": reason or "重新提交立案",
        })
    elif reason:
        current["note"] = reason
    save(case_dir, case)
    return {"ok": True, "current": new_filing_id, "superseded": superseded,
            "total": len(chain)}


def record_status(case_dir: Path, status: Dict[str, Any],
                  case: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把一次查询结果回填到链上对应那条（也用于历史单）。"""
    case = case if case is not None else load(case_dir)
    if not case:
        return {"ok": False}
    fid = str(status.get("id") or status.get("filing_id") or "").strip()
    if not fid:
        return {"ok": False, "message": "状态里没有 filing_id"}
    ensure(case)
    chain = filings(case)
    record = _find(chain, fid)
    if record is None:
        record = {"filing_id": fid, "first_seen_at": _now()}
        chain.append(record)
    record.update({
        "status": status.get("status_text") or record.get("status") or "",
        "status_code": status.get("status"),
        "submitted_at": status.get("submitted_at") or record.get("submitted_at"),
        "review_at": status.get("review_at") or record.get("review_at"),
        "reviewer": status.get("reviewer") or record.get("reviewer"),
        "review_opinion": status.get("review_opinion") if status.get("review_opinion") is not None
        else record.get("review_opinion"),
        "case_no": status.get("case_no") or record.get("case_no"),
        "checked_at": status.get("checked_at") or _now(),
    })
    if status.get("review_records"):
        record["review_records"] = status["review_records"]
    save(case_dir, case)
    return {"ok": True, "filing_id": fid, "is_current": fid == str(case.get("filing_id") or "")}


def summary(case: Dict[str, Any]) -> List[str]:
    """人读的历史链（一行一单）。"""
    chain = filings(case)
    current = str(case.get("filing_id") or "")
    if not chain:
        return ["（未登记立案单）"]
    lines = []
    for idx, item in enumerate(chain, 1):
        tag = "当前" if str(item.get("filing_id")) == current else "历史"
        line = (f"[{idx}] {item.get('filing_id')}  {tag}  "
                f"{item.get('status') or '状态未知'}")
        if item.get("case_no"):
            line += f"  案号 {item['case_no']}"
        if item.get("superseded_by"):
            line += f"  → 被 {item['superseded_by']} 取代（{item.get('superseded_at') or ''}）"
        if item.get("note"):
            line += f"  · {item['note']}"
        lines.append(line)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="案件目录 ↔ 立案单（filing_id）绑定链")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--show", action="store_true", help="打印绑定链")
    ap.add_argument("--new-filing-id", default="", help="绑定一个新的立案单为当前单")
    ap.add_argument("--from-spec", action="store_true", help="用 spec.json 里的 filing_id 作为当前单")
    ap.add_argument("--reason", default="", help="换单原因（会写进历史记录）")
    ap.add_argument("--check", action="store_true", help="只补漏登记，不改当前单")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    case = load(case_dir)
    if not case:
        print(f"[ERROR] 找不到 {case_json_of(case_dir)}")
        return 1

    changed = None
    if args.from_spec:
        spec_path = case_dir / "05案件信息" / "spec.json"
        spec = json.loads(spec_path.read_text(encoding="utf-8")) if spec_path.exists() else {}
        new_id = str(spec.get("filing_id") or "").strip()
        if not new_id:
            print("[ERROR] spec.json 里没有 filing_id")
            return 1
        changed = bind_new(case_dir, new_id, args.reason or "从 spec.json 重新绑定", case)
    elif args.new_filing_id:
        changed = bind_new(case_dir, args.new_filing_id, args.reason, case)
    elif args.check:
        record = ensure(case)
        save(case_dir, case)
        changed = {"ok": True, "message": f"已登记 {record.get('filing_id') or '（无当前单）'}"}
    elif args.show and case.get("filing_id") and not filings(case):
        # --show 顺手把现有 filing_id 登记进链，老案件不用先跑一次查询
        ensure(case)
        save(case_dir, case)

    if changed and not changed.get("ok"):
        print(f"[ERROR] {changed.get('message')}")
        return 2
    if changed:
        if changed.get("superseded"):
            print(f"已换单：{changed['superseded']} → {changed['current']}（旧单已封存进历史）")
        else:
            print(f"当前立案单：{changed.get('current') or '（未变）'}")

    print("绑定链：")
    for line in summary(load(case_dir)):
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
