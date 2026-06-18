from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class StockEventContextConfig:
    input_csv_path: str
    output_dir: str
    source_type: str = "stock_event_calendar"
    encoding: str = "utf-8-sig"

    # 현재 stock_event_calendar에는 source_provenance / subtype / structured anchor가 약하므로,
    # market causal claim은 기본 금지한다.
    allow_earnings_as_news_trigger: bool = True
    allow_earnings_as_market_cause: bool = False

    # industry/macro-policy는 개별 종목 뉴스 trigger로 쓰지 않는다.
    allow_industry_as_news_trigger: bool = False
    allow_industry_as_market_cause: bool = False

    # 직접 종목명 언급 + 실적 anchor가 있어야 curated earnings event로 인정한다.
    require_direct_stock_mention_for_earnings_trigger: bool = True
    require_earnings_anchor_for_earnings_trigger: bool = True


@dataclass
class AnnotatedStockEvent:
    evidence_id: str
    source_type: str

    event_date: str
    stock_code: str
    stock_name: str
    event_type: str
    title: str
    description: str
    sector: str
    region: str
    related_stocks: str

    original_direction: str
    original_severity: str

    stock_event_class: str
    stock_specificity: str
    has_direct_stock_mention: bool
    has_earnings_anchor: bool
    earnings_anchor_terms: List[str]

    source_provenance: str
    source_provenance_status: str

    structured_match_fields: Dict[str, Any]
    structured_match_status: str

    subtype: str
    subtype_status: str

    evidence_role: str
    allowed_llm_usage_ceiling: str

    # 새로 분리된 권한
    can_be_news_trigger_ceiling: bool
    can_be_market_cause_ceiling: bool

    # 기존 컬럼 호환용. market cause와 동일하게 둔다.
    can_be_main_cause_ceiling: bool

    trigger_eligible_ceiling: bool
    do_not_use_as_cause: bool
    reaction_not_cause: bool

    directionality_default: str
    directionality_source: str
    directionality_required: bool
    directionality_inherited_from_dart: bool

    materiality_basis: str

    dart_mapping_class: Optional[str]
    dart_mapping_family: Optional[str]

    cluster_role_eligibility: str
    fallback_floor_contribution: str

    allowed_claim_scope: List[str]
    forbidden_claim_scope: List[str]
    validator_flags: List[str]

    confidence_tier: str

    raw_row: Dict[str, Any]

    def to_flat_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("raw_row", None)

        json_cols = [
            "earnings_anchor_terms",
            "structured_match_fields",
            "allowed_claim_scope",
            "forbidden_claim_scope",
            "validator_flags",
        ]

        for col in json_cols:
            d[col] = json.dumps(d[col], ensure_ascii=False)

        return d

    def to_jsonl_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "event_date": self.event_date,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "event_type": self.event_type,
            "stock_event_class": self.stock_event_class,
            "text": {
                "title": self.title,
                "description": self.description,
                "sector": self.sector,
                "region": self.region,
                "related_stocks": self.related_stocks,
                "original_direction": self.original_direction,
                "original_severity": self.original_severity,
            },
            "permission": {
                "evidence_role": self.evidence_role,
                "allowed_llm_usage_ceiling": self.allowed_llm_usage_ceiling,
                "can_be_news_trigger_ceiling": self.can_be_news_trigger_ceiling,
                "can_be_market_cause_ceiling": self.can_be_market_cause_ceiling,
                "can_be_main_cause_ceiling": self.can_be_main_cause_ceiling,
                "trigger_eligible_ceiling": self.trigger_eligible_ceiling,
                "do_not_use_as_cause": self.do_not_use_as_cause,
                "reaction_not_cause": self.reaction_not_cause,
                "cluster_role_eligibility": self.cluster_role_eligibility,
                "fallback_floor_contribution": self.fallback_floor_contribution,
            },
            "directionality": {
                "default": self.directionality_default,
                "source": self.directionality_source,
                "required": self.directionality_required,
                "inherited_from_dart": self.directionality_inherited_from_dart,
            },
            "mapping": {
                "dart_mapping_class": self.dart_mapping_class,
                "dart_mapping_family": self.dart_mapping_family,
                "subtype": self.subtype,
                "subtype_status": self.subtype_status,
            },
            "quality": {
                "stock_specificity": self.stock_specificity,
                "has_direct_stock_mention": self.has_direct_stock_mention,
                "has_earnings_anchor": self.has_earnings_anchor,
                "earnings_anchor_terms": self.earnings_anchor_terms,
                "source_provenance": self.source_provenance,
                "source_provenance_status": self.source_provenance_status,
                "structured_match_fields": self.structured_match_fields,
                "structured_match_status": self.structured_match_status,
                "confidence_tier": self.confidence_tier,
                "validator_flags": self.validator_flags,
            },
            "claim_contract": {
                "allowed_claim_scope": self.allowed_claim_scope,
                "forbidden_claim_scope": self.forbidden_claim_scope,
            },
        }


