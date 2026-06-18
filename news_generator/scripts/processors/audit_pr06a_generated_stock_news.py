from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


AI_STYLE_PHRASES = [
    "이벤트",
    "맥락",
    "관점",
    "의미",
    "시사",
    "해석",
    "해석된다",
    "주목",
    "주목된다",
    "확인할 필요",
    "관심이 필요",
    "시장 참여자",
    "투자자들은",
    "향후 흐름",
    "영향을 미칠 수",
    "가능성이 있다",
    "관련 사안",
    "중요한 변수",
    "긍정적 요인",
    "부정적 요인",
    "분류된다",
]

MARKET_CAUSAL_PHRASES = [
    "주가 상승",
    "주가 하락",
    "주가가 상승",
    "주가가 하락",
    "매수세",
    "매도세",
    "투자심리",
    "호재",
    "악재",
    "시장 반응",
    "원인",
    "영향으로",
    "때문에",
    "힘입어",
    "부담으로 작용",
    "긍정적으로 받아들",
    "부정적으로 받아들",
    "거래량",
]

DISCLOSURE_RESTATEMENT_PHRASES = [
    "관련 내용을 공시했다",
    "주요 경영사항을 공시했다",
    "계획을 공시했다",
    "내용을 밝혔다",
    "공시를 통해 밝혔다",
    "투자판단 관련 주요 경영사항",
    "관련 내용을 밝혔다",
]

SENTENCE_END_RE = re.compile(r"[.!?。]|다\.|했다\.|된다\.|됐다\.|한다\.|이다\.")


