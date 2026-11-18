#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撤回 → 补传 → 重新提交（用于"已提交但材料缺件"的返工）。

为什么单独一个脚本：材料一旦提交就被锁（`ssclfj` 关联接口返回 500），只能先撤回成草稿
（`POST /api/v3/layy/chlayy/{id}`）才能补传。撤回有冷却期，脚本会按间隔重试。

用法：
    python reapply_case.py --case-dir "<案件目录>"                # 完整返工
    python reapply_case.py --case-dir "<案件目录>" --withdraw-only  # 只撤
    python reapply_case.py --case-dir "<案件目录>" --no-submit      # 补传后不提交
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from court_client import CourtApiError, CourtClient, session_file_for

HERE = Path(__file__).resolve().parent
PY = sys.executable
DRAFT = "11800007-100"


def run(cmd: list[str]) -> int:
    print("$", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--withdraw-only", action="store_true")
    ap.add_argument("--no-submit", action="store_true")
    ap.add_argument("--wait-minutes", type=int, default=30, help="撤回冷却失败时最多等多久")
    ap.add_argument("--interval", type=int, default=300, help="重试间隔秒")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    spec_path = case_dir / "05案件信息" / "spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    filing_id = str(spec.get("filing_id") or "")
    if not filing_id:
        print("[ERROR] spec 无 filing_id")
        return 1

    c = CourtClient(session_file=str(session_file_for(case_dir)), rate=args.rate)
    c.ensure_login()

    status = c.get_filing_status(filing_id)
    print(f"当前状态：{status['status_text']}（{status.get('status')}）")

    if (status.get("status") or status.get("status_code")) != DRAFT:
        deadline = time.time() + args.wait_minutes * 60
        while True:
            try:
                c.withdraw_layy(filing_id)
                print("撤回成功")
                break
            except CourtApiError as exc:
                print(f"撤回失败：{exc.message}")
                if time.time() >= deadline:
                    print("[STOP] 冷却期未过，稍后重跑本脚本")
                    return 3
                print(f"  {args.interval}s 后重试（最多等到 {args.wait_minutes} 分钟）")
                time.sleep(args.interval)
        c.invalidate_detail(filing_id)
        status = c.get_filing_status(filing_id)
        print(f"撤回后状态：{status['status_text']}（{status.get('status')}）")
        if (status.get("status") or status.get("status_code")) != DRAFT:
            print("[STOP] 撤回后仍不是草稿，人工确认")
            return 4

    if args.withdraw_only:
        return 0

    if run([PY, str(HERE / "preflight_materials.py"), "--case-dir", str(case_dir)]) != 0:
        print("[STOP] 材料门禁未通过，不提交")
        return 2

    # 撤回后重新写一遍字段（案由/当事人/代理人/标的额/诉请），避免撤回把表单清空
    if run([PY, str(HERE / "filing_runner.py"), str(spec_path), "--apply", "--stage", "fill"]) != 0:
        print("[STOP] 重新填写字段失败，不提交")
        return 2

    if run([PY, str(HERE / "resume_upload.py"), "--case-dir", str(case_dir), "--rate", str(args.rate)]) != 0:
        print("[STOP] 补传未全部成功，不提交")
        return 2

    confirm = run([PY, str(HERE / "confirm_filing.py"), "--case-dir", str(case_dir),
                   "--mediation", "yes", "--defendant-id-photo", "skip"])
    if confirm != 0:
        print("[STOP] 回读确认失败，不提交")
        return 2

    if args.no_submit:
        print("已补传并回读，按 --no-submit 停在提交前")
        return 0

    rc = run([PY, str(HERE / "filing_runner.py"), str(spec_path), "--apply", "--stage", "submit",
              "--mediation", "yes", "--defendant-id-photo", "skip", "--submit", "--yes"])
    if rc == 0:
        run([PY, str(HERE / "query_filing.py"), "--case-dir", str(case_dir), "--write"])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
