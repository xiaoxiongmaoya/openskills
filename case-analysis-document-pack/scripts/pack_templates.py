#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把律师实战模板包脱敏后打包进技能 templates/（平铺）。

原始模板来自真实案件（含当事人姓名、身份证号、手机号、银行账号、案号、
住址、公司名），**禁止原样入库**。

实现要点：
- 只改 `<w:p>` 段落内的 `w:t` 文本，不动样式、表格结构、页眉页脚关系；
- 所有替换**等长**（姓名→某、证件号→0、电话→13800000000），因此可以按字符
  偏移把脱敏后的文本写回原来的每个 run，版式与下划线不丢；
- .doc 旧二进制格式无法无损脱敏，一律跳过，不上仓库（留 _raw 本地用）。

用法：
  python scripts/pack_templates.py sanitize --raw _raw_doc --out templates
  python scripts/pack_templates.py scan --dir templates
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

TEXT_PART = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
PARA = re.compile(r"<w:p(?:\s[^>]*)?>.*?</w:p>", re.S)
WT = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)

PH_ORIGIN = "{{原告}}"
PH_DEFEND = "{{被告}}"
PH_PARTY = "{{当事人}}"

# 姓名不能以这些字开头（多为动词/介词），避免「受托人在我与」这类误判
NAME_BAD_HEAD = "在于是的和与及为向系将已未可应如若就并且其本该此前后对由从等再又还也都只被把让给至到之此该类各本双"

KEY = (
    "被申请人|被上诉人|申请执行人|被执行人|法定代理人|委托诉讼代理人|"
    "申请人|上诉人|原告|被告|第三人|委托人|受托人|说明人|提交人|"
    "法定代表人|负责人|指派人员|姓名|本人|兹证明|代理人|担保人|证人"
)

ROLE_ORIGIN = ("申请人", "原告", "申请执行人", "权利人", "债权人", "委托人")
ROLE_DEFEND = ("被申请人", "被告", "被执行人", "义务人", "债务人")
ROLE_LAWYER = ("指派人员", "承办律师", "承办人", "代理律师")
PH_LAWYER = "{{承办律师}}"

ADDRESS_HEAD = (
    r"(?:住址|住所地|住所|户籍所在地|户籍地|联系地址|现住址|家庭住址|送达地址|地址"
    r"|住(?=[\u4e00-\u9fa5]{2,6}(?:省|自治区|市|县|区|旗)))"
    r"(?:\s*[（(][^）)]{1,10}[）)])?"
)
ADDRESS_SKIP = ("电话", "送达", "确认", "方式", "邮箱", "微信", "变更")
ADDRESS_TOKENS = ("路", "街", "镇", "乡", "村", "屯", "组", "号", "室", "栋", "单元", "小区", "大道", "巷", "号楼", "楼", "社", "队", "嘎查", "牧委会")
COMPANY_FORM_TOKENS = ("类型", "有限责任", "股份", "上市", "企业法人", "其他", "□", "☑")

NAME_STOP_TOKENS = (
    "本案", "本院", "贵院", "双方", "对方", "原告", "被告", "申请", "被申请", "当事", "该案",
    "公司", "单位", "承担", "履行", "支付", "赔偿", "名下", "其他", "依法", "依据", "根据",
    "合同", "纠纷", "借款", "欠款", "劳务", "民间", "服务", "买卖", "建设", "交通", "财产",
    "证据", "材料", "费用", "利息", "本金", "账户", "银行", "法院", "仲裁", "律师", "委托",
)


def _role_of(keyword: str) -> str:
    for token in ROLE_LAWYER:
        if token in keyword:
            return PH_LAWYER
    for token in ROLE_DEFEND:
        if token in keyword:
            return PH_DEFEND
    for token in ROLE_ORIGIN:
        if token in keyword:
            return PH_ORIGIN
    return PH_PARTY


ROLE_SCAN = re.compile(
    r"(被申请人|被上诉人|申请执行人|被执行人|法定代理人|委托诉讼代理人|"
    r"申请人|上诉人|原告|被告|第三人|委托人|受托人|说明人|提交人|"
    r"法定代表人|负责人|指派人员|承办人|承办律师|代理律师|姓名|本人)"
)


