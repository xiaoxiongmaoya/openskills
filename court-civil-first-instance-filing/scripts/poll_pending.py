#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询本地「还会变」的案件状态（待审核 + 审核通过等案号，其余不重复刷）。

为什么是这两个状态：
- 待审核是唯一「放着不管就会变」的入口状态
- 审核通过已经批了但**还没出案号**，必须继续查，否则案号出了也不知道
- 已立案且已有案号 → 结果已定，查了也是重复请求
- 补充材料 / 审核未通过 → 等人工返工，轮询推不动

每个案件调 `query_filing.py --case-dir ... --write`（复用它的回写、意见抽取、
退回件下载、绑定链、说明文档刷新），本脚本只负责筛选 + 串行 + 汇总。

用法：
    python poll_pending.py --root "<工作空间根>"              # 预览要查哪些
    python poll_pending.py --root "<工作空间根>" --apply      # 真查 + 回写
    python poll_pending.py --root "<工作空间根>" --apply --status 待审核 --status 补充材料
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
CASE_LIB = "案件库"
# 待审核 = 等结果；审核通过 = 等案号。两者都要查。
DEFAULT_STATUS = ("待审核", "审核通过")
# 结果里这些状态要单独提示（需人工处理）
ATTENTION = {"审核未通过", "补充材料"}


