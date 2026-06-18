# news_generator/scripts/processors/pr05b_llm_context_judge.py
# -*- coding: utf-8 -*-
"""
pr05b_llm_context_judge.py

목적:
    pr05a에서 생성한 GDELT-stock context judge 후보를 LLM 판정용 request로 변환하고,
    LLM/Batch API 결과를 다시 stock context card와 병합한다.

역할:
    - 뉴스 생성 스크립트가 아니다.
    - GDELT context와 개별 종목의 연결성이 충분한지 판정한다.
    - rule 기반 pr05a 결과 중 llm_judge_required=True 후보만 대상으로 한다.

흐름:
    1) build-requests
       pr05a의 *_judge_input.jsonl -> OpenAI Batch/Chat Completions용 JSONL

    2) parse-results
       OpenAI Batch output JSONL -> judge_result.csv/jsonl

    3) merge
       pr05a context cards csv + judge_result.csv -> judged context cards csv/jsonl

권장 사용:
    python scripts/processors/pr05b_llm_context_judge.py build-requests \
      --judge-input-jsonl data/processed/gdelt_context/gdelt_stock_context_cards_test_v3_judge_input.jsonl \
      --output-jsonl data/processed/gdelt_context/gdelt_stock_context_cards_test_v3_openai_requests.jsonl \
      --model gpt-4o-mini \
      --limit 200

    # Batch 결과를 받은 후
    python scripts/processors/pr05b_llm_context_judge.py parse-results \
      --batch-output-jsonl data/processed/gdelt_context/gdelt_stock_context_cards_test_v3_batch_output.jsonl \
      --output-csv data/processed/gdelt_context/gdelt_stock_context_judge_results_v3.csv

    python scripts/processors/pr05b_llm_context_judge.py merge \
      --cards-csv data/processed/gdelt_context/gdelt_stock_context_cards_test_v3.csv \
      --judge-results-csv data/processed/gdelt_context/gdelt_stock_context_judge_results_v3.csv \
      --output-csv data/processed/gdelt_context/gdelt_stock_context_cards_judged_v3.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


# =============================================================================
# Utilities
# =============================================================================


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>", "nat"}:
        return ""
    return re.sub(r"\s+", " ", text)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = clean_text(value).lower()
    return text in {"1", "true", "t", "yes", "y"}


def normalize_stock_code(value: Any) -> str:
    text = clean_text(value)
    digits = re.sub(r"[^0-9]", "", text)
    if digits:
        return digits.zfill(6)[-6:]
    return text


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as exc:
                raise ValueError(f"JSONL parse failed: {path} line={line_no}: {exc}") from exc
            if isinstance(obj, dict):
                yield obj


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def stable_hash(obj: Any, n: int = 16) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:n]


def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract first JSON object from model text. Handles ```json fences."""
    text = clean_text(text)
    if not text:
        return {}

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


# =============================================================================
# Prompt
# =============================================================================

SYSTEM_PROMPT = """You are a strict relevance judge for Korean stock news generation.

You are NOT writing news.
You judge whether a GDELT macro/sector context can be attached to one specific Korean listed stock.

Core principles:
- Reject weak, generic, or overextended relationships.
- Do not invent causal links or facts not present in the stock profile or context.
- A context is usable only when the company's business model, revenue source, cost structure, demand driver, supply chain, financing condition, or regulatory exposure has a clear connection to the context.
- Distinguish direct business exposure from broad market background.
- Do not treat generic risk-management words such as credit risk, liquidity risk, financial risk, derivatives, or capital management as proof that a non-financial company is a financial-sector stock.
- Do not treat exports, overseas markets, FX exposure, logistics cost, or foreign sales as proof that a company is a shipping/logistics-sector stock.
- Oil/energy context should usually be direct only for refiners, petrochemicals, airlines, shipping, utilities, transportation, or energy-intensive manufacturers. For hotels/retail/consumer firms, oil is usually indirect or reject unless the profile clearly supports it.
- Trade/export context is direct for exporters or companies whose profile clearly shows overseas revenue/export exposure. For tourism/duty-free/hotel firms, prefer tourism/China/FX logic over export/import framing.
- Sector context can be accepted only when the context sector aligns with the company's actual business segment or material revenue/cost driver.

Decision labels:
- accept_direct: clear direct relationship. Can be used as supporting context for stock-specific news.
- accept_indirect: plausible but indirect relationship. Requires corroboration from DART, price-volume, or community before generation.
- background_only: broad context only. Never use as a stock-specific driver.
- reject: relationship is too weak, generic, misleading, or mismatched.

Directness scale:
0 = none / reject
1 = weak or 2+ step indirect relation
2 = plausible indirect relation
3 = direct business/revenue/cost/financing exposure

Return STRICT JSON only. No markdown. No extra text.
"""