class TextNormalizer:
    @staticmethod
    def clean(value: Any) -> str:
        if pd.isna(value):
            return ""

        text = str(value).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def compact(value: Any) -> str:
        text = TextNormalizer.clean(value)
        text = text.lower()
        text = re.sub(r"\s+", "", text)
        text = text.replace("ㆍ", "")
        text = text.replace("·", "")
        text = text.replace("-", "")
        text = text.replace("_", "")
        text = text.replace("/", "")
        return text


class StockCodeNormalizer:
    @staticmethod
    def normalize(value: Any) -> str:
        if pd.isna(value):
            return ""

        text = str(value).strip()

        if text.endswith(".0"):
            text = text[:-2]

        if text.isdigit():
            return text.zfill(6)

        return text


class StockNameMatcher:
    MANUAL_ALIASES: Dict[str, List[str]] = {
        "기아": ["기아", "기아차", "kia"],
        "현대차": ["현대차", "현대자동차", "hyundai"],
        "SK하이닉스": ["SK하이닉스", "하이닉스", "sk하이닉스"],
        "삼성전자": ["삼성전자"],
        "삼성전기": ["삼성전기"],
        "S-Oil": ["S-Oil", "에쓰오일", "에스오일", "soil"],
        "HMM": ["HMM", "현대상선"],
        "대한항공": ["대한항공"],
        "LG화학": ["LG화학"],
        "LG에너지솔루션": ["LG에너지솔루션", "LG엔솔"],
    }

    def build_aliases(self, stock_name: str) -> List[str]:
        name = TextNormalizer.clean(stock_name)
        aliases = {name}

        if name in self.MANUAL_ALIASES:
            aliases.update(self.MANUAL_ALIASES[name])

        return sorted({a for a in aliases if a})

    def has_direct_mention(self, stock_name: str, title: str, description: str) -> bool:
        joined = TextNormalizer.compact(f"{title} {description}")

        for alias in self.build_aliases(stock_name):
            alias_norm = TextNormalizer.compact(alias)

            if alias_norm and alias_norm in joined:
                return True

        return False


class EarningsAnchorDetector:
    """
    stock_event_calendar의 earnings 행이 실제 실적 뉴스 주제로 쓸 수 있는지 판단한다.
    단, 이 anchor는 market causal claim을 허용하지 않는다.
    """

    EARNINGS_KEYWORDS = [
        "실적",
        "영업이익",
        "영업익",
        "매출",
        "순이익",
        "당기순이익",
        "손익",
        "흑자",
        "적자",
        "어닝",
        "컨센서스",
        "예상 상회",
        "예상 하회",
        "사상 최대",
        "최대",
        "급감",
        "급증",
        "부진",
        "호조",
        "개선",
        "악화",
        "쇼크",
        "서프라이즈",
    ]

    QUARTER_PATTERN = re.compile(r"(\d)\s*분기|([1-4])Q", re.IGNORECASE)
    PROFIT_PATTERN = re.compile(r"(영업이익|영업익|순이익|매출)[^\d]{0,10}\d")
    MONEY_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:조|억|만)?\s*원")
    PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%")

    def detect(self, title: str, description: str) -> Tuple[bool, List[str]]:
        text = f"{TextNormalizer.clean(title)} {TextNormalizer.clean(description)}"
        terms: List[str] = []

        for keyword in self.EARNINGS_KEYWORDS:
            if keyword in text:
                terms.append(keyword)

        if self.QUARTER_PATTERN.search(text):
            terms.append("quarter_mention")

        if self.PROFIT_PATTERN.search(text):
            terms.append("profit_number_pattern")

        if self.MONEY_PATTERN.search(text):
            terms.append("money_value")

        if self.PERCENT_PATTERN.search(text):
            terms.append("percent_value")

        terms = sorted(set(terms))

        return len(terms) > 0, terms