def _role_before(text: str) -> str:
    """取姓名前最近的一个角色关键词定占位符（「被申请人」不会被拆成「申请人」）。"""
    matches = list(ROLE_SCAN.finditer(text[-40:]))
    if not matches:
        return PH_PARTY
    return _role_of(matches[-1].group(1))


def _ph_name(m: re.Match) -> str:
    name = m.group(2)
    if not name or name[0] in NAME_BAD_HEAD:
        return m.group(0)
    return m.group(1) + _role_of(m.group(1))


def _ph_known_name(m: re.Match) -> str:
    return _role_before(m.string[: m.start()])


def _ph_company(m: re.Match) -> str:
    head = m.group(1)
    if any(token in head for token in COMPANY_FORM_TOKENS):
        return m.group(0)
    return "{{公司名称}}"


def _ph_address(m: re.Match) -> str:
    prefix, body = m.group(1), m.group(2)
    if any(skip in body for skip in ADDRESS_SKIP):
        return m.group(0)
    if not any(t in body for t in ADDRESS_TOKENS):
        return m.group(0)
    return prefix + "{{地址}}"


def _ph_fixed(m: re.Match, placeholder: str) -> str:
    prefix = m.group(1) if m.lastindex and m.lastindex >= 1 else ""
    return prefix + placeholder


# 顺序敏感：先定长字段，再机构/地址，最后姓名（名单兜底放最后）
# 全部替换为 {{字段}} 占位符，不再输出「某某某」，便于后续精准替换。
RULES: List[Tuple[str, re.Pattern, Callable[[re.Match], str]]] = [
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), lambda m: "{{身份证号}}"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), lambda m: "{{手机号}}"),
    ("电话", re.compile(r"(?<![\d-])0\d{2,3}[-－]?\d{7,8}(?![\d-])"), lambda m: "{{电话}}"),
    ("银行账号", re.compile(r"(?<!\d)\d{16,25}(?!\d)"), lambda m: "{{银行账号}}"),
    ("案号", re.compile(r"[（(]\s*20\d{2}\s*[）)][^，。；;<\n]{2,30}?号"), lambda m: "{{案号}}"),
    # 「1973年11月16日出生」「出生日期：1981年10月29日」都是出生日期
    ("出生日期", re.compile(r"(?:出生日期|出生年月日|出生年月)?[：:]?\s*"
                            r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?=\s*出生)"),
     lambda m: "{{出生日期}}"),
    ("出生日期", re.compile(r"((?:出生日期|出生年月日|出生年月)[：:]?\s*)\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"),
     lambda m: m.group(1) + "{{出生日期}}"),
    ("律师事务所", re.compile(r"[\u4e00-\u9fa5]{2,8}律师事务所"), lambda m: "{{律师事务所}}"),
    ("受理法院", re.compile(r"[\u4e00-\u9fa5]{0,10}(?:省|市|州|县|区|旗)[\u4e00-\u9fa5]{0,10}人民法院"), lambda m: "{{受理法院}}"),
    ("地址", re.compile(rf"({ADDRESS_HEAD}[：:]?\s*)([^，,。；;：\n<]{{4,60}})"), _ph_address),
    ("公司名称", re.compile(r"([\u4e00-\u9fa5（）()]{2,20})"
                            r"(有限公司|有限责任公司|股份有限公司|个体工商户)"), _ph_company),
    ("姓名-行内", re.compile(r"((?:姓名|代表人|负责人)[：:]\s*)([\u4e00-\u9fa5]{2,4})\s*$"),
     lambda m: m.group(1) + "{{当事人}}"),
    ("姓名", re.compile(
        rf"((?:{KEY})[：:]?\s*)([\u4e00-\u9fa5]{{2,4}})"
        r"(?=[，,]\s*(?:男|女|汉族|公民身份号码|身份证|联系电话|电话|职业|住|系|，)"
        r"|\s*(?:性别|民族|出生))"), _ph_name),
    # 金额：人民币 10000 元 / 诉讼费 25 元
    ("金额", re.compile(r"(?<![\d.])\d{2,}(?:\.\d{1,2})?\s*[）)]?\s*(?=元)"), lambda m: "{{金额}}"),
    # 一般日期（落款、开庭时间等）：2025年 月 日 / 2026年7月1日
    ("日期", re.compile(r"\d{4}\s*年\s*(?:\d{1,2}\s*月\s*\d{1,2}\s*日|月\s*日)"),
     lambda m: "{{日期}}"),
    ("微信号", re.compile(r"wxid_[A-Za-z0-9_\-]{4,}"), lambda m: "{{微信号}}"),
    ("民族", re.compile(r"(民族[：:]\s*)(?:汉|回|满|藏|维吾尔|壮|苗|彝|土家|蒙古|布依|侗|朝鲜|其他)族?"),
     lambda m: m.group(1) + "{{民族}}"),
]

