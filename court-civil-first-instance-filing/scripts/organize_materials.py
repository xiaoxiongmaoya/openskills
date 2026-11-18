#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整理客户交付的乱材料：解压 → 中文转码 → 按类别归位 → 规范命名。

规则（固定，不靠临场判断）：
  - 压缩包原样复制到 `01原始包/`，解压结果放 `01原始包/解压/`（保留原层级，只读）
  - 已签字 PDF → `02签字件/`；图片 → `03图片件/`；Word / 未签 PDF → `04未签稿/`
  - 目标文件名统一 `序号-材料名-页序.ext`，序号对齐法院材料槽位
  - **只复制不删除**，源目录保持原样

用法：
    python organize_materials.py --case-dir "<案件目录>" --source "<zip 或 目录>"
    python organize_materials.py --case-dir "<案件目录>" --source "<zip>" --dry-run

输出 JSON：每条的 src → dst、未能识别的文件清单（需人工核对）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

D_SIGN = "02签字件"
D_IMG = "03图片件"
D_DRAFT = "04未签稿"
D_RAW = "01原始包"

# (文件名关键词, 槽位序号, 规范材料名)
NAME_RULES: List[Tuple[str, int, str]] = [
    ("起诉状", 1, "起诉状"),
    ("授权委托书", 3, "授权委托书"),
    ("律师证", 3, "律师执业证"),
    ("执业证", 3, "律师执业证"),
    ("所函", 3, "律师事务所函"),
    ("律师函", 3, "律师事务所函"),
    ("送达地址", 5, "送达地址确认书"),
    ("账户确认书", 6, "账户确认书"),
    ("收款账户", 6, "账户确认书"),
    ("借条", 4, "借条"),
    ("欠条", 4, "借条"),
    ("证据", 4, "证据材料"),
]

DOC_EXT = {".doc", ".docx", ".wps"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """还原中文文件名：Windows 打包常见 GBK，Python 默认按 cp437 解。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            return name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name


def extract_zip(zip_path: Path, dest: Path) -> List[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            rel = decode_zip_name(info)
            if rel.endswith("/"):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            written.append(target)
    return written


def guess_target(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """返回 (目标目录, 规范文件名)；None 表示未能识别，交人工。"""
    stem = path.stem
    ext = path.suffix.lower()
    parent = path.parent.name

    slot = 0
    label = ""
    for keyword, number, canonical in NAME_RULES:
        if keyword in stem or keyword in parent:
            slot, label = number, canonical
            break

    if "身份证" in stem or "身份证" in parent or parent in ("原告", "被告"):
        slot = 2
        label = "被告身份证" if ("被告" in parent or "被告" in stem) else "原告身份证"
    if not label:
        return None, None

    page = ""
    if label == "起诉状":
        digits = "".join(ch for ch in stem if ch.isdigit())
        if digits:
            page = f"-{digits[-1]}"

    if ext in DOC_EXT:
        folder = D_DRAFT
    elif ext in IMG_EXT:
        folder = D_IMG
    elif ext == ".pdf":
        folder = D_DRAFT if ("未签" in stem or "模板" in stem or "草稿" in stem) else D_SIGN
    else:
        return None, None

    return folder, f"{slot:02d}-{label}{page}{ext}"


def plan_target(target_dir: Path, name: str, src_size: int, taken: set) -> Optional[Path]:
    """阶段 A：单线程决定落地路径（并发下也不会撞名）。

    返回 None 表示已存在同大小文件（幂等，跳过复制）。
    `taken` 只记**本次运行**已分配的名字，不要预置磁盘上的文件名，否则第二次跑就不再幂等。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    if target.exists() and target.stat().st_size == src_size and target.name.lower() not in taken:
        taken.add(target.name.lower())
        return None
    if target.exists() or target.name.lower() in taken:
        stem, suffix = target.stem, target.suffix
        index = 2
        while True:
            candidate = target_dir / f"{stem}-v{index}{suffix}"
            if candidate.exists() and candidate.stat().st_size == src_size and candidate.name.lower() not in taken:
                taken.add(candidate.name.lower())
                return None
            if not candidate.exists() and candidate.name.lower() not in taken:
                taken.add(candidate.name.lower())
                return candidate
            index += 1
    taken.add(target.name.lower())
    return target


def copy_into(case_dir: Path, src: Path, folder: str, name: str) -> Path:
    """单文件复制（兼容旧调用）：先定名再复制，不做并发。"""
    target = plan_target(case_dir / folder, name, src.stat().st_size, set())
    if target is None:
        return case_dir / folder / name
    shutil.copy2(src, target)
    return target


def organize(case_dir: Path, source: Path, dry_run: bool, workers: int = 6) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    for sub in (D_RAW, D_SIGN, D_IMG, D_DRAFT):
        (case_dir / sub).mkdir(parents=True, exist_ok=True)

    source = source.resolve()
    extracted: List[Path] = []
    if source.is_dir():
        extracted = [p for p in source.rglob("*") if p.is_file()]
    else:
        if not dry_run:
            archive_copy = case_dir / D_RAW / source.name
            if not archive_copy.exists():
                shutil.copy2(source, archive_copy)
        if zipfile.is_zipfile(source):
            extracted = extract_zip(source, case_dir / D_RAW / "解压") if not dry_run else []
            if dry_run:
                with zipfile.ZipFile(source) as zf:
                    extracted = [Path(decode_zip_name(i)) for i in zf.infolist() if not i.is_dir()]
        else:
            raise SystemExit(f"不支持的来源类型：{source}")

    # 阶段 A：单线程定名（唯一能保证不撞名的地方）
    taken: set = set()
    moved: List[Dict[str, str]] = []
    unknown: List[str] = []
    jobs: List[Tuple[Path, Path]] = []
    for path in extracted:
        if any(part.startswith(".") for part in path.parts):
            continue
        folder, name = guess_target(path)
        if not folder or not name:
            unknown.append(str(path))
            continue
        if dry_run:
            moved.append({"src": str(path), "dst": f"{folder}/{name}", "dry_run": "1"})
            continue
        target = plan_target(case_dir / folder, name, path.stat().st_size, taken)
        if target is None:
            moved.append({"src": str(path), "dst": f"{folder}/{name}", "skipped": "已存在同大小文件"})
            continue
        jobs.append((path, target))
        moved.append({"src": str(path), "dst": str(target.relative_to(case_dir))})

    # 阶段 B：线程池复制（IO 密集，GIL 在这里不挡路）
    copied = 0
    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(shutil.copy2, src, dst) for src, dst in jobs]
            for future in as_completed(futures):
                future.result()
                copied += 1

    return {
        "ok": True,
        "case_dir": str(case_dir),
        "organized": moved,
        "unrecognized": unknown,
        "copied": copied,
        "workers": workers,
        "note": "PDF 是否已签字/盖章需视觉复核；未识别文件请人工归位后重跑（幂等，已存在不覆盖）",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="整理案件材料到中文分类目录")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--source", required=True, help="压缩包或原始材料目录")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=6, help="并行复制线程数，默认 6")
    args = ap.parse_args()
    result = organize(Path(args.case_dir), Path(args.source), args.dry_run, args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