USER_PROMPT_TEMPLATE = """Judge whether the following GDELT context can be attached to the stock.

Return strict JSON with exactly this schema:
{{
  "decision": "accept_direct | accept_indirect | background_only | reject",
  "directness": 0,
  "confidence": 0.0,
  "allowed_usage": "primary_driver | supporting_context | background_only | do_not_use",
  "requires_corroboration": true,
  "better_factor_tags": [],
  "reason_ko": "짧은 한국어 판정 사유"
}}

Rules for allowed_usage:
- accept_direct -> supporting_context. Use primary_driver only if the context is clearly company-specific, not merely sector/macro.
- accept_indirect -> supporting_context only after corroboration; requires_corroboration must be true.
- background_only -> background_only; requires_corroboration should usually be true.
- reject -> do_not_use.

Input JSON:
{payload_json}
"""


# =============================================================================
# Build request
# =============================================================================


def compact_payload(payload: Dict[str, Any], max_profile_chars: int = 900) -> Dict[str, Any]:
    """Keep enough information for judgement but reduce token cost."""
    stock = dict(payload.get("stock") or {})
    context = dict(payload.get("context") or {})
    prior = dict(payload.get("rule_prior") or {})

    profile = clean_text(stock.get("business_profile_excerpt"))
    if len(profile) > max_profile_chars:
        profile = profile[:max_profile_chars].rstrip() + "..."

    compact = {
        "custom_id": clean_text(payload.get("custom_id")),
        "stock": {
            "stock_code": normalize_stock_code(stock.get("stock_code")),
            "stock_name": clean_text(stock.get("stock_name")),
            "business_year": clean_text(stock.get("business_year")),
            "business_profile_excerpt": profile,
            "sector_tags": stock.get("sector_tags") or [],
            "profile_factor_tags": stock.get("profile_factor_tags") or [],
            "sensitivity_tags": stock.get("sensitivity_tags") or [],
        },
        "context": {
            "date": clean_text(context.get("date")),
            "theme": clean_text(context.get("theme")),
            "evidence_class": clean_text(context.get("evidence_class")),
            "source": clean_text(context.get("source")),
            "confidence_level": clean_text(context.get("confidence_level")),
            "stock_link_score": safe_float(context.get("stock_link_score"), 0.0),
            "raw_count": safe_int(context.get("raw_count"), 0),
            "hard_match_tags": context.get("hard_match_tags") or [],
            "profile_factor_tags": context.get("profile_factor_tags") or [],
            "modifier_tags": context.get("modifier_tags") or [],
            "matched_tokens": context.get("matched_tokens") or [],
            "reason_code": clean_text(context.get("reason_code")),
        },
        "rule_prior": {
            "attach_score": safe_float(prior.get("attach_score"), 0.0),
            "matched_stock_tags": prior.get("matched_stock_tags") or [],
            "gate_reason": clean_text(prior.get("gate_reason")),
            "relation_directness_prior": safe_int(prior.get("relation_directness_prior"), 0),
            "judge_reason": clean_text(prior.get("judge_reason")),
            "corroboration_sources": prior.get("corroboration_sources") or [],
        },
    }
    return compact


