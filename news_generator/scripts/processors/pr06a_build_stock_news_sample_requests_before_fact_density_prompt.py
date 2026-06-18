#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr06a_build_stock_news_sample_requests.py
ANTI_AI_FILLER_PHRASES = ['이번 결정', '이번 투자는', '이번 소송', '조치로 보인다', '조치로 알려졌다', '목표로 한다', '추후 공지될 예정이다', '공개되지 않았다', '추가 발표가 예상된다', '예상된다', '보인다', '알려졌다', '이루어졌다', '자금 조달을 위한 조치', '자본 확충을 위한 조치']

Build a small, reviewable set of stock-news generation requests from pr05e
stock evidence bundles.

This script does NOT call the OpenAI API and does NOT generate final news.
It writes:

1. stock_news_sample_requests.jsonl       - OpenAI Chat/Batch request JSONL
2. stock_news_sample_candidates.csv       - selected sample metadata
3. stock_news_sample_candidate_pool.csv   - full eligible pool audit metadata
4. stock_news_sample_report.md            - sampling/report summary
5. prompt_preview.txt                     - first prompt preview for inspection

Revision focus
--------------
pr05e now emits action primitives in writing_frame:
  action_type, plain_action_ko, corporate_actor, actor_topic_particle_ko,
  object_ko, allowed_verbs_ko, avoid_verbs_ko, usable_fact_slots, etc.

This script must NOT ask the model to reuse recommended_lead_template_ko or
reference_lead_template_ko. Those fields are treated as audit-only.

The prompt is designed to reduce AI/report tone and disclosure-restatement tone:
- no "이벤트/맥락/의미/해석/주목/확인할 필요"
- no market reaction or stock-price causality under no_market_claim
- no "관련 내용을 공시했다" style boilerplate
- build from concrete corporate action primitives

Default input:
  /Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.jsonl

Default output:
  /Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


# =============================================================================
# Constants
# =============================================================================

DEFAULT_BUNDLES_JSONL = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.jsonl"
)

DEFAULT_OUTPUT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests"
)

# First style-test run should over-sample concrete action families.
# Generic families are not deleted; they are downranked/excluded from sample selection.
DEFAULT_FAMILY_QUOTAS: dict[str, int] = {
    "capital_financing": 6,
    "treasury_stock": 5,
    "dividend": 4,
    "equity_investment": 6,
    "investment": 6,
    "asset_transaction": 4,
    "legal_regulatory": 5,
    "earnings": 5,
    "contract": 5,
    "business_transfer": 4,
    "trading_status": 3,
    "listing_risk": 3,
    "management_governance": 3,
    "guidance": 3,
    "major_management_matter": 1,
    "other_company_event": 0,
}

CLAIM_ORDER: dict[str, int] = {
    "insufficient_evidence": 0,
    "no_market_claim": 1,
    "reaction_only": 2,
    "plausible_market_context": 3,
    "likely_contributor": 4,
    "strongest_attributable_disclosed_factor": 5,
    # Legacy compatibility.
    "primary_market_driver_candidate": 5,
}

CONCRETE_PREFERRED_FAMILIES = {
    "capital_financing",
    "treasury_stock",
    "dividend",
    "equity_investment",
    "investment",
    "asset_transaction",
    "legal_regulatory",
    "earnings",
    "contract",
    "business_transfer",
    "trading_status",
    "listing_risk",
    "management_governance",
    "guidance",
}

GENERIC_ACTION_TYPES = {
    "unspecified_disclosure",
    "generic_management_matter",
    "plain_disclosure",
    "other_disclosure",
    "unknown",
    "",
}

LOW_QUALITY_RISKS = {
    "generic_disclosure_title",
    "template_like_lead",
    "generic_major_management_matter",
    "low_writing_quality",
}

AI_REPORT_PHRASES_KO = [
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

MARKET_CAUSAL_PHRASES_KO = [
    "주가 상승",
    "주가 하락",
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
    "여파로",
    "덕분에",
    "탓에",
    "이끌었다",
    "불렀다",
    "작용했다",
    "주요 요인",
    "주된 요인",
]

DISCLOSURE_RESTATEMENT_PHRASES_KO = [
    "관련 내용을 공시했다",
    "주요 경영사항을 공시했다",
    "계획을 공시했다",
    "내용을 밝혔다",
    "공시를 통해 밝혔다",
    "투자판단 관련 주요 경영사항",
    "관련 사항을 공시했다",
    "관련 공시를 냈다",
]

BANNED_SENTENCE_OPENERS_KO = [
    "이번 이벤트는",
    "이번 공시는",
    "해당 이벤트는",
    "해당 사안은",
    "해당 공시는",
    "시장 참여자들은",
    "투자자들은",
]

PREFERRED_VERBS_KO = [
    "결정했다",
    "체결했다",
    "취득하기로 했다",
    "처분하기로 했다",
    "발행하기로 했다",
    "지급하기로 했다",
    "진행한다",
    "보고했다",
    "제출했다",
    "제시했다",
    "기록했다",
    "집계됐다",
    "확정했다",
]

AVOID_LEAD_VERBS_KO = [
    "공시했다",
    "밝혔다",
    "관련 내용을 공시했다",
    "주요 경영사항을 공시했다",
    "계획을 공시했다",
]


# =============================================================================
# Utility classes
# =============================================================================


class JsonlIO:
    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"JSONL not found: {path}")

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
        count = 0
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                count += 1
        return count


