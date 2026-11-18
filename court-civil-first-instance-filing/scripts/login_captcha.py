#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""非交互登录：截图验证码 → 人工/视觉识别 → 用同一 captcha id 登录。

    python login_captcha.py fetch --case-dir "<案件目录>"
    python login_captcha.py login --case-dir "<案件目录>" --code jeut

为何不用 ddddocr：本机 onnxruntime 上 import 即段错误（exit 139），无法兜底。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from court_client import CourtClient, session_file_for  # noqa: E402


def paths(case_dir: str) -> dict:
    base = Path(case_dir).resolve()
    runtime = base / "06运行记录"
    runtime.mkdir(parents=True, exist_ok=True)
    return {
        "session": session_file_for(base),
        "image": runtime / "captcha.png",
        "state": runtime / "captcha_state.json",
    }


def build(p: dict) -> CourtClient:
    return CourtClient(base_url="https://zxfw.court.gov.cn",
                       session_file=str(p["session"]), interactive=False, polite_delay=1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="非交互登录（截图验证码 + 人工识别）")
    ap.add_argument("cmd", choices=["fetch", "login", "status"])
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--code", default="", help="验证码 4 位，login 时必填")
    args = ap.parse_args()

    p = paths(args.case_dir)
    client = build(p)

    if args.cmd == "fetch":
        cap = client.dump_captcha(str(p["image"]))
        p["state"].write_text(json.dumps(cap, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"image": cap["image"], "captcha_id": cap["id"]}, ensure_ascii=False))
        return 0

    if args.cmd == "status":
        print(json.dumps(client.status(), ensure_ascii=False))
        return 0

    if not args.code:
        raise SystemExit("login 需要 --code")
    state = json.loads(p["state"].read_text(encoding="utf-8"))
    ok = client.login_with_captcha(state["id"], args.code)
    print(json.dumps(ok, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