def build_openai_request(
    payload: Dict[str, Any],
    model: str,
    temperature: float,
    max_tokens: int,
    response_format: str = "json_object",
) -> Dict[str, Any]:
    compact = compact_payload(payload)
    custom_id = clean_text(compact.get("custom_id"))
    if not custom_id:
        custom_id = f"gdelt_judge__{stable_hash(compact)}"
    # Make custom_id stable and reasonably short for Batch API.
    if len(custom_id) > 120:
        custom_id = custom_id[:90] + "__" + stable_hash(custom_id, 12)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        payload_json=json.dumps(compact, ensure_ascii=False, indent=2)
    )

    body: Dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    if response_format == "json_object":
        body["response_format"] = {"type": "json_object"}

    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": body,
    }


def cmd_build_requests(args: argparse.Namespace) -> None:
    input_path = Path(args.judge_input_jsonl).expanduser().resolve()
    output_path = Path(args.output_jsonl).expanduser().resolve()

    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for payload in read_jsonl(input_path):
        compact = compact_payload(payload)
        cid = clean_text(compact.get("custom_id")) or f"gdelt_judge__{stable_hash(compact)}"
        if args.dedupe and cid in seen:
            continue
        seen.add(cid)
        req = build_openai_request(
            payload=payload,
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            response_format=args.response_format,
        )
        rows.append(req)
        if args.limit and len(rows) >= args.limit:
            break

    n = write_jsonl(output_path, rows)
    report = [
        "=" * 100,
        "[pr05b LLM judge request 생성 완료]",
        f"input_jsonl: {input_path}",
        f"output_jsonl: {output_path}",
        f"model: {args.model}",
        f"rows: {n:,}",
        f"temperature: {args.temperature}",
        f"max_tokens: {args.max_tokens}",
        "=" * 100,
    ]
    print("\n".join(report))


# =============================================================================
# Parse result
# =============================================================================

ALLOWED_DECISIONS = {"accept_direct", "accept_indirect", "background_only", "reject"}
ALLOWED_USAGE = {"primary_driver", "supporting_context", "background_only", "do_not_use"}


def normalize_decision(parsed: Dict[str, Any]) -> Dict[str, Any]:
    decision = clean_text(parsed.get("decision")).lower()
    if decision not in ALLOWED_DECISIONS:
        decision = "reject"

    directness = max(0, min(3, safe_int(parsed.get("directness"), 0)))
    confidence = max(0.0, min(1.0, safe_float(parsed.get("confidence"), 0.0)))
    allowed_usage = clean_text(parsed.get("allowed_usage")).lower()
    if allowed_usage not in ALLOWED_USAGE:
        allowed_usage = {
            "accept_direct": "supporting_context",
            "accept_indirect": "supporting_context",
            "background_only": "background_only",
            "reject": "do_not_use",
        }[decision]

    requires_corroboration = parsed.get("requires_corroboration")
    if requires_corroboration is None:
        requires_corroboration = decision in {"accept_indirect", "background_only"}
    else:
        requires_corroboration = normalize_bool(requires_corroboration)

    # Hard safety normalization.
    if decision == "reject":
        directness = 0
        allowed_usage = "do_not_use"
        requires_corroboration = True
    elif decision == "background_only":
        allowed_usage = "background_only"
        requires_corroboration = True
        directness = min(directness, 1)
    elif decision == "accept_indirect":
        requires_corroboration = True
        directness = max(1, min(directness, 2))
        if allowed_usage == "primary_driver":
            allowed_usage = "supporting_context"
    elif decision == "accept_direct":
        directness = max(2, directness)
        if allowed_usage == "do_not_use":
            allowed_usage = "supporting_context"

    tags = parsed.get("better_factor_tags") or []
    if not isinstance(tags, list):
        tags = [clean_text(tags)] if clean_text(tags) else []
    tags = [clean_text(x) for x in tags if clean_text(x)][:10]

    return {
        "llm_decision": decision,
        "llm_directness": directness,
        "llm_confidence": round(confidence, 4),
        "llm_allowed_usage": allowed_usage,
        "llm_requires_corroboration": bool(requires_corroboration),
        "llm_better_factor_tags": "|".join(tags),
        "llm_reason_ko": clean_text(parsed.get("reason_ko"))[:500],
    }


