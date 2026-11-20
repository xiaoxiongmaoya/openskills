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


def _clean_party_punct(xml: str) -> str:
    """当事人行空字段会留下多余标点（“，，”“，住，身份证号：，”）。

    对含“原告：”/“被告：”的段落：合并其 run 文本，压缩多余分隔、去掉空标签，
    再用段落内最后一个普通 run 的 rPr 重建为单 run，保留 pPr 与段落样式。
    """
    import re

    def clean_text(text: str) -> str:
        # 去掉空的“标签：”，如“身份证号：”“联系电话：”后紧跟分隔或结尾
        text = re.sub(r"(身份证号|联系电话|电话|住址)[：:]\s*(?=[，,。；;\s]|$)", "", text)
        # 压缩连续逗号（中英文）为一个
        text = re.sub(r"[，,]\s*(?=[，,])", "", text)
        text = re.sub(r"，{2,}", "，", text)
        # “，。”、“， ” 收尾清理
        text = re.sub(r"[，,]\s*$", "", text)
        text = re.sub(r"[，,]\s*(住)(?=[，,。])", "", text)
        # 去掉“住”后紧跟分隔的空住址
        text = re.sub(r"住[，,](\s*)(身份证号|联系电话|电话)", r"\2", text)
        # 行尾残留的“，住”“，身份证号：”“，联系电话：”一并清掉
        text = re.sub(r"[，,]\s*住\s*$", "", text)
        text = re.sub(r"[，,]\s*$", "", text)
        text = re.sub(r"([，,]){2,}", "，", text)
        return text

    def do_para(m):
        pxml = m.group(0)
        flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", pxml, re.S))
        if not re.match(r"\s*(原告|被告)[：:]", flat):
            return pxml
        new_text = clean_text(flat)
        # 取段落内一个普通 run 的 rPr（非加粗那个）作为重建格式
        runs = re.findall(r"<w:r>.*?</w:r>", pxml, re.S)
        rpr = ""
        for r in runs:
            if "<w:b " not in r and "<w:b/" not in r:
                rp = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
                if rp:
                    rpr = rp.group(0)
                    break
        if not rpr and runs:
            rp = re.search(r"<w:rPr>.*?</w:rPr>", runs[0], re.S)
            rpr = rp.group(0) if rp else ""
        ppr = ""
        mppr = re.search(r"<w:pPr>.*?</w:pPr>", pxml, re.S)
        if mppr:
            ppr = mppr.group(0)
        new_run = f'<w:r>{rpr}<w:t xml:space="preserve">{new_text}</w:t></w:r>'
        return f"<w:p>{ppr}{new_run}</w:p>"

    return re.sub(r"<w:p[ >](?:(?!</w:p>).)*?</w:p>", do_para, xml, flags=re.S)


def fill_complaint_rows(path: Path, plaintiff_count: int, defendant_count: int,
                        mapping: Dict[str, str], row_capacity: int = 2) -> Path:
    """按当事人数量填充“一人一行”的诉状模板，并删除多余的空行。

    模板约定每行 7 个语义占位符：{前缀}姓名/性别/民族/出生日期/住址/身份证号/电话。
    实际当事人少于模板行数时，把多余行的整段（<w:p>）删除，避免正文留下“原告：”空行。
    无值字段填空串（留空），不写“待补/待确认”。
    """
    import re
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是可编辑 docx：{path}")
    entries = _read_entries(path)
    new_entries = []
    for info, data in entries:
        if info.filename != "word/document.xml":
            new_entries.append((info, data))
            continue
        xml = data.decode("utf-8", "ignore")
        # 先删除超出实际人数的行（原告/被告），整段删掉
        def drop_rows(xml_text: str, prefix: str, keep: int) -> str:
            for i in range(keep + 1, row_capacity + 2):  # 可能预留更多行
                para_pat = re.compile(
                    r'<w:p[ >](?:(?!</w:p>).)*?\{\{' + re.escape(prefix) + str(i) + r'姓名\}\}.*?</w:p>',
                    re.S)
                m = para_pat.search(xml_text)
                if m:
                    xml_text = xml_text[:m.start()] + xml_text[m.end():]
            return xml_text
        xml = drop_rows(xml, "原告", plaintiff_count)
        xml = drop_rows(xml, "被告", defendant_count)
        # 再替换所有占位符（含被保留行的语义键与单值键），无值留空
        flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
        for key in set(re.findall(r"\{\{([^}]+)\}\}", flat)):
            value = mapping.get(key, "")
            value = _esc(str(value)).replace(
                "\n", '</w:t><w:br/><w:t xml:space="preserve">')
            xml = xml.replace("{{" + key + "}}", value)
        # 清理当事人行里因空字段留下的多余标点（如“，住，身份证号：，”），
        # 只对含“原告：/被告：”的段落做，避免误伤正文。
        xml = _clean_party_punct(xml)
        new_entries.append((info, xml.encode("utf-8")))
    _write_entries(path, new_entries)
    return path


def find_placeholders(path: Path) -> List[str]:
    """列出 docx 模板中出现的全部占位符键（合并跨 run 的文本后识别）。"""
    import re
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
    return sorted(set(re.findall(r"\{\{([^}]+)\}\}", flat)))