class StructuredFieldExtractor:
    QUARTER_PATTERN = re.compile(r"(\d)\s*분기|([1-4])Q", re.IGNORECASE)
    PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%")
    MONEY_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:조|억|만)?\s*원")
    NUMBER_WITH_UNIT_PATTERN = re.compile(
        r"\d+(?:\.\d+)?\s*(?:조원|억원|만원|%|배|포인트|p|달러|원)"
    )

    KEYWORD_CANDIDATES = [
        "D램", "DRAM", "NAND", "낸드", "반도체",
        "스마트폰", "전기차", "배터리", "정제마진",
        "컨테이너", "운임", "유가", "납사", "석유화학",
        "자동차", "신차", "철강", "조선", "바이오",
        "임상", "승인", "수주", "계약", "영업이익",
        "매출", "순이익", "시장점유율",
    ]

    def extract(self, row: pd.Series) -> Tuple[Dict[str, Any], str]:
        title = TextNormalizer.clean(row.get("title"))
        description = TextNormalizer.clean(row.get("description"))
        text = f"{title} {description}"

        quarters = []

        for match in self.QUARTER_PATTERN.finditer(text):
            quarter = match.group(1) or match.group(2)
            quarters.append(f"Q{quarter}")

        percentages = self.PERCENT_PATTERN.findall(text)
        money_values = self.MONEY_PATTERN.findall(text)
        numeric_terms = self.NUMBER_WITH_UNIT_PATTERN.findall(text)
        keywords = [
            keyword
            for keyword in self.KEYWORD_CANDIDATES
            if keyword.lower() in text.lower()
        ]

        fields = {
            "event_type": TextNormalizer.clean(row.get("event_type")),
            "sector": TextNormalizer.clean(row.get("sector")),
            "region": TextNormalizer.clean(row.get("region")),
            "quarter_mentions": sorted(set(quarters)),
            "percentage_mentions": sorted(set(percentages)),
            "money_mentions": sorted(set(money_values)),
            "numeric_terms": sorted(set(numeric_terms)),
            "keyword_anchors": sorted(set(keywords)),
        }

        has_weak_anchor = bool(quarters or percentages or money_values or keywords)
        status = "WEAK_TEXT_DERIVED" if has_weak_anchor else "MISSING"

        return fields, status


class StockEventClassifier:
    MACRO_POLICY_KEYWORDS = [
        "금리", "환율", "원화", "달러", "유가", "국제유가",
        "납사", "운임", "관세", "정책", "규제", "보조금",
        "수출", "수입", "물가", "경기", "중국", "미국",
        "정부", "정제마진",
    ]

    def classify(
        self,
        row: pd.Series,
        has_direct_stock_mention: bool,
        has_earnings_anchor: bool,
    ) -> str:
        event_type = TextNormalizer.clean(row.get("event_type")).lower()
        title = TextNormalizer.clean(row.get("title"))
        description = TextNormalizer.clean(row.get("description"))
        text = f"{title} {description}"

        if event_type == "earnings":
            if has_direct_stock_mention and has_earnings_anchor:
                return "stock_curated_earnings_event"

            return "stock_ambiguous_earnings_context"

        if event_type == "industry":
            if any(keyword in text for keyword in self.MACRO_POLICY_KEYWORDS):
                return "stock_macro_policy_exposure_context"

            return "stock_sector_theme_context"

        return "stock_ambiguous_mapped_context"