class GeneratedOutputReader:
    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        rows = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except Exception:
                    rows.append({
                        "line_no": line_no,
                        "parse_ok": False,
                        "raw_text": line,
                        "error": "outer_json_parse_failed",
                    })
                    continue

                parsed = GeneratedOutputReader._extract_model_json(raw)
                parsed["line_no"] = line_no
                parsed["outer_custom_id"] = raw.get("custom_id", "")
                rows.append(parsed)
        return rows

    @staticmethod
    def _extract_model_json(raw: dict[str, Any]) -> dict[str, Any]:
        # OpenAI batch format
        try:
            content = raw["response"]["body"]["choices"][0]["message"]["content"]
            return GeneratedOutputReader._parse_content(content, raw)
        except Exception:
            pass

        # Direct chat completion-like
        try:
            content = raw["choices"][0]["message"]["content"]
            return GeneratedOutputReader._parse_content(content, raw)
        except Exception:
            pass

        # Already final JSON
        if "headline" in raw or "detail_news" in raw:
            out = dict(raw)
            out["parse_ok"] = True
            return out

        return {
            "parse_ok": False,
            "raw_text": json.dumps(raw, ensure_ascii=False)[:1000],
            "error": "cannot_extract_model_content",
        }

    @staticmethod
    def _parse_content(content: str, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            obj = json.loads(content)
            if isinstance(obj, dict):
                obj["parse_ok"] = True
                return obj
        except Exception:
            pass

        # Try extracting first JSON object
        m = re.search(r"\{.*\}", content, flags=re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    obj["parse_ok"] = True
                    obj["recovered_from_text"] = True
                    return obj
            except Exception:
                pass

        return {
            "parse_ok": False,
            "raw_text": content[:1000],
            "error": "model_json_parse_failed",
        }


class NewsAuditor:
    def __init__(self, candidates_csv: Path, outputs_jsonl: Path, output_dir: Path) -> None:
        self.candidates_csv = candidates_csv
        self.outputs_jsonl = outputs_jsonl
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        candidates = pd.read_csv(self.candidates_csv, dtype={"stock_code": str}).fillna("")
        outputs = pd.DataFrame(GeneratedOutputReader.read_jsonl(self.outputs_jsonl)).fillna("")

        audited = self._audit(outputs, candidates)
        audited.to_csv(self.output_dir / "generated_news_audit.csv", index=False, encoding="utf-8-sig")

        fail = audited[audited["pass_all"] == False].copy()
        fail.to_csv(self.output_dir / "generated_news_failed.csv", index=False, encoding="utf-8-sig")

        self._write_report(audited, fail)

        print("=" * 100)
        print("[pr06a generated news audit]")
        print("outputs:", len(audited))
        print("pass:", int(audited["pass_all"].sum()) if len(audited) else 0)
        print("fail:", len(fail))
        print("output_dir:", self.output_dir)
        print("=" * 100)

        if len(fail):
            cols = [
                "used_bundle_id",
                "headline",
                "detail_news",
                "fail_reasons",
                "ai_style_hits",
                "market_causal_hits",
                "disclosure_restatement_hits",
            ]
            cols = [c for c in cols if c in fail.columns]
            print(fail[cols].head(50).to_string(index=False))
        else:
            print("OK")

    def _audit(self, outputs: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
        candidate_by_bundle = {}
        if "bundle_id" in candidates.columns:
            candidate_by_bundle = candidates.set_index("bundle_id").to_dict("index")

        rows = []
        for _, row in outputs.iterrows():
            obj = row.to_dict()
            headline = str(obj.get("headline", "")).strip()
            detail = str(obj.get("detail_news", "")).strip()
            used_bundle_id = str(obj.get("used_bundle_id", "")).strip()
            claim_level = str(obj.get("claim_level", "")).strip()

            text = headline + "\n" + detail

            ai_hits = [p for p in AI_STYLE_PHRASES if p in text]
            market_hits = [p for p in MARKET_CAUSAL_PHRASES if p in text]
            disclosure_hits = [p for p in DISCLOSURE_RESTATEMENT_PHRASES if p in text]

            sentence_count = self._count_sentences(detail)
            char_len = len(detail.replace(" ", ""))

            style_self_check = obj.get("style_self_check", {})
            if isinstance(style_self_check, str):
                try:
                    style_self_check = json.loads(style_self_check)
                except Exception:
                    style_self_check = {}

            candidate = candidate_by_bundle.get(used_bundle_id, {})
            expected_claim = "no_market_claim"

            fail_reasons = []

            if not bool(obj.get("parse_ok", False)):
                fail_reasons.append("json_parse_failed")
            if not headline:
                fail_reasons.append("missing_headline")
            if not detail:
                fail_reasons.append("missing_detail_news")
            if sentence_count != 2:
                fail_reasons.append(f"sentence_count_{sentence_count}")
            if char_len < 70 or char_len > 190:
                fail_reasons.append(f"detail_length_{char_len}")
            if claim_level != expected_claim:
                fail_reasons.append(f"claim_level_not_{expected_claim}")
            if ai_hits:
                fail_reasons.append("ai_style_phrase")
            if market_hits:
                fail_reasons.append("market_or_causal_phrase")
            if disclosure_hits:
                fail_reasons.append("disclosure_restatement_phrase")
            if not used_bundle_id:
                fail_reasons.append("missing_used_bundle_id")
            if used_bundle_id and used_bundle_id not in candidate_by_bundle:
                fail_reasons.append("used_bundle_id_not_in_candidates")

            # self-check consistency
            if style_self_check:
                if style_self_check.get("has_forbidden_market_claim") is True:
                    fail_reasons.append("self_check_forbidden_market_claim")
                if style_self_check.get("has_ai_style_phrase") is True:
                    fail_reasons.append("self_check_ai_style")
                if style_self_check.get("used_reference_template") is True:
                    fail_reasons.append("self_check_used_reference_template")
                if style_self_check.get("used_only_allowed_evidence") is False:
                    fail_reasons.append("self_check_unallowed_evidence")

            out = {
                "outer_custom_id": obj.get("outer_custom_id", ""),
                "used_bundle_id": used_bundle_id,
                "headline": headline,
                "detail_news": detail,
                "claim_level": claim_level,
                "parse_ok": bool(obj.get("parse_ok", False)),
                "sentence_count": sentence_count,
                "detail_char_len_no_space": char_len,
                "ai_style_hits": "|".join(ai_hits),
                "market_causal_hits": "|".join(market_hits),
                "disclosure_restatement_hits": "|".join(disclosure_hits),
                "fail_reasons": "|".join(fail_reasons),
                "pass_all": len(fail_reasons) == 0,
                "candidate_event_family": candidate.get("event_family", ""),
                "candidate_action_type": candidate.get("action_type", ""),
                "candidate_plain_action_ko": candidate.get("plain_action_ko", ""),
            }
            rows.append(out)

        return pd.DataFrame(rows)

    def _count_sentences(self, text: str) -> int:
        text = text.strip()
        if not text:
            return 0
        # Korean financial copy usually ends with periods.
        parts = [p for p in re.split(r"(?<=[.!?。])\s+", text) if p.strip()]
        if len(parts) > 1:
            return len(parts)
        return len(re.findall(r"[.!?。]", text)) or 1

    def _write_report(self, audited: pd.DataFrame, fail: pd.DataFrame) -> None:
        lines = []
        total = len(audited)
        passed = int(audited["pass_all"].sum()) if total else 0
        pass_rate = passed / total if total else 0

        lines.append("# pr06a Generated Stock News Audit")
        lines.append("")
        lines.append(f"- total: {total}")
        lines.append(f"- passed: {passed}")
        lines.append(f"- failed: {len(fail)}")
        lines.append(f"- pass_rate: {pass_rate:.1%}")
        lines.append("")

        if len(audited):
            lines.append("## Fail Reason Counts")
            reasons = (
                audited["fail_reasons"]
                .str.get_dummies(sep="|")
                .sum()
                .sort_values(ascending=False)
            )
            if len(reasons):
                for reason, count in reasons.items():
                    if reason:
                        lines.append(f"- {reason}: {int(count)}")
            else:
                lines.append("- none")
            lines.append("")

        lines.append("## Judgment")
        if pass_rate >= 0.85:
            lines.append("- PASS: usable as next prompt baseline.")
        elif pass_rate >= 0.65:
            lines.append("- PARTIAL: prompt needs tightening before bulk generation.")
        else:
            lines.append("- FAIL: do not proceed to bulk generation.")
        lines.append("")

        (self.output_dir / "generated_news_audit_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidates-csv",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests/stock_news_sample_candidates.csv",
    )
    parser.add_argument(
        "--outputs-jsonl",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/stock_news_sample_outputs.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/audit",
    )
    args = parser.parse_args()

    NewsAuditor(
        candidates_csv=Path(args.candidates_csv),
        outputs_jsonl=Path(args.outputs_jsonl),
        output_dir=Path(args.output_dir),
    ).run()


if __name__ == "__main__":
    main()