class TextUtil:
    @staticmethod
    def clean_text(value: Any, max_len: int | None = None) -> str:
        if value is None:
            return ""
        text = str(value)
        text = re.sub(r"\s+", " ", text).strip()
        if max_len is not None and len(text) > max_len:
            return text[: max_len - 1].rstrip() + "…"
        return text

    @staticmethod
    def normalize_stock_code(value: Any) -> str:
        text = TextUtil.clean_text(value)
        if not text:
            return ""
        text = text.replace(".0", "")
        text = re.sub(r"[^0-9]", "", text)
        return text.zfill(6)[-6:] if text else ""

    @staticmethod
    def normalize_date(value: Any) -> str:
        text = TextUtil.clean_text(value)
        if not text:
            return ""
        digits = re.sub(r"[^0-9]", "", text)
        if len(digits) >= 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        return text[:10]

    @staticmethod
    def dedupe_keep_order(items: Iterable[Any]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            text = TextUtil.clean_text(item)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out

    @staticmethod
    def contains_any(text: Any, keywords: Iterable[str]) -> bool:
        s = TextUtil.clean_text(text)
        return any(k and k in s for k in keywords)


class DictUtil:
    @staticmethod
    def get_any(obj: dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        return default

    @staticmethod
    def as_list(value: Any) -> list[Any]:
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
            except json.JSONDecodeError:
                return [text]
        return [value]

    @staticmethod
    def as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text:
                try:
                    decoded = json.loads(text)
                    if isinstance(decoded, dict):
                        return decoded
                except json.JSONDecodeError:
                    pass
        return {}

    @staticmethod
    def to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = TextUtil.clean_text(value).lower()
        return text in {"true", "1", "yes", "y", "t"}


# =============================================================================
# Configuration
# =============================================================================


@dataclass(frozen=True)
class Pr06aConfig:
    bundles_jsonl: Path = DEFAULT_BUNDLES_JSONL
    output_dir: Path = DEFAULT_OUTPUT_DIR
    model: str = "gpt-4o"
    max_total_requests: int = 50
    max_per_stock: int = 3
    random_seed: int = 42
    family_quotas: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_FAMILY_QUOTAS))
    allowed_claim_levels: set[str] = field(default_factory=lambda: {"no_market_claim"})
    include_judge_blocked: bool = True
    min_candidate_topic_chars: int = 2
    max_evidence_items_per_source: int = 2
    temperature: float = 0.1
    max_tokens: int = 520
    exclude_generic_disclosure_titles: bool = True
    max_generic_action_samples: int = 0


# =============================================================================
# Bundle model / loading / scoring
# =============================================================================


@dataclass
class BundleRecord:
    raw: dict[str, Any]
    bundle_id: str
    event_group_id: str
    stock_code: str
    stock_name: str
    anchor_date: str
    event_family: str
    candidate_topic: str
    primary_topic_source: str
    claim_level: str
    bundle_candidate_rank: int
    judge_input_allowed: bool
    writing_frame: dict[str, Any]
    sample_priority_score: float
    sample_exclusion_reason: str
    has_generic_action: bool
    has_reference_template_only: bool


class WritingFrameNormalizer:
    """Normalize current and older pr05e writing_frame schemas.

    New pr05e emits action primitives. Older bundles may only include
    recommended_lead_template_ko/preferred_verbs_ko. We keep compatibility but
    mark weak frames so they are downranked/excluded from style samples.
    """

    @staticmethod
    def normalize(frame: dict[str, Any], record_defaults: dict[str, Any]) -> dict[str, Any]:
        frame = dict(frame or {})

        # Primitive fields from new pr05e.
        action_type = TextUtil.clean_text(frame.get("action_type"))
        plain_action_ko = TextUtil.clean_text(frame.get("plain_action_ko"))
        corporate_actor = TextUtil.clean_text(frame.get("corporate_actor")) or TextUtil.clean_text(record_defaults.get("stock_name"))
        actor_particle = TextUtil.clean_text(frame.get("actor_topic_particle_ko")) or "은/는"
        object_ko = TextUtil.clean_text(frame.get("object_ko"))
        allowed_verbs = DictUtil.as_list(frame.get("allowed_verbs_ko"))
        avoid_verbs = DictUtil.as_list(frame.get("avoid_verbs_ko"))
        usable_slots = DictUtil.as_list(frame.get("usable_fact_slots"))

        # Backward compatibility with older pr05e.
        if not allowed_verbs:
            allowed_verbs = DictUtil.as_list(frame.get("preferred_verbs_ko"))
        if not usable_slots:
            usable_slots = DictUtil.as_list(frame.get("usable_facts"))

        if not action_type:
            action_type = "unspecified_disclosure"
        if not corporate_actor:
            corporate_actor = "회사"
        if not allowed_verbs:
            allowed_verbs = ["결정했다", "진행한다"]

        blocked = TextUtil.dedupe_keep_order(
            DictUtil.as_list(frame.get("blocked_phrases"))
            + DictUtil.as_list(frame.get("banned_sentence_openers"))
            + AI_REPORT_PHRASES_KO
            + MARKET_CAUSAL_PHRASES_KO
            + DISCLOSURE_RESTATEMENT_PHRASES_KO
        )

        reference_template = TextUtil.clean_text(
            frame.get("reference_lead_template_ko") or frame.get("recommended_lead_template_ko"),
            max_len=260,
        )

        normalized = {
            "frame_type": TextUtil.clean_text(frame.get("frame_type")) or TextUtil.clean_text(record_defaults.get("event_family")),
            "lead_focus": TextUtil.clean_text(frame.get("lead_focus")) or "concrete_company_action",
            "action_type": action_type,
            "plain_action_ko": plain_action_ko,
            "corporate_actor": corporate_actor,
            "actor_topic_particle_ko": actor_particle,
            "object_ko": object_ko,
            "allowed_verbs_ko": TextUtil.dedupe_keep_order(allowed_verbs),
            "avoid_verbs_ko": TextUtil.dedupe_keep_order(avoid_verbs + AVOID_LEAD_VERBS_KO + ["의미가 있다", "주목된다"]),
            "usable_fact_slots": TextUtil.dedupe_keep_order(usable_slots),
            "sentence_style": TextUtil.clean_text(frame.get("sentence_style")) or "short_korean_financial_wire",
            "do_not_reuse_template": bool(frame.get("do_not_reuse_template", True)),
            "blocked_phrases": blocked,
            "banned_sentence_openers": TextUtil.dedupe_keep_order(
                DictUtil.as_list(frame.get("banned_sentence_openers")) + BANNED_SENTENCE_OPENERS_KO
            ),
            "style_rules": DictUtil.as_list(frame.get("style_rules")),
            "writing_quality_risk": TextUtil.clean_text(frame.get("writing_quality_risk")),
            "price_sentence_allowed": bool(frame.get("price_sentence_allowed", False)),
        }

        if "subject_of_action_ko" in frame:
            normalized["subject_of_action_ko"] = TextUtil.clean_text(frame.get("subject_of_action_ko"))

        # Audit only. Prompt explicitly forbids copying/paraphrasing this.
        if reference_template:
            normalized["audit_only_do_not_use"] = {
                "reference_lead_template_ko": reference_template,
                "reason": "Provided only for debugging. Do not copy, reuse, or paraphrase.",
            }

        return normalized