class DirectionalityResolver:
    POSITIVE_TERMS = ["positive", "up", "increase", "호조", "개선", "상회", "급증", "최대"]
    NEGATIVE_TERMS = ["negative", "down", "decrease", "부진", "악화", "하회", "급감", "쇼크"]

    def resolve(self, direction: str, title: str, description: str) -> Tuple[str, str]:
        direction_clean = TextNormalizer.clean(direction).lower()
        text = f"{title} {description}"

        if direction_clean in {"positive", "negative", "neutral", "mixed"}:
            return direction_clean, "calendar_direction_field"

        if any(term in text for term in self.POSITIVE_TERMS):
            return "positive", "title_description_heuristic"

        if any(term in text for term in self.NEGATIVE_TERMS):
            return "negative", "title_description_heuristic"

        return "ambiguous", "unresolved"


class PermissionPolicy:
    def __init__(self, config: StockEventContextConfig):
        self.config = config
        self.directionality_resolver = DirectionalityResolver()

    def annotate_permission(
        self,
        row: pd.Series,
        stock_event_class: str,
        stock_specificity: str,
        has_direct_stock_mention: bool,
        has_earnings_anchor: bool,
        earnings_anchor_terms: List[str],
        structured_match_status: str,
    ) -> Dict[str, Any]:
        title = TextNormalizer.clean(row.get("title"))
        description = TextNormalizer.clean(row.get("description"))
        direction = TextNormalizer.clean(row.get("direction"))
        severity = TextNormalizer.clean(row.get("severity")).lower()
        match_status = TextNormalizer.clean(row.get("_match_status"))

        directionality_default, directionality_source = self.directionality_resolver.resolve(
            direction=direction,
            title=title,
            description=description,
        )

        validator_flags: List[str] = []
        allowed_claim_scope: List[str] = []
        forbidden_claim_scope: List[str] = []

        source_provenance = "dataset_level_unknown"
        source_provenance_status = "MISSING"
        validator_flags.append("MISSING_SOURCE_PROVENANCE")

        if structured_match_status != "PASS_DIRECT":
            validator_flags.append("WEAK_STRUCTURED_MATCH_FIELDS")

        if match_status == "matched_multi_exploded":
            validator_flags.append("MULTI_STOCK_EXPLODED_ROW")

        if not has_direct_stock_mention:
            validator_flags.append("STOCK_NAME_NOT_DIRECTLY_MENTIONED")

        if not has_earnings_anchor and stock_event_class in {
            "stock_curated_earnings_event",
            "stock_ambiguous_earnings_context",
        }:
            validator_flags.append("EARNINGS_ANCHOR_MISSING")

        base = {
            "source_provenance": source_provenance,
            "source_provenance_status": source_provenance_status,
            "evidence_role": "background_context",
            "allowed_llm_usage_ceiling": "background_context",
            "can_be_news_trigger_ceiling": False,
            "can_be_market_cause_ceiling": False,
            "can_be_main_cause_ceiling": False,
            "trigger_eligible_ceiling": False,
            "do_not_use_as_cause": False,
            "reaction_not_cause": False,
            "directionality_default": directionality_default,
            "directionality_source": directionality_source,
            "directionality_required": False,
            "directionality_inherited_from_dart": False,
            "materiality_basis": "not_available_in_stock_event_calendar",
            "dart_mapping_class": None,
            "dart_mapping_family": None,
            "cluster_role_eligibility": "background_only",
            "fallback_floor_contribution": "none",
            "subtype": "not_available",
            "subtype_status": "MISSING_OR_COARSE",
            "allowed_claim_scope": allowed_claim_scope,
            "forbidden_claim_scope": forbidden_claim_scope,
            "validator_flags": validator_flags,
            "confidence_tier": self._confidence_tier(
                severity=severity,
                stock_specificity=stock_specificity,
                source_provenance_status=source_provenance_status,
                stock_event_class=stock_event_class,
            ),
        }

        if stock_event_class == "stock_curated_earnings_event":
            base.update(
                {
                    "evidence_role": "primary_candidate_for_news_topic",
                    "allowed_llm_usage_ceiling": "earnings_event_trigger",
                    "can_be_news_trigger_ceiling": True,
                    "can_be_market_cause_ceiling": False,
                    "can_be_main_cause_ceiling": False,
                    "trigger_eligible_ceiling": True,
                    "directionality_required": True,
                    "materiality_basis": "earnings_text_anchor_only_without_dart_numbers",
                    "dart_mapping_class": "true_earnings_disclosure",
                    "dart_mapping_family": "earnings_family",
                    "directionality_inherited_from_dart": False,
                    "cluster_role_eligibility": "news_trigger_unless_duplicate_dart_exists",
                    "fallback_floor_contribution": "weak_floor_only_with_companion_signal",
                    "subtype": "curated_earnings_event",
                    "subtype_status": "DERIVED_FROM_EVENT_TYPE_AND_TEXT_ANCHOR",
                }
            )

            base["validator_flags"].append("MARKET_CAUSAL_CLAIM_FORBIDDEN_WITHOUT_PRICE_REACTION")
            base["validator_flags"].append("EARNINGS_NUMERIC_CLAIM_REQUIRES_DART_OR_FIN_NUMBERS")

            base["allowed_claim_scope"] = [
                "May use this row as the topic trigger of a company-specific earnings news item.",
                "May describe earnings attention, earnings pressure, earnings surprise, or earnings-related investor focus if supported by title/description wording.",
                "May state the qualitative earnings direction only when the wording directly supports it.",
                "May mention that the event drew market attention, but only in a hedged form.",
            ]

            base["forbidden_claim_scope"] = [
                "Do not claim that the earnings event caused a stock price rise or fall without separate price-volume reaction evidence.",
                "Do not claim exact revenue, operating profit, or net income numbers unless they appear in the text or are supported by DART/financial data.",
                "Do not treat this row as an official DART filing.",
                "Do not override a matched DART earnings disclosure. If DART duplicate exists, DART becomes primary and this row becomes supporting.",
            ]

            return base

        if stock_event_class == "stock_ambiguous_earnings_context":
            base.update(
                {
                    "evidence_role": "background_context",
                    "allowed_llm_usage_ceiling": "earnings_background_context",
                    "can_be_news_trigger_ceiling": False,
                    "can_be_market_cause_ceiling": False,
                    "can_be_main_cause_ceiling": False,
                    "trigger_eligible_ceiling": False,
                    "materiality_basis": "ambiguous_stock_mapping_or_missing_earnings_anchor",
                    "dart_mapping_class": "true_earnings_disclosure",
                    "dart_mapping_family": "earnings_family",
                    "cluster_role_eligibility": "background_only",
                    "fallback_floor_contribution": "none",
                    "subtype": "ambiguous_earnings_context",
                    "subtype_status": "DERIVED_FROM_EVENT_TYPE_BUT_WEAK_STOCK_SPECIFICITY",
                }
            )

            base["validator_flags"].append("AMBIGUOUS_EARNINGS_CONTEXT_PRIMARY_FORBIDDEN")

            base["allowed_claim_scope"] = [
                "May be used only as broad earnings-season or peer/sector earnings background.",
                "May not be framed as a confirmed company-specific earnings event.",
            ]

            base["forbidden_claim_scope"] = [
                "Do not use as the main topic trigger.",
                "Do not claim the named stock reported the earnings result unless the stock is directly mentioned.",
                "Do not use as market cause.",
                "Do not use for fallback generation.",
            ]

            return base

        if stock_event_class == "stock_sector_theme_context":
            base.update(
                {
                    "evidence_role": "background_context",
                    "allowed_llm_usage_ceiling": "sector_context",
                    "can_be_news_trigger_ceiling": False,
                    "can_be_market_cause_ceiling": False,
                    "can_be_main_cause_ceiling": False,
                    "trigger_eligible_ceiling": False,
                    "materiality_basis": "sector_theme_only",
                    "cluster_role_eligibility": "background_or_supporting_only",
                    "fallback_floor_contribution": "none",
                    "subtype": "sector_theme",
                    "subtype_status": "COARSE_DIRECT_FROM_EVENT_TYPE",
                }
            )

            base["validator_flags"].append("INDUSTRY_EVENT_PRIMARY_FORBIDDEN")

            base["allowed_claim_scope"] = [
                "May describe broad sector context.",
                "May say the stock is related to a sector theme only as background.",
            ]

            base["forbidden_claim_scope"] = [
                "Do not use as company-specific news trigger.",
                "Do not claim sector context caused stock price movement.",
                "Do not imply confirmed firm-level earnings impact from sector context alone.",
            ]

            return base

        if stock_event_class == "stock_macro_policy_exposure_context":
            base.update(
                {
                    "evidence_role": "background_context",
                    "allowed_llm_usage_ceiling": "macro_policy_exposure_context",
                    "can_be_news_trigger_ceiling": False,
                    "can_be_market_cause_ceiling": False,
                    "can_be_main_cause_ceiling": False,
                    "trigger_eligible_ceiling": False,
                    "materiality_basis": "macro_or_policy_exposure_only",
                    "cluster_role_eligibility": "background_only",
                    "fallback_floor_contribution": "none",
                    "subtype": "macro_policy_exposure",
                    "subtype_status": "COARSE_DIRECT_FROM_EVENT_TYPE_AND_KEYWORDS",
                }
            )

            base["validator_flags"].append("MACRO_POLICY_EVENT_PRIMARY_FORBIDDEN")

            base["allowed_claim_scope"] = [
                "May describe macro, policy, FX, oil, rate, or trade exposure context.",
                "May connect the stock to macro sensitivity only as background.",
            ]

            base["forbidden_claim_scope"] = [
                "Do not use as company-specific news trigger.",
                "Do not claim macro or policy exposure directly caused stock movement.",
                "Do not claim confirmed company-specific operational impact unless supported by separate evidence.",
            ]

            return base

        base.update(
            {
                "evidence_role": "background_context",
                "allowed_llm_usage_ceiling": "background_only_or_exclude",
                "can_be_news_trigger_ceiling": False,
                "can_be_market_cause_ceiling": False,
                "can_be_main_cause_ceiling": False,
                "trigger_eligible_ceiling": False,
                "cluster_role_eligibility": "background_only",
                "fallback_floor_contribution": "none",
                "subtype": "ambiguous",
                "subtype_status": "MISSING",
            }
        )

        base["validator_flags"].append("AMBIGUOUS_STOCK_EVENT_CLASS")

        base["allowed_claim_scope"] = [
            "May be retained only for audit or weak background context.",
        ]

        base["forbidden_claim_scope"] = [
            "Do not use as news trigger.",
            "Do not use as market cause.",
            "Do not use for fallback generation.",
        ]

        return base

    @staticmethod
    def _confidence_tier(
        severity: str,
        stock_specificity: str,
        source_provenance_status: str,
        stock_event_class: str,
    ) -> str:
        if stock_event_class == "stock_curated_earnings_event":
            if stock_specificity == "direct" and severity == "high":
                return "medium_curated_but_unofficial"
            return "low_to_medium_curated_but_unofficial"

        if source_provenance_status == "MISSING":
            return "low_unanchored"

        return "low"


