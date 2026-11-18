#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 .doc / .docx 正文，用于提取起诉状信息。

    python read_office_text.py "<起诉状.doc>"
    python read_office_text.py "<起诉状.docx>" --out 起诉状_文本.txt

为什么不用 PDF：扫描版 PDF 没有文本层，OCR 容易把 200000 读成 20000、把「李明」读成「李朋」。
当事人姓名、证件号、金额一律以 Word 原稿为准，PDF 只用来核对签字页。

依赖：.docx 用标准库；.doc 需要 olefile（pip install olefile）。
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

KEEP = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9，。、；：（）《》%．\.\-—\s]")


def read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = xml.replace("</w:p>", "\n")
    text = re.sub(r"<[^>]+>", "", xml)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def read_doc(path: Path) -> str:
    try:
        import olefile
    except ImportError:
        raise SystemExit("读取 .doc 需要 olefile：pip install olefile")
    ole = olefile.OleFileIO(str(path))
    raw = ole.openstream("WordDocument").read()
    text = raw.decode("utf-16-le", "ignore")
    return text


def clean(text: str) -> str:
    text = text.replace("\r", "\n")
    text = KEEP.sub("", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if len(ln) >= 4)


def main() -> int:
    ap = argparse.ArgumentParser(description="读取 Word 正文")
    ap.add_argument("path")
    ap.add_argument("--out", default="", help="另存为 txt")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.exists():
        raise SystemExit(f"文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix == ".docx":
        text = clean(read_docx(p))
    elif suffix == ".doc":
        text = clean(read_doc(p))
    else:
        raise SystemExit("只支持 .doc / .docx；PDF 扫描件请用阅读工具核对签字页")

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已保存：{args.out}（{len(text)} 字）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
