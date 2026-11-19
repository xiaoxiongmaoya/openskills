#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多源法律检索层：华宇元典 + 企查查法律数据。

本脚本不直接调用 MCP；Agent 层用 ToolSearch 反查真实工具名后，用 DeferExecuteTool 执行。
脚本负责：探测、构造调用参数、校验返回、双源合并与冲突标记。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROVIDERS = ("yuandian", "qcc")
PROVIDER_NAME = {"yuandian": "华宇元典", "qcc": "企查查·法律数据"}

EXPECTED = {
    "yuandian": {
        "law": "mcp__yuandian-mcp__yuandian_semantic_search_legal_articles",
        "case": "mcp__yuandian-mcp__yuandian_semantic_search_cases",
    },
    "qcc": {
        "law": "mcp__qcc-legal__get_legal_article_semantic_search",
        "case": "mcp__qcc-legal__get_judicial_case_search",
    },
}

CANDIDATES = {
    "qcc": {
        "law": [
            "mcp__qcc-legal__get_legal_article_semantic_search",
            "mcp__qcc-legal-regulation__get_legal_article_semantic_search",
            "get_legal_article_semantic_search",
        ],
        "case": [
            "mcp__qcc-legal__get_judicial_case_search",
            "mcp__qcc-legal-case__get_judicial_case_search",
            "get_judicial_case_search",
        ],
    },
    "yuandian": {
        "law": [
            "mcp__yuandian-mcp__yuandian_semantic_search_legal_articles",
            "mcp__yuandian-mcp__yuandian_law_vector_search",
        ],
        "case": [
            "mcp__yuandian-mcp__yuandian_semantic_search_cases",
            "mcp__yuandian-mcp__yuandian_case_vector_search",
        ],
    },
}

# 元典案例过滤的 province 只接受简称（浙江），不接受“浙江省”
PROVINCE_SHORT = {
    "北京市": "北京", "天津市": "天津", "上海市": "上海", "重庆市": "重庆",
    "河北省": "河北", "山西省": "山西", "辽宁省": "辽宁", "吉林省": "吉林",
    "黑龙江省": "黑龙江", "江苏省": "江苏", "浙江省": "浙江", "安徽省": "安徽",
    "福建省": "福建", "江西省": "江西", "山东省": "山东", "河南省": "河南",
    "湖北省": "湖北", "湖南省": "湖南", "广东省": "广东", "海南省": "海南",
    "四川省": "四川", "贵州省": "贵州", "云南省": "云南", "陕西省": "陕西",
    "甘肃省": "甘肃", "青海省": "青海", "台湾省": "台湾",
    "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西", "西藏自治区": "西藏",
    "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆",
    "香港特别行政区": "香港", "澳门特别行政区": "澳门",
}


