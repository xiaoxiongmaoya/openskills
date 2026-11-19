#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 DOCX 读写工具：只用标准库，不依赖 python-docx。"""
from __future__ import annotations

import html
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _para_xml(text: str, bold: bool = False, size: int = 21) -> str:
    lines = str(text).split("\n") or [""]
    chunks: List[str] = []
    for line in lines:
        line_bold, line_size = bold, size
        if line.startswith("## "):
            line, line_bold, line_size = line[3:], True, 24
        elif line.startswith("# "):
            line, line_bold, line_size = line[2:], True, 26
        rpr = "<w:rPr>"
        if line_bold:
            rpr += "<w:b/>"
        rpr += f'<w:sz w:val="{line_size}"/>'
        rpr += "</w:rPr>"
        chunks.append(
            '<w:p><w:r>' + rpr +
            f'<w:t xml:space="preserve">{_esc(line)}</w:t></w:r></w:p>'
        )
    return "".join(chunks)


def _document_xml(title: str, paragraphs: Iterable[str]) -> str:
    body = _para_xml(title, bold=True, size=32)
    for para in paragraphs:
        text = str(para)
        if text.startswith("## "):
            body += _para_xml(text[3:], bold=True, size=26)
        elif text.startswith("# "):
            body += _para_xml(text[2:], bold=True, size=28)
        else:
            body += _para_xml(text)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        '</w:sectPr></w:body></w:document>'
    )


def write_docx(path: Path, title: str, paragraphs: Iterable[str], notice: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    paras: List[str] = list(paragraphs)
    if notice:
        paras.insert(0, f"【{notice}】")
    xml = _document_xml(title, paras)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", xml)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
    return path


def _read_entries(path: Path) -> List[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(path) as z:
        return [(info, z.read(info.filename)) for info in z.infolist()]


def _write_entries(path: Path, entries: List[tuple[zipfile.ZipInfo, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in entries:
            z.writestr(info.filename, data)


def append_to_docx(path: Path, heading: str, paragraphs: Iterable[str], notice: str = "") -> Path:
    """在官方模板正文末尾追加填写内容；不动原文字段。"""
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是可编辑 docx：{path}")
    entries = _read_entries(path)
    doc_entry: Optional[zipfile.ZipInfo] = None
    doc_xml = ""
    for info, data in entries:
        if info.filename == "word/document.xml":
            doc_entry = info
            doc_xml = data.decode("utf-8", "ignore")
            break
    if doc_entry is None:
        raise ValueError(f"docx 缺少 word/document.xml：{path}")
    insert = _para_xml(heading, bold=True, size=26)
    if notice:
        insert += _para_xml(f"【{notice}】", bold=True, size=22)
    for para in paragraphs:
        insert += _para_xml(para)
    marker = "<w:sectPr"
    idx = doc_xml.rfind(marker)
    if idx >= 0:
        doc_xml = doc_xml[:idx] + insert + doc_xml[idx:]
    else:
        doc_xml = doc_xml.replace("</w:body>", insert + "</w:body>")
    new_entries = []
    for info, data in entries:
        if info.filename == "word/document.xml":
            new_entries.append((info, doc_xml.encode("utf-8")))
        else:
            new_entries.append((info, data))
    _write_entries(path, new_entries)
    return path


def fill_placeholders(path: Path, mapping: Dict[str, str], blank_missing: bool = False) -> Path:
    """把 {{key}} 替换为值；blank_missing=True 时，模板里有但 mapping 没给的占位符留空。

    默认只替换 mapping 里出现的键，未给出的占位符原样保留（兼容按模板部分填充的场景）。
    文书生成用 blank_missing=True，保证不残留 {{...}}，且无值字段留空。
    """
    import re
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是可编辑 docx：{path}")
    entries = _read_entries(path)
    new_entries = []
    for info, data in entries:
        if info.filename == "word/document.xml":
            xml = data.decode("utf-8", "ignore")
            # 占位符可能被拆到多个 <w:t> run 里：先在文本层合并可见占位符再替换
            flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
            keys = set(re.findall(r"\{\{([^}]+)\}\}", flat))
            for key in keys:
                value = mapping.get(key)
                if value is None:
                    if not blank_missing:
                        continue
                    value = ""
                # 多行值转成 Word 换行（</w:t><w:br/><w:t>），否则 \n 在 w:t 里不会换行
                value = _esc(str(value)).replace(
                    "\n", '</w:t><w:br/><w:t xml:space="preserve">')
                xml = xml.replace("{{" + key + "}}", value)
            data = xml.encode("utf-8")
        new_entries.append((info, data))
    _write_entries(path, new_entries)
    return path


def find_placeholders(path: Path) -> List[str]:
    """列出 docx 模板中出现的全部占位符键（合并跨 run 的文本后识别）。"""
    import re
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
    return sorted(set(re.findall(r"\{\{([^}]+)\}\}", flat)))
