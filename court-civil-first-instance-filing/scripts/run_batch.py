#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多案批量立案：几个案子跑到「停在提交前」，最后给一张总表。

并发模型（为什么不各自睡觉，又为什么默认单线程）：
- 每个案子一个子进程（互不污染、崩溃不互相带倒）
- **请求速率是全局的**：所有进程共享 `<根>/通用件/.court_rate.json` 令牌桶，默认 1 次/秒。
- **上传默认串行**：材料上传（签名→OSS→关联）是风控最敏感的一段，多线程并发上传
  极易触发 OSS 403 /「获取上传信息失败」，所以 `--workers` 默认 1；即使调大，
  上传仍被 `.court_upload.state` 那把跨进程锁串起来（同一时刻只有一个进程在传）。
- 上传节奏另有单独开关：`--upload-interval/--upload-jitter/--upload-batch/--upload-cooldown`，
  默认每份间隔 3.5s±1.5s，每 6 份长停 25s。嫌慢再调，别一次调太猛。
- 登录态也是全局的：共享 `<根>/通用件/.court_session.json`，一小时内只登录一次。

用法：
    # 先看会跑哪些案子
    python run_batch.py --root "<工作空间根>" --dry-run

    # 3 个案子并发（上传仍串行），跑到「材料传完、等确认」
    python run_batch.py --root "<工作空间根>" --workers 3 --stage upload

    # 全部准备完、看过总表后再一起提交（不可逆）
    python run_batch.py --root "<工作空间根>" --stage all \
        --mediation no --defendant-id-photo skip --submit --yes
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

RUNNER = Path(__file__).resolve().parent / "filing_runner.py"
SUBMITTED_STATUS = {"待审核", "审核通过", "已立案", "审核未通过", "补充材料"}


def discover_cases(root: Path) -> List[Path]:
    return sorted((root / "案件库").glob("*/05案件信息/spec.json"))


def load_case(spec_path: Path) -> Dict[str, Any]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    case_dir = spec_path.parent.parent
    case_json = spec_path.parent / "case.json"
    case = json.loads(case_json.read_text(encoding="utf-8")) if case_json.exists() else {}
    return {
        "dir": case_dir,
        "spec": spec,
        "case": case,
        "name": case_dir.name,
        "plaintiff": (spec.get("plaintiff") or {}).get("name", ""),
        "defendant": (spec.get("defendant") or {}).get("name", ""),
        "court": (spec.get("court") or {}).get("fyid", ""),
        "cause": (spec.get("case") or {}).get("cause", ""),
        "amount": (spec.get("case") or {}).get("amount", ""),
        "files": sum(len(i.get("files") or []) for i in (spec.get("materials") or [])),
        "filing_id": spec.get("filing_id") or case.get("filing_id") or "",
        "status": case.get("status") or "",
    }


def build_command(spec_path: Path, args: argparse.Namespace) -> List[str]:
    cmd = [sys.executable, str(RUNNER), str(spec_path), "--apply", "--stage", args.stage,
           "--rate", str(args.rate)]
    for flag, value in (("--upload-interval", args.upload_interval),
                        ("--upload-jitter", args.upload_jitter),
                        ("--upload-cooldown", args.upload_cooldown)):
        if value is not None:
            cmd += [flag, str(value)]
    if args.upload_batch is not None:
        cmd += ["--upload-batch", str(args.upload_batch)]
    if args.mediation:
        cmd += ["--mediation", args.mediation]
    if args.defendant_id_photo:
        cmd += ["--defendant-id-photo", args.defendant_id_photo]
    if args.skip_preflight:
        cmd.append("--skip-preflight")
    if args.submit:
        cmd += ["--submit", "--yes"]
    return cmd


