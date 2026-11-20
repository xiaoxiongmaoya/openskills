#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把聊天导出文本/Word 与截图解析成带来源定位的证据序列。

只写案件目录，不写 Skill 目录、工作空间根或个人目录。
截图 OCR 为可选能力：缺依赖时标 needs_review，不把猜测当事实。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
OLD_SCRIPTS = HERE.parent.parent / "court-civil-first-instance-filing" / "scripts"
if OLD_SCRIPTS.exists():
    sys.path.insert(0, str(OLD_SCRIPTS))
from read_office_text import clean, read_doc, read_docx  # noqa: E402

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
# 语义名副本（rename_images.py 生成）跳过，原件才是证据来源 key
SEMANTIC_PREFIX = "语义_"
DOC_EXT = {".doc", ".docx"}
TXT_EXT = {".txt", ".md"}

TIME_RE = re.compile(
    r"(?:(?P<y>\d{4})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})日?\s*)?"
    r"(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?"
)
PREFIX_RE = re.compile(
    r"^\s*(?:\[(?P<br>[^\]]+)\]\s*)?(?:(?P<dt>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?\s+\d{1,2}:\d{2}(?::\d{2})?)\s*)?"
    r"(?P<who>[^:：\s][^:：]{0,30}?)\s*[:：]\s*(?P<body>.*)$"
)
AMOUNT_RE = re.compile(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*(万元|元)")
DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")

TAG_RULES = [
    ("借款合意", ("借", "借款", "借给", "欠我")),
    ("交付", ("转账", "已转", "打款", "汇了", "收到")),
    ("还款", ("还款", "还了", "归还", "还钱")),
    ("催款", ("催", "什么时候还", "尽快还", "还欠")),
    ("利息", ("利息", "利率", "月息")),
    ("身份", ("身份证", "微信号", "银行卡")),
]


def _rel(case_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(case_dir.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _norm_time(br: Optional[str], dt: Optional[str], line: str) -> Tuple[str, str]:
    raw = (dt or br or line).strip()
    m = TIME_RE.search(raw)
    if not m:
        return "", "null"
    d = DATE_RE.search(raw)
    date_part = d.group(0) if d else ""
    date_part = date_part.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    if date_part:
        parts = [p for p in date_part.split("-") if p]
        if len(parts) == 3:
            date_part = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    hh = int(m.group("H"))
    mm = int(m.group("M"))
    ss = int(m.group("S") or 0)
    if hh > 23 or mm > 59:
        return "", "null"
    stamp = f"{date_part} {hh:02d}:{mm:02d}:{ss:02d}".strip()
    return stamp, "high" if date_part else "low"


def _tags(text: str) -> List[str]:
    return [name for name, words in TAG_RULES if any(w in text for w in words)]


def _content_type(text: str) -> str:
    if "[图片]" in text or "[照片]" in text:
        return "image"
    if "转账" in text or "已转" in text:
        return "transfer"
    if "红包" in text:
        return "redpacket"
    return "text"


def _line_to_msg(line: str, source_file: str, locator: str, seq: int) -> Optional[Dict[str, Any]]:
    text = line.strip()
    if not text or len(text) < 2:
        return None
    # 系统提示行跳过
    if text.startswith(("以下为", "聊天记录", "导出时间", "---", "===")):
        return None
    m = PREFIX_RE.match(text)
    if m:
        who = (m.group("who") or "").strip()
        body = (m.group("body") or "").strip()
        stamp, conf = _norm_time(m.group("br"), m.group("dt"), text)
    else:
        who, body = "", text
        stamp, conf = _norm_time("", "", text)
    if not body:
        return None
    amount = ""
    am = AMOUNT_RE.search(body)
    if am:
        number = float(am.group(1))
        if am.group(2) == "万元":
            number *= 10000
        amount = str(int(number)) if number.is_integer() else f"{number:.2f}"
    return {
        "id": f"m-{seq:04d}",
        "source_file": source_file,
        "source_locator": locator,
        "timestamp": stamp,
        "timestamp_confidence": conf,
        "sender": who,
        "recipient": "",
        "content": body,
        "content_type": _content_type(body),
        "amount": amount,
        "tags": _tags(body),
        "needs_review": False,
    }


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _read_word(path: Path) -> str:
    try:
        raw = read_docx(path) if path.suffix.lower() == ".docx" else read_doc(path)
        return clean(raw)
    except Exception as exc:  # 由 warnings 记录，不中断整批
        raise RuntimeError(f"读取 Word 失败：{exc}") from exc


def _ocr_available() -> bool:
    try:
        import PIL  # noqa: F401
        import pytesseract  # noqa: F401
        return True
    except Exception:
        return False


def _ocr_image(path: Path) -> Tuple[str, float, str]:
    if not _ocr_available():
        return "", 0.0, "pytesseract 不可用，图片未识别；请人工核对"
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(path), lang="chi_sim+eng")
        return text.strip(), 0.8, "pytesseract"
    except Exception as exc:
        return "", 0.0, f"OCR 失败：{exc}"


def _collect_inputs(case_dir: Path, source: Optional[Path]) -> Tuple[List[Path], List[Path], List[Path]]:
    images: List[Path] = []
    texts: List[Path] = []
    words: List[Path] = []
    roots = [case_dir / "01原始包", case_dir / "03图片件", case_dir / "04未签稿"]
    if source:
        roots.append(source)
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for p in paths:
            if not p.is_file() or any(part.startswith(".") for part in p.parts):
                continue
            if p.name.startswith(SEMANTIC_PREFIX):
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            ext = p.suffix.lower()
            if root == case_dir / "04未签稿" and ext in DOC_EXT:
                if not any(key in p.name for key in ("聊天", "导出", "记录")):
                    continue
            if ext in IMG_EXT:
                images.append(p)
            elif ext in DOC_EXT:
                words.append(p)
            elif ext in TXT_EXT:
                texts.append(p)
    return images, words, texts


def parse(case_dir: Path, source: Optional[Path], threshold: float,
          ocr_json: Optional[Path], participants_json: Optional[Path]) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    images, words, texts = _collect_inputs(case_dir, source)
    sources: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seq = 0

    ocr_map: Dict[str, List[Dict[str, Any]]] = {}
    if ocr_json and ocr_json.exists():
        try:
            data = json.loads(ocr_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ocr_map = {str(k): list(v or []) for k, v in data.items()}
        except Exception as exc:
            warnings.append(f"OCR 补充文件解析失败：{exc}")

    for path in texts:
        rel = _rel(case_dir, path)
        try:
            content = _read_text_file(path)
        except Exception as exc:
            warnings.append(f"读取文本失败 {rel}：{exc}")
            continue
        sources.append({"file": rel, "type": "text", "encoding": "utf-8"})
        for idx, line in enumerate(content.splitlines(), 1):
            msg = _line_to_msg(line, rel, f"{rel}:L{idx}", seq + 1)
            if msg:
                messages.append(msg)
                seq += 1

    for path in words:
        rel = _rel(case_dir, path)
        try:
            content = _read_word(path)
        except Exception as exc:
            warnings.append(str(exc))
            continue
        sources.append({"file": rel, "type": path.suffix.lower().lstrip(".")})
        for idx, line in enumerate(content.splitlines(), 1):
            msg = _line_to_msg(line, rel, f"{rel}:L{idx}", seq + 1)
            if msg:
                messages.append(msg)
                seq += 1

    for path in images:
        rel = _rel(case_dir, path)
        supplied = ocr_map.get(rel) or ocr_map.get(path.name) or []
        if supplied:
            text_lines = [str(x.get("content") or "") for x in supplied if isinstance(x, dict)]
            text = "\n".join(t for t in text_lines if t.strip())
            confidence = 0.9
            engine = "agent-vision"
            note = ""
        else:
            text, confidence, engine = _ocr_image(path)
            note = "OCR 未安装，需人工看图补录" if confidence == 0.0 else ""
        needs_review = confidence < threshold or not text.strip()
        sources.append({
            "file": rel, "type": "image", "ocr_engine": engine,
            "confidence": round(confidence, 2), "needs_review": needs_review,
        })
        for idx, line in enumerate(text.splitlines(), 1):
            msg = _line_to_msg(line, rel, f"{rel}:图内第{idx}行", seq + 1)
            if not msg:
                continue
            msg["needs_review"] = needs_review
            if confidence < threshold:
                warnings.append(f"OCR 置信度低需人工核对：{rel}（{confidence:.2f}）")
            messages.append(msg)
            seq += 1
        if needs_review:
            messages.append({
                "id": f"m-{seq:04d}",
                "source_file": rel,
                "source_locator": f"{rel}:整图",
                "timestamp": "",
                "timestamp_confidence": "null",
                "sender": "",
                "recipient": "",
                "content": note or "图片内容未可靠识别，需人工补录",
                "content_type": "image",
                "amount": "",
                "tags": [],
                "needs_review": True,
            })
            seq += 1

    # 去重：同源同位置同内容
    dedup: List[Dict[str, Any]] = []
    keys: set[Tuple[str, str, str]] = set()
    for msg in messages:
        key = (msg["source_file"], msg["source_locator"], msg["content"])
        if key in keys:
            continue
        keys.add(key)
        dedup.append(msg)

    participants: List[Dict[str, Any]] = []
    mapping: Dict[str, str] = {}
    if participants_json and participants_json.exists():
        try:
            raw = json.loads(participants_json.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                mapping = {str(k): str(v) for k, v in raw.items()}
        except Exception as exc:
            warnings.append(f"参与者映射解析失败：{exc}")
    for sender in sorted({m["sender"] for m in dedup if m["sender"]}):
        participants.append({
            "raw": sender,
            "normalized": mapping.get(sender, sender),
            "role": "",
            "map_source": "rule" if sender in mapping else "raw",
        })

    dates = sorted({m["timestamp"][:10] for m in dedup if m["timestamp"] and len(m["timestamp"]) >= 10})
    result: Dict[str, Any] = {
        "case_dir": str(case_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": sources,
        "participants": participants,
        "messages": dedup,
        "warnings": warnings,
        "stats": {
            "message_count": len(dedup),
            "image_count": len(images),
            "date_range": [dates[0], dates[-1]] if dates else [],
            "low_confidence_count": sum(1 for m in dedup if m["needs_review"]),
            "source_count": len(sources),
        },
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="解析聊天记录为带来源定位的证据序列")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--source", default="", help="额外来源文件或目录（可选）")
    ap.add_argument("--confidence-threshold", type=float, default=0.75)
    ap.add_argument("--ocr-json", default="", help="Agent 视觉识别补充 JSON：{图片相对路径:[{content,...}]}")
    ap.add_argument("--participants-json", default="", help="昵称映射 JSON：{'昵称':'真实姓名'}")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")
    source = Path(args.source).resolve() if args.source else None
    ocr_json = Path(args.ocr_json).resolve() if args.ocr_json else None
    participants_json = Path(args.participants_json).resolve() if args.participants_json else None

    result = parse(case_dir, source, args.confidence_threshold, ocr_json, participants_json)
    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "stats": result["stats"],
            "warnings": result["warnings"][:10],
        }, ensure_ascii=False, indent=2))
        return 0

    out = case_dir / "05案件信息" / "chat_evidence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(out),
        "stats": result["stats"],
        "warnings": result["warnings"][:10],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
