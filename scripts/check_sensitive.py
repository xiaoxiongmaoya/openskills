#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""敏感信息红线检查：提交前拦截当事人隐私。

红线（不可商量）：skills 与项目仓库不得出现真实当事人信息——
姓名、身份证号、手机号、银行卡号、完整案号、住址门牌，无论写在代码、
文档、测试样例还是 Word 模板里。示例一律用张三/李四、13800000000、
91330000TEST000001 这类假数据。

用法：
  python scripts/check_sensitive.py --staged     # 只查 git 暂存区（pre-commit 用它）
  python scripts/check_sensitive.py --all        # 查整个仓库
  python scripts/check_sensitive.py --paths a.py b.md
退出码 1 = 命中隐私，禁止提交。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

REPO = Path(__file__).resolve().parent.parent
CASES_DIR = Path("D:/Project/cases/案件库")

TEXT_SUFFIX = {".py", ".md", ".json", ".txt", ".yaml", ".yml", ".sh", ".bat",
               ".csv", ".html", ".htm", ".js", ".ts", ".tsx", ".jsx", ".xml", ".ini", ".toml"}
DOCX_SUFFIX = {".docx", ".xlsx", ".pptx"}

# 假数据白名单：示例/占位/测试固定值，命中这些不算泄露
WHITELIST = (
    "13800000000", "13900000000", "18600000000",
    "91330000TEST", "110101199001010000", "000000000000000000",
    "123456789012345678", "370101199001011234",
)
# 明确允许的示例姓名
DEMO_NAMES = {"张三", "李四", "王五", "赵六", "某甲", "某乙", "甲公司", "乙公司"}

PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("身份证号", re.compile(r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                          r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("银行卡号", re.compile(r"(?<!\d)(?:62|60|43|45|51|52|53|54|55)\d{14,17}(?!\d)")),
    ("完整案号", re.compile(r"[（(]\d{4}[）)][\u4e00-\u9fa5]{1,3}\d{0,4}"
                        r"(?:民初|民终|民申|执|执恢|刑初|行初)\d+号")),
    ("街道门牌", re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|市|县|区)[\u4e00-\u9fa5]{2,12}"
                        r"(?:路|街|道|巷|村|屯|组)\d+号?")),
]


def _case_names(cases_dir: Path) -> List[str]:
    """从案件目录名解析当事人姓名：<日期>-<fyid>-<原告>诉<被告>-<案由>。"""
    names: List[str] = []
    if not cases_dir.exists():
        return names
    for child in cases_dir.iterdir():
        if not child.is_dir():
            continue
        parts = child.name.split("-")
        if len(parts) < 4:
            continue
        for side in parts[2].split("诉"):
            name = side.strip().replace("等", "")
            if 2 <= len(name) <= 4 and name not in DEMO_NAMES:
                names.append(name)
    return sorted(set(names), key=len, reverse=True)


def _read_text(path: Path) -> str:
    if path.suffix.lower() in DOCX_SUFFIX:
        try:
            with zipfile.ZipFile(path) as zf:
                chunks = []
                for member in zf.namelist():
                    if member.endswith(".xml") and ("document" in member or "sharedStrings" in member):
                        raw = zf.read(member).decode("utf-8", "ignore")
                        chunks.append(re.sub(r"<[^>]+>", "", raw))
                return "\n".join(chunks)
        except Exception:
            return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _scannable(paths: Iterable[Path]) -> List[Path]:
    out = []
    for p in paths:
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in TEXT_SUFFIX or suffix in DOCX_SUFFIX:
            out.append(p)
    return out


def _staged_paths() -> List[Path]:
    try:
        raw = subprocess.run(["git", "-C", str(REPO), "diff", "--cached", "--name-only",
                              "--diff-filter=ACMR"],
                             capture_output=True, text=True, check=True).stdout
    except Exception:
        return []
    return [REPO / line.strip() for line in raw.splitlines() if line.strip()]


def _tracked_paths() -> List[Path]:
    try:
        raw = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                             capture_output=True, text=True, check=True).stdout
    except Exception:
        return []
    return [REPO / line.strip() for line in raw.splitlines() if line.strip()]


def scan(paths: List[Path], names: List[str]) -> List[Dict[str, object]]:
    hits: List[Dict[str, object]] = []
    for path in _scannable(paths):
        text = _read_text(path)
        if not text:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0)
                if any(w in value for w in WHITELIST):
                    continue
                if "某" in value:  # “某市某区某路1号”这类演示写法
                    continue
                if label == "完整案号" and any(
                        x in value for x in ("0000", "0100", "0123", "1234", "示例")):
                    continue  # 占位法院代码/“示例”字样，明显不是真案号
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append({"file": str(path.relative_to(REPO)), "line": line_no,
                             "type": label, "value": value})
        for name in names:
            idx = text.find(name)
            if idx >= 0:
                hits.append({"file": str(path.relative_to(REPO)),
                             "line": text.count("\n", 0, idx) + 1,
                             "type": "真实当事人姓名", "value": name})
    # 同一文件同一行同一类型去重
    seen = set()
    unique = []
    for h in hits:
        key = (h["file"], h["line"], h["type"], h["value"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    return unique


def main() -> int:
    ap = argparse.ArgumentParser(description="敏感信息红线检查")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="只查 git 暂存区")
    group.add_argument("--all", action="store_true", help="查全部已跟踪文件")
    ap.add_argument("--paths", nargs="*", default=[], help="指定文件")
    ap.add_argument("--cases-dir", default=str(CASES_DIR),
                    help="案件库目录，用于提取真实当事人姓名做黑名单")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
    elif args.staged:
        targets = _staged_paths()
    elif args.all:
        targets = _tracked_paths()
    else:
        targets = _tracked_paths()

    names = _case_names(Path(args.cases_dir))
    hits = scan(targets, names)
    if args.json:
        import json
        print(json.dumps({"hits": hits, "blacklist_names": names}, ensure_ascii=False, indent=2))
    elif hits:
        print("红线拦截：以下位置含真实当事人信息，禁止提交/写入文档", file=sys.stderr)
        for h in hits:
            print(f"  {h['file']}:{h['line']}  [{h['type']}] {h['value']}", file=sys.stderr)
        print("改法：姓名→张三/李四，身份证→110101199001010000，"
              "手机→13800000000，银行卡→123456789012345678，案号→（2024）示例民初1号",
              file=sys.stderr)
    else:
        print(f"红线检查通过：{len(targets)} 个文件，{len(names)} 个当事人姓名已排除",
              file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