def run_one(item: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    spec_path = item["dir"] / "05案件信息" / "spec.json"
    cmd = build_command(spec_path, args)
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
        output = (completed.stdout or "") + (completed.stderr or "")
        return {**item, "returncode": completed.returncode, "output": output,
                "ok": completed.returncode == 0}
    except subprocess.TimeoutExpired:
        return {**item, "returncode": -1, "output": f"超时（>{args.timeout}s）", "ok": False}


def print_table(results: List[Dict[str, Any]]) -> None:
    headers = ("案件", "法院", "标的额", "材料", "filing_id", "结果")
    widths = (34, 6, 9, 5, 34, 8)

    def cell(text: str, width: int) -> str:
        text = str(text)
        while len(text) < width:
            text += " "
        return text[:width]

    print("─" * (sum(widths) + 6))
    print("  ".join(cell(h, w) for h, w in zip(headers, widths)))
    print("─" * (sum(widths) + 6))
    for item in results:
        mark = "OK" if item["ok"] else f"失败({item['returncode']})"
        print("  ".join([
            cell(item["name"], widths[0]), cell(item["court"], widths[1]),
            cell(item["amount"], widths[2]), cell(item["files"], widths[3]),
            cell(item.get("filing_id") or "-", widths[4]), cell(mark, widths[5]),
        ]))
    print("─" * (sum(widths) + 6))
    failed = [i for i in results if not i["ok"]]
    print(f"共 {len(results)} 件，成功 {len(results) - len(failed)}，失败 {len(failed)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="多案批量立案（全局限速 + 子进程隔离）")
    ap.add_argument("--root", default=".", help="工作空间根（含 案件库/ 的目录）")
    ap.add_argument("--case", action="append", default=[], help="只跑指定案件目录（可重复）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并发案件数，默认 1（串行）。上传本身也会被全局锁串起来，调大主要省「填字段」时间")
    ap.add_argument("--stage", choices=["create", "fill", "upload", "all"], default="upload",
                    help="跑到哪一步；确认提交用 all + --submit")
    ap.add_argument("--rate", type=float, default=1.0, help="全局请求速率（次/秒，跨进程共享）")
    ap.add_argument("--upload-interval", type=float, default=None, help="材料上传间隔秒（默认 3.5）")
    ap.add_argument("--upload-jitter", type=float, default=None, help="上传间隔随机抖动秒（默认 1.5）")
    ap.add_argument("--upload-batch", type=int, default=None, help="每 N 份材料长停一次（默认 6，0 关闭）")
    ap.add_argument("--upload-cooldown", type=float, default=None, help="批间长停秒（默认 25）")
    ap.add_argument("--allow-parallel-upload", action="store_true",
                    help="允许并发上传（不推荐：多进程同时传是 OSS 403 的主要来源）")
    ap.add_argument("--mediation", choices=["yes", "no"], default="")
    ap.add_argument("--defendant-id-photo", choices=["upload", "skip"], default="")
    ap.add_argument("--submit", action="store_true", help="准备完直接提交（不可逆）")
    ap.add_argument("--yes", action="store_true", help="配合 --submit")
    ap.add_argument("--include-submitted", action="store_true", help="已提交过的案子也重跑")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只列出会跑的案子")
    ap.add_argument("--timeout", type=int, default=1800, help="单个案子超时秒数")
    args = ap.parse_args()

    if args.submit and not args.yes:
        print("--submit 需要同时给 --yes（提交不可逆）")
        return 2

    # 上传阶段默认不允许并发进程：以前 --workers 2/3 各传各的，是 OSS 403 的主要来源。
    # 即使这里放行，court_client 的跨进程上传锁仍然会把上传串起来，只是没意义地多开进程。
    if args.workers > 1 and args.stage in ("upload", "all") and not args.allow_parallel_upload:
        print(f"[限制] 上传阶段串行执行：--workers {args.workers} → 1"
              "（多进程同时上传易触发风控；确需并发加 --allow-parallel-upload）")
        args.workers = 1

    root = Path(args.root).resolve()
    if args.case:
        spec_paths = [Path(c).resolve() / "05案件信息" / "spec.json" for c in args.case]
    else:
        spec_paths = discover_cases(root)
    items = [load_case(p) for p in spec_paths if p.exists()]

    if not items:
        print("没找到可跑的案子（需要有 05案件信息/spec.json）")
        return 1

    skipped = []
    if not args.include_submitted:
        kept = []
        for item in items:
            if item["status"] in SUBMITTED_STATUS:
                skipped.append(item)
            else:
                kept.append(item)
        items = kept
        for item in skipped:
            print(f"跳过已提交：{item['name']}（{item['status']}）；要重跑加 --include-submitted")

    print(f"计划：{len(items)} 件，并发 {args.workers}，全局 {args.rate:g} 请求/秒，阶段 {args.stage}")
    for item in items:
        print(f"  · {item['name']}  {item['files']} 份材料  filing_id={item['filing_id'] or '-'}")
    if args.dry_run or not items:
        return 0

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_one, item, args): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            tag = "OK" if result["ok"] else f"失败({result['returncode']})"
            print(f"[{result['name']}] {tag}")
            if not result["ok"]:
                tail = (result["output"] or "").strip().splitlines()[-6:]
                for line in tail:
                    print(f"    {line}")

    print_table(sorted(results, key=lambda r: r["name"]))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