class BundleLoader:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config

    def load(self) -> list[BundleRecord]:
        rows = JsonlIO.read_jsonl(self.config.bundles_jsonl)
        records = [self._to_record(row) for row in rows]
        return [record for record in records if self._is_eligible(record)]

    def _to_record(self, row: dict[str, Any]) -> BundleRecord:
        bundle_id = TextUtil.clean_text(DictUtil.get_any(row, ["bundle_id"]))
        event_group_id = TextUtil.clean_text(DictUtil.get_any(row, ["event_group_id", "group_id"]))
        stock_code = TextUtil.normalize_stock_code(DictUtil.get_any(row, ["stock_code", "ticker"]))
        stock_name = TextUtil.clean_text(DictUtil.get_any(row, ["stock_name", "corp_name", "company_name"]))
        anchor_date = TextUtil.normalize_date(DictUtil.get_any(row, ["anchor_date", "event_date", "date"]))
        event_family = TextUtil.clean_text(DictUtil.get_any(row, ["event_family", "family"], "unknown"))
        candidate_topic = TextUtil.clean_text(
            DictUtil.get_any(row, ["candidate_topic", "topic", "representative_topic", "event_title"]),
            max_len=180,
        )
        primary_topic_source = TextUtil.clean_text(
            DictUtil.get_any(row, ["primary_topic_source", "topic_source"], "unknown")
        )
        claim_level = TextUtil.clean_text(
            DictUtil.get_any(
                row,
                ["max_allowed_market_claim_level_pre_llm", "claim_level", "market_claim_level"],
                "no_market_claim",
            )
        )
        rank_raw = DictUtil.get_any(row, ["bundle_candidate_rank", "rank"], 999)
        try:
            rank = int(float(rank_raw))
        except (TypeError, ValueError):
            rank = 999

        judge_input_allowed = DictUtil.to_bool(DictUtil.get_any(row, ["judge_input_allowed"], False))
        normalized_frame = WritingFrameNormalizer.normalize(
            DictUtil.as_dict(row.get("writing_frame")),
            {"stock_name": stock_name, "event_family": event_family},
        )
        has_generic_action = self._has_generic_action(normalized_frame, event_family, candidate_topic)
        has_reference_template_only = bool(normalized_frame.get("audit_only_do_not_use"))
        exclusion_reason = self._sample_exclusion_reason(
            row=row,
            event_family=event_family,
            candidate_topic=candidate_topic,
            writing_frame=normalized_frame,
            has_generic_action=has_generic_action,
        )
        score = self._score(
            row=row,
            rank=rank,
            judge_input_allowed=judge_input_allowed,
            event_family=event_family,
            writing_frame=normalized_frame,
            has_generic_action=has_generic_action,
            exclusion_reason=exclusion_reason,
        )

        return BundleRecord(
            raw=row,
            bundle_id=bundle_id,
            event_group_id=event_group_id,
            stock_code=stock_code,
            stock_name=stock_name,
            anchor_date=anchor_date,
            event_family=event_family,
            candidate_topic=candidate_topic,
            primary_topic_source=primary_topic_source,
            claim_level=claim_level,
            bundle_candidate_rank=rank,
            judge_input_allowed=judge_input_allowed,
            writing_frame=normalized_frame,
            sample_priority_score=score,
            sample_exclusion_reason=exclusion_reason,
            has_generic_action=has_generic_action,
            has_reference_template_only=has_reference_template_only,
        )

    def _is_eligible(self, record: BundleRecord) -> bool:
        if not record.bundle_id:
            return False
        if record.claim_level not in self.config.allowed_claim_levels:
            return False
        if not self.config.include_judge_blocked and not record.judge_input_allowed:
            return False
        if len(record.candidate_topic) < self.config.min_candidate_topic_chars:
            return False
        return True

    @staticmethod
    def _has_generic_action(frame: dict[str, Any], event_family: str, candidate_topic: str) -> bool:
        action_type = TextUtil.clean_text(frame.get("action_type"))
        plain_action = TextUtil.clean_text(frame.get("plain_action_ko"))
        risk = TextUtil.clean_text(frame.get("writing_quality_risk"))
        if action_type in GENERIC_ACTION_TYPES:
            return True
        if not plain_action:
            return True
        if risk in LOW_QUALITY_RISKS:
            return True
        if event_family == "major_management_matter" and "투자판단관련주요경영사항" in candidate_topic and action_type in GENERIC_ACTION_TYPES:
            return True
        return False

    @staticmethod
    def _sample_exclusion_reason(
        row: dict[str, Any],
        event_family: str,
        candidate_topic: str,
        writing_frame: dict[str, Any],
        has_generic_action: bool,
    ) -> str:
        action_type = TextUtil.clean_text(writing_frame.get("action_type"))
        plain_action = TextUtil.clean_text(writing_frame.get("plain_action_ko"))
        risk = TextUtil.clean_text(writing_frame.get("writing_quality_risk"))

        if risk in LOW_QUALITY_RISKS:
            return risk
        if has_generic_action:
            return "generic_action_or_empty_plain_action"
        if event_family == "major_management_matter" and "투자판단관련주요경영사항" in candidate_topic:
            return "generic_major_management_matter"
        if action_type == "unspecified_disclosure":
            return "unspecified_disclosure"
        if not plain_action:
            return "empty_plain_action_ko"
        return ""

    @staticmethod
    def _score(
        row: dict[str, Any],
        rank: int,
        judge_input_allowed: bool,
        event_family: str,
        writing_frame: dict[str, Any],
        has_generic_action: bool,
        exclusion_reason: str,
    ) -> float:
        score = 0.0
        action_type = TextUtil.clean_text(writing_frame.get("action_type"))
        plain_action = TextUtil.clean_text(writing_frame.get("plain_action_ko"))
        usable_slots = DictUtil.as_list(writing_frame.get("usable_fact_slots"))
        allowed_verbs = DictUtil.as_list(writing_frame.get("allowed_verbs_ko"))

        if event_family in CONCRETE_PREFERRED_FAMILIES:
            score += 20.0
        if action_type and action_type not in GENERIC_ACTION_TYPES:
            score += 25.0
        if plain_action:
            score += 15.0
        if allowed_verbs:
            score += min(5.0, float(len(allowed_verbs)))
        if usable_slots:
            score += min(8.0, float(len(usable_slots)))
        if judge_input_allowed:
            score += 3.0
        if rank < 999:
            score += max(0.0, 5.0 - float(rank) * 0.5)
        if DictUtil.as_list(row.get("dart_evidence")):
            score += 3.0
        if DictUtil.as_list(row.get("stock_event_evidence")):
            score += 2.0

        if has_generic_action:
            score -= 50.0
        if exclusion_reason:
            score -= 40.0
        if event_family == "other_company_event":
            score -= 20.0
        return score


