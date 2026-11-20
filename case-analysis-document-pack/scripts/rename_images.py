#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 image_labels.json 的「建议文件名」生成语义名图片副本（原名保留）。

为什么不直接改名：chat_evidence.json / image_labels.json / 聊天记录 txt 引用
的都是原文件名（如 企业微信图片_202611161739_1.jpg），改名会让这些引用断链、
报告和证据目录无法追溯。所以采用「语义名副本 + 原名原件」：
  - 原件不动，所有下游 key 继续有效；
  - 副本放同一目录、加固定前缀「语义_」，人看图名就知道用途。

副本前缀「语义_」被 label_images.py 与 parse_chat_records.py 跳过，
不会被当成未标注图片，也不会重复计入消息截图数。

用法：
  python scripts/rename_images.py --case-dir "<案件目录>"            # 生成副本
  python scripts/rename_images.py --case-dir "<案件目录>" --dry-run  # 只看计划
  python scripts/rename_images.py --case-dir "<案件目录>" --clean    # 删除已生成副本

image_labels.json 每条新增可选字段：
  "建议文件名": "01-判决书首页"
（不含扩展名、不含「语义_」前缀；脚本自动补原名扩展名并加前缀）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

STATE = "05案件信息"
IMG_DIR = "03图片件"
SEMANTIC_PREFIX = "语义_"
MAP_FILE = "semantic_names.json"
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _safe(name: str) -> str:
    clean = _ILLEGAL.sub("_", str(name or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean[:80]


def rename_images(case_dir: Path, dry_run: bool, clean: bool) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    img_dir = case_dir / IMG_DIR
    labels_path = case_dir / STATE / "image_labels.json"
    labels = _load(labels_path)
    if not isinstance(labels, dict):
        labels = {}

    # 已生成的副本（含上一轮映射表里的），clean 时统一删除
    existing = sorted(p for p in img_dir.glob(f"{SEMANTIC_PREFIX}*") if p.is_file()) if img_dir.exists() else []

    if clean:
        removed = []
        for p in existing:
            if not dry_run:
                p.unlink()
            removed.append(p.name)
        map_path = case_dir / STATE / MAP_FILE
        if map_path.exists() and not dry_run:
            map_path.unlink()
        return {"ok": True, "clean": True, "removed": removed,
                "removed_count": len(removed), "dry_run": dry_run}

    plan: List[Dict[str, str]] = []
    for key, info in labels.items():
        if not isinstance(info, dict):
            continue
        sug = _safe(info.get("建议文件名") or "")
        if not sug:
            continue
        src = case_dir / key
        if not src.exists():
            plan.append({"source": key, "semantic": "", "status": "原件不存在，跳过"})
            continue
        semantic_name = f"{SEMANTIC_PREFIX}{sug}{src.suffix.lower()}"
        dst = img_dir / semantic_name
        status = "副本已存在" if dst.exists() else ("将生成" if dry_run else "已生成")
        if not dry_run and not dst.exists():
            shutil.copyfile(src, dst)
        plan.append({"source": key, "semantic": f"{IMG_DIR}/{semantic_name}", "status": status})

    map_path = case_dir / STATE / MAP_FILE
    if not dry_run:
        map_path.write_text(json.dumps({
            "说明": "语义名副本映射：原件不改，副本仅供人工快速识别；下游引用仍用 source 原名。",
            "prefix": SEMANTIC_PREFIX,
            "items": plan,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    made = sum(1 for r in plan if r["status"] in ("已生成", "将生成"))
    return {
        "ok": True,
        "case_dir": str(case_dir),
        "labeled": len(labels),
        "with_suggestion": len(plan),
        "created_or_planned": made,
        "map_file": str(map_path),
        "dry_run": dry_run,
        "items": plan,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="按建议文件名生成语义名图片副本（原名保留）")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不落盘")
    ap.add_argument("--clean", action="store_true", help="删除已生成的语义名副本与映射表")
    args = ap.parse_args()
    result = rename_images(Path(args.case_dir), args.dry_run, args.clean)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
