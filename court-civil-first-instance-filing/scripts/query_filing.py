#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按立案单 id 查询审核状态，并可回写到案件目录。

    # 只查（任意目录，给 id 即可）
    python query_filing.py --filing-id 0123456789abcdef0123456789abcdef \
        --session-file "<案件目录>/06运行记录/.court_session.json"

    # 查 + 回写案件目录（更新 case.json、存快照、状态变化追加日志）
    python query_filing.py --case-dir "<案件目录>" --write
    python query_filing.py --case-dir "<案件目录>" --write --materials

立案单 id 就是 `layy` 的 id（提交后不会变），可长期作为查询钥匙。

输出分两段：先给人看的审核结果（含**驳回原因 / 审核意见全文 / 退回附件**），
再输出完整 JSON。只想机器读时加 `--json-only`。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from court_client import CourtClient, session_file_for  # noqa: E402

RETURNED_DIR = Path("05案件信息") / "法院退回件"


def resolve(args: argparse.Namespace) -> Dict[str, Any]:
    case_dir: Optional[Path] = Path(args.case_dir).resolve() if args.case_dir else None
    session = Path(args.session_file) if args.session_file else (
        session_file_for(case_dir) if case_dir else Path(".court_session.json"))
    case_json = case_dir / "05案件信息" / "case.json" if case_dir else None

    filing_id = args.filing_id
    if not filing_id and case_json and case_json.exists():
        filing_id = json.loads(case_json.read_text(encoding="utf-8")).get("filing_id") or ""
    if not filing_id and case_dir:
        draft = case_dir / "05案件信息" / "draft.json"
        if draft.exists():
            filing_id = json.loads(draft.read_text(encoding="utf-8")).get("filing_id") or ""
    if not filing_id:
        raise SystemExit("需要 --filing-id，或 --case-dir 下 case.json 里已有 filing_id")
    return {"case_dir": case_dir, "session": session, "case_json": case_json, "filing_id": filing_id}


def _safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip()).strip("._ ")
    return cleaned[:120] or fallback


def download_returned(ctx: Dict[str, Any], status: Dict[str, Any], client: "CourtClient") -> List[Dict[str, str]]:
    """把法院退回的附件（模板 / 补正材料）落到 `05案件信息/法院退回件/`。

    不下载会丢：那些 URL 带签名且会过期，过期后前端也打不开。
    """
    case_dir: Optional[Path] = ctx["case_dir"]
    if not case_dir:
        return []
    dest_dir = case_dir / RETURNED_DIR
    saved: List[Dict[str, str]] = []
    seen = set()
    for rec in (status.get("review_records") or []):
        for item in (rec.get("returned_files") or []):
            url = item.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            name = _safe_name(item.get("name") or "", f"法院退回件-{len(saved) + 1}")
            if not Path(name).suffix and item.get("name"):
                name += Path(str(item["name"])).suffix
            dest = dest_dir / name
            try:
                if not (dest.exists() and dest.stat().st_size > 0):
                    client.download_attachment(url, dest)
                saved.append({"name": dest.name, "path": str(dest)})
            except Exception as exc:  # 下载失败不该让状态查询整体失败
                print(f"[WARN] 退回附件下载失败 {name}：{exc}", file=sys.stderr)
    return saved