# =============================================================================
# Sampling
# =============================================================================


class SampleSelector:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config
        self.rng = random.Random(config.random_seed)

    def select(self, records: list[BundleRecord]) -> list[BundleRecord]:
        if self.config.exclude_generic_disclosure_titles:
            preferred = [r for r in records if not r.sample_exclusion_reason]
            generic = [r for r in records if r.sample_exclusion_reason]
        else:
            preferred = list(records)
            generic = []

        selected: list[BundleRecord] = []
        selected_ids: set[str] = set()
        stock_counter: Counter[str] = Counter()
        generic_count = 0

        by_family: dict[str, list[BundleRecord]] = defaultdict(list)
        for record in preferred:
            by_family[record.event_family].append(record)

        # Family quota pass: concrete records only.
        for family, quota in self.config.family_quotas.items():
            if quota <= 0:
                continue
            chosen_in_family = 0
            for record in self._ranked_records(by_family.get(family, [])):
                if chosen_in_family >= quota or len(selected) >= self.config.max_total_requests:
                    break
                if not self._can_add(record, selected_ids, stock_counter):
                    continue
                self._add(record, selected, selected_ids, stock_counter)
                chosen_in_family += 1

        # Fill pass: remaining concrete records.
        if len(selected) < self.config.max_total_requests:
            remaining = [r for r in preferred if r.bundle_id not in selected_ids]
            for record in self._ranked_records(remaining):
                if len(selected) >= self.config.max_total_requests:
                    break
                if not self._can_add(record, selected_ids, stock_counter):
                    continue
                self._add(record, selected, selected_ids, stock_counter)

        # Optional fallback: allow a few generic records only if configured.
        if len(selected) < self.config.max_total_requests and self.config.max_generic_action_samples > 0:
            for record in self._ranked_records(generic):
                if len(selected) >= self.config.max_total_requests:
                    break
                if generic_count >= self.config.max_generic_action_samples:
                    break
                if not self._can_add(record, selected_ids, stock_counter):
                    continue
                self._add(record, selected, selected_ids, stock_counter)
                generic_count += 1

        return selected[: self.config.max_total_requests]

    def _ranked_records(self, records: list[BundleRecord]) -> list[BundleRecord]:
        grouped: dict[tuple[float, int], list[BundleRecord]] = defaultdict(list)
        for record in records:
            grouped[(record.sample_priority_score, -record.bundle_candidate_rank)].append(record)

        out: list[BundleRecord] = []
        for key in sorted(grouped.keys(), reverse=True):
            bucket = grouped[key]
            self.rng.shuffle(bucket)
            bucket.sort(key=lambda r: (r.anchor_date, r.stock_code, r.bundle_id))
            out.extend(bucket)
        return out

    def _can_add(self, record: BundleRecord, selected_ids: set[str], stock_counter: Counter[str]) -> bool:
        if record.bundle_id in selected_ids:
            return False
        if record.stock_code and stock_counter[record.stock_code] >= self.config.max_per_stock:
            return False
        return True

    def _add(
        self,
        record: BundleRecord,
        selected: list[BundleRecord],
        selected_ids: set[str],
        stock_counter: Counter[str],
    ) -> None:
        selected.append(record)
        selected_ids.add(record.bundle_id)
        if record.stock_code:
            stock_counter[record.stock_code] += 1


# =============================================================================
# Evidence compacting and prompt construction
# =============================================================================


class EvidenceCompactor:
    """Build compact factual payloads from pr05e bundles.

    The model receives action primitives and short evidence facts. It is not
    asked to summarize raw disclosure titles.
    """

    SOURCE_FIELDS: dict[str, list[str]] = {
        "dart_evidence": [
            "evidence_id",
            "dart_evidence_id",
            "rcept_no",
            "rcept_dt",
            "report_nm",
            "report_name",
            "disclosure_title",
            "corp_name",
            "stock_name",
            "summary",
            "detail",
            "amount",
            "contract_amount",
            "contract_counterparty",
            "counterparty",
            "contract_period",
            "funding_purpose",
            "purpose",
            "share_count",
            "issue_price",
            "dividend_per_share",
            "record_date",
            "payment_date",
            "total_amount",
            "target_company",
            "acquisition_amount",
            "disposal_amount",
            "stake_ratio",
            "claim_amount",
            "court",
            "case_name",
            "ruling_result",
            "sales",
            "operating_profit",
            "net_income",
            "yoy_change",
            "period",
        ],
        "stock_event_evidence": [
            "evidence_id",
            "event_id",
            "event_date",
            "event_title",
            "event_name",
            "event_type",
            "summary",
        ],
        "stock_event_context_evidence": [
            "evidence_id",
            "event_id",
            "event_date",
            "context_type",
            "summary",
        ],
        # Kept for completeness, but no_market_claim prompts still ban price/volume use.
        "price_volume_evidence": [
            "evidence_id",
            "date",
            "return",
            "volume_change",
            "direction",
            "summary",
        ],
        "gdelt_evidence": [
            "evidence_id",
            "published_at",
            "source_name",
            "domain",
            "summary",
            "title",
        ],
        "macro_evidence": [
            "evidence_id",
            "date",
            "event_name",
            "indicator_name",
            "summary",
        ],
    }

    def __init__(self, max_items_per_source: int = 2) -> None:
        self.max_items_per_source = max_items_per_source

    def compact(self, record: BundleRecord) -> dict[str, Any]:
        raw = record.raw
        compact_sources: dict[str, list[dict[str, Any]]] = {}
        for source_name, field_names in self.SOURCE_FIELDS.items():
            items = DictUtil.as_list(raw.get(source_name))
            compact_sources[source_name] = self._compact_items(items, field_names)

        writing_frame = self._payload_writing_frame(record.writing_frame)
        available_facts = self._extract_available_facts(record, compact_sources, writing_frame)

        return {
            "bundle_id": record.bundle_id,
            "event_group_id": record.event_group_id,
            "stock_code": record.stock_code,
            "stock_name": record.stock_name,
            "anchor_date": record.anchor_date,
            "event_family": record.event_family,
            "candidate_topic": record.candidate_topic,
            "primary_topic_source": record.primary_topic_source,
            "claim_level": record.claim_level,
            "market_claim_permission": self._market_claim_permission(record.claim_level),
            "writing_frame": writing_frame,
            "available_facts": available_facts,
            "source_evidence": compact_sources,
            "evidence_counts": self._evidence_counts(raw),
            "allowed_claims": self._as_text_list(raw.get("allowed_claims")),
            "forbidden_claims": self._as_text_list(raw.get("forbidden_claims")),
            "generation_policy": DictUtil.as_dict(raw.get("generation_policy")),
            "sampling_metadata": {
                "sample_priority_score": round(record.sample_priority_score, 3),
                "sample_exclusion_reason": record.sample_exclusion_reason,
                "has_generic_action": record.has_generic_action,
                "has_reference_template_only": record.has_reference_template_only,
            },
        }

    def _payload_writing_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        # Deliberately exclude recommended/reference templates from the main instruction fields.
        payload_frame = {
            "frame_type": frame.get("frame_type", ""),
            "lead_focus": frame.get("lead_focus", ""),
            "action_type": frame.get("action_type", ""),
            "plain_action_ko": frame.get("plain_action_ko", ""),
            "corporate_actor": frame.get("corporate_actor", ""),
            "actor_topic_particle_ko": frame.get("actor_topic_particle_ko", ""),
            "object_ko": frame.get("object_ko", ""),
            "allowed_verbs_ko": DictUtil.as_list(frame.get("allowed_verbs_ko")),
            "avoid_verbs_ko": DictUtil.as_list(frame.get("avoid_verbs_ko")),
            "usable_fact_slots": DictUtil.as_list(frame.get("usable_fact_slots")),
            "sentence_style": frame.get("sentence_style", "short_korean_financial_wire"),
            "do_not_reuse_template": True,
            "blocked_phrases": TextUtil.dedupe_keep_order(
                DictUtil.as_list(frame.get("blocked_phrases"))
                + AI_REPORT_PHRASES_KO
                + MARKET_CAUSAL_PHRASES_KO
                + DISCLOSURE_RESTATEMENT_PHRASES_KO
            ),
            "banned_sentence_openers": TextUtil.dedupe_keep_order(
                DictUtil.as_list(frame.get("banned_sentence_openers")) + BANNED_SENTENCE_OPENERS_KO
            ),
            "writing_quality_risk": frame.get("writing_quality_risk", ""),
        }
        if frame.get("subject_of_action_ko"):
            payload_frame["subject_of_action_ko"] = frame.get("subject_of_action_ko")
        if frame.get("audit_only_do_not_use"):
            payload_frame["audit_only_do_not_use"] = frame.get("audit_only_do_not_use")
        return payload_frame

    def _extract_available_facts(
        self,
        record: BundleRecord,
        compact_sources: dict[str, list[dict[str, Any]]],
        writing_frame: dict[str, Any],
    ) -> dict[str, Any]:
        wanted_slots = set(DictUtil.as_list(writing_frame.get("usable_fact_slots")))
        if not wanted_slots:
            return {}

        # Map common evidence keys into normalized fact slots.
        aliases: dict[str, list[str]] = {
            "amount": ["amount", "contract_amount", "acquisition_amount", "disposal_amount", "total_amount"],
            "funding_purpose": ["funding_purpose", "purpose"],
            "counterparty": ["counterparty", "contract_counterparty", "target_company"],
            "contract_amount": ["contract_amount", "amount"],
            "contract_period": ["contract_period", "period"],
            "share_count": ["share_count"],
            "issue_price": ["issue_price"],
            "dividend_per_share": ["dividend_per_share"],
            "record_date": ["record_date"],
            "payment_date": ["payment_date"],
            "target_company": ["target_company", "counterparty"],
            "stake_ratio": ["stake_ratio"],
            "claim_amount": ["claim_amount", "amount"],
            "court": ["court"],
            "case_name": ["case_name"],
            "sales": ["sales"],
            "operating_profit": ["operating_profit"],
            "net_income": ["net_income"],
            "period": ["period"],
            "yoy_change": ["yoy_change"],
        }

        facts: dict[str, Any] = {}
        flattened: list[dict[str, Any]] = []
        for items in compact_sources.values():
            flattened.extend(items)

        for slot in wanted_slots:
            search_keys = aliases.get(slot, [slot])
            for item in flattened:
                for key in search_keys:
                    value = item.get(key)
                    if value not in (None, "", [], {}):
                        facts[slot] = value
                        break
                if slot in facts:
                    break
        return facts

    def _compact_items(self, items: list[Any], field_names: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in items[: self.max_items_per_source]:
            if isinstance(item, str):
                text = TextUtil.clean_text(item, max_len=240)
                if text:
                    out.append({"text": text})
                continue
            if not isinstance(item, dict):
                continue

            compact: dict[str, Any] = {}
            for field_name in field_names:
                value = item.get(field_name)
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, (dict, list)):
                    compact[field_name] = value
                else:
                    compact[field_name] = TextUtil.clean_text(value, max_len=180)
            if compact:
                out.append(compact)
        return out

    @staticmethod
    def _evidence_counts(raw: dict[str, Any]) -> dict[str, int]:
        names = [
            "dart_evidence",
            "stock_event_evidence",
            "stock_event_context_evidence",
            "price_volume_evidence",
            "gdelt_evidence",
            "macro_evidence",
        ]
        return {name: len(DictUtil.as_list(raw.get(name))) for name in names}

    @staticmethod
    def _as_text_list(value: Any) -> list[str]:
        items = DictUtil.as_list(value)
        out: list[str] = []
        for item in items:
            if isinstance(item, str):
                out.append(TextUtil.clean_text(item, max_len=180))
            elif isinstance(item, dict):
                text = TextUtil.clean_text(
                    DictUtil.get_any(item, ["text", "claim", "description", "reason"]),
                    max_len=180,
                )
                if text:
                    out.append(text)
        return TextUtil.dedupe_keep_order(out)

    @staticmethod
    def _market_claim_permission(claim_level: str) -> dict[str, Any]:
        level = CLAIM_ORDER.get(claim_level, 0)
        return {
            "can_describe_company_action": level >= CLAIM_ORDER["no_market_claim"],
            "can_describe_price_or_volume_reaction": level >= CLAIM_ORDER["reaction_only"],
            "can_link_event_to_price_or_volume": level >= CLAIM_ORDER["likely_contributor"],
            "can_use_market_reaction_language": level >= CLAIM_ORDER["likely_contributor"],
            "required_tone": "plain_company_action_news",
        }


