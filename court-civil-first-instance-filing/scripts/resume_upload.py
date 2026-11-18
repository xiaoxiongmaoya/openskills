#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断点续传：只补没传上的材料，不重传已成功的。

为什么需要它：`filing_runner --stage upload` 失败后重跑会**整批重传**，
一案几十份材料等于白烧上百次请求，还容易再撞上传频控（OSS 403 / 获取上传信息失败）。
这里改为「先读法院端已存在的文件名，再只传差额」，失败单份重试 3 次并退避。

用法：
    python resume_upload.py --case-dir "<案件目录>"
    python resume_upload.py --case-dir "<案件目录>" --rate 1.0 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from court_client import (
    DEFAULT_UPLOAD_INTERVAL,
    CourtClient,
    CourtApiError,
    session_file_for,
)
from filing_runner import EXTRA_COPY, SLOT_ALIAS, plaintiffs_of, defendants_of

MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def desired_uploads(spec: Dict[str, Any]) -> List[Tuple[str, str, str, Path, int]]:
    """(slot_name, cllx, 上传文件名, 本地路径, 同槽序号) 列表，与 runner._upload 完全一致。"""
    out: List[Tuple[str, str, str, Path, int]] = []
    id_photo = str((spec.get("decisions") or {}).get("defendant_id_photo") or "")
    for item in spec.get("materials") or []:
        slot_name = str(item.get("slot") or "")
        cllx = str(item.get("cllx") or SLOT_ALIAS.get(slot_name, ""))
        files = [Path(f) for f in item.get("files") or []]
        if id_photo == "skip":
            files = [f for f in files if "被告身份证" not in f.name]
        for idx, path in enumerate(files, start=1):
            out.append((slot_name, cllx, path.name, path, idx))
            extra = EXTRA_COPY.get(slot_name)
            if extra:
                out.append((extra, SLOT_ALIAS[extra], f"{extra}-{path.name}", path, idx))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--upload-interval", type=float, default=None,
                    help=f"每份材料间隔秒，默认 {DEFAULT_UPLOAD_INTERVAL}（调小更易撞 OSS 403）")
    ap.add_argument("--upload-jitter", type=float, default=None)
    ap.add_argument("--upload-batch", type=int, default=None)
    ap.add_argument("--upload-cooldown", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    spec_path = case_dir / "05案件信息" / "spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    filing_id = str(spec.get("filing_id") or "")
    if not filing_id:
        print("[ERROR] spec 里没有 filing_id，先跑 --stage create")
        return 1
    fyid = str(spec["court"]["fyid"])

    c = CourtClient(session_file=str(session_file_for(case_dir)), rate=args.rate,
                    upload_interval=args.upload_interval,
                    upload_jitter=args.upload_jitter,
                    upload_batch=args.upload_batch,
                    upload_cooldown=args.upload_cooldown)
    c.ensure_login()
    detail = c.get_filing_detail(filing_id, refresh=True)["data"]
    slots = {s["cllx"]: s for s in detail.get("sscls") or []}

    existing: Dict[str, set] = {}
    for cllx, slot in slots.items():
        try:
            rows = c.list_slot_files(filing_id, slot["id"], refresh=True)
        except CourtApiError:
            rows = []
        existing[cllx] = {str(r.get("wjmc") or r.get("name") or "") for r in rows}

    todo = [d for d in desired_uploads(spec) if d[2] not in existing.get(d[1], set())]
    print(f"应传 {len(desired_uploads(spec))} 份 / 法院端已有 {sum(len(v) for v in existing.values())} 份 / 待补 {len(todo)} 份")
    if args.dry_run:
        for name, cllx, wjmc, path, order in todo:
            print("  [待补]", name, wjmc)
        return 0

    ok = fail = 0
    for name, cllx, wjmc, path, order in todo:
        slot = slots.get(cllx)
        if not slot:
            print(f"  [跳过] 槽位编码 {cllx}（{name}）法院模板里没有")
            fail += 1
            continue
        if not path.exists():
            print(f"  [跳过] 本地文件不存在：{path}")
            fail += 1
            continue
        for attempt in range(1, args.retries + 1):
            try:
                c.upload_material(
                    filing_id, fyid,
                    {"court_material_code": slot["cllx"], "court_material_id": slot["id"],
                     "court_material_name": name, "court_material_order": order},
                    str(path), wjmc, MIME.get(path.suffix.lower(), "application/octet-stream"),
                )
                print(f"  [OK] {name}: {wjmc}")
                ok += 1
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == args.retries:
                    print(f"  [FAIL] {name}: {wjmc} -> {exc}")
                    fail += 1
                else:
                    wait = 2 ** attempt
                    print(f"  [重试 {attempt}/{args.retries}] {wjmc}：{exc}（{wait}s 后）")
                    time.sleep(wait)
    print(f"补传完成：成功 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