def write_back(ctx: Dict[str, Any], status: Dict[str, Any], client: "CourtClient") -> Dict[str, Any]:
    case_dir: Optional[Path] = ctx["case_dir"]
    case_json: Path = ctx["case_json"]
    changed: Dict[str, Any] = {}
    returned = download_returned(ctx, status, client) if case_dir else []
    if returned:
        status["returned_files_local"] = returned
    if case_json and case_json.exists():
        case = json.loads(case_json.read_text(encoding="utf-8"))
        before = str(case.get("status") or "")
        after = str(status.get("status_text") or status.get("status") or "")
        current = str(case.get("filing_id") or "")
        queried = str(status["id"])
        # 查历史单时不能把「当前单」顶掉，只把它记进 filings 链
        is_current = (not current) or current == queried
        if is_current:
            case["filing_id"] = queried
        case.update({
            "status": after,
            "status_code": status["status"],
            "submitted_at": status.get("submitted_at"),
            "case_no": status.get("case_no"),
            # 审核结论：意见正文 + 结果 + 时间 + 审核人 + 驳回原因 + 退回附件
            "review_opinion": status.get("review_opinion"),
            "review_result": status.get("review_result"),
            "review_at": status.get("review_at"),
            "reviewer": status.get("reviewer"),
            "reject_reason": status.get("reject_reason"),
            "review_records": status.get("review_records"),
            "returned_files": returned or case.get("returned_files"),
            "last_checked_at": status["checked_at"],
        })
        # 同步刷新法院当前排期在前的案件数（get 接口，失败不影响状态回写）
        try:
            fyid = (case.get("court") or {}).get("fyid")
            if fyid and client:
                case["court_queue"] = client.get_court_queue(fyid)
                case["court_queue_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        case_json.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        if before != after:
            changed = {"from": before, "to": after}

        log = case_dir / "06运行记录" / "状态变更.log"
        records = status.get("review_records") or []
        latest_id = str((records[0] or {}).get("id") or "") if records else ""
        existing = log.read_text(encoding="utf-8") if log.exists() else ""
        # 状态没变但法院新写了审核意见时也要补记一次，否则驳回理由永远进不了日志
        if changed or not existing or (latest_id and latest_id not in existing):
            # 意见里带换行，日志必须单行 —— 压成 " / " 否则后续 grep 全乱
            opinion = re.sub(r"\s*\n\s*", " / ", str(status.get("review_opinion") or ""))
            line = (f"{status['checked_at']}\t{status['id']}\t{after}\t"
                    f"{status.get('review_result') or ''}\t{latest_id}\t{opinion}\n")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(line)

        # 绑定的历史链：本次查到哪一单就回填哪一单（含已作废的历史单）
        try:
            from bind_filing import record_status
            record_status(case_dir, status)
        except Exception as exc:
            print(f"[WARN] 立案单绑定链未更新：{exc}", file=sys.stderr)

    snapshot = case_dir / "05案件信息" / "立案单状态.json"
    if case_dir:
        if snapshot.exists():
            old = json.loads(snapshot.read_text(encoding="utf-8"))
            # 本次没拉材料清单时，别把上次的抹掉（否则进度会回退）
            if not status.get("materials") and old.get("materials"):
                status["materials"] = old["materials"]
        snapshot.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            from write_case_note import update as update_case_note
            note_path = update_case_note(case_dir)
            print(f"[OK] 说明文档已更新：{note_path}")
        except Exception as exc:  # 说明文件写失败不该影响状态查询
            print(f"[WARN] 案件说明未更新：{exc}", file=sys.stderr)
    return {"changed": changed, "snapshot": str(snapshot) if case_dir else "", "returned_files": returned}


def human_report(status: Dict[str, Any]) -> str:
    """给人看的审核结果：一条记录里把驳回理由原样摊开，不用再登网站。"""
    lines = [f"状态：{status['status_text']}（zt={status['status']}）  立案单：{status['id']}"]
    if status.get("court"):
        lines.append(f"法院：{status['court']}　案号：{status.get('case_no') or '—'}"
                     f"　提交时间：{status.get('submitted_at') or '—'}")
    records = status.get("review_records") or []
    if not records:
        lines.append("审核意见：法院端暂无记录（还没出结果，或法院未填写）")
        return "\n".join(lines)
    for idx, rec in enumerate(records, 1):
        lines.append("")
        lines.append(f"[{idx}] {rec.get('reviewed_at') or '时间未记录'}　"
                     f"结果：{rec.get('result') or '—'}　审核人：{rec.get('reviewer') or '—'}")
        if rec.get("reject_reason"):
            lines.append(f"    驳回原因：{rec['reject_reason']}")
        if rec.get("material_fix_at"):
            lines.append(f"    材料补正时间：{rec['material_fix_at']}")
        opinion = str(rec.get("opinion") or "").strip()
        rows = [line.strip() for line in opinion.splitlines() if line.strip()]
        if rows:
            lines.append("    审核意见：")
            for line in rows:
                lines.append(f"      {line}")
    local = status.get("returned_files_local") or []
    if local:
        lines.append("")
        lines.append("法院退回/附带文件（已下载）：")
        for item in local:
            lines.append(f"  - {item['name']}  →  {item['path']}")
    else:
        remote = [f for rec in records for f in (rec.get("returned_files") or [])]
        if remote:
            lines.append("")
            lines.append("法院退回/附带文件（未下载，加 --write 落盘）：")
            for item in remote:
                lines.append(f"  - {item.get('name') or '(无文件名)'}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="按立案单 id 查询审核状态")
    ap.add_argument("--filing-id", default="", help="立案单 id（layy id）")
    ap.add_argument("--case-dir", default="", help="案件目录（可从中读 filing_id 与会话文件）")
    ap.add_argument("--session-file", default="", help="会话文件路径")
    ap.add_argument("--materials", action="store_true", help="附带材料清单（多几次请求）")
    ap.add_argument("--write", action="store_true", help="回写 case.json / 快照 / 状态日志")
    ap.add_argument("--no-download", action="store_true", help="不下载法院退回附件")
    ap.add_argument("--json-only", action="store_true", help="只输出 JSON，不打印人读摘要")
    args = ap.parse_args()

    ctx = resolve(args)
    client = CourtClient(base_url="https://zxfw.court.gov.cn",
                         session_file=str(ctx["session"]), polite_delay=0.8)
    status = client.get_filing_status(ctx["filing_id"], include_materials=args.materials or args.write)
    if args.no_download:
        status["returned_files_local"] = []
    if args.write:
        status["_write"] = write_back(ctx, status, client)
    if not args.json_only:
        print(human_report(status))
        print("\n--- JSON ---")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