def extract_message_content(batch_row: Dict[str, Any]) -> Tuple[str, str]:
    """Return (content, error_message) from OpenAI Batch output row."""
    if batch_row.get("error"):
        return "", json.dumps(batch_row.get("error"), ensure_ascii=False)

    response = batch_row.get("response") or {}
    if response.get("status_code") and int(response.get("status_code")) >= 400:
        return "", json.dumps(response, ensure_ascii=False)

    body = response.get("body") or batch_row.get("body") or {}
    try:
        choices = body.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in content)
            return clean_text(content), ""
    except Exception as exc:
        return "", f"content_extract_failed: {exc}"
    return "", "no_message_content"


def parse_batch_output_row(row: Dict[str, Any]) -> Dict[str, Any]:
    custom_id = clean_text(row.get("custom_id"))
    content, error_message = extract_message_content(row)
    parsed = extract_json_object(content)
    norm = normalize_decision(parsed) if parsed else normalize_decision({"decision": "reject", "reason_ko": "LLM 응답 JSON 파싱 실패"})
    norm.update({
        "custom_id": custom_id,
        "parse_ok": bool(parsed),
        "raw_response_text": content[:2000],
        "error_message": error_message,
    })
    return norm


def cmd_parse_results(args: argparse.Namespace) -> None:
    input_path = Path(args.batch_output_jsonl).expanduser().resolve()
    output_csv = Path(args.output_csv).expanduser().resolve()
    output_jsonl = Path(args.output_jsonl).expanduser().resolve() if args.output_jsonl else output_csv.with_suffix(".jsonl")

    rows = [parse_batch_output_row(row) for row in read_jsonl(input_path)]
    df = pd.DataFrame(rows)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    write_jsonl(output_jsonl, df.to_dict("records"))

    print("=" * 100)
    print("[pr05b LLM judge result 파싱 완료]")
    print(f"input_jsonl: {input_path}")
    print(f"output_csv: {output_csv}")
    print(f"output_jsonl: {output_jsonl}")
    print(f"rows: {len(df):,}")
    if not df.empty:
        print("\n[llm_decision]")
        print(df["llm_decision"].value_counts(dropna=False).to_string())
        print("\n[llm_allowed_usage]")
        print(df["llm_allowed_usage"].value_counts(dropna=False).to_string())
        print("\n[parse_ok]")
        print(df["parse_ok"].value_counts(dropna=False).to_string())
    print("=" * 100)


# =============================================================================
# Merge
# =============================================================================