class StockEventContextAnnotator:
    def __init__(self, config: StockEventContextConfig):
        self.config = config
        self.matcher = StockNameMatcher()
        self.earnings_detector = EarningsAnchorDetector()
        self.structured_extractor = StructuredFieldExtractor()
        self.classifier = StockEventClassifier()
        self.policy = PermissionPolicy(config)

    def annotate_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        events: List[AnnotatedStockEvent] = []

        for idx, row in df.iterrows():
            events.append(self.annotate_row(row, idx))

        return pd.DataFrame([event.to_flat_dict() for event in events])

    def annotate_for_jsonl(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        events = []

        for idx, row in df.iterrows():
            events.append(self.annotate_row(row, idx).to_jsonl_dict())

        return events

    def annotate_row(self, row: pd.Series, idx: int) -> AnnotatedStockEvent:
        event_date = TextNormalizer.clean(row.get("event_date"))
        event_type = TextNormalizer.clean(row.get("event_type"))
        region = TextNormalizer.clean(row.get("region"))
        sector = TextNormalizer.clean(row.get("sector"))
        related_stocks = TextNormalizer.clean(row.get("related_stocks"))
        title = TextNormalizer.clean(row.get("title"))
        description = TextNormalizer.clean(row.get("description"))
        direction = TextNormalizer.clean(row.get("direction"))
        severity = TextNormalizer.clean(row.get("severity"))
        stock_code = StockCodeNormalizer.normalize(row.get("stock_code"))
        stock_name = TextNormalizer.clean(row.get("stock_name"))

        has_direct_stock_mention = self.matcher.has_direct_mention(
            stock_name=stock_name,
            title=title,
            description=description,
        )
        stock_specificity = "direct" if has_direct_stock_mention else "weak"

        has_earnings_anchor, earnings_anchor_terms = self.earnings_detector.detect(
            title=title,
            description=description,
        )

        structured_fields, structured_status = self.structured_extractor.extract(row)

        stock_event_class = self.classifier.classify(
            row=row,
            has_direct_stock_mention=has_direct_stock_mention,
            has_earnings_anchor=has_earnings_anchor,
        )

        permission = self.policy.annotate_permission(
            row=row,
            stock_event_class=stock_event_class,
            stock_specificity=stock_specificity,
            has_direct_stock_mention=has_direct_stock_mention,
            has_earnings_anchor=has_earnings_anchor,
            earnings_anchor_terms=earnings_anchor_terms,
            structured_match_status=structured_status,
        )

        evidence_id = f"STOCK_EVENT_{idx + 1:06d}"

        return AnnotatedStockEvent(
            evidence_id=evidence_id,
            source_type=self.config.source_type,
            event_date=event_date,
            stock_code=stock_code,
            stock_name=stock_name,
            event_type=event_type,
            title=title,
            description=description,
            sector=sector,
            region=region,
            related_stocks=related_stocks,
            original_direction=direction,
            original_severity=severity,
            stock_event_class=stock_event_class,
            stock_specificity=stock_specificity,
            has_direct_stock_mention=has_direct_stock_mention,
            has_earnings_anchor=has_earnings_anchor,
            earnings_anchor_terms=earnings_anchor_terms,
            structured_match_fields=structured_fields,
            structured_match_status=structured_status,
            raw_row=row.to_dict(),
            **permission,
        )


class StockEventContextPipeline:
    REQUIRED_COLUMNS = [
        "event_date",
        "event_type",
        "region",
        "sector",
        "related_stocks",
        "title",
        "description",
        "direction",
        "severity",
        "stock_code",
        "stock_name",
    ]

    def __init__(self, config: StockEventContextConfig):
        self.config = config
        self.annotator = StockEventContextAnnotator(config)

    def run(self) -> None:
        input_path = Path(self.config.input_csv_path)
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        df = pd.read_csv(input_path)
        self._validate_required_columns(df)

        annotated_df = self.annotator.annotate_dataframe(df)
        jsonl_records = self.annotator.annotate_for_jsonl(df)

        annotated_csv_path = output_dir / "stock_event_context_annotations.csv"
        jsonl_path = output_dir / "stock_event_context_annotations.jsonl"
        report_path = output_dir / "stock_event_context_annotation_report.md"

        annotated_df.to_csv(
            annotated_csv_path,
            index=False,
            encoding=self.config.encoding,
        )
        self._write_jsonl(jsonl_path, jsonl_records)
        self._write_report(report_path, annotated_df)

        print("[DONE] stock_event_context annotation completed")
        print(f"input: {input_path}")
        print(f"rows: {len(df):,}")
        print(f"annotated_csv: {annotated_csv_path}")
        print(f"jsonl: {jsonl_path}")
        print(f"report: {report_path}")

        self._print_summary(annotated_df)

    def _validate_required_columns(self, df: pd.DataFrame) -> None:
        missing = [col for col in self.REQUIRED_COLUMNS if col not in df.columns]

        if missing:
            raise ValueError(f"Missing required columns: {missing}")

    @staticmethod
    def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _parse_json_list(value: Any) -> List[str]:
        if pd.isna(value):
            return []

        try:
            parsed = json.loads(str(value))
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass

        return []

    def _write_report(self, path: Path, annotated_df: pd.DataFrame) -> None:
        lines: List[str] = []

        lines.append("# stock_event_context annotation report")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- rows: {len(annotated_df):,}")
        lines.append(
            f"- can_be_news_trigger_ceiling=True: "
            f"{int(annotated_df['can_be_news_trigger_ceiling'].sum()):,}"
        )
        lines.append(
            f"- can_be_market_cause_ceiling=True: "
            f"{int(annotated_df['can_be_market_cause_ceiling'].sum()):,}"
        )
        lines.append("")

        summary_columns = [
            "stock_event_class",
            "evidence_role",
            "allowed_llm_usage_ceiling",
            "stock_specificity",
            "can_be_news_trigger_ceiling",
            "can_be_market_cause_ceiling",
            "can_be_main_cause_ceiling",
            "directionality_default",
        ]

        for col in summary_columns:
            lines.append(f"## {col} counts")
            lines.append("")
            lines.append(annotated_df[col].value_counts(dropna=False).to_string())
            lines.append("")

        flag_counter: Dict[str, int] = {}

        for value in annotated_df["validator_flags"].tolist():
            for flag in self._parse_json_list(value):
                flag_counter[flag] = flag_counter.get(flag, 0) + 1

        lines.append("## Validator flag counts")
        lines.append("")

        for flag, count in sorted(flag_counter.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- {flag}: {count}")

        lines.append("")
        lines.append("## Interpretation")
        lines.append("")
        lines.append(
            "This revised annotation separates news-topic trigger permission from market-causal permission."
        )
        lines.append(
            "Curated earnings rows with direct stock mention and earnings anchors may be used as news topic triggers."
        )
        lines.append(
            "However, no stock_event row is allowed to claim stock price causality without separate price-volume or official evidence."
        )
        lines.append(
            "Industry and macro-policy rows remain background context only."
        )

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    @staticmethod
    def _print_summary(annotated_df: pd.DataFrame) -> None:
        print("\n[CLASS COUNTS]")
        print(annotated_df["stock_event_class"].value_counts(dropna=False).to_string())

        print("\n[ROLE COUNTS]")
        print(annotated_df["evidence_role"].value_counts(dropna=False).to_string())

        print("\n[NEWS TRIGGER CEILING]")
        print(
            annotated_df["can_be_news_trigger_ceiling"]
            .value_counts(dropna=False)
            .to_string()
        )

        print("\n[MARKET CAUSE CEILING]")
        print(
            annotated_df["can_be_market_cause_ceiling"]
            .value_counts(dropna=False)
            .to_string()
        )

        print("\n[MAIN CAUSE CEILING - DEPRECATED COMPAT]")
        print(
            annotated_df["can_be_main_cause_ceiling"]
            .value_counts(dropna=False)
            .to_string()
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Annotate stock_event_calendar for pr05c stock_event_context."
    )

    parser.add_argument(
        "--input",
        default="/Users/hgs/Desktop/IISE CD/data/raw/market_event/stock_event_calendar_2013_2023_with_stock_code.csv",
        help="Path to stock_event_calendar_2013_2023_with_stock_code.csv",
    )

    parser.add_argument(
        "--output-dir",
        default="/Users/hgs/Desktop/IISE CD/data/interim/pr05c_stock_event_context",
        help="Directory to save stock_event_context annotation outputs.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    config = StockEventContextConfig(
        input_csv_path=args.input,
        output_dir=args.output_dir,
    )

    pipeline = StockEventContextPipeline(config)
    pipeline.run()