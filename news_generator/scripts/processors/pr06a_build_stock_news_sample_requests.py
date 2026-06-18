#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr06a_build_stock_news_sample_requests.py

Build stock-news generation sample requests from pr05f stock news briefs.

v3.4 change:
- Build detail_source_facts_ko from write_safe_facts_ko before prompting.
- Split multi-sentence safe facts into atomic Korean sentences.
- Filter unsafe/AI-style/detail-expansion sentences before request creation.
- news_lines must use detail_source_facts_ko only.
- Default max_detail_facts=1 for strict one-sentence financial-wire output.
- Stronger bans for "이는", "이로써", "이에 따라", "기여했다", "정점이었다", "폭발적".
- Payload includes raw write_safe_facts_ko for audit, but news generation uses detail_source_facts_ko.
- Output uses news_lines instead of headline/detail_news to avoid repeated headline-body copy.

This version reads pr05f output, not pr05e bundles.
The script does not call the OpenAI API.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BRIEFS_JSONL = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05f_stock_news_briefs_v2_1/stock_news_briefs.jsonl"
)

DEFAULT_OUTPUT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests_from_briefs"
)

DEFAULT_READY_STATUSES = {"ready"}
DEFAULT_NEWS_TYPES = {"stock_event_trigger"}


MARKET_BLOCKLIST_KO = [
    "주가", "급등", "급락", "상승", "하락", "강세", "약세", "거래량",
    "매수세", "매도세", "투자심리", "시장 반응", "호재", "악재",
    "수혜주", "테마주", "부각되며", "주목받", "관심을 받",
]

AI_STYLE_BLOCKLIST_KO = [
    "이벤트", "맥락", "의미", "시사", "해석", "주목된다", "확인할 필요",
    "가능성이 있다", "전망된다", "예상된다", "보인다", "알려졌다",
    "해당 사안", "이번 사안", "이번 이벤트", "이번 공시", "이번 결정",
    "종목 이벤트", "종목 주제", "중심의 종목 단신", "자료에는", "항목이 포함됐다",
    "공개되지 않았다", "추후 공지될 예정이다",
    "최근", "것으로 나타났다", "것으로 분석된다", "분석된다",
    "영향을 미친 것으로", "영향을 미친", "영향을 미쳤다",
    "이는", "이로써", "이에 따라", "성과다", "이루어진", "이루어졌다",
    "시장에서는", "투자자", "관심", "주목", "부각",
    "기여했다", "기여한", "기여하며",
    "정점이었다", "슈퍼사이클의 정점", "폭발적으로", "폭발적",
]

GENERIC_FILLER_KO = [
    "결정 자료에는", "공시 자료에는", "보고서에는", "항목이 포함됐다",
    "관련 내용은", "세부 내용은", "추후", "공개되지 않았다",
]

DETAIL_FACT_REJECT_PHRASES = [
    # Market/price claims without price evidence
    "주가", "거래량", "급등", "급락", "강세", "약세",
    "매수세", "매도세", "투자심리", "시장 반응",
    "호재", "악재", "수혜주", "테마주",

    # AI/report/copied connective style
    "이는", "이로써", "이에 따라",
    "것으로 나타났다", "것으로 분석된다", "분석된다",
    "영향을 미친 것으로", "영향을 미친", "영향을 미쳤다",
    "기여했다", "기여한", "기여하며",

    # Over-interpretive / too generated
    "성과다", "이루어진", "이루어졌다",
    "정점이었다", "슈퍼사이클의 정점",
    "폭발적으로", "폭발적", "폭발했다",
    "폭락", "돌풍", "최악", "쇼크", "충격",
    "수혜", "기대", "우려", "부각", "고조",
    "본격화", "절정", "신호", "현실화",
    "확인했다", "확인됐다", "성공했다",
    "강화됐다", "고공행진", "주된 요인",
    "주된 원인", "요인이었다",

    # Generic filler
    "자료에는", "보고서에는", "항목이 포함됐다",
    "공개되지 않았다", "추후",
]

STRICT_OUTPUT_SCHEMA = {
    "status": "accepted 또는 rejected",
    "news_id": "요청에 포함된 target_news_id",
    "news_lines": [
        "accepted일 때만 한국어 뉴스 문장 배열. news_line_count_rule을 따라 1개 또는 2개 문장만 작성. rejected이면 빈 배열"
    ],
    "used_brief_id": "입력 brief_id",
    "news_type": "입력 news_type",
    "claim_level": "입력 allowed_claim_level",
    "used_facts": ["기사에 실제로 사용한 detail_source_facts_ko"],
    "reject_reason": "accepted이면 빈 문자열",
    "style_self_check": {
        "used_only_detail_source_facts": True,
        "used_raw_write_safe_facts_directly": False,
        "used_restricted_facts": False,
        "has_market_claim_without_price_evidence": False,
        "has_ai_style_phrase": False,
        "has_generic_filler": False,
        "has_source_label_artifacts": False,
        "has_headline_body_repetition": False,
        "sounds_like_korean_financial_news": True,
    },
}