# —— 正文块占位：标题之后整段案情换成 {{字段}}，避免真实金额/日期/案情躺在模板里 ——
BLOCK_HEADINGS = {
    "申请事项": "{{申请事项}}",
    "申请执行事项": "{{申请事项}}",
    "执行请求": "{{申请事项}}",
    "事实与理由": "{{事实与理由}}",
    "事实和理由": "{{事实与理由}}",
    "执行依据": "{{执行依据}}",
    "案件基本情况": "{{案件基本情况}}",
    "执行依据判项主文": "{{执行依据}}",
    "案由": "{{案由}}",
    "文书号": "{{案号}}",
    "生效日期": "{{日期}}",
}
BLOCK_END = ("此致", "申请人", "申请执行人", "具状人", "时间：", "日期：", "特此", "签名", "盖章", "＿＿", "具状")
# 只对这些文书启用正文块占位（说明类/表单类不做块处理，避免误伤模板说明文字）
BLOCK_FILES = {
    "财产保全申请书.docx",
    "强制执行申请书.docx",
    "延期开庭申请书.docx",
    "执行结案申请书.docx",
    "在线开庭书.docx",
    "撤回财产保全申请书.docx",
    "要素式执行.docx",
    "追加被告申请书.doc",
}
# 情况说明类：以「对于/本人」开头的长段落是案情叙述，占位成 {{情况说明}}
STORY_PREFIX = ("对于", "本人已依法", "本人于", "本人与", "我于", "我方因")

# 由 _raw 原件自动提取的当事人姓名，脱敏时全文命中替换（正文里的姓名靠这条兜住）
_KNOWN_NAMES: List[str] = []
_KNOWN_NAME_PATTERN: Optional[re.Pattern] = None


def load_known_names(raw_dir: Path) -> List[str]:
    """从原始包里按「姓名，男/女，X族」和「关键词：姓名」抽真实姓名，作为兜底名单。"""
    found: set[str] = set()
    patterns = [
        re.compile(r"([\u4e00-\u9fa5]{2,4})[，,]\s*(?:男|女)[，,]\s*(?:汉族|回族|满族|藏族|维吾尔族|壮族|苗族|彝族|土家族|蒙古族|布依族|侗族|朝鲜族|其他)"),
        re.compile(r"(?:申请人|被申请人|原告|被告|申请执行人|被执行人|说明人|提交人|委托人|受托人)"
                   r"[：:]?\s*([\u4e00-\u9fa5]{2,4})(?=[，,]\s*(?:男|女|汉族))"),
        re.compile(r"(?:与|和)(?:被告|被申请人|原告|申请人|第三人)([\u4e00-\u9fa5]{2,4}?)"
                   r"(?=(?:劳务|民间|服务|买卖|建设|交通|合同|纠纷|借|支付|赔偿|承担|名下))"),
        re.compile(r"本人([\u4e00-\u9fa5]{2,3})(?=[，,]\s*(?:身份证|男|女|系|，))"),
        re.compile(r"(?:指派人员|承办人|承办律师|代理律师|执业律师)[：:]?\s*([\u4e00-\u9fa5]{2,4})\s*(?=[，,]\s*(?:职业|执业|律师))"),
        re.compile(r"姓名[：:]\s*([\u4e00-\u9fa5]{2,4})\s*(?=\n|$)"),
        re.compile(r"(?:与|偿还申请人|向原告|判令被告|判令被申请人)([\u4e00-\u9fa5]{2,4}?)"
                   r"(?=(?:劳务|民间借贷|服务|买卖|建设|交通|合同|纠纷|借|支付|赔偿|承担|名下))"),
    ]
    for path in sorted(raw_dir.glob("*.docx")):
        if not zipfile.is_zipfile(path):
            continue
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not TEXT_PART.match(name):
                    continue
                xml = z.read(name).decode("utf-8", "ignore")
                xml = re.sub(r"</w:p>", "\n", xml)
                flat = re.sub(r"<[^>]+>", "", xml)
                for pattern in patterns:
                    for m in pattern.finditer(flat):
                        candidate = m.group(1)
                        if not candidate or candidate[0] in NAME_BAD_HEAD:
                            continue
                        if any(bad in candidate for bad in NAME_STOP_TOKENS):
                            continue
                        found.add(candidate)
    return sorted(found, key=len, reverse=True)


def _install_names(names: List[str]) -> None:
    global _KNOWN_NAMES, _KNOWN_NAME_PATTERN, RULES
    _KNOWN_NAMES = list(names)
    _KNOWN_NAME_PATTERN = re.compile("|".join(re.escape(n) for n in names)) if names else None
    if _KNOWN_NAME_PATTERN is not None:
        RULES = [r for r in RULES if r[0] != "姓名-名单"]
        RULES.append(("姓名-名单", _KNOWN_NAME_PATTERN, _ph_known_name))


def _mask_plain(text: str, stats: Dict[str, int]) -> str:
    out = text
    for label, pattern, repl in RULES:
        def _sub(m: re.Match) -> str:
            masked = repl(m)
            if masked != m.group(0):
                stats[label] = stats.get(label, 0) + 1
            return masked
        out = pattern.sub(_sub, out)
    return out


def _redistribute(paragraph: str, stats: Dict[str, int]) -> str:
    """段落级脱敏：拼接所有 w:t → 脱敏 → 按原偏移写回。"""
    runs = list(WT.finditer(paragraph))
    if not runs:
        return paragraph
    texts = [m.group(2) for m in runs]
    joined = "".join(texts)
    if not joined.strip():
        return paragraph
    masked = _mask_plain(joined, stats)
    if masked == joined:
        return paragraph
    if len(masked) != len(joined):
        # 兜底：整段塞进第一个 run（等长设计下正常不会走到这里）
        pieces = [masked] + [""] * (len(texts) - 1)
    else:
        pieces, pos = [], 0
        for t in texts:
            pieces.append(masked[pos: pos + len(t)])
            pos += len(t)
    out, cursor = [], 0
    for idx, m in enumerate(runs):
        out.append(paragraph[cursor: m.start(2)])
        out.append(pieces[idx])
        cursor = m.end(2)
    out.append(paragraph[cursor:])
    return "".join(out)


def _para_plain(para: str) -> str:
    return "".join(m.group(2) for m in WT.finditer(para))


def _rewrite_para(para: str, text: str) -> str:
    """把整段文本写进第一个 run，其余 run 清空（保留段落样式与表格结构）。"""
    runs = list(WT.finditer(para))
    if not runs:
        return para
    out, cursor = [], 0
    for idx, m in enumerate(runs):
        out.append(para[cursor: m.start(2)])
        out.append(text if idx == 0 else "")
        cursor = m.end(2)
    out.append(para[cursor:])
    return "".join(out)


def _is_heading(text: str) -> str:
    """判断是不是「申请事项 / 事实与理由」这类标题段，返回对应占位符。"""
    body = re.sub(r"^[一二三四五六七八九十\s、.．]+", "", text.strip()).strip("：: ")
    if not body or len(body) > 12:
        return ""
    return BLOCK_HEADINGS.get(body, "")


def _is_block_end(text: str) -> bool:
    """落款/此致这类短行才算块结束；正文「申请人与被申请人…」不能误判。"""
    body = text.strip()
    if not body or len(body) > 20:
        return False
    return any(body.startswith(marker) for marker in BLOCK_END)


def _sanitize_xml(xml: str, stats: Dict[str, int], filename: str = "") -> str:
    use_blocks = filename in BLOCK_FILES
    is_statement = "情况说明" in filename
    state = {"ph": "", "filled": False, "story_done": False}

    def _one(match: re.Match) -> str:
        para = match.group(0)
        text = _para_plain(para)
        stripped = text.strip()

        if use_blocks:
            heading_ph = _is_heading(stripped)
            if heading_ph:
                state["ph"], state["filled"] = heading_ph, False
                return _redistribute(para, stats)
            if state["ph"]:
                if not stripped or _is_block_end(stripped):
                    state["ph"] = ""
                else:
                    new_text = "" if state["filled"] else state["ph"]
                    if not state["filled"]:
                        state["filled"] = True
                        stats["正文块"] = stats.get("正文块", 0) + 1
                    return _rewrite_para(para, new_text)

        if is_statement and len(stripped) > 40 and stripped.startswith(STORY_PREFIX):
            if not state["story_done"]:
                state["story_done"] = True
                stats["情况说明正文"] = stats.get("情况说明正文", 0) + 1
                return _rewrite_para(para, "{{情况说明}}")
            return _rewrite_para(para, "")

        return _redistribute(para, stats)

    return PARA.sub(_one, xml)


def sanitize_docx(src: Path, dst: Path) -> Dict[str, int]:
    stats: Dict[str, int] = {}
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():  # 目标可能被设成只读，先解锁再写
        try:
            dst.chmod(0o666)
        except OSError:
            pass
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if TEXT_PART.match(item.filename):
                data = _sanitize_xml(data.decode("utf-8", "ignore"), stats, src.name).encode("utf-8")
            zout.writestr(item, data)
    return stats


def scan(path: Path) -> Dict[str, int]:
    found: Dict[str, int] = {}
    if not zipfile.is_zipfile(path):
        return found
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not TEXT_PART.match(name):
                continue
            xml = z.read(name).decode("utf-8", "ignore")
            for paragraph in PARA.findall(xml):
                flat = "".join(m.group(2) for m in WT.finditer(paragraph))
                for label, pattern, repl in RULES:
                    for m in pattern.finditer(flat):
                        if repl(m) != m.group(0):
                            found[label] = found.get(label, 0) + 1
    return found


def _target_map() -> Dict[str, str]:
    """原包文件名 -> templates 下的规范文件名（唯一来源：manage_templates 登记表）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import manage_templates as mt
    except Exception:
        return {}
    return {v["src"]: v["file"] for v in mt.TEMPLATES.values()}


def cmd_sanitize(raw: Path, out: Path) -> int:
    if not raw.is_dir():
        raise SystemExit(f"原始模板目录不存在：{raw}")
    out.mkdir(parents=True, exist_ok=True)
    target_map = _target_map()
    names = load_known_names(raw)
    _install_names(names)
    manifest: Dict[str, object] = {"templates": {}, "known_names": names}
    skipped: List[str] = []
    total: Dict[str, int] = {}
    for src in sorted(raw.iterdir()):
        if src.is_dir():
            continue
        if src.suffix.lower() != ".docx" or not zipfile.is_zipfile(src):
            skipped.append(src.name)
            continue
        rel = target_map.get(src.name)
        if not rel:
            skipped.append(f"{src.name}（未在 manage_templates 登记，跳过）")
            continue
        dst = out / rel
        try:
            stats = sanitize_docx(src, dst)
        except PermissionError:
            skipped.append(f"{src.name}（文件被占用，跳过）")
            continue
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
        manifest["templates"][src.name] = {
            "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
            "bytes": dst.stat().st_size,
            "masked": stats,
            "residual": scan(dst),
        }
    print(json.dumps({
        "ok": True,
        "out": str(out),
        "sanitized": len(manifest["templates"]),
        "skipped_non_docx": skipped,
        "masked_total": total,
        "known_names": names,
        "residual_files": [k for k, v in manifest["templates"].items() if v["residual"]],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_scan(target: Path) -> int:
    files = [p for p in sorted(target.rglob("*.docx")) if "_raw" not in p.parts]
    rows = {str(p.relative_to(target)): scan(p) for p in files}
    dirty = {k: v for k, v in rows.items() if v}
    print(json.dumps({"checked": len(rows), "dirty": dirty}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="实战模板脱敏打包")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sanitize")
    s.add_argument("--raw", required=True)
    s.add_argument("--out", required=True)
    c = sub.add_parser("scan")
    c.add_argument("--dir", required=True)
    args = ap.parse_args()
    if args.cmd == "sanitize":
        return cmd_sanitize(Path(args.raw), Path(args.out))
    return cmd_scan(Path(args.dir))


if __name__ == "__main__":
    raise SystemExit(main())
