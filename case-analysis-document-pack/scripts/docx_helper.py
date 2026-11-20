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


# 实战模板里「申请人」「被申请人」两段常共用同一组无角色占位符（{{地址}}/{{身份证号}}…），
# 填充时会把原告信息写进被告栏。这几个键需要按段落角色加前缀。
_ROLE_UNROLED = ("出生日期", "地址", "身份证号", "手机号", "电话", "民族",
                 "性别", "住所", "联系方式", "微信号")


def qualify_role_placeholders(path: Path) -> Path:
    """把「被申请人/被执行人/被告」段落里的无角色占位符改写成「被告X」。

    模板两段共用 {{地址}} 这类键时，统一取值会导致被申请人栏填进申请人的信息。
    这里按段落角色给被告段加前缀（{{地址}} -> {{被告地址}}），正文其余部分不动。
    """
    import re
    path = Path(path)
    if not zipfile.is_zipfile(path):
        return path
    entries = _read_entries(path)
    new_entries = []
    for info, data in entries:
        if info.filename != "word/document.xml":
            new_entries.append((info, data))
            continue
        xml = data.decode("utf-8", "ignore")

        def do_para(m: "re.Match") -> str:
            pxml = m.group(0)
            flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", pxml, re.S))
            head = flat.strip()
            # 只处理“被申请人/被执行人/被告”开头的段落；原告段用通用键即可
            if not re.match(r"^(被申请人|被执行人|被告[：:])", head):
                return pxml
            if "{{" not in flat:
                return pxml
            new_flat = flat
            for k in _ROLE_UNROLED:
                new_flat = new_flat.replace("{{" + k + "}}", "{{被告" + k + "}}")
            if new_flat == flat:
                return pxml
            ppr = re.search(r"<w:pPr>.*?</w:pPr>", pxml, re.S)
            ppr = ppr.group(0) if ppr else ""
            runs = re.findall(r"<w:r(?:\s[^>]*)?>.*?</w:r>", pxml, re.S)
            rpr = ""
            for r in runs:
                if "<w:b " in r or "<w:b/" in r:
                    continue
                rp = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
                if rp:
                    rpr = rp.group(0)
                    break
            if not rpr and runs:
                rp = re.search(r"<w:rPr>.*?</w:rPr>", runs[0], re.S)
                rpr = rp.group(0) if rp else ""
            body = _esc(new_flat).replace("\n", '</w:t><w:br/><w:t xml:space="preserve">')
            return f'<w:p>{ppr}<w:r>{rpr}<w:t xml:space="preserve">{body}</w:t></w:r></w:p>'

        xml = re.sub(r"<w:p[ >](?:(?!</w:p>).)*?</w:p>", do_para, xml, flags=re.S)
        new_entries.append((info, xml.encode("utf-8")))
    _write_entries(path, new_entries)
    return path


def fill_placeholders(path: Path, mapping: Dict[str, str], blank_missing: bool = False) -> Path:
    """把 {{key}} 替换为值；blank_missing=True 时，模板里有但 mapping 没给的占位符留空。

    默认只替换 mapping 里出现的键，未给出的占位符原样保留（兼容按模板部分填充的场景）。
    文书生成用 blank_missing=True，保证不残留 {{...}}，且无值字段留空。

    实战模板（法院/律所原件）里占位符常被 Word 拆到多个 run，纯 XML 字符串替换会漏掉。
    这里分两阶段：先做 run 内替换（完整保留原格式），再对残留占位符做段落级合并重建。
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
            # 多行值（诉请逐条、情况说明分段）先拆成独立段落，再逐段填充
            xml = _expand_multiline_paragraphs(xml, mapping)
            # 阶段一：run 内完整出现的占位符，直接换（不动格式）
            flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
            keys = set(re.findall(r"\{\{([^}]+)\}\}", flat))
            for key in keys:
                value = mapping.get(key)
                if value is None:
                    if not blank_missing:
                        continue
                    value = ""
                value = _esc(str(value)).replace(
                    "\n", '</w:t><w:br/><w:t xml:space="preserve">')
                xml = xml.replace("{{" + key + "}}", value)
            # 阶段二：跨 run 的残留占位符，按段落合并文本后重建
            xml = _fill_split_placeholders(xml, mapping, blank_missing)
            data = xml.encode("utf-8")
        new_entries.append((info, data))
    _write_entries(path, new_entries)
    return path


def _fill_split_placeholders(xml: str, mapping: Dict[str, str],
                             blank_missing: bool) -> str:
    """处理被 Word 拆到多个 run 的占位符：合并段落可见文本→替换→重建为单 run。

    只重写「确实含残留占位符」的段落，其余段落原样保留，避免无谓的格式损失。
    """
    import re

    def do_para(m: "re.Match") -> str:
        pxml = m.group(0)
        flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", pxml, re.S))
        toks = re.findall(r"\{\{([^}]+)\}\}", flat)
        if not toks:
            return pxml
        out = flat
        for key in set(toks):
            if "{{" + key + "}}" not in out:
                continue
            value = mapping.get(key)
            if value is None:
                if not blank_missing:
                    continue
                value = ""
            out = out.replace("{{" + key + "}}", str(value))
        if out == flat:
            return pxml
        # 取段落内一个非加粗 run 的 rPr 承载重建文本（加粗通常是标签），保留段落 pPr
        ppr = re.search(r"<w:pPr>.*?</w:pPr>", pxml, re.S)
        ppr = ppr.group(0) if ppr else ""
        runs = re.findall(r"<w:r(?:\s[^>]*)?>.*?</w:r>", pxml, re.S)
        rpr = ""
        for r in runs:
            if "<w:b " in r or "<w:b/" in r:
                continue
            rp = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
            if rp:
                rpr = rp.group(0)
                break
        if not rpr and runs:
            rp = re.search(r"<w:rPr>.*?</w:rPr>", runs[0], re.S)
            rpr = rp.group(0) if rp else ""
        body = _esc(out).replace("\n", '</w:t><w:br/><w:t xml:space="preserve">')
        return f'<w:p>{ppr}<w:r>{rpr}<w:t xml:space="preserve">{body}</w:t></w:r></w:p>'

    return re.sub(r"<w:p[ >](?:(?!</w:p>).)*?</w:p>", do_para, xml, flags=re.S)


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


def _expand_multiline_paragraphs(xml: str, mapping: Dict[str, str]) -> str:
    """把「单占位符段落」的多行值拆成多个独立段落。

    诉状里诉讼请求要一条一段（各自首行缩进、独立编号），靠 <w:br/> 软换行
    在同一段里堆着不成样子。这里在填充前把段落按 \\n 拆开，逐行生成同格式段落。
    """
    import re

    def do_para(m: "re.Match") -> str:
        pxml = m.group(0)
        flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", pxml, re.S))
        toks = re.findall(r"\{\{([^}]+)\}\}", flat)
        if len(toks) != 1:  # 只处理独占一段的单占位符
            return pxml
        val = str(mapping.get(toks[0]) or "")
        if "\n" not in val:
            return pxml
        lines = [ln.strip() for ln in val.split("\n") if ln.strip()]
        if len(lines) <= 1:
            return pxml
        ppr = re.search(r"<w:pPr>.*?</w:pPr>", pxml, re.S)
        ppr = ppr.group(0) if ppr else ""
        rpr = ""
        for r in re.findall(r"<w:r(?:\s[^>]*)?>.*?</w:r>", pxml, re.S):
            rp = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
            if rp:
                rpr = rp.group(0)
                break
        return "".join(
            f'<w:p>{ppr}<w:r>{rpr}<w:t xml:space="preserve">{_esc(ln)}</w:t></w:r></w:p>'
            for ln in lines
        )

    return re.sub(r"<w:p[ >](?:(?!</w:p>).)*?</w:p>", do_para, xml, flags=re.S)


def _align_signature_right(xml: str) -> str:
    """具状人、年月日落款右对齐（诉状惯例，模板默认是左对齐）。"""
    import re

    def do_para(m: "re.Match") -> str:
        pxml = m.group(0)
        flat = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", pxml, re.S)).strip()
        if not re.match(r"^(具状人|年\s*月\s*日)", flat):
            return pxml
        if re.search(r'<w:jc\s+w:val="[^"]*"\s*/>', pxml):
            return re.sub(r'<w:jc\s+w:val="[^"]*"\s*/>', '<w:jc w:val="right"/>', pxml, count=1)
        if "<w:pPr>" in pxml:
            return pxml.replace("<w:pPr>", '<w:pPr><w:jc w:val="right"/>', 1)
        return pxml.replace("<w:p>", '<w:p><w:pPr><w:jc w:val="right"/></w:pPr>', 1)

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
        # 多行值（诉讼请求逐条）先拆成独立段落，再逐段填充
        xml = _expand_multiline_paragraphs(xml, mapping)
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
        # 落款右对齐
        xml = _align_signature_right(xml)
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