class PromptBuilder:
    def build_messages(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._user_prompt(payload)},
        ]

    @staticmethod
    def _system_prompt() -> str:
        return "\n".join([
            "You write short Korean financial wire-news items for an investment simulation.",
            "Write like a human financial news editor, not like an analyst report, disclosure summary, or AI assistant.",
            "Use concrete corporate actions built from structured action primitives.",
            "Do not explain why the item matters. Do not write interpretation paragraphs.",
            "Do not mention stock price, trading volume, investors, market reaction, sentiment, bullish/bearish framing, or causality unless explicitly allowed.",
            "For no_market_claim, write only company-action facts and never mention price, volume, investors, or market reaction.",
            "Do not use: 이벤트, 맥락, 의미, 시사, 해석, 주목, 확인할 필요, 투자자, 시장 참여자.",
            "Do not reuse, copy, or paraphrase any reference template, even if provided under audit_only_do_not_use.",
            "Return only one valid JSON object. No markdown. No explanations outside JSON.",
        ])

    @staticmethod
    def _user_prompt(payload: dict[str, Any]) -> str:
        instruction = {
            "task": "Write one short Korean stock-news sample from the provided structured bundle.",
            "core_instruction": [
                "Construct sentences from writing_frame.corporate_actor, actor_topic_particle_ko, plain_action_ko, object_ko, allowed_verbs_ko, and available_facts.",
                "Do not summarize the disclosure title mechanically.",
                "Do not copy or paraphrase writing_frame.audit_only_do_not_use.reference_lead_template_ko.",
                "Use only facts present in available_facts or source_evidence. If a fact is missing, omit it.",
            ],
            "output_schema": {
                "news_id": payload.get("target_news_id", ""),
                "headline": "짧은 한국어 제목. 콜론 금지. 과장 금지.",
                "detail_news": "정확히 2문장. 90~160자 권장.",
                "used_bundle_id": payload.get("bundle_id", ""),
                "claim_level": payload.get("claim_level", "no_market_claim"),
                "used_facts": ["실제로 사용한 근거 사실"],
                "style_self_check": {
                    "has_forbidden_market_claim": False,
                    "has_ai_style_phrase": False,
                    "has_disclosure_restatement_tone": False,
                    "used_reference_template": False,
                    "used_only_allowed_evidence": True,
                },
            },
            "hard_safety_rules": [
                "For no_market_claim, do not mention stock price, trading volume, investor reaction, market reaction, sentiment, 호재, 악재, 매수세, 매도세, or 투자심리.",
                "Do not imply causality through adjacent sentences.",
                "Do not use because/therefore/impact wording such as 때문에, 영향으로, 이에 따라, 힘입어, 부담으로 작용.",
                "Do not add forecasts, advice, or reader guidance.",
                "Do not invent amount, counterparty, period, purpose, share count, or payment date.",
            ],
            "style_rules_ko": [
                "제목은 12~32자 정도로 짧게 쓴다.",
                "본문은 정확히 2문장으로 쓴다.",
                "첫 문장은 회사명 또는 행위 주체로 시작한다.",
                "첫 문장은 allowed_verbs_ko 중 자연스러운 동사를 우선 사용한다.",
                "가능하면 결정했다, 체결했다, 취득하기로 했다, 처분하기로 했다, 발행하기로 했다, 지급하기로 했다, 보고했다, 제출했다, 제시했다, 기록했다, 집계됐다를 사용한다.",
                "공시했다, 밝혔다, 관련 내용을 공시했다, 계획을 공시했다를 반복하지 않는다.",
                "이번 공시는, 이번 이벤트는, 해당 사안은, 향후 확인할 필요가 있다로 문장을 시작하거나 끝내지 않는다.",
                "분석 보고서식 결론 문장을 쓰지 않는다.",
            ],
            "blocked_phrases": {
                "ai_report_phrases": AI_REPORT_PHRASES_KO,
                "market_causal_phrases": MARKET_CAUSAL_PHRASES_KO,
                "disclosure_restatement_phrases": DISCLOSURE_RESTATEMENT_PHRASES_KO,
                "banned_sentence_openers": BANNED_SENTENCE_OPENERS_KO,
            },
            "bundle": payload,
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)


class RequestBuilder:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config
        self.compactor = EvidenceCompactor(config.max_evidence_items_per_source)
        self.prompt_builder = PromptBuilder()

    def build_request(self, record: BundleRecord, index: int) -> dict[str, Any]:
        payload = self.compactor.compact(record)
        news_id = f"STOCK_SAMPLE_{index:06d}"
        payload["target_news_id"] = news_id
        messages = self.prompt_builder.build_messages(payload)
        return {
            "custom_id": f"stock_news_sample_{record.bundle_id}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "response_format": {"type": "json_object"},
            },
        }

    def build_prompt_preview(self, record: BundleRecord) -> str:
        payload = self.compactor.compact(record)
        payload["target_news_id"] = "STOCK_SAMPLE_000001"
        messages = self.prompt_builder.build_messages(payload)
        chunks: list[str] = []
        for msg in messages:
            chunks.append(f"[{msg['role'].upper()}]\n{msg['content']}")
        return "\n\n".join(chunks)


# =============================================================================
# CSV / report writing
# =============================================================================


class CandidateCsvWriter:
    FIELDS = [
        "sample_no",
        "selected",
        "bundle_id",
        "event_group_id",
        "stock_code",
        "stock_name",
        "anchor_date",
        "event_family",
        "candidate_topic",
        "primary_topic_source",
        "claim_level",
        "bundle_candidate_rank",
        "judge_input_allowed",
        "frame_type",
        "lead_focus",
        "action_type",
        "plain_action_ko",
        "corporate_actor",
        "actor_topic_particle_ko",
        "object_ko",
        "allowed_verbs_ko",
        "avoid_verbs_ko",
        "usable_fact_slots",
        "writing_quality_risk",
        "sample_priority_score",
        "sample_exclusion_reason",
        "has_generic_action",
        "has_reference_template_only",
        "dart_count",
        "stock_event_count",
        "stock_event_context_count",
        "price_volume_count",
        "gdelt_count",
        "macro_count",
    ]

    @classmethod
    def write(cls, path: Path, records: list[BundleRecord], *, selected_ids: Optional[set[str]] = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        selected_ids = selected_ids or {r.bundle_id for r in records}
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cls.FIELDS)
            writer.writeheader()
            for idx, record in enumerate(records, start=1):
                writer.writerow(cls._row(record, idx, record.bundle_id in selected_ids))

    @classmethod
    def _row(cls, record: BundleRecord, sample_no: int, selected: bool) -> dict[str, Any]:
        raw = record.raw
        frame = record.writing_frame
        return {
            "sample_no": sample_no if selected else "",
            "selected": selected,
            "bundle_id": record.bundle_id,
            "event_group_id": record.event_group_id,
            "stock_code": record.stock_code,
            "stock_name": record.stock_name,
            "anchor_date": record.anchor_date,
            "event_family": record.event_family,
            "candidate_topic": record.candidate_topic,
            "primary_topic_source": record.primary_topic_source,
            "claim_level": record.claim_level,
            "bundle_candidate_rank": record.bundle_candidate_rank,
            "judge_input_allowed": record.judge_input_allowed,
            "frame_type": frame.get("frame_type", ""),
            "lead_focus": frame.get("lead_focus", ""),
            "action_type": frame.get("action_type", ""),
            "plain_action_ko": frame.get("plain_action_ko", ""),
            "corporate_actor": frame.get("corporate_actor", ""),
            "actor_topic_particle_ko": frame.get("actor_topic_particle_ko", ""),
            "object_ko": frame.get("object_ko", ""),
            "allowed_verbs_ko": json.dumps(DictUtil.as_list(frame.get("allowed_verbs_ko")), ensure_ascii=False),
            "avoid_verbs_ko": json.dumps(DictUtil.as_list(frame.get("avoid_verbs_ko")), ensure_ascii=False),
            "usable_fact_slots": json.dumps(DictUtil.as_list(frame.get("usable_fact_slots")), ensure_ascii=False),
            "writing_quality_risk": frame.get("writing_quality_risk", ""),
            "sample_priority_score": round(record.sample_priority_score, 3),
            "sample_exclusion_reason": record.sample_exclusion_reason,
            "has_generic_action": record.has_generic_action,
            "has_reference_template_only": record.has_reference_template_only,
            "dart_count": len(DictUtil.as_list(raw.get("dart_evidence"))),
            "stock_event_count": len(DictUtil.as_list(raw.get("stock_event_evidence"))),
            "stock_event_context_count": len(DictUtil.as_list(raw.get("stock_event_context_evidence"))),
            "price_volume_count": len(DictUtil.as_list(raw.get("price_volume_evidence"))),
            "gdelt_count": len(DictUtil.as_list(raw.get("gdelt_evidence"))),
            "macro_count": len(DictUtil.as_list(raw.get("macro_evidence"))),
        }


class ReportWriter:
    @staticmethod
    def write(path: Path, all_records: list[BundleRecord], selected: list[BundleRecord], config: Pr06aConfig) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        selected_family_counts = Counter(r.event_family for r in selected)
        available_family_counts = Counter(r.event_family for r in all_records)
        selected_action_counts = Counter(r.writing_frame.get("action_type", "unknown") for r in selected)
        exclusion_counts = Counter(r.sample_exclusion_reason or "eligible_for_sampling" for r in all_records)
        claim_counts = Counter(r.claim_level for r in selected)

        lines: list[str] = []
        lines.append("# pr06a Stock News Sample Request Report")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- input_bundles_jsonl: `{config.bundles_jsonl}`")
        lines.append(f"- output_dir: `{config.output_dir}`")
        lines.append(f"- model: `{config.model}`")
        lines.append(f"- eligible_bundles_after_filter: {len(all_records):,}")
        lines.append(f"- selected_sample_requests: {len(selected):,}")
        lines.append(f"- max_total_requests: {config.max_total_requests:,}")
        lines.append(f"- max_per_stock: {config.max_per_stock:,}")
        lines.append(f"- allowed_claim_levels: {sorted(config.allowed_claim_levels)}")
        lines.append(f"- exclude_generic_disclosure_titles: {config.exclude_generic_disclosure_titles}")
        lines.append(f"- max_generic_action_samples: {config.max_generic_action_samples}")
        lines.append("")

        ReportWriter._append_counter(lines, "Selected Families", selected_family_counts)
        ReportWriter._append_counter(lines, "Selected Action Types", selected_action_counts)
        ReportWriter._append_counter(lines, "Available Families After Filter", available_family_counts)
        ReportWriter._append_counter(lines, "Sample Exclusion Reasons in Pool", exclusion_counts)
        ReportWriter._append_counter(lines, "Selected Claim Levels", claim_counts)

        lines.append("## Style/Safety Purpose")
        lines.append("")
        lines.append("- This stage checks whether no_market_claim bundles can be written as plain company-action news.")
        lines.append("- The prompt is built from action primitives, not from reusable lead templates.")
        lines.append("- It blocks market reaction, investor reaction, bullish/bearish interpretation, and stock-price causality.")
        lines.append("- It explicitly blocks AI/report wording such as `이벤트`, `맥락`, `의미`, `해석`, `주목`, and `확인할 필요`.")
        lines.append("- `reference_lead_template_ko` is audit-only and must not be copied or paraphrased.")
        lines.append("")

        lines.append("## Output Files")
        lines.append("")
        lines.append("- `stock_news_sample_requests.jsonl`")
        lines.append("- `stock_news_sample_candidates.csv`")
        lines.append("- `stock_news_sample_candidate_pool.csv`")
        lines.append("- `stock_news_sample_report.md`")
        lines.append("- `prompt_preview.txt`")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _append_counter(lines: list[str], title: str, counter: Counter) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not counter:
            lines.append("- none: 0")
        else:
            for key, count in counter.most_common():
                lines.append(f"- {key}: {count:,}")
        lines.append("")