class JsonlIO:
    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(path)

        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSONL at {path}:{line_no}: {e}") from e

                if not isinstance(obj, dict):
                    raise ValueError(f"Expected JSON object at {path}:{line_no}")

                rows.append(obj)

        return rows

    @staticmethod
    def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)

        n = 0
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1

        return n


class TextUtil:
    @staticmethod
    def clean(value: Any, max_len: int | None = None) -> str:
        if value is None:
            return ""

        text = str(value)
        text = text.replace("\u3000", " ")
        text = re.sub(r"\s+", " ", text).strip()

        if max_len is not None and len(text) > max_len:
            return text[: max_len - 1].rstrip() + "…"

        return text

    @staticmethod
    def normalize_date(value: Any) -> str:
        text = TextUtil.clean(value)
        if not text:
            return ""

        digits = re.sub(r"[^0-9]", "", text)
        if len(digits) >= 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"

        return text[:10]

    @staticmethod
    def normalize_stock_code(value: Any) -> str:
        text = TextUtil.clean(value)
        text = re.sub(r"[^0-9]", "", text)

        if not text:
            return ""

        return text.zfill(6)[-6:]

    @staticmethod
    def parse_json_list(value: Any) -> list[Any]:
        if value is None or value == "":
            return []

        if isinstance(value, list):
            return value

        if isinstance(value, tuple):
            return list(value)

        if isinstance(value, dict):
            return [value]

        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []

            try:
                decoded = json.loads(text)
                if isinstance(decoded, list):
                    return decoded
                if isinstance(decoded, dict):
                    return [decoded]
                return [decoded]
            except json.JSONDecodeError:
                return [text]

        return [value]

    @staticmethod
    def parse_json_dict_list(value: Any) -> list[dict[str, Any]]:
        return [item for item in TextUtil.parse_json_list(value) if isinstance(item, dict)]

    @staticmethod
    def dedupe(items: Iterable[Any]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        for item in items:
            text = TextUtil.clean(item)
            if not text or text in seen:
                continue

            seen.add(text)
            out.append(text)

        return out

    @staticmethod
    def contains_any(text: str, keywords: Iterable[str]) -> bool:
        return any(k and k in text for k in keywords)


class BriefTextCleaner:
    """Remove pr05f/source labels and separate topic hints from usable facts."""

    LABEL_PATTERNS = [
        r"^종목\s*:\s*",
        r"^종목\s*이벤트\s*설명\s*:\s*",
        r"^종목\s*이벤트\s*주제\s*:\s*",
        r"^종목\s*주제\s*:\s*",
        r"^주제\s*:\s*",
        r"^이벤트\s*주제\s*:\s*",
        r"^설명\s*:\s*",
    ]

    TOPIC_LABEL_PATTERNS = [
        r"^종목\s*이벤트\s*주제\s*:\s*",
        r"^종목\s*주제\s*:\s*",
        r"^주제\s*:\s*",
        r"^이벤트\s*주제\s*:\s*",
    ]

    @classmethod
    def is_topic_hint(cls, raw: str) -> bool:
        text = TextUtil.clean(raw)
        return any(re.search(p, text) for p in cls.TOPIC_LABEL_PATTERNS)

    @classmethod
    def strip_labels(cls, raw: str) -> str:
        text = TextUtil.clean(raw)

        for pattern in cls.LABEL_PATTERNS:
            text = re.sub(pattern, "", text).strip()

        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def remove_artifacts(text: str) -> str:
        text = TextUtil.clean(text)

        replacements = [
            "종목 :",
            "종목:",
            "종목 주제:",
            "종목 이벤트 주제:",
            "종목 이벤트 설명:",
            "write_safe_facts:",
            "restricted_facts:",
            "brief:",
            "bundle:",
            "evidence:",
            "context:",
        ]

        for old in replacements:
            text = text.replace(old, "")

        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def split_facts_and_hints(cls, values: list[Any]) -> tuple[list[str], list[str]]:
        facts: list[str] = []
        hints: list[str] = []

        for item in values:
            raw = TextUtil.clean(item)
            if not raw:
                continue

            cleaned = cls.strip_labels(raw)
            if not cleaned:
                continue

            if cls.is_topic_hint(raw):
                hints.append(cleaned)
            else:
                facts.append(cleaned)

        return TextUtil.dedupe(facts), TextUtil.dedupe(hints)


class DetailFactBuilder:
    """Build atomic body-source facts from write_safe_facts_ko."""

    FACT_ANCHOR_RE = re.compile(
        r"(영업이익|영업적자|매출|판매|인수|퇴진|동참|흑자 전환|사상 최대|적자|"
        r"채산성|실적|순이익|계약|투자|배당|자기주식)"
    )
    CAUSE_HEAD_REWRITE_TERMS = [
        "폭락", "폭증", "급증", "급락", "가격", "수요", "매출",
        "수혜", "기대", "우려", "호조",
    ]

    @staticmethod
    def split_sentences(text: str) -> list[str]:
        text = BriefTextCleaner.remove_artifacts(text)
        if not text:
            return []

        text = text.replace("다. ", "다.\n")
        text = re.sub(r"([.!?])\s+", r"\1\n", text)
        text = re.sub(r"\n+", "\n", text)

        parts = [TextUtil.clean(x) for x in text.split("\n")]
        parts = [x for x in parts if x]

        out: list[str] = []
        for part in parts:
            part = DetailFactBuilder._normalize_sentence(part)
            if part:
                out.append(part)

        return out

    @staticmethod
    def _normalize_sentence(text: str) -> str:
        text = TextUtil.clean(text)
        if not text:
            return ""

        text = text.replace("K5·스포티지", "K5와 스포티지")
        text = text.replace("흑자 전환에 성공했다", "흑자 전환했다")
        text = text.replace("흑자전환에 성공했다", "흑자 전환했다")
        text = text.replace("적자 전환에 성공했다", "적자 전환했다")
        text = text.replace("적자전환에 성공했다", "적자 전환했다")
        text = re.sub(r"\s+", " ", text).strip()

        return text

    @staticmethod
    def _has_reject_phrase(text: str) -> bool:
        return TextUtil.contains_any(text, DETAIL_FACT_REJECT_PHRASES)

    @staticmethod
    def _strip_leading_cause_clause(text: str) -> str:
        text = TextUtil.clean(text)
        if not text:
            return ""

        m = re.match(r"^(.+?)(?:으로|로)\s+(.+다\.)$", text)
        if m:
            head = TextUtil.clean(m.group(1))
            tail = DetailFactBuilder._normalize_sentence(m.group(2))

            # Avoid chopping numeric predicates such as "3조7000억원으로".
            if (
                (
                    DetailFactBuilder._has_reject_phrase(head)
                    or TextUtil.contains_any(head, DetailFactBuilder.CAUSE_HEAD_REWRITE_TERMS)
                )
                and DetailFactBuilder.FACT_ANCHOR_RE.search(tail)
            ):
                return tail

        for pattern in [r"^(.+?)에\s+따라\s+(.+다\.)$", r"^(.+?)때문에\s+(.+다\.)$"]:
            m = re.match(pattern, text)
            if not m:
                continue
            head = TextUtil.clean(m.group(1))
            tail = DetailFactBuilder._normalize_sentence(m.group(2))
            if (
                (
                    DetailFactBuilder._has_reject_phrase(head)
                    or TextUtil.contains_any(head, DetailFactBuilder.CAUSE_HEAD_REWRITE_TERMS)
                )
                and DetailFactBuilder.FACT_ANCHOR_RE.search(tail)
            ):
                return tail

        return ""

    @staticmethod
    def _derive_candidates(sentence: str) -> list[str]:
        raw_sentence = TextUtil.clean(sentence)
        sentence = DetailFactBuilder._normalize_sentence(sentence)
        if not sentence:
            return []

        candidates = [sentence]

        cause_stripped = ""
        if (
            DetailFactBuilder._has_reject_phrase(raw_sentence)
            or TextUtil.contains_any(raw_sentence, DetailFactBuilder.CAUSE_HEAD_REWRITE_TERMS)
        ):
            cause_stripped = DetailFactBuilder._strip_leading_cause_clause(sentence)
        if cause_stripped:
            return [cause_stripped]

        return TextUtil.dedupe(candidates)

    @staticmethod
    def is_valid_detail_fact(text: str) -> bool:
        text = TextUtil.clean(text)

        if not text:
            return False

        if len(text) < 12:
            return False

        if TextUtil.contains_any(text, DETAIL_FACT_REJECT_PHRASES):
            return False

        if re.search(r"(주가|거래량).*(상승|하락|급등|급락|증가|감소)", text):
            return False

        if re.search(r"(투자자|시장).*(반응|관심|주목|기대|우려)", text):
            return False

        if "다." not in text and not text.endswith((".", "!", "?")):
            return False

        return True

    @classmethod
    def build(cls, facts: list[str], max_detail_facts: int = 1) -> list[str]:
        atomic: list[str] = []

        for fact in facts:
            for sentence in cls.split_sentences(fact):
                for candidate in cls._derive_candidates(sentence):
                    if not cls.is_valid_detail_fact(candidate):
                        continue
                    atomic.append(candidate)

        atomic = TextUtil.dedupe(atomic)

        if max_detail_facts > 0:
            atomic = atomic[:max_detail_facts]

        return atomic


@dataclass(frozen=True)
class Pr06aConfig:
    briefs_jsonl: Path = DEFAULT_BRIEFS_JSONL
    output_dir: Path = DEFAULT_OUTPUT_DIR
    model: str = "gpt-4o"
    max_total_requests: int = 35
    max_per_stock: int = 12
    random_seed: int = 42
    temperature: float = 0.25
    max_tokens: int = 700
    include_readiness: set[str] = field(default_factory=lambda: set(DEFAULT_READY_STATUSES))
    include_news_types: set[str] = field(default_factory=lambda: set(DEFAULT_NEWS_TYPES))
    min_write_safe_facts: int = 2
    max_detail_facts: int = 1
    include_borderline: bool = False


@dataclass
class BriefRecord:
    raw: dict[str, Any]
    bundle_id: str
    stock_code: str
    stock_name: str
    anchor_date: str
    event_family: str
    action_type: str
    news_type: str
    generation_readiness: str
    brief_quality_tier: str
    allowed_claim_level: str
    editorial_angle_ko: str
    lead_fact_ko: str
    write_safe_facts_ko: list[str]
    topic_hints_ko: list[str]
    detail_source_facts_ko: list[str]
    restricted_facts_ko: list[dict[str, Any]]
    write_safe_fact_count: int
    topic_hint_count: int
    detail_source_fact_count: int
    usable_support_count: int
    restricted_fact_count: int
    sample_exclusion_reason: str = ""
    sample_priority_score: float = 0.0


class BriefLoader:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config

    def load(self) -> list[BriefRecord]:
        rows = JsonlIO.read_jsonl(self.config.briefs_jsonl)
        records = [self._to_record(row) for row in rows]

        for r in records:
            r.sample_priority_score = self._score(r)
            r.sample_exclusion_reason = self._exclusion_reason(r)

        return records

    def _to_record(self, row: dict[str, Any]) -> BriefRecord:
        raw_safe = TextUtil.parse_json_list(row.get("write_safe_facts_ko"))

        if not raw_safe:
            raw_safe = TextUtil.parse_json_list(row.get("supporting_facts_ko"))

        facts, hints = BriefTextCleaner.split_facts_and_hints(raw_safe)

        related_fact_groups = TextUtil.parse_json_dict_list(row.get("related_fact_groups_ko"))
        best_related_group_facts = self._best_related_group_facts(related_fact_groups)
        layered_facts = TextUtil.dedupe(
            TextUtil.parse_json_list(row.get("official_detail_facts_ko"))
            + TextUtil.parse_json_list(row.get("main_source_facts_ko"))
            + TextUtil.parse_json_list(row.get("supporting_context_facts_ko"))
        )
        layered_facts = [
            BriefTextCleaner.remove_artifacts(TextUtil.clean(x))
            for x in layered_facts
            if TextUtil.clean(x)
        ]
        detail_source_facts = DetailFactBuilder.build(
            facts=best_related_group_facts or layered_facts or facts,
            max_detail_facts=self.config.max_detail_facts,
        )

        lead_fact = BriefTextCleaner.strip_labels(TextUtil.clean(row.get("lead_fact_ko")))
        editorial_angle = BriefTextCleaner.strip_labels(TextUtil.clean(row.get("editorial_angle_ko")))

        restricted_raw = TextUtil.parse_json_list(row.get("restricted_facts_ko"))
        restricted: list[dict[str, Any]] = []

        for item in restricted_raw:
            if isinstance(item, dict):
                restricted.append({
                    "text_ko": BriefTextCleaner.remove_artifacts(TextUtil.clean(item.get("text_ko"), 360)),
                    "reason": TextUtil.clean(item.get("reason"), 120),
                    "source": TextUtil.clean(item.get("source"), 80),
                })
            else:
                restricted.append({
                    "text_ko": BriefTextCleaner.remove_artifacts(TextUtil.clean(item, 360)),
                    "reason": "restricted_by_pr05f",
                    "source": "unknown",
                })

        return BriefRecord(
            raw=row,
            bundle_id=TextUtil.clean(row.get("bundle_id")),
            stock_code=TextUtil.normalize_stock_code(row.get("stock_code")),
            stock_name=TextUtil.clean(row.get("stock_name")),
            anchor_date=TextUtil.normalize_date(row.get("anchor_date")),
            event_family=TextUtil.clean(row.get("event_family")),
            action_type=TextUtil.clean(row.get("action_type")),
            news_type=TextUtil.clean(row.get("news_type")),
            generation_readiness=TextUtil.clean(row.get("generation_readiness")),
            brief_quality_tier=TextUtil.clean(row.get("brief_quality_tier")),
            allowed_claim_level=TextUtil.clean(row.get("allowed_claim_level")) or "no_market_claim",
            editorial_angle_ko=editorial_angle,
            lead_fact_ko=lead_fact,
            write_safe_facts_ko=facts,
            topic_hints_ko=hints,
            detail_source_facts_ko=detail_source_facts,
            restricted_facts_ko=restricted,
            write_safe_fact_count=len(facts),
            topic_hint_count=len(hints),
            detail_source_fact_count=len(detail_source_facts),
            usable_support_count=len(facts) + len(hints),
            restricted_fact_count=len(restricted),
        )

    def _best_related_group_facts(self, groups: list[dict[str, Any]]) -> list[str]:
        ranked: list[tuple[tuple[int, int, int], list[str]]] = []

        for group in groups:
            raw_facts = TextUtil.parse_json_list(group.get("facts_ko"))
            facts = [
                BriefTextCleaner.remove_artifacts(TextUtil.clean(x))
                for x in raw_facts
                if TextUtil.clean(x)
            ]
            facts = TextUtil.dedupe(facts)
            if not facts:
                continue

            support_bonus = 1 if group.get("has_supporting_fact") else 0
            source_bonus = 1 if TextUtil.clean(group.get("source")) == "dart" else 0
            score = (min(len(facts), self.config.max_detail_facts), support_bonus, source_bonus)
            ranked.append((score, facts))

        if not ranked:
            return []

        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1][: self.config.max_detail_facts]

    def _exclusion_reason(self, r: BriefRecord) -> str:
        if not r.bundle_id:
            return "missing_bundle_id"

        if r.generation_readiness not in self.config.include_readiness:
            return f"readiness_not_included:{r.generation_readiness}"

        if r.news_type not in self.config.include_news_types:
            return f"news_type_not_included:{r.news_type}"

        if r.write_safe_fact_count < 1:
            return f"no_concrete_write_safe_fact:{r.write_safe_fact_count}"

        if r.detail_source_fact_count < 1:
            return "no_atomic_detail_source_fact"

        if r.usable_support_count < self.config.min_write_safe_facts:
            return f"too_few_usable_supports:{r.usable_support_count}"

        if not r.stock_name:
            return "missing_stock_name"

        if not r.anchor_date:
            return "missing_anchor_date"

        return ""

    def _score(self, r: BriefRecord) -> float:
        score = 0.0

        if r.generation_readiness == "ready":
            score += 100
        elif r.generation_readiness == "borderline":
            score += 55

        if r.news_type == "stock_event_trigger":
            score += 30

        score += min(r.usable_support_count, 4) * 8
        score += min(r.detail_source_fact_count, 2) * 12

        if r.brief_quality_tier == "B":
            score += 10
        elif r.brief_quality_tier == "C":
            score += 2

        numeric_blob = " ".join(r.detail_source_facts_ko + r.topic_hints_ko)

        if re.search(r"\d", numeric_blob):
            score += 12

        if any(unit in numeric_blob for unit in ["억원", "조원", "%", "만대", "영업이익", "영업적자", "매출"]):
            score += 10

        if any(len(f) < 18 for f in r.detail_source_facts_ko):
            score -= 8

        return score


class CandidateSelector:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config

    def select(self, records: list[BriefRecord]) -> list[BriefRecord]:
        eligible = [r for r in records if not r.sample_exclusion_reason]

        rnd = random.Random(self.config.random_seed)
        rnd.shuffle(eligible)

        eligible.sort(
            key=lambda r: (r.sample_priority_score, r.anchor_date, r.bundle_id),
            reverse=True,
        )

        selected: list[BriefRecord] = []
        per_stock: dict[str, int] = {}

        for r in eligible:
            key = r.stock_code or r.stock_name

            if per_stock.get(key, 0) >= self.config.max_per_stock:
                continue

            selected.append(r)
            per_stock[key] = per_stock.get(key, 0) + 1

            if len(selected) >= self.config.max_total_requests:
                break

        selected.sort(key=lambda r: (r.anchor_date, r.stock_code, r.bundle_id))
        return selected


class PromptBuilder:
    @staticmethod
    def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": PromptBuilder._system_prompt()},
            {"role": "user", "content": PromptBuilder._user_prompt(payload)},
        ]

    @staticmethod
    def _system_prompt() -> str:
        return "\n".join([
            "You are a Korean financial news editor writing concise company-specific stock news for a market simulation.",
            "The goal is natural, high-quality Korean financial news copy that does not sound AI-generated.",
            "Use a plain financial wire style, not an analyst report, disclosure digest, public-relations memo, or assistant explanation.",
            "",
            "Most important source rule:",
            "- news_lines must use detail_source_facts_ko only.",
            "- Do not write a separate headline.",
            "- No background, community, macro, restricted, or audit-only evidence is provided to the writer.",
            "",
            "Do not copy source labels such as 종목, 종목 주제, 종목 이벤트, write_safe_facts, detail_source_facts, brief, bundle, evidence, or context.",
            "Do not mention the internal pipeline or data source labels.",
            "",
            "If claim_level is no_market_claim:",
            "- Do not mention stock price, share price, trading volume, investors, market reaction, buying pressure, selling pressure, 호재, 악재, 투자심리, 급등, 급락.",
            "- Do not say a stock moved because of the event.",
            "- Do not infer market interpretation.",
            "",
            "Allowed writing:",
            "- Convert detail_source_facts_ko into natural Korean financial-wire copy.",
            "- Return news_lines as an array of Korean news sentences.",
            "- If news_line_count_rule is exactly_one_line, news_lines must contain exactly one sentence.",
            "- If news_line_count_rule is one_or_two_lines, news_lines may contain one or two sentences.",
            "- Each news_lines item must be a complete sentence ending with a period.",
            "- You may make light grammatical edits for readability, but do not add interpretation.",
            "- If a fact contains Chinese or Japanese characters (CJK), use only the Latin/English transliteration in the fact, or describe the entity as '현지 자회사' or '중국 법인'. Do not output CJK characters in news_lines.",
            "",
            "Earnings-style guidance (event_family = earnings):",
            "- When writing two sentences about financial results, do NOT end both sentences with '공시됐다.'",
            "- Preferred pattern: Line 1 states 매출액; Line 2 combines 영업이익 and 당기순이익 ending with '기록했다' or '집계됐다'.",
            "- Example: '○○의 매출액은 약 ○조원으로 공시됐다. / 영업이익은 약 ○억원, 당기순이익은 약 ○억원을 기록했다.'",
            "- Never end consecutive news_lines with the same verb.",
            "",
            "Forbidden writing:",
            "- Generic filler: 결정 자료에는..., 보고서에는..., 항목이 포함됐다, 공개되지 않았다, 추후 공지될 예정이다.",
            "- AI/report words: 이벤트, 맥락, 의미, 시사, 해석, 주목된다, 확인할 필요, 가능성이 있다.",
            "- Mechanical phrases: 중심의 종목 단신, 관련 사안, 이번 이벤트, 해당 사안.",
            "- Test-observed AI phrases: 최근, 것으로 나타났다, 것으로 분석된다, 분석된다, 영향을 미친 것으로, 이는, 이로써, 이에 따라, 성과다, 이루어진, 이루어졌다.",
            "- Causal filler: 기여했다, 기여한, 기여하며.",
            "- Overwritten cycle phrases: 정점이었다, 슈퍼사이클의 정점, 폭발적으로.",
            "- Unsupported cause/effect or outlook beyond provided detail_source_facts_ko.",
            "- Any news line based on topic_hints_ko.",
            "- A headline field or detail_news field.",
            "",
            "Return one valid JSON object only. No markdown.",
            "The JSON must always contain status.",
        ])

    @staticmethod
    def _user_prompt(payload: dict[str, Any]) -> str:
        instruction = {
            "task": "Write one Korean stock-news sample as news_lines from this pr05f news brief, or reject if it cannot be written safely.",
            "news_style_target": {
                "language": "Korean",
                "style": "concise financial wire / market news brief",
                "tone": "plain, factual, natural",
                "length": "news_lines following news_line_count_rule; no separate headline",
                "not_allowed": "AI explanation, analyst note, disclosure-summary filler, investment advice",
            },
            "accept_rules": [
                "Accept if detail_source_facts_ko has at least one concrete fact.",
                "Use detail_source_facts_ko as the only material for news_lines.",
                "Do not write a headline.",
                "If detail_source_fact_count is 1, news_lines must contain exactly one sentence and must only rewrite that one detail source fact.",
                "If detail_source_fact_count is 2 or more, news_lines may contain one or two sentences using only detail_source_facts_ko.",
                "used_facts must list only facts actually used from detail_source_facts_ko.",
            ],
            "rejection_rules": [
                "Reject if the article would need stock price, market reaction, or investor sentiment under no_market_claim.",
                "Reject if news_lines would need generic filler or facts absent from detail_source_facts_ko.",
                "Reject if no concrete detail_source_facts_ko item remains.",
                "Reject if the only detail source fact is too vague to support even a one-sentence brief.",
            ],
            "output_schema": STRICT_OUTPUT_SCHEMA,
            "brief_payload": payload,
        }

        return json.dumps(instruction, ensure_ascii=False, indent=2)