def compute_final_usage(row: pd.Series) -> Dict[str, Any]:
    """Final post-judge generation flags."""
    rule_decision = clean_text(row.get("rule_decision"))
    evidence_class = clean_text(row.get("evidence_class"))
    llm_required = normalize_bool(row.get("llm_judge_required"))
    llm_decision = clean_text(row.get("llm_decision"))
    llm_usage = clean_text(row.get("llm_allowed_usage"))
    llm_requires_corr = normalize_bool(row.get("llm_requires_corroboration"))
    has_corr = bool(clean_text(row.get("corroboration_sources"))) or normalize_bool(row.get("has_dart")) or normalize_bool(row.get("has_price_volume")) or normalize_bool(row.get("has_community"))

    # Rule auto-accept rows that were not judged.
    if not llm_required:
        if rule_decision == "auto_accept_rule" and normalize_bool(row.get("usable_for_generation")):
            return {
                "final_decision": "accept_rule_direct",
                "final_allowed_usage": "supporting_context",
                "final_usable_for_generation": True,
                "final_requires_corroboration": False,
                "final_reason": "rule_auto_accept_no_llm_required",
            }
        if evidence_class == "broad_macro" or rule_decision == "background_candidate":
            return {
                "final_decision": "background_only",
                "final_allowed_usage": "background_only",
                "final_usable_for_generation": True,
                "final_requires_corroboration": True,
                "final_reason": "broad_macro_background_only",
            }
        return {
            "final_decision": "hold_or_reject_rule",
            "final_allowed_usage": "do_not_use",
            "final_usable_for_generation": False,
            "final_requires_corroboration": True,
            "final_reason": "not_auto_accepted_and_no_llm_result",
        }

    # LLM-required rows without result are not usable.
    if not llm_decision:
        return {
            "final_decision": "missing_llm_result",
            "final_allowed_usage": "do_not_use",
            "final_usable_for_generation": False,
            "final_requires_corroboration": True,
            "final_reason": "llm_required_but_missing_result",
        }

    if llm_decision == "reject":
        return {
            "final_decision": "reject_by_llm",
            "final_allowed_usage": "do_not_use",
            "final_usable_for_generation": False,
            "final_requires_corroboration": True,
            "final_reason": "llm_rejected_context_stock_relation",
        }

    if llm_decision == "background_only":
        return {
            "final_decision": "background_only_by_llm",
            "final_allowed_usage": "background_only",
            "final_usable_for_generation": True,
            "final_requires_corroboration": True,
            "final_reason": "llm_background_only",
        }

    if llm_decision == "accept_indirect":
        return {
            "final_decision": "accept_indirect_needs_corroboration" if not has_corr else "accept_indirect_with_corroboration",
            "final_allowed_usage": "supporting_context" if has_corr else "hold_until_corroboration",
            "final_usable_for_generation": bool(has_corr),
            "final_requires_corroboration": True,
            "final_reason": "llm_indirect_requires_external_corroboration",
        }

    if llm_decision == "accept_direct":
        if llm_usage == "primary_driver":
            # We still keep this cautious; GDELT is not company-specific evidence by itself.
            llm_usage = "supporting_context"
        return {
            "final_decision": "accept_direct_by_llm",
            "final_allowed_usage": llm_usage or "supporting_context",
            "final_usable_for_generation": True,
            "final_requires_corroboration": bool(llm_requires_corr),
            "final_reason": "llm_direct_accept",
        }

    return {
        "final_decision": "unknown_llm_decision",
        "final_allowed_usage": "do_not_use",
        "final_usable_for_generation": False,
        "final_requires_corroboration": True,
        "final_reason": "unknown_decision_fallback",
    }


def add_custom_id_to_cards(cards: pd.DataFrame) -> pd.DataFrame:
    """Recreate pr05a custom_id if not present."""
    df = cards.copy()
    if "custom_id" in df.columns and df["custom_id"].notna().any():
        return df

    def make_id(row: pd.Series) -> str:
        date = clean_text(row.get("date"))
        code = normalize_stock_code(row.get("stock_code"))
        theme = clean_text(row.get("theme"))
        evidence = clean_text(row.get("evidence_class"))
        source = clean_text(row.get("source"))
        suffix = abs(hash((theme, evidence, source))) % 10**10
        return f"gdelt_judge__{date}__{code}__{suffix}"

    df["custom_id"] = df.apply(make_id, axis=1)
    return df


