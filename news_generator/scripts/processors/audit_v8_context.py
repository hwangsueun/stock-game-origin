#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""맥락 인식(v8) 뉴스 오딧 게이트.

- market_context 없는 이벤트: 기존 strict no_market_claim 게이트(시장어 전면 금지).
- market_context 있는 이벤트(claim_level=market_reaction_adjacency): 사실적 주가/거래량 표현은
  허용하되 인과·감정·전망어는 차단. 가격 수치(%/배)는 market_context에서 온 것으로 허용.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from run_v4_1_full_chunk_download_and_audit import (
    BAD_TERMS, SOURCE_LABEL_TERMS, _read_jsonl, _extract_model_json, _as_str_list,
    _ISO_DATE_RE, _KR_ABBREV_DATE_RE,
)

# 인과/감정/전망 — 어느 뉴스에서도 금지(재료성 포함)
ALWAYS_BAN = [
    # 인과
    "때문에", "때문", "덕분에", "덕분", "영향으로", "영향을 미친", "로 인해", "으로 인해", "반응했다", "화답",
    # 감정/심리
    "호재", "악재", "수혜주", "테마주", "수혜", "투자심리", "시장 반응", "투자자 반응",
    "주목", "기대", "우려", "긍정적", "부정적", "관심", "부각",
    # 과열성 시장어
    "급등", "급락", "강세", "약세", "매수세", "매도세",
    # 전망
    "전망", "예상", "보인다", "알려졌다",
]
# 사실적 시장어 — market_reaction_adjacency에서만 허용, no_market_claim에서는 금지
FACTUAL_MARKET = ["주가", "거래량", "상승", "하락", "올랐", "내렸", "거래일", "종가", "업종지수"]
FACTUAL_MEDIA = ["글로벌 보도 데이터", "평균 톤"]

NUM_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(조|억|만|원|억원|조원|%|퍼센트|배)?")


def _date_extras(source_text: str) -> list[str]:
    extra = []
    for m in _ISO_DATE_RE.finditer(source_text):
        extra.append(f"{m.group(1)}년{m.group(2).lstrip('0') or '0'}월{m.group(3).lstrip('0') or '0'}일")
    for m in _KR_ABBREV_DATE_RE.finditer(source_text):
        extra.append(f"20{m.group(1)}년{m.group(2).lstrip('0') or '0'}월{m.group(3).lstrip('0') or '0'}일")
    return extra