class RequestBuilder:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config

    def build(self, records: list[BriefRecord]) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []

        for idx, r in enumerate(records, start=1):
            news_id = f"stock_news_sample_{idx:03d}"
            payload = self._payload(r, news_id)

            requests.append({
                "custom_id": f"stock_news__{r.bundle_id}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": self.config.model,
                    "messages": PromptBuilder.build_messages(payload),
                    "temperature": self.config.temperature,
                    "top_p": 1,
                    "max_tokens": self.config.max_tokens,
                    "response_format": {"type": "json_object"},
                },
            })

        return requests

    @staticmethod
    def _payload(r: BriefRecord, news_id: str) -> dict[str, Any]:
        news_line_count_rule = (
            "exactly_one_line"
            if r.detail_source_fact_count <= 1
            else "one_or_two_lines"
        )

        return {
            "target_news_id": news_id,
            "brief_id": r.bundle_id,
            "stock_code": r.stock_code,
            "stock_name": r.stock_name,
            "anchor_date": r.anchor_date,
            "event_family": r.event_family,
            "action_type": r.action_type,
            "news_type": r.news_type,
            "generation_readiness": r.generation_readiness,
            "brief_quality_tier": r.brief_quality_tier,
            "claim_level": r.allowed_claim_level,

            "detail_source_fact_count": r.detail_source_fact_count,
            "news_line_count_rule": news_line_count_rule,

            "detail_source_facts_ko": [
                BriefTextCleaner.remove_artifacts(x)
                for x in r.detail_source_facts_ko
            ],
        }


CANDIDATE_CSV_FIELDS = [
    "sample_no",
    "bundle_id",
    "stock_code",
    "stock_name",
    "anchor_date",
    "event_family",
    "action_type",
    "news_type",
    "generation_readiness",
    "brief_quality_tier",
    "allowed_claim_level",
    "write_safe_fact_count",
    "topic_hint_count",
    "detail_source_fact_count",
    "usable_support_count",
    "restricted_fact_count",
    "sample_priority_score",
    "sample_exclusion_reason",
    "editorial_angle_ko",
    "lead_fact_ko",
    "detail_source_facts_ko",
    "write_safe_facts_ko",
    "topic_hints_ko",
    "restricted_facts_ko",
]


class OutputWriter:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config
        self.output_dir = config.output_dir

    def write(
        self,
        all_records: list[BriefRecord],
        selected: list[BriefRecord],
        requests: list[dict[str, Any]],
    ) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        JsonlIO.write_jsonl(self.output_dir / "stock_news_sample_requests.jsonl", requests)
        self._write_candidates_csv(self.output_dir / "stock_news_sample_candidates.csv", selected)
        self._write_candidate_pool_csv(self.output_dir / "stock_news_sample_candidate_pool.csv", all_records)
        self._write_report(self.output_dir / "stock_news_sample_report.md", all_records, selected, requests)
        self._write_preview(self.output_dir / "prompt_preview.txt", requests)

    def _write_candidates_csv(self, path: Path, records: list[BriefRecord]) -> None:
        rows = [self._record_to_row(r, sample_no=i) for i, r in enumerate(records, start=1)]
        self._write_csv(path, rows, CANDIDATE_CSV_FIELDS)

    def _write_candidate_pool_csv(self, path: Path, records: list[BriefRecord]) -> None:
        rows = [self._record_to_row(r, sample_no="") for r in records]
        self._write_csv(path, rows, CANDIDATE_CSV_FIELDS)

    @staticmethod
    def _record_to_row(r: BriefRecord, sample_no: int | str) -> dict[str, Any]:
        return {
            "sample_no": sample_no,
            "bundle_id": r.bundle_id,
            "stock_code": r.stock_code,
            "stock_name": r.stock_name,
            "anchor_date": r.anchor_date,
            "event_family": r.event_family,
            "action_type": r.action_type,
            "news_type": r.news_type,
            "generation_readiness": r.generation_readiness,
            "brief_quality_tier": r.brief_quality_tier,
            "allowed_claim_level": r.allowed_claim_level,
            "write_safe_fact_count": r.write_safe_fact_count,
            "topic_hint_count": r.topic_hint_count,
            "detail_source_fact_count": r.detail_source_fact_count,
            "usable_support_count": r.usable_support_count,
            "restricted_fact_count": r.restricted_fact_count,
            "sample_priority_score": round(r.sample_priority_score, 3),
            "sample_exclusion_reason": r.sample_exclusion_reason,
            "editorial_angle_ko": r.editorial_angle_ko,
            "lead_fact_ko": r.lead_fact_ko,
            "detail_source_facts_ko": json.dumps(r.detail_source_facts_ko, ensure_ascii=False),
            "write_safe_facts_ko": json.dumps(r.write_safe_facts_ko, ensure_ascii=False),
            "topic_hints_ko": json.dumps(r.topic_hints_ko, ensure_ascii=False),
            "restricted_facts_ko": json.dumps(r.restricted_facts_ko, ensure_ascii=False),
        }

    @staticmethod
    def _write_csv(
        path: Path,
        rows: list[dict[str, Any]],
        fieldnames: list[str] | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        if fieldnames is None:
            fieldnames = list(rows[0].keys()) if rows else ["empty"]

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            if rows:
                writer.writerows(rows)

    def _write_report(
        self,
        path: Path,
        all_records: list[BriefRecord],
        selected: list[BriefRecord],
        requests: list[dict[str, Any]],
    ) -> None:
        def counts(values: Iterable[str]) -> dict[str, int]:
            out: dict[str, int] = {}

            for v in values:
                out[v] = out.get(v, 0) + 1

            return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

        eligible = [r for r in all_records if not r.sample_exclusion_reason]

        lines = [
            "# pr06a Stock News Sample Requests From pr05f Briefs",
            "",
            "## Input",
            f"- briefs_jsonl: `{self.config.briefs_jsonl}`",
            "",
            "## Output",
            f"- output_dir: `{self.output_dir}`",
            f"- requests: `{self.output_dir / 'stock_news_sample_requests.jsonl'}`",
            f"- candidates: `{self.output_dir / 'stock_news_sample_candidates.csv'}`",
            "",
            "## Config",
            f"- model: `{self.config.model}`",
            f"- max_total_requests: `{self.config.max_total_requests}`",
            f"- max_per_stock: `{self.config.max_per_stock}`",
            f"- temperature: `{self.config.temperature}`",
            f"- max_tokens: `{self.config.max_tokens}`",
            f"- include_readiness: `{sorted(self.config.include_readiness)}`",
            f"- include_news_types: `{sorted(self.config.include_news_types)}`",
            f"- min_write_safe_facts: `{self.config.min_write_safe_facts}`",
            f"- max_detail_facts: `{self.config.max_detail_facts}`",
            "",
            "## Counts",
            f"- input_records: {len(all_records)}",
            f"- eligible_records: {len(eligible)}",
            f"- selected_records: {len(selected)}",
            f"- request_count: {len(requests)}",
            "",
            "## Generation Readiness",
            "```json",
            json.dumps(counts(r.generation_readiness for r in all_records), ensure_ascii=False, indent=2),
            "```",
            "",
            "## News Types",
            "```json",
            json.dumps(counts(r.news_type for r in all_records), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Exclusion Reasons",
            "```json",
            json.dumps(counts(r.sample_exclusion_reason or "selected_pool" for r in all_records), ensure_ascii=False, indent=2),
            "```",
            "",
            "## Selected Sample",
            "| no | date | stock | news_type | detail_facts | restricted | bundle_id |",
            "|---:|---|---|---|---:|---:|---|",
        ]

        for i, r in enumerate(selected, start=1):
            lines.append(
                f"| {i} | {r.anchor_date} | {r.stock_name} | {r.news_type} | "
                f"{r.detail_source_fact_count} | {r.restricted_fact_count} | {r.bundle_id} |"
            )

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _write_preview(path: Path, requests: list[dict[str, Any]]) -> None:
        if not requests:
            path.write_text("[NO REQUESTS]", encoding="utf-8")
            return

        req = requests[0]
        messages = req["body"]["messages"]

        chunks = [
            "# Prompt Preview",
            "",
            f"custom_id: {req.get('custom_id')}",
            "",
        ]

        for m in messages:
            chunks.append(f"## {m['role'].upper()}")
            chunks.append(m["content"])
            chunks.append("")

        path.write_text("\n".join(chunks), encoding="utf-8")


class Pipeline:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config

    def run(self) -> None:
        records = BriefLoader(self.config).load()
        selected = CandidateSelector(self.config).select(records)
        requests = RequestBuilder(self.config).build(selected)

        OutputWriter(self.config).write(records, selected, requests)

        print("=" * 100)
        print("[pr06a] Build stock news sample requests from pr05f briefs")
        print(f"briefs_jsonl: {self.config.briefs_jsonl}")
        print(f"output_dir: {self.config.output_dir}")
        print(f"input_records: {len(records)}")
        print(f"eligible_records: {sum(1 for r in records if not r.sample_exclusion_reason)}")
        print(f"selected_records: {len(selected)}")
        print(f"requests: {len(requests)}")
        print("=" * 100)
        print(f"[saved] {self.config.output_dir / 'stock_news_sample_requests.jsonl'}")
        print(f"[saved] {self.config.output_dir / 'stock_news_sample_candidates.csv'}")
        print(f"[saved] {self.config.output_dir / 'stock_news_sample_candidate_pool.csv'}")
        print(f"[saved] {self.config.output_dir / 'stock_news_sample_report.md'}")
        print(f"[saved] {self.config.output_dir / 'prompt_preview.txt'}")


def parse_set_arg(value: str | None, default: set[str]) -> set[str]:
    if not value:
        return set(default)

    return {x.strip() for x in value.split(",") if x.strip()}


def parse_args() -> Pr06aConfig:
    p = argparse.ArgumentParser(
        description="Build pr06a stock-news sample requests from pr05f briefs."
    )

    p.add_argument("--briefs-jsonl", type=Path, default=DEFAULT_BRIEFS_JSONL)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--model", default="gpt-4o")
    p.add_argument("--max-total-requests", type=int, default=35)
    p.add_argument("--max-per-stock", type=int, default=12)
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument("--temperature", type=float, default=0.25)
    p.add_argument("--max-tokens", type=int, default=700)
    p.add_argument("--include-readiness", default="ready")
    p.add_argument("--include-news-types", default="stock_event_trigger")
    p.add_argument("--min-write-safe-facts", type=int, default=2)
    p.add_argument("--max-detail-facts", type=int, default=1)
    p.add_argument("--include-borderline", action="store_true")

    args = p.parse_args()

    readiness = parse_set_arg(args.include_readiness, DEFAULT_READY_STATUSES)

    if args.include_borderline:
        readiness.add("borderline")

    return Pr06aConfig(
        briefs_jsonl=args.briefs_jsonl,
        output_dir=args.output_dir,
        model=args.model,
        max_total_requests=args.max_total_requests,
        max_per_stock=args.max_per_stock,
        random_seed=args.random_seed,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        include_readiness=readiness,
        include_news_types=parse_set_arg(args.include_news_types, DEFAULT_NEWS_TYPES),
        min_write_safe_facts=args.min_write_safe_facts,
        max_detail_facts=args.max_detail_facts,
        include_borderline=args.include_borderline,
    )


def main() -> None:
    config = parse_args()
    Pipeline(config).run()


if __name__ == "__main__":
    main()
