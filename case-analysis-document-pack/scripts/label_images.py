#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片材料体检：确保每张截图都被 Agent 看过并写清用途。

为什么需要它：OCR 常不可用，如果因为“识别不了”就把图片标成
“需人工核对”了事，就会像某劳务合同纠纷案那样——身份证、银行卡、
送达地址都在截图里，却没读出来，文书只能留空。

规则（写死，不靠记性）：
1. 列出 03图片件/ 下所有图片；
2. 与 05案件信息/image_labels.json 对照，找出未标注的；
3. 有未标注 → 退出码 1，阻止生成文书；Agent 必须逐张 ViewImage 后写入标注。

用法：
  python scripts/label_images.py --case-dir "<案件目录>"            # 体检，未标注则失败
  python scripts/label_images.py --case-dir "<案件目录>" --json     # 机器可读
  python scripts/label_images.py --case-dir "<案件目录>" --list     # 只列未标注
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def collect(case_dir: Path) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    img_dir = case_dir / "03图片件"
    labels_path = case_dir / "05案件信息" / "image_labels.json"
    labels = _load(labels_path)
    if not isinstance(labels, dict):
        labels = {}

    images: List[str] = []
    if img_dir.exists():
        for p in sorted(img_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in IMG_EXT and not p.name.startswith("."):
                images.append(p.relative_to(case_dir).as_posix())

    labeled, missing = [], []
    for rel in images:
        info = labels.get(rel)
        if isinstance(info, dict) and str(info.get("用途") or "").strip():
            labeled.append(rel)
        else:
            missing.append(rel)

    return {
        "case_dir": str(case_dir),
        "labels_file": str(labels_path),
        "image_count": len(images),
        "labeled_count": len(labeled),
        "missing_count": len(missing),
        "missing": missing,
        "ok": len(missing) == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="图片材料体检：未标注用途即失败")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="只列未标注图片")
    args = ap.parse_args()

    result = collect(Path(args.case_dir))

    if args.list:
        for rel in result["missing"]:
            print(rel)
        return 0 if result["ok"] else 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if result["ok"]:
        print(f"图片体检通过：{result['labeled_count']}/{result['image_count']} 张已标注用途")
        return 0

    print(f"图片未标注：{result['missing_count']}/{result['image_count']} 张。"
          f"必须先逐张看图，把用途写进 {result['labels_file']}，再生成文书：")
    for rel in result["missing"]:
        print(f"  - {rel}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