def cmd_merge(args: argparse.Namespace) -> None:
    cards_csv = Path(args.cards_csv).expanduser().resolve()
    judge_csv = Path(args.judge_results_csv).expanduser().resolve()
    output_csv = Path(args.output_csv).expanduser().resolve()
    output_jsonl = Path(args.output_jsonl).expanduser().resolve() if args.output_jsonl else output_csv.with_suffix(".jsonl")
    report_txt = Path(args.report_txt).expanduser().resolve() if args.report_txt else output_csv.with_name(output_csv.stem + "_report.txt")

    cards = pd.read_csv(cards_csv, dtype={"stock_code": str})
    cards["stock_code"] = cards["stock_code"].map(normalize_stock_code)
    cards = add_custom_id_to_cards(cards)

    judge = pd.read_csv(judge_csv, dtype={"custom_id": str})
    judge = judge.drop_duplicates(subset=["custom_id"], keep="last")

    merged = cards.merge(judge, on="custom_id", how="left", suffixes=("", "_judge"))

    final_rows = merged.apply(compute_final_usage, axis=1, result_type="expand")
    merged = pd.concat([merged, final_rows], axis=1)

    # Optional: only keep final usable rows.
    if args.only_final_usable:
        merged_out = merged[merged["final_usable_for_generation"].astype(bool)].copy()
    else:
        merged_out = merged.copy()

    sort_cols = [c for c in ["date", "stock_code", "final_usable_for_generation", "attach_score"] if c in merged_out.columns]
    if sort_cols:
        ascending = [True, True, False, False][:len(sort_cols)]
        merged_out = merged_out.sort_values(sort_cols, ascending=ascending)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    merged_out.to_csv(output_csv, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
    write_jsonl(output_jsonl, merged_out.to_dict("records"))

    lines = []
    lines.append("=" * 100)
    lines.append("[pr05b LLM judge merge 완료]")
    lines.append(f"cards_csv: {cards_csv}")
    lines.append(f"judge_results_csv: {judge_csv}")
    lines.append(f"output_csv: {output_csv}")
    lines.append(f"output_jsonl: {output_jsonl}")
    lines.append(f"input_cards: {len(cards):,}")
    lines.append(f"judge_results: {len(judge):,}")
    lines.append(f"merged_rows: {len(merged):,}")
    lines.append(f"output_rows: {len(merged_out):,}")
    lines.append("\n[final_decision]")
    lines.append(merged["final_decision"].value_counts(dropna=False).to_string())
    lines.append("\n[final_allowed_usage]")
    lines.append(merged["final_allowed_usage"].value_counts(dropna=False).to_string())
    lines.append("\n[final_usable_for_generation]")
    lines.append(merged["final_usable_for_generation"].value_counts(dropna=False).to_string())
    if "llm_decision" in merged.columns:
        lines.append("\n[llm_decision]")
        lines.append(merged["llm_decision"].fillna("NO_LLM_RESULT").value_counts(dropna=False).to_string())
    lines.append("=" * 100)
    report = "\n".join(lines)
    report_txt.parent.mkdir(parents=True, exist_ok=True)
    report_txt.write_text(report, encoding="utf-8")
    print(report)


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="pr05b LLM context-stock relevance judge")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("build-requests", help="Build OpenAI Batch request JSONL from pr05a judge input JSONL")
    p1.add_argument("--judge-input-jsonl", required=True)
    p1.add_argument("--output-jsonl", required=True)
    p1.add_argument("--model", default="gpt-4o-mini")
    p1.add_argument("--temperature", type=float, default=0.0)
    p1.add_argument("--max-tokens", type=int, default=260)
    p1.add_argument("--response-format", choices=["json_object", "none"], default="json_object")
    p1.add_argument("--limit", type=int, default=0, help="0 means no limit")
    p1.add_argument("--dedupe", action="store_true", help="Deduplicate by custom_id")
    p1.set_defaults(func=cmd_build_requests)

    p2 = sub.add_parser("parse-results", help="Parse OpenAI Batch output JSONL into judge result CSV/JSONL")
    p2.add_argument("--batch-output-jsonl", required=True)
    p2.add_argument("--output-csv", required=True)
    p2.add_argument("--output-jsonl", default="")
    p2.set_defaults(func=cmd_parse_results)

    p3 = sub.add_parser("merge", help="Merge pr05a cards with judge results")
    p3.add_argument("--cards-csv", required=True)
    p3.add_argument("--judge-results-csv", required=True)
    p3.add_argument("--output-csv", required=True)
    p3.add_argument("--output-jsonl", default="")
    p3.add_argument("--report-txt", default="")
    p3.add_argument("--only-final-usable", action="store_true")
    p3.set_defaults(func=cmd_merge)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