def _numeric_leaks_v8(text: str, facts: list[str], market_ctx: dict | None) -> list[str]:
    """금융 단위(조/억/만/원/%/배) 있는 토큰만 검사.

    - 금액(조/억/만/원): facts에 있어야 함.
    - %/배: facts의 토큰과 일치하거나 market_context의 수치와 일치해야 함.
    - 단위 없는 정수(일수 'N거래일', 지분율 정수 등)는 검사 제외(오탐 방지).
    """
    source_text = " ".join(facts)
    allowed = re.sub(r"\s+", "", source_text + "".join(_date_extras(source_text)))
    context_values: dict[str, set[float]] = {"%": set(), "배": set()}

    def collect_context(obj: object, key: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                collect_context(v, k)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if key.endswith("_pct"):
                context_values["%"].add(abs(float(obj)))
            elif key.endswith("_mult"):
                context_values["배"].add(abs(float(obj)))

    collect_context(market_ctx or {})
    leaks = []
    for m in NUM_RE.finditer(text):
        unit = m.group(1) or ""
        if not unit:
            continue  # 단위 없는 정수는 무시
        token = re.sub(r"\s+", "", m.group(0))
        if unit in ("%", "퍼센트", "배"):
            canonical_unit = "%" if unit in ("%", "퍼센트") else "배"
            raw_number = re.sub(r"[^0-9.,]", "", token).replace(",", "")
            try:
                value = float(raw_number)
            except ValueError:
                value = float("nan")
            from_context = any(abs(value - v) < 0.005 for v in context_values[canonical_unit])
            if token in allowed or from_context:
                continue
            leaks.append(token)
        else:  # 조/억/만/원 금액
            if token not in allowed:
                leaks.append(token)
    return leaks


def _gdelt_measurement_leaks(text: str, market_ctx: dict | None) -> list[str]:
    gdelt = (market_ctx or {}).get("gdelt_context") or {}
    if not gdelt:
        return ["media_claim_without_gdelt_context"] if any(t in text for t in FACTUAL_MEDIA) else []

    leaks = []
    count_match = re.search(r"(?:보도|기사)\s*([0-9,]+)\s*건", text)
    if count_match and int(count_match.group(1).replace(",", "")) != int(gdelt.get("raw_count", -1)):
        leaks.append(f"gdelt_count:{count_match.group(1)}건")

    tone_match = re.search(r"평균\s*톤(?:은|이)?\s*(?:약\s*)?(-?[0-9]+(?:\.[0-9]+)?)", text)
    if tone_match:
        actual = float(tone_match.group(1))
        expected = float(gdelt.get("avg_tone"))
        if abs(actual - expected) >= 0.005:
            leaks.append(f"gdelt_tone:{actual}")
    return leaks


def audit_one(obj: dict, requests: dict[str, dict]) -> dict:
    parsed, perr = _extract_model_json(obj)
    cid = str(obj.get("custom_id", "")).strip()
    payload = requests.get(cid, {})
    market_ctx = payload.get("market_context")
    claim = str(payload.get("claim_level", ""))
    facts = payload.get("detail_source_facts_ko") or []
    status = str(parsed.get("status", "")).strip()
    lines = _as_str_list(parsed.get("news_lines", []))
    row = {"custom_id": cid, "status": status, "claim_level": claim,
           "has_market_context": bool(market_ctx), "news_lines": " / ".join(lines), "fail_reasons": []}
    if perr:
        row["fail_reasons"].append("json_parse_failed"); return row
    if status not in {"accepted", "rejected"}:
        row["fail_reasons"].append("missing_output_for_request"); return row
    if status == "rejected":
        return row
    text = " ".join(lines)

    bad = [t for t in BAD_TERMS if t in text]
    src = [t for t in SOURCE_LABEL_TERMS if t in text]
    always = [t for t in ALWAYS_BAN if t in text]
    fmarket = [t for t in FACTUAL_MARKET if t in text]
    fmedia = [t for t in FACTUAL_MEDIA if t in text]

    if bad:
        row["fail_reasons"].append(f"bad_terms:{'|'.join(bad)}")
    if src:
        row["fail_reasons"].append(f"source_label:{'|'.join(src)}")
    if always:
        row["fail_reasons"].append(f"causal_sentiment_forecast:{'|'.join(always)}")
    if not market_ctx and fmarket:
        # no_market_claim인데 시장어 사용
        row["fail_reasons"].append(f"market_terms_under_no_market_claim:{'|'.join(fmarket)}")
    if fmedia and not (market_ctx or {}).get("gdelt_context"):
        row["fail_reasons"].append(f"media_terms_without_gdelt_context:{'|'.join(fmedia)}")

    leaks = _numeric_leaks_v8(text, facts, market_ctx)
    if leaks:
        row["fail_reasons"].append(f"numeric_leak:{'|'.join(leaks)}")
    gdelt_leaks = _gdelt_measurement_leaks(text, market_ctx)
    if gdelt_leaks:
        row["fail_reasons"].append(f"gdelt_measurement_leak:{'|'.join(gdelt_leaks)}")
    row["pass"] = not row["fail_reasons"]
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-jsonl", type=Path, required=True)
    ap.add_argument("--outputs-jsonl", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    requests: dict[str, dict] = {}
    for r in _read_jsonl(args.requests_jsonl):
        cid = str(r.get("custom_id", "")).strip()
        msgs = r.get("body", {}).get("messages", [])
        um = next((m for m in msgs if m.get("role") == "user"), {})
        try:
            requests[cid] = json.loads(um.get("content", "{}")).get("brief_payload", {})
        except json.JSONDecodeError:
            requests[cid] = {}

    rows = [audit_one(o, requests) for o in _read_jsonl(args.outputs_jsonl)]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "audit_v8.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pass", "custom_id", "status", "claim_level", "has_market_context", "news_lines", "fail_reasons"])
        for r in rows:
            if r["status"] == "rejected":
                continue
            w.writerow([r.get("pass", False), r["custom_id"], r["status"], r["claim_level"],
                        r["has_market_context"], r["news_lines"], " ; ".join(r["fail_reasons"])])

    audited = [r for r in rows if r["status"] == "accepted"]
    passed = [r for r in audited if r.get("pass")]
    ctx = [r for r in audited if r["has_market_context"]]
    rej = sum(1 for r in rows if r["status"] == "rejected")
    print(f"audited(accepted)={len(audited)} pass={len(passed)} fail={len(audited)-len(passed)} "
          f"rejected={rej} pass_rate={len(passed)/len(audited):.1%}" if audited else "no accepted rows")
    print(f"  with_market_context={len(ctx)} | ctx_pass={sum(1 for r in ctx if r.get('pass'))}")
    fails = [r for r in audited if not r.get("pass")]
    from collections import Counter
    fc = Counter(fr.split(":")[0] for r in fails for fr in r["fail_reasons"])
    print("  fail reasons:", dict(fc))
    print(f"  -> {csv_path}")


if __name__ == "__main__":
    main()