def _province_short(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    if text in PROVINCE_SHORT:
        return PROVINCE_SHORT[text]
    for suffix in ("省", "市", "自治区", "特别行政区"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text

PAUSE_TEXT = (
    "第三方法律数据源未连接完整：案件分析需要同时连接华宇元典（yuandian-mcp）与企查查法律数据"
    "（qcc-legal）。未连接部分已暂停检索；报告只呈现有来源的事实，不输出未检索到的法条/案号，"
    "不虚构，也不以记忆或推断代替检索结果。请连接后重跑。"
)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _lookup_tool(resolved: Dict[str, Any], provider: str, kind: str) -> str:
    keys = [f"{provider}.{kind}", EXPECTED[provider][kind], *CANDIDATES.get(provider, {}).get(kind, [])]
    for key in keys:
        value = resolved.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is True:
            return key
    return ""


def probe(case_dir: Path, tools_json: Path, required: Tuple[str, ...]) -> int:
    case_dir = case_dir.resolve()
    resolved: Dict[str, Any] = {}
    if tools_json.exists():
        try:
            loaded = _load_json(tools_json)
            if isinstance(loaded, dict):
                resolved = loaded
        except Exception as exc:
            resolved = {"__error__": str(exc)}

    providers: Dict[str, Any] = {}
    missing: List[str] = []
    for provider in required:
        tools = {kind: _lookup_tool(resolved, provider, kind) for kind in ("law", "case")}
        ok = all(tools.values())
        providers[provider] = {
            "name": PROVIDER_NAME[provider],
            "connected": ok,
            "tools": tools,
            "missing": [k for k, v in tools.items() if not v],
        }
        missing += [f"{provider}.{k}" for k, v in tools.items() if not v]

    status = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "required_providers": list(required),
        "providers": providers,
        "connected": not missing and "__error__" not in resolved,
        "missing": missing,
        "pause_text": PAUSE_TEXT if missing else "",
    }
    _write_json(case_dir / "05案件信息" / "mcp_status.json", status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["connected"] else 1


def build_call(provider: str, kind: str, query: str, rewrite_flag: bool, province: str,
               fatiao_filter: Optional[Dict[str, Any]], return_num: int, out: Optional[Path]) -> int:
    if provider not in PROVIDERS:
        raise SystemExit(f"provider 只能是 {'/'.join(PROVIDERS)}：{provider}")
    if kind not in ("law", "case"):
        raise SystemExit(f"kind 只能是 law 或 case：{kind}")

    if provider == "yuandian":
        params: Dict[str, Any] = {
            "query": query,
            "rewrite_enabled": rewrite_flag,
            "limit": return_num,
        }
        if kind == "law":
            params["law_article_filter"] = fatiao_filter or {"validity_status": ["现行有效"]}
        else:
            case_filter: Dict[str, Any] = {"decision_date_start": "2021-01-01"}
            if province:
                case_filter["province"] = _province_short(province)
            params["case_document_filter"] = case_filter
        note = "先用 ToolSearch 反查 expected_tool 的真实全名，再用 DeferExecuteTool 调用。"
    else:
        if kind == "law":
            params = {"query": query, "effectStatus": ["现行有效"]}
            note = "企查查法规语义检索：query 直接传完整自然语言问题，不要压成关键词。"
        else:
            params = {
                "keyword": query,
                "caseType": ["民事案件"],
                "docType": ["判决书"],
                "judgeDateGte": "2021-01-01",
            }
            if province:
                params["province"] = [_province_short(province)]
            note = "企查查普通案例检索：keyword 传案情关键词；权威案例需另用权威案例工具。"

    payload = {
        "provider": provider,
        "provider_name": PROVIDER_NAME[provider],
        "kind": kind,
        "expected_tool": EXPECTED[provider][kind],
        "params": params,
        "note": note + " 本脚本不联网。",
    }
    if out is not None:
        _write_json(out, payload)
        print(json.dumps({"ok": True, "output": str(out), "payload": payload}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _extract_items(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        # 元典：results.law_article_list / results.document
        # 企查查：顶层「法条列表」/「案例列表」（中文键）
        for key in ("law_article_list", "document", "documents",
                    "法条列表", "案例列表", "法规列表", "laws", "articles", "cases",
                    "case_list", "data", "list", "items", "records", "results", "rows"):
            if key in obj:
                found = _extract_items(obj[key])
                if found:
                    return found
        if any(k in obj for k in ("title", "name", "content", "case_number", "案号", "caseNo")):
            return [obj]
    return []


def _fmt_date(value: Any) -> str:
    """元典返回 20250730 这种数字日期，统一成 2025-07-30。"""
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and text[:8] == digits[:8]:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _pick(item: Dict[str, Any], names: List[str], default: str = "") -> str:
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _source_id(item: Dict[str, Any], prefix: str) -> str:
    raw = _pick(item, ["法条ID", "案例ID", "article_id", "scid", "id", "source_id", "url",
                       "case_number", "caseNo", "案号", "title", "name"])
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _normalize_law(items: List[Dict[str, Any]], provider: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        title = _pick(item, ["所属法规名称", "regulation_name", "title", "name", "法规名称",
                             "law_name", "regulationName"])
        article = _pick(item, ["条号", "article_number", "article", "article_no", "条款",
                               "法条号", "articleNo"])
        content = _pick(item, ["条文正文", "content", "text", "条文内容", "article_content",
                               "articleContent"])
        if not (title or article or content):
            continue
        sid = _source_id(item, "LAW")
        if sid in seen:
            continue
        seen.add(sid)
        out.append({
            "source_id": sid,
            "source_provider": provider,
            "title": title,
            "article": article,
            "content": content,
            "validity": _pick(item, ["时效性", "validity_status", "validity", "effectStatus", "status"]),
            "effect_level": _pick(item, ["效力级别一级", "primary_effect_level", "effectRank"]),
            "url": _pick(item, ["引用链接", "url", "link"]),
        })
        if len(out) >= 30:
            break
    return out


def _date_ok(item: Dict[str, Any]) -> bool:
    date_text = _pick(item, ["decision_date", "裁判日期", "jbdh", "date", "结案日期",
                             "judgmentDate", "judgment_date", "裁判时间"])
    nums = "".join(ch for ch in date_text if ch.isdigit())
    if len(nums) < 8:
        return False
    year, month, day = int(nums[:4]), int(nums[4:6]), int(nums[6:8])
    return (year, month, day) >= (2021, 1, 1)


def _normalize_case(items: List[Dict[str, Any]], province: str,
                    provider: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    kept: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not _date_ok(item):
            warnings.append("已过滤：裁判日期早于 2021-01-01 或缺失")
            continue
        province_text = _pick(item, ["省份", "province", "法院省份", "region", "court_name", "court", "courtName"])
        if province and province_text and _province_short(province) not in province_text:
            warnings.append(f"已过滤：省份不匹配（{province_text[:20]}）")
            continue
        case_no = _pick(item, ["案号", "case_number", "caseNo", "ah", "cah"])
        court = _pick(item, ["法院名称", "court_name", "court", "courtName", "审理法院"])
        date = _fmt_date(_pick(item, ["裁判日期", "decision_date", "date", "结案日期",
                                      "judgmentDate", "judgment_date"]))
        digest_key = case_no or f"{court}|{date}|{_pick(item, ['title', 'name'])}"
        if not digest_key.strip("|"):
            continue
        sid = _source_id(item, "CASE")
        if sid in seen:
            continue
        seen.add(sid)
        kept.append({
            "source_id": sid,
            "source_provider": provider,
            "case_number": case_no,
            "court": court,
            "decision_date": date,
            "title": _pick(item, ["案件标题", "title", "name", "案件名称"]),
            "summary": _pick(item, ["案例摘要", "summary", "basic_facts", "基本案情", "content"]),
            "holding": _pick(item, ["holding", "裁判要旨", "裁判理由"]),
            "province": province_text,
            "court_level": _pick(item, ["法院级别", "court_level"]),
            "url": _pick(item, ["案例详情页链接", "引用链接", "url", "link"]),
            "reference_level": _pick(item, ["referenceLevel", "参照级别", "案例等级", "case_type"]),
        })
        if len(kept) >= 5:
            break
    return kept, warnings


def _merge_results(provider_results: Dict[str, List[Dict[str, Any]]],
                   kind: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    merged: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    for provider, items in provider_results.items():
        for item in items:
            if kind == "law":
                key = f"{item.get('title', '')}|{item.get('article', '')}"
            else:
                key = str(item.get("case_number") or item.get("source_id"))
            if key not in index:
                copy = dict(item)
                copy["providers"] = [provider]
                index[key] = copy
                merged.append(copy)
            else:
                existing = index[key]
                if provider not in existing["providers"]:
                    existing["providers"].append(provider)
                if kind == "law" and existing.get("content") and item.get("content") \
                        and existing["content"].strip() != item["content"].strip():
                    conflicts.append({"key": key, "providers": [existing["providers"][0], provider],
                                      "field": "content"})
                if kind == "case" and existing.get("court") and item.get("court") \
                        and existing["court"].strip() != item["court"].strip():
                    conflicts.append({"key": key, "providers": [existing["providers"][0], provider],
                                      "field": "court"})
    return merged[:5], conflicts


def ingest(kind: str, raw: Path, case_dir: Path, province: str, provider: str) -> int:
    case_dir = case_dir.resolve()
    if provider not in PROVIDERS:
        raise SystemExit(f"provider 只能是 {'/'.join(PROVIDERS)}：{provider}")
    if kind not in ("law", "case"):
        raise SystemExit(f"kind 只能是 law 或 case：{kind}")
    if not raw.exists():
        raise SystemExit(f"原始返回不存在：{raw}")
    try:
        payload = _load_json(raw)
    except Exception as exc:
        raise SystemExit(f"原始返回不是合法 JSON：{exc}")

    code = payload.get("code") if isinstance(payload, dict) else None
    message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
    if code is not None and str(code) != "200":
        raise SystemExit(f"MCP 返回业务失败：code={code} message={message}")
    if isinstance(payload, dict) and payload.get("success") is False:
        raise SystemExit(f"MCP 返回业务失败：{message or 'success=false'}")

    items = _extract_items(payload)
    if kind == "law":
        normalized: List[Dict[str, Any]] = _normalize_law(items, provider)
        warnings: List[str] = [] if normalized else ["未检索到可用法条，法律章节留空并标待核验"]
    else:
        normalized, warnings = _normalize_case(items, province, provider)
        if not normalized:
            warnings.append("未检索到符合条件的类案，类案章节留空并标待核验")

    state_path = case_dir / "05案件信息" / "analysis.json"
    state = _load_json(state_path) if state_path.exists() else {}
    state.setdefault("law_search", {"results": [], "providers": {}})
    state.setdefault("case_search", {"results": [], "providers": {}})
    bucket = state["law_search"] if kind == "law" else state["case_search"]
    bucket.setdefault("providers", {})
    bucket["providers"][provider] = normalized
    merged, conflicts = _merge_results(bucket["providers"], kind)
    bucket["results"] = merged
    bucket["cross_check"] = {
        "providers": list(bucket["providers"].keys()),
        "counts": {p: len(v) for p, v in bucket["providers"].items()},
        "both_returned": all(bool(v) for v in bucket["providers"].values()) and len(bucket["providers"]) >= 2,
        "conflicts": conflicts,
    }
    if kind == "case":
        bucket["province"] = province
        bucket["date_filter"] = ">=2021-01-01"
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(state_path, state)

    print(json.dumps({
        "ok": True,
        "provider": provider,
        "kind": kind,
        "raw_items": len(items),
        "kept": len(normalized),
        "warnings": warnings[:10],
        "cross_check": bucket["cross_check"],
        "state": str(state_path),
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="多源法律检索：元典 + 企查查")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe")
    p.add_argument("--case-dir", required=True)
    p.add_argument("--tools-json", required=True)
    p.add_argument("--providers", default="yuandian,qcc")

    b = sub.add_parser("build-call")
    b.add_argument("--provider", choices=list(PROVIDERS), required=True)
    b.add_argument("--kind", choices=["law", "case"], required=True)
    b.add_argument("--query", required=True)
    b.add_argument("--rewrite-flag", default="true", choices=["true", "false"])
    b.add_argument("--province", default="")
    b.add_argument("--fatiao-filter", default="")
    b.add_argument("--return-num", type=int, default=0)
    b.add_argument("--out", default="")

    i = sub.add_parser("ingest")
    i.add_argument("--provider", choices=list(PROVIDERS), required=True)
    i.add_argument("--kind", choices=["law", "case"], required=True)
    i.add_argument("--raw", required=True)
    i.add_argument("--case-dir", required=True)
    i.add_argument("--province", default="")

    args = ap.parse_args()
    if args.cmd == "probe":
        required = tuple(x.strip() for x in args.providers.split(",") if x.strip() in PROVIDERS)
        if not required:
            required = PROVIDERS
        return probe(Path(args.case_dir), Path(args.tools_json), required)
    if args.cmd == "build-call":
        default_num = 20 if args.kind == "law" else 12
        filter_obj = None
        if args.fatiao_filter:
            try:
                filter_obj = json.loads(args.fatiao_filter)
            except Exception as exc:
                raise SystemExit(f"--fatiao-filter 不是合法 JSON：{exc}")
        return build_call(
            args.provider, args.kind, args.query, args.rewrite_flag == "true", args.province,
            filter_obj, args.return_num or default_num, Path(args.out) if args.out else None,
        )
    return ingest(args.kind, Path(args.raw), Path(args.case_dir), args.province, args.provider)


if __name__ == "__main__":
    raise SystemExit(main())