# =============================================================================
# Pipeline
# =============================================================================


class Pr06aPipeline:
    def __init__(self, config: Pr06aConfig) -> None:
        self.config = config
        self.output_dir = config.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        loader = BundleLoader(self.config)
        selector = SampleSelector(self.config)
        request_builder = RequestBuilder(self.config)

        eligible_records = loader.load()
        selected = selector.select(eligible_records)
        selected_ids = {r.bundle_id for r in selected}
        requests = [request_builder.build_request(record, i) for i, record in enumerate(selected, start=1)]

        requests_path = self.output_dir / "stock_news_sample_requests.jsonl"
        candidates_path = self.output_dir / "stock_news_sample_candidates.csv"
        pool_path = self.output_dir / "stock_news_sample_candidate_pool.csv"
        report_path = self.output_dir / "stock_news_sample_report.md"
        preview_path = self.output_dir / "prompt_preview.txt"

        request_count = JsonlIO.write_jsonl(requests_path, requests)
        CandidateCsvWriter.write(candidates_path, selected, selected_ids=selected_ids)
        CandidateCsvWriter.write(pool_path, eligible_records, selected_ids=selected_ids)
        ReportWriter.write(report_path, eligible_records, selected, self.config)

        if selected:
            preview_path.write_text(request_builder.build_prompt_preview(selected[0]), encoding="utf-8")
        else:
            preview_path.write_text("No selected records.\n", encoding="utf-8")

        self._print_summary(eligible_records, selected, request_count)

    def _print_summary(self, eligible_records: list[BundleRecord], selected: list[BundleRecord], request_count: int) -> None:
        print("=" * 100)
        print("[pr06a] Build stock news sample generation requests")
        print(f"bundles_jsonl: {self.config.bundles_jsonl}")
        print(f"output_dir: {self.output_dir}")
        print(f"model: {self.config.model}")
        print("=" * 100)
        print(f"[load] eligible bundles: {len(eligible_records):,}")
        print(f"[sample] selected bundles: {len(selected):,}")
        print(f"[requests] jsonl rows: {request_count:,}")
        print("")
        print("[selected event families]")
        for family, count in Counter(r.event_family for r in selected).most_common():
            print(f"  {family}: {count}")
        print("")
        print("[selected action types]")
        for action, count in Counter(r.writing_frame.get("action_type", "unknown") for r in selected).most_common(30):
            print(f"  {action}: {count}")
        print("")
        print("[sample exclusion reasons in eligible pool]")
        for reason, count in Counter(r.sample_exclusion_reason or "eligible_for_sampling" for r in eligible_records).most_common(20):
            print(f"  {reason}: {count}")
        print("")
        print("[claim levels]")
        for claim, count in Counter(r.claim_level for r in selected).most_common():
            print(f"  {claim}: {count}")
        print("")
        print("[outputs]")
        for name in [
            "stock_news_sample_requests.jsonl",
            "stock_news_sample_candidates.csv",
            "stock_news_sample_candidate_pool.csv",
            "stock_news_sample_report.md",
            "prompt_preview.txt",
        ]:
            print(f"  {self.output_dir / name}")


# =============================================================================
# CLI
# =============================================================================


def parse_family_quotas_json(value: str | None) -> dict[str, int]:
    if not value:
        return dict(DEFAULT_FAMILY_QUOTAS)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"Invalid JSON for family quotas: {e}") from e
    if not isinstance(decoded, dict):
        raise argparse.ArgumentTypeError("family quotas must be a JSON object")
    out: dict[str, int] = {}
    for key, raw_value in decoded.items():
        try:
            out[str(key)] = int(raw_value)
        except (TypeError, ValueError) as e:
            raise argparse.ArgumentTypeError(f"Invalid quota for {key}: {raw_value}") from e
    return out


def parse_claim_levels(value: str | None) -> set[str]:
    if not value:
        return {"no_market_claim"}
    levels = {x.strip() for x in value.split(",") if x.strip()}
    if not levels:
        return {"no_market_claim"}
    unknown = levels - set(CLAIM_ORDER.keys())
    if unknown:
        raise argparse.ArgumentTypeError(f"Unknown claim levels: {sorted(unknown)}")
    return levels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build sample stock-news generation request JSONL from pr05e evidence bundles."
    )
    parser.add_argument("--bundles-jsonl", default=str(DEFAULT_BUNDLES_JSONL))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--max-total-requests", type=int, default=50)
    parser.add_argument("--max-per-stock", type=int, default=3)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--family-quotas-json", default=None)
    parser.add_argument("--allowed-claim-levels", default="no_market_claim")
    parser.add_argument("--exclude-judge-blocked", action="store_true")
    parser.add_argument("--min-candidate-topic-chars", type=int, default=2)
    parser.add_argument("--max-evidence-items-per-source", type=int, default=2)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=520)
    parser.add_argument(
        "--include-generic-disclosure-titles",
        action="store_true",
        help="Allow generic/low-quality writing frames into the sample pool. Default excludes them.",
    )
    parser.add_argument(
        "--max-generic-action-samples",
        type=int,
        default=0,
        help="Maximum generic/low-quality frames allowed as fallback samples.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Pr06aConfig:
    return Pr06aConfig(
        bundles_jsonl=Path(args.bundles_jsonl),
        output_dir=Path(args.output_dir),
        model=args.model,
        max_total_requests=args.max_total_requests,
        max_per_stock=args.max_per_stock,
        random_seed=args.random_seed,
        family_quotas=parse_family_quotas_json(args.family_quotas_json),
        allowed_claim_levels=parse_claim_levels(args.allowed_claim_levels),
        include_judge_blocked=not args.exclude_judge_blocked,
        min_candidate_topic_chars=args.min_candidate_topic_chars,
        max_evidence_items_per_source=args.max_evidence_items_per_source,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        exclude_generic_disclosure_titles=not args.include_generic_disclosure_titles,
        max_generic_action_samples=args.max_generic_action_samples,
    )


def main() -> None:
    args = parse_args()
    config = build_config(args)
    Pr06aPipeline(config).run()


if __name__ == "__main__":
    main()