def load_cases(root: Path) -> List[Dict[str, Any]]:
    base = root / CASE_LIB
    if not base.exists():
        return []
    out: List[Dict[str, Any]] = []
    for case_dir in sorted(base.iterdir()):
        if not case_dir.is_dir() or case_dir.name.startswith("_"):
            continue
        case_json = case_dir / "05案件信息" / "case.json"
        if not case_json.exists():
            out.append({"dir": case_dir, "name": case_dir.name, "status": "",
                        "filing_id": "", "no_case_json": True})
            continue
        try:
            case = json.loads(case_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out.append({"dir": case_dir, "name": case_dir.name, "status": "",
                        "filing_id": "", "no_case_json": True})
            continue
        out.append({
            "dir": case_dir,
            "name": case_dir.name,
            "status": str(case.get("status") or ""),
            "filing_id": str(case.get("filing_id") or ""),
            "case_no": str(case.get("case_no") or ""),
            "no_case_json": False,
        })
    return out


def should_poll(case: Dict[str, Any], statuses: set[str]) -> bool:
    """只查目标状态；已有案号的一律跳过（案号到手就是终点）。"""
    if case["no_case_json"] or not case["filing_id"]:
        return False
    if case["case_no"]:
        return False
    return case["status"] in statuses


def poll_one(case: Dict[str, Any], env: Dict[str, str], timeout: int) -> Dict[str, Any]:
    cmd = [sys.executable, str(HERE / "query_filing.py"),
           "--case-dir", str(case["dir"]), "--write", "--json-only"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {**case, "ok": False, "error": "timeout"}
    out = (done.stdout or "").strip()
    err = (done.stderr or "").strip()
    if "会话已失效" in err or "会话已失效" in out:
        return {**case, "ok": False, "error": "session_expired"}
    if not out:
        return {**case, "ok": False, "error": (err or "无输出")[-300:]}
    try:
        status = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        return {**case, "ok": False, "error": (err or out)[-300:]}
    return {
        **case, "ok": True,
        "new_status": status.get("status_text") or "",
        "review_opinion": status.get("review_opinion") or "",
        "review_at": status.get("review_at") or "",
        "reviewer": status.get("reviewer") or "",
        "case_no_before": case.get("case_no") or "",
        "case_no": status.get("case_no") or "",
        "returned": len(status.get("returned_files_local") or []),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="只轮询「待审核」案件的审核状态")
    ap.add_argument("--root", required=True, help="工作空间根（含 案件库/ 的目录）")
    ap.add_argument("--status", action="append", default=[],
                    help="要查的状态，可重复，默认只查「待审核」")
    ap.add_argument("--apply", action="store_true", help="真查并回写（默认只列出要查的案件）")
    ap.add_argument("--timeout", type=int, default=180, help="单案超时秒数")
    ap.add_argument("--interval", type=float, default=1.0, help="案间间隔秒数")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    statuses = set(args.status or DEFAULT_STATUS)
    cases = load_cases(root)
    if not cases:
        print(f"[ERROR] {root / CASE_LIB} 下没有案件目录")
        return 1

    targets = [c for c in cases if should_poll(c, statuses)]
    skipped = [c for c in cases if not should_poll(c, statuses)]
    print(f"共 {len(cases)} 案；待查「{'/'.join(sorted(statuses))}」{len(targets)} 案")

    if not targets:
        print("没有需要查询的案件。")
        return 0
    if not args.apply:
        for c in targets:
            print(f"  · {c['name']}  ({c['status']})")
        print("\n预览模式，未发起任何请求。确认后加 --apply。")
        return 0

    env = dict(os.environ)
    session = root / "通用件" / ".court_session.json"
    env.setdefault("COURT_SESSION_FILE", str(session))

    started = time.time()
    results: List[Dict[str, Any]] = []
    for idx, case in enumerate(targets, 1):
        print(f"[{idx}/{len(targets)}] {case['name']} … ", end="", flush=True)
        res = poll_one(case, env, args.timeout)
        results.append(res)
        if not res["ok"]:
            print(f"失败（{res['error']}）")
            if res["error"] == "session_expired":
                print("\n法院会话已失效，先登录再重跑：")
                print("  python login_captcha.py fetch --case-dir <任一案件目录>")
                print("  python login_captcha.py login --case-dir <同一目录> --code XXXX")
                break
        else:
            changed = "" if res["new_status"] == case["status"] else f" ← 原 {case['status']}"
            print(f"{res['new_status']}{changed}"
                  + (f"，新案号 {res['case_no']}" if res["case_no"] and not case.get("case_no") else ""))
        if idx < len(targets):
            time.sleep(args.interval)

    ok = [r for r in results if r["ok"]]
    moved = [r for r in ok if r["new_status"] != r["status"]]
    attention = [r for r in ok if r["new_status"] in ATTENTION]
    pending_left = len([r for r in ok if r["new_status"] == "待审核"])
    waiting_no = len([r for r in ok if r["new_status"] == "审核通过" and not r["case_no"]])
    new_case_no = [r for r in ok if r["case_no"] and not r.get("case_no_before")]
    not_checked = len(targets) - len(results)

    print("\n" + "=" * 70)
    print(f"查到 {len(ok)} 案，耗时 {time.time() - started:.0f}s")
    if new_case_no:
        print(f"\n拿到案号 {len(new_case_no)} 案（可以结案了）：")
        for r in new_case_no:
            print(f"  · {r['name']} — {r['case_no']}")
    if moved:
        print(f"\n状态变化 {len(moved)} 案：")
        for r in moved:
            print(f"  {r['name']}")
            print(f"    {r['status']} → {r['new_status']}"
                  + (f"（{r['review_at']} {r['reviewer']}）" if r["reviewer"] else ""))
            if r["review_opinion"]:
                first = r["review_opinion"].strip().splitlines()[0]
                print(f"    意见：{first[:80]}{'…' if len(first) > 80 else ''}")
            if r["returned"]:
                print(f"    退回附件 {r['returned']} 份已落盘")
    if attention:
        print(f"\n需处理 {len(attention)} 案：")
        for r in attention:
            print(f"  · {r['name']} — {r['new_status']}")
    failed = [r for r in results if not r["ok"]]
    if failed:
        print(f"\n失败 {len(failed)} 案：")
        for r in failed:
            print(f"  · {r['name']} — {r['error'][:80]}")
    print(f"\n仍在待审核 {pending_left} 案"
          + (f"，等案号 {waiting_no} 案" if waiting_no else "")
          + (f"，未查 {not_checked} 案" if not_checked else ""))
    print("结果由 AI 辅助整理，最终以法院审核为准。")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
