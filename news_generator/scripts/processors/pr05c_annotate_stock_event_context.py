from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False


@dataclass
class StockEventContextConfig:
    input_csv_path: str
    output_dir: str

    source_type: str = "stock_event_calendar"
    encoding: str = "utf-8-sig"

    use_llm_judge: bool = False
    llm_model: str = "gpt-4o-mini"
    llm_sleep_sec: float = 0.15
    llm_max_retries: int = 3
    env_path: Optional[str] = None

    source_market_claim_level: str = "no_market_claim"
    judge_earnings_only: bool = True


class TextNormalizer:
    @staticmethod
    def clean(value: Any) -> str:
        if value is None:
            return ""

        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass

        text = str(value).strip()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def compact(value: Any) -> str:
        text = TextNormalizer.clean(value).lower()
        text = re.sub(r"\s+", "", text)
        text = text.replace("ㆍ", "")
        text = text.replace("·", "")
        text = text.replace("-", "")
        text = text.replace("_", "")
        text = text.replace("/", "")
        text = text.replace(" ", "")
        return text


class StockCodeNormalizer:
    @staticmethod
    def normalize(value: Any) -> str:
        text = TextNormalizer.clean(value)

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
        stock_name = TextNormalizer.clean(stock_name)
        aliases = {stock_name}

        if stock_name in self.MANUAL_ALIASES:
            aliases.update(self.MANUAL_ALIASES[stock_name])

        return sorted({x for x in aliases if x})

    def has_direct_mention(self, stock_name: str, title: str, description: str) -> bool:
        joined = TextNormalizer.compact(f"{title} {description}")

        for alias in self.build_aliases(stock_name):
            alias_compact = TextNormalizer.compact(alias)
            if alias_compact and alias_compact in joined:
                return True

        return False


class EarningsAnchorDetector:
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
        "전망",
    ]

    QUARTER_PATTERN = re.compile(r"(\d)\s*분기|([1-4])Q", re.IGNORECASE)
    PROFIT_PATTERN = re.compile(r"(영업이익|영업익|순이익|매출|영업적자|적자)[^\d]{0,12}\d")
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
        "영업익", "매출", "순이익", "영업적자", "적자",
        "시장점유율", "경영권", "분쟁", "법정관리", "IPO",
    ]

    def extract(
        self,
        title: str,
        description: str,
        event_type: str,
        sector: str,
        region: str,
    ) -> Tuple[Dict[str, Any], str]:
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
            "event_type": event_type,
            "sector": sector,
            "region": region,
            "quarter_mentions": sorted(set(quarters)),
            "percentage_mentions": sorted(set(percentages)),
            "money_mentions": sorted(set(money_values)),
            "numeric_terms": sorted(set(numeric_terms)),
            "keyword_anchors": sorted(set(keywords)),
        }

        if quarters or percentages or money_values or numeric_terms or keywords:
            return fields, "WEAK_TEXT_DERIVED"

        return fields, "MISSING"


class DirectionalityResolver:
    POSITIVE_TERMS = [
        "positive", "up", "increase",
        "호조", "개선", "상회", "급증", "최대", "서프라이즈",
    ]
    NEGATIVE_TERMS = [
        "negative", "down", "decrease",
        "부진", "악화", "하회", "급감", "쇼크", "적자",
    ]

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


class IndustryClassifier:
    MACRO_POLICY_KEYWORDS = [
        "금리", "환율", "원화", "달러", "유가", "국제유가",
        "납사", "운임", "관세", "정책", "규제", "보조금",
        "수출", "수입", "물가", "경기", "중국", "미국",
        "정부", "정제마진",
    ]

    def classify(self, title: str, description: str) -> str:
        text = f"{title} {description}"

        if any(keyword in text for keyword in self.MACRO_POLICY_KEYWORDS):
            return "stock_macro_policy_exposure_context"

        return "stock_sector_theme_context"


class RulePrechecker:
    def __init__(self, config: StockEventContextConfig):
        self.config = config
        self.matcher = StockNameMatcher()
        self.earnings_detector = EarningsAnchorDetector()
        self.structured_extractor = StructuredFieldExtractor()
        self.directionality_resolver = DirectionalityResolver()
        self.industry_classifier = IndustryClassifier()

    def precheck_row(self, row: pd.Series, idx: int) -> Dict[str, Any]:
        event_date = TextNormalizer.clean(row.get("event_date"))
        event_type = TextNormalizer.clean(row.get("event_type")).lower()
        region = TextNormalizer.clean(row.get("region"))
        sector = TextNormalizer.clean(row.get("sector"))
        related_stocks = TextNormalizer.clean(row.get("related_stocks"))
        title = TextNormalizer.clean(row.get("title"))
        description = TextNormalizer.clean(row.get("description"))
        direction = TextNormalizer.clean(row.get("direction"))
        severity = TextNormalizer.clean(row.get("severity")).lower()
        stock_code = StockCodeNormalizer.normalize(row.get("stock_code"))
        stock_name = TextNormalizer.clean(row.get("stock_name"))
        match_status = TextNormalizer.clean(row.get("_match_status"))

        evidence_id = f"STOCK_EVENT_{idx + 1:06d}"

        has_direct_stock_mention = self.matcher.has_direct_mention(
            stock_name=stock_name,
            title=title,
            description=description,
        )
        stock_specificity_rule = "direct" if has_direct_stock_mention else "weak"

        has_earnings_anchor, earnings_anchor_terms = self.earnings_detector.detect(
            title=title,
            description=description,
        )

        structured_fields, structured_status = self.structured_extractor.extract(
            title=title,
            description=description,
            event_type=event_type,
            sector=sector,
            region=region,
        )

        directionality_default, directionality_source = self.directionality_resolver.resolve(
            direction=direction,
            title=title,
            description=description,
        )

        validator_flags: List[str] = [
            "MISSING_SOURCE_PROVENANCE",
            "SOURCE_LEVEL_MARKET_CAUSE_NOT_DECIDED",
        ]

        if match_status == "matched_multi_exploded":
            validator_flags.append("MULTI_STOCK_EXPLODED_ROW")

        if not has_direct_stock_mention:
            validator_flags.append("STOCK_NAME_NOT_DIRECTLY_MENTIONED")

        if event_type == "earnings" and not has_earnings_anchor:
            validator_flags.append("EARNINGS_ANCHOR_MISSING_RULE")

        if event_type == "earnings":
            rule_news_trigger_precheck = "review"
            llm_judge_required = True
            stock_event_class_rule = "stock_earnings_candidate_for_llm_judge"
            rule_allowed_usage = "earnings_semantic_review"
        elif event_type == "industry":
            rule_news_trigger_precheck = "reject_company_trigger"
            llm_judge_required = False
            stock_event_class_rule = self.industry_classifier.classify(title, description)
            rule_allowed_usage = (
                "macro_policy_exposure_context"
                if stock_event_class_rule == "stock_macro_policy_exposure_context"
                else "sector_context"
            )
        else:
            rule_news_trigger_precheck = "reject_company_trigger"
            llm_judge_required = False
            stock_event_class_rule = "stock_ambiguous_mapped_context"
            rule_allowed_usage = "background_context"

        return {
            "evidence_id": evidence_id,
            "source_type": self.config.source_type,
            "event_date": event_date,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "event_type": event_type,
            "title": title,
            "description": description,
            "sector": sector,
            "region": region,
            "related_stocks": related_stocks,
            "original_direction": direction,
            "original_severity": severity,
            "source_provenance": "dataset_level_unknown",
            "source_provenance_status": "MISSING",
            "has_direct_stock_mention_rule": has_direct_stock_mention,
            "stock_specificity_rule": stock_specificity_rule,
            "has_earnings_anchor_rule": has_earnings_anchor,
            "earnings_anchor_terms_rule": earnings_anchor_terms,
            "structured_match_fields": structured_fields,
            "structured_match_status": structured_status,
            "directionality_default": directionality_default,
            "directionality_source": directionality_source,
            "rule_news_trigger_precheck": rule_news_trigger_precheck,
            "rule_allowed_usage": rule_allowed_usage,
            "stock_event_class_rule": stock_event_class_rule,
            "llm_judge_required": llm_judge_required,
            "source_market_claim_level": self.config.source_market_claim_level,
            "validator_flags": validator_flags,
            "raw_row": row.to_dict(),
        }


class OpenAiStockEventJudge:
    DECISIONS = [
        "accept_company_trigger",
        "accept_group_or_peer_context",
        "background_only",
        "reject",
    ]

    SPECIFICITIES = [
        "direct_company",
        "group_or_affiliate",
        "peer_or_sector",
        "macro_context",
        "ambiguous",
        "unrelated",
    ]

    EVENT_INTERPRETATIONS = [
        "earnings_result",
        "earnings_guidance",
        "group_earnings_context",
        "peer_earnings_context",
        "sector_or_macro_context",
        "non_earnings_event",
        "unclear",
    ]

    def __init__(self, config: StockEventContextConfig):
        self.config = config
        self.cache_path = Path(config.output_dir) / "stock_event_llm_judge_cache.jsonl"
        self.cache: Dict[str, Dict[str, Any]] = self._load_cache()
        self.client = None

        if config.use_llm_judge:
            self._load_env()

            if not os.getenv("OPENAI_API_KEY"):
                raise EnvironmentError("OPENAI_API_KEY is not set after loading .env.")

            from openai import OpenAI
            self.client = OpenAI()

    def _load_env(self) -> None:
        candidate_paths: List[Path] = []

        if self.config.env_path:
            candidate_paths.append(Path(self.config.env_path))

        candidate_paths.extend(
            [
                Path.cwd() / ".env",
                Path.cwd().parent / ".env",
                Path("/Users/hgs/Desktop/IISE CD/news_generator/.env"),
                Path("/Users/hgs/Desktop/IISE CD/.env"),
            ]
        )

        for path in candidate_paths:
            if path.exists():
                load_dotenv(dotenv_path=path, override=False)
                return

        load_dotenv(override=False)

    def judge(self, record: Dict[str, Any]) -> Dict[str, Any]:
        cache_key = self._make_cache_key(record)

        if cache_key in self.cache:
            cached = dict(self.cache[cache_key])
            cached["llm_cache_hit"] = True
            cached["llm_cache_key"] = cache_key
            return cached

        payload = self._build_payload(record)

        for attempt in range(1, self.config.llm_max_retries + 1):
            try:
                result = self._call_openai(payload)
                result["llm_cache_hit"] = False
                result["llm_cache_key"] = cache_key
                self._append_cache(cache_key, result)
                time.sleep(self.config.llm_sleep_sec)
                return result
            except Exception as e:
                if attempt >= self.config.llm_max_retries:
                    return {
                        "evidence_id": record["evidence_id"],
                        "decision": "background_only",
                        "specificity": "ambiguous",
                        "event_interpretation": "unclear",
                        "confidence": 0.0,
                        "reason": f"LLM judge failed after retries: {e}",
                        "allowed_claim_summary": "Use only as background context.",
                        "forbidden_claim_summary": "Do not use as company trigger or market cause.",
                        "evidence_used": [],
                        "llm_cache_hit": False,
                        "llm_cache_key": cache_key,
                        "llm_error": str(e),
                    }

                time.sleep(1.5 * attempt)

        raise RuntimeError("Unreachable LLM judge state.")

    def _call_openai(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("OpenAI client is not initialized.")

        response = self.client.chat.completions.create(
            model=self.config.llm_model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": self._system_prompt(),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "stock_event_semantic_judgment",
                    "strict": True,
                    "schema": self._json_schema(),
                },
            },
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError("Empty LLM response content.")

        return json.loads(content)

    def _system_prompt(self) -> str:
        return """
You are judging whether a stock_event_calendar row can be used as a company-specific news topic trigger.

Important constraints:
1. This source is a curated stock event calendar, not an official DART disclosure.
2. Judge only whether the row can be used as a company-specific news topic trigger.
3. Do not decide stock price causality here.
4. If the row can only support group, peer, sector, or macro context, do not mark it as company trigger.
5. Compound company expressions can be considered if the target stock is clearly included.
   Example: "현대·기아차" may include "기아" if the target stock is 기아.
6. If the title mainly refers to another company and the target stock is only related by sector mapping, use accept_group_or_peer_context or background_only.
7. The original event_type may be wrong or coarse. If the row describes governance, litigation, management dispute, bankruptcy, ownership, IPO, restructuring, or other firm-specific issue, mark event_interpretation as non_earnings_event.
8. If the row describes earnings forecast, operating loss forecast, profit/loss outlook, or expected earnings recovery, mark event_interpretation as earnings_guidance.
9. Never classify a row as an official filing.
10. Output only the JSON schema.

Decision meanings:
- accept_company_trigger: the row can be used as the main company-specific news topic for the target stock.
- accept_group_or_peer_context: relevant to target stock through group, affiliate, peer, or sector, but not a direct company trigger.
- background_only: broad background only.
- reject: unrelated, misleading, or unsafe to use.
""".strip()

    def _json_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "evidence_id": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": self.DECISIONS,
                },
                "specificity": {
                    "type": "string",
                    "enum": self.SPECIFICITIES,
                },
                "event_interpretation": {
                    "type": "string",
                    "enum": self.EVENT_INTERPRETATIONS,
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "reason": {"type": "string"},
                "allowed_claim_summary": {"type": "string"},
                "forbidden_claim_summary": {"type": "string"},
                "evidence_used": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "evidence_id",
                "decision",
                "specificity",
                "event_interpretation",
                "confidence",
                "reason",
                "allowed_claim_summary",
                "forbidden_claim_summary",
                "evidence_used",
            ],
        }

    def _build_payload(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "task": "stock_event_semantic_trigger_judgment",
            "evidence_id": record["evidence_id"],
            "target_stock": {
                "stock_code": record["stock_code"],
                "stock_name": record["stock_name"],
            },
            "event": {
                "event_date": record["event_date"],
                "event_type": record["event_type"],
                "title": record["title"],
                "description": record["description"],
                "sector": record["sector"],
                "region": record["region"],
                "related_stocks": record["related_stocks"],
                "original_direction": record["original_direction"],
                "original_severity": record["original_severity"],
            },
            "rule_features": {
                "has_direct_stock_mention_rule": record["has_direct_stock_mention_rule"],
                "stock_specificity_rule": record["stock_specificity_rule"],
                "has_earnings_anchor_rule": record["has_earnings_anchor_rule"],
                "earnings_anchor_terms_rule": record["earnings_anchor_terms_rule"],
                "structured_match_fields": record["structured_match_fields"],
                "structured_match_status": record["structured_match_status"],
            },
            "judge_scope": {
                "judge_news_trigger_only": True,
                "do_not_judge_market_causality": True,
                "source_is_not_official_disclosure": True,
                "event_type_may_be_wrong_or_coarse": True,
            },
        }

    def _make_cache_key(self, record: Dict[str, Any]) -> str:
        key_obj = {
            "evidence_id": record["evidence_id"],
            "stock_code": record["stock_code"],
            "stock_name": record["stock_name"],
            "event_date": record["event_date"],
            "event_type": record["event_type"],
            "title": record["title"],
            "description": record["description"],
            "model": self.config.llm_model,
            "judge_version": "stock_event_semantic_judge_v0.4",
        }

        raw = json.dumps(key_obj, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        cache: Dict[str, Dict[str, Any]] = {}

        if not self.cache_path.exists():
            return cache

        with open(self.cache_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                try:
                    obj = json.loads(line)
                    cache_key = obj.get("cache_key")
                    judgment = obj.get("judgment")

                    if cache_key and isinstance(judgment, dict):
                        cache[cache_key] = judgment
                except Exception:
                    continue

        return cache

    def _append_cache(self, cache_key: str, judgment: Dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.cache_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "cache_key": cache_key,
                        "judgment": judgment,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


class FinalPermissionMerger:
    BASE_FORBIDDEN_CLAIMS = [
        "Do not claim stock price causality from stock_event alone.",
        "Do not describe this source as an official DART filing.",
        "Do not claim exact financial numbers unless explicitly present in the row or supported by separate financial evidence.",
        "Do not override stronger official evidence if DART or audited financial evidence exists.",
    ]

    @staticmethod
    def _correct_event_interpretation(out: Dict[str, Any], event_interpretation: str) -> str:
        title = TextNormalizer.clean(out.get("title"))
        description = TextNormalizer.clean(out.get("description"))
        text = f"{title} {description}"

        earnings_guidance_terms = [
            "실적 전망",
            "영업이익 전망",
            "영업익 전망",
            "적자 전망",
            "영업적자",
            "대규모 적자",
            "최대 적자",
            "역대 최대 적자",
            "흑자 전환 기대",
            "실적 회복 기대",
            "영업이익 급감 전망",
            "실적 개선 전망",
            "실적 부진 전망",
        ]

        if event_interpretation == "non_earnings_event":
            if any(term in text for term in earnings_guidance_terms):
                return "earnings_guidance"

        return event_interpretation

    def merge(self, record: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(record)

        if record["event_type"] == "earnings":
            decision = TextNormalizer.clean(record.get("llm_decision"))

            if not decision:
                decision = self._fallback_rule_decision(record)

            self._merge_earnings(out, decision)
        else:
            self._merge_non_earnings(out)

        out["can_be_market_cause_ceiling"] = False
        out["can_be_main_cause_ceiling"] = False
        out["source_market_claim_level"] = "no_market_claim"

        out["can_be_news_trigger_ceiling"] = out["final_can_be_news_trigger"]

        return out

    def _merge_earnings(self, out: Dict[str, Any], decision: str) -> None:
        flags = list(out.get("validator_flags", []))

        event_interpretation = TextNormalizer.clean(out.get("llm_event_interpretation"))
        event_interpretation = self._correct_event_interpretation(out, event_interpretation)
        out["final_event_interpretation"] = event_interpretation

        if decision == "accept_company_trigger":
            if event_interpretation in {
                "earnings_result",
                "earnings_guidance",
                "group_earnings_context",
            }:
                self._merge_company_earnings_trigger(out, flags)
                return

            if event_interpretation == "non_earnings_event":
                self._merge_company_non_earnings_trigger(out, flags)
                return

            out["stock_event_class"] = "stock_ambiguous_company_event_context"
            out["evidence_role"] = "background_context"
            out["final_can_be_news_trigger"] = False
            out["trigger_eligible_ceiling"] = False
            out["final_allowed_usage"] = "background_context"
            out["allowed_llm_usage_ceiling"] = "background_context"
            out["bundle_market_claim_level_hint"] = "background_only"
            out["cluster_role_eligibility"] = "background_only"
            out["fallback_floor_contribution"] = "none"
            out["directionality_required"] = False
            out["dart_mapping_class"] = None
            out["dart_mapping_family"] = None
            out["materiality_basis"] = "llm_company_trigger_but_event_type_unclear"
            flags.append("LLM_COMPANY_TRIGGER_DOWNGRADED_DUE_TO_UNCLEAR_EVENT_INTERPRETATION")
            out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
            out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
            out["validator_flags"] = sorted(set(flags))
            return

        if decision == "accept_group_or_peer_context":
            if event_interpretation in {
                "peer_earnings_context",
                "group_earnings_context",
                "earnings_result",
                "earnings_guidance",
            }:
                out["stock_event_class"] = "stock_group_or_peer_earnings_context"
                out["materiality_basis"] = "llm_semantic_group_or_peer_earnings_context"
            elif event_interpretation == "non_earnings_event":
                out["stock_event_class"] = "stock_group_or_peer_company_event_context"
                out["materiality_basis"] = "llm_semantic_group_or_peer_non_earnings_context"
            elif event_interpretation == "sector_or_macro_context":
                out["stock_event_class"] = "stock_group_or_peer_sector_context"
                out["materiality_basis"] = "llm_semantic_sector_or_macro_context"
            else:
                out["stock_event_class"] = "stock_group_or_peer_context"
                out["materiality_basis"] = "llm_semantic_group_or_peer_context"

            out["evidence_role"] = "supporting_context"
            out["final_can_be_news_trigger"] = False
            out["trigger_eligible_ceiling"] = False
            out["final_allowed_usage"] = "group_or_peer_context"
            out["allowed_llm_usage_ceiling"] = "group_or_peer_context"
            out["bundle_market_claim_level_hint"] = "context_may_support_bundle_only"
            out["cluster_role_eligibility"] = "supporting_or_background_only"
            out["fallback_floor_contribution"] = "none"
            out["directionality_required"] = False
            out["dart_mapping_class"] = None
            out["dart_mapping_family"] = None

            flags.append("LLM_ACCEPTED_GROUP_OR_PEER_CONTEXT_ONLY")

            out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
            out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
            out["validator_flags"] = sorted(set(flags))
            return

        if decision == "background_only":
            out["stock_event_class"] = "stock_ambiguous_earnings_context"
            out["evidence_role"] = "background_context"
            out["final_can_be_news_trigger"] = False
            out["trigger_eligible_ceiling"] = False
            out["final_allowed_usage"] = "background_context"
            out["allowed_llm_usage_ceiling"] = "background_context"
            out["bundle_market_claim_level_hint"] = "background_only"
            out["cluster_role_eligibility"] = "background_only"
            out["fallback_floor_contribution"] = "none"
            out["directionality_required"] = False
            out["dart_mapping_class"] = None
            out["dart_mapping_family"] = None
            out["materiality_basis"] = "llm_semantic_background_only"
            flags.append("LLM_BACKGROUND_ONLY")
            out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
            out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
            out["validator_flags"] = sorted(set(flags))
            return

        out["stock_event_class"] = "stock_event_rejected"
        out["evidence_role"] = "excluded"
        out["final_can_be_news_trigger"] = False
        out["trigger_eligible_ceiling"] = False
        out["final_allowed_usage"] = "do_not_use"
        out["allowed_llm_usage_ceiling"] = "do_not_use"
        out["bundle_market_claim_level_hint"] = "none"
        out["cluster_role_eligibility"] = "excluded"
        out["fallback_floor_contribution"] = "none"
        out["directionality_required"] = False
        out["dart_mapping_class"] = None
        out["dart_mapping_family"] = None
        out["materiality_basis"] = "llm_semantic_reject"
        flags.append("LLM_REJECTED_STOCK_EVENT_ROW")
        out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
        out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
        out["validator_flags"] = sorted(set(flags))

    def _merge_company_earnings_trigger(self, out: Dict[str, Any], flags: List[str]) -> None:
        out["stock_event_class"] = "stock_curated_earnings_event"
        out["evidence_role"] = "primary_candidate_for_news_topic"
        out["final_can_be_news_trigger"] = True
        out["trigger_eligible_ceiling"] = True
        out["final_allowed_usage"] = "earnings_news_topic"
        out["allowed_llm_usage_ceiling"] = "earnings_event_trigger"
        out["bundle_market_claim_level_hint"] = "eligible_for_bundle_market_judge"
        out["cluster_role_eligibility"] = "news_trigger_unless_duplicate_official_evidence_exists"
        out["fallback_floor_contribution"] = "weak_floor_only_with_companion_signal"
        out["directionality_required"] = True
        out["dart_mapping_class"] = "true_earnings_disclosure"
        out["dart_mapping_family"] = "earnings_family"
        out["materiality_basis"] = "llm_semantic_company_earnings_trigger"

        flags.append("LLM_ACCEPTED_COMPANY_EARNINGS_NEWS_TRIGGER")

        out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
        out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
        out["validator_flags"] = sorted(set(flags))

    def _merge_company_non_earnings_trigger(self, out: Dict[str, Any], flags: List[str]) -> None:
        out["stock_event_class"] = "stock_curated_company_event"
        out["evidence_role"] = "primary_candidate_for_news_topic"
        out["final_can_be_news_trigger"] = True
        out["trigger_eligible_ceiling"] = True
        out["final_allowed_usage"] = "company_specific_event_topic"
        out["allowed_llm_usage_ceiling"] = "company_specific_event_trigger"
        out["bundle_market_claim_level_hint"] = "eligible_for_bundle_market_judge"
        out["cluster_role_eligibility"] = "news_trigger_unless_duplicate_official_evidence_exists"
        out["fallback_floor_contribution"] = "weak_floor_only_with_companion_signal"
        out["directionality_required"] = True

        out["dart_mapping_class"] = None
        out["dart_mapping_family"] = None
        out["materiality_basis"] = "llm_semantic_company_specific_non_earnings_event"

        flags.append("LLM_ACCEPTED_COMPANY_NON_EARNINGS_NEWS_TRIGGER")

        out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
        out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
        out["validator_flags"] = sorted(set(flags))

    def _merge_non_earnings(self, out: Dict[str, Any]) -> None:
        flags = list(out.get("validator_flags", []))
        stock_event_class_rule = out["stock_event_class_rule"]

        out["final_event_interpretation"] = None
        out["stock_event_class"] = stock_event_class_rule
        out["evidence_role"] = "background_context"
        out["final_can_be_news_trigger"] = False
        out["trigger_eligible_ceiling"] = False
        out["directionality_required"] = False
        out["dart_mapping_class"] = None
        out["dart_mapping_family"] = None
        out["fallback_floor_contribution"] = "none"
        out["cluster_role_eligibility"] = "background_only"

        if stock_event_class_rule == "stock_macro_policy_exposure_context":
            out["final_allowed_usage"] = "macro_policy_exposure_context"
            out["allowed_llm_usage_ceiling"] = "macro_policy_exposure_context"
            out["bundle_market_claim_level_hint"] = "background_only"
            out["materiality_basis"] = "macro_or_policy_exposure_only"
            flags.append("MACRO_POLICY_EVENT_COMPANY_TRIGGER_FORBIDDEN")
        elif stock_event_class_rule == "stock_sector_theme_context":
            out["final_allowed_usage"] = "sector_context"
            out["allowed_llm_usage_ceiling"] = "sector_context"
            out["bundle_market_claim_level_hint"] = "background_only"
            out["materiality_basis"] = "sector_theme_only"
            flags.append("INDUSTRY_EVENT_COMPANY_TRIGGER_FORBIDDEN")
        else:
            out["final_allowed_usage"] = "background_context"
            out["allowed_llm_usage_ceiling"] = "background_context"
            out["bundle_market_claim_level_hint"] = "background_only"
            out["materiality_basis"] = "ambiguous_or_unknown_stock_event"

        out["allowed_claim_scope"] = self._allowed_claims_for(out["final_allowed_usage"], out)
        out["forbidden_claim_scope"] = self.BASE_FORBIDDEN_CLAIMS
        out["validator_flags"] = sorted(set(flags))

    @staticmethod
    def _fallback_rule_decision(record: Dict[str, Any]) -> str:
        if record.get("has_direct_stock_mention_rule") and record.get("has_earnings_anchor_rule"):
            return "accept_company_trigger"

        if record.get("has_earnings_anchor_rule"):
            return "accept_group_or_peer_context"

        return "background_only"

    @staticmethod
    def _allowed_claims_for(usage: str, out: Dict[str, Any]) -> List[str]:
        if usage == "earnings_news_topic":
            return [
                "May use this row as a company-specific earnings news topic trigger.",
                "May describe earnings strength, weakness, surprise, pressure, or attention if supported by title/description.",
                "May use cautious wording such as '부각', '관심', '부담', '기대'.",
                "Market impact may only be evaluated later at evidence-bundle level.",
            ]

        if usage == "company_specific_event_topic":
            return [
                "May use this row as a company-specific non-earnings news topic trigger.",
                "May describe the company-specific issue, dispute, management event, risk, or attention if supported by title/description.",
                "May use cautious wording such as '부각', '논란', '관심', '부담', '변수'.",
                "Market impact may only be evaluated later at evidence-bundle level.",
            ]

        if usage == "group_or_peer_context":
            return [
                "May use as group, affiliate, peer, sector, or related-company context.",
                "May not frame this as the target company's own confirmed event.",
                "May support a later evidence bundle as background or auxiliary context.",
            ]

        if usage == "macro_policy_exposure_context":
            return [
                "May describe macro, policy, FX, oil, rate, or trade exposure as background.",
                "May connect the stock to macro sensitivity only cautiously.",
            ]

        if usage == "sector_context":
            return [
                "May describe broad sector context.",
                "May mention that the stock is related to the sector theme only as background.",
            ]

        if usage == "do_not_use":
            return [
                "Do not use for generation.",
            ]

        return [
            "May use only as weak background context.",
        ]


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

    JSON_COLUMNS = [
        "earnings_anchor_terms_rule",
        "structured_match_fields",
        "validator_flags",
        "allowed_claim_scope",
        "forbidden_claim_scope",
        "raw_row",
        "llm_evidence_used",
    ]

    def __init__(self, config: StockEventContextConfig):
        self.config = config
        self.prechecker = RulePrechecker(config)
        self.llm_judge = OpenAiStockEventJudge(config)
        self.merger = FinalPermissionMerger()

    def run(self) -> None:
        input_path = Path(self.config.input_csv_path)
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        df = pd.read_csv(input_path)
        self._validate_required_columns(df)

        records: List[Dict[str, Any]] = []

        for idx, row in df.iterrows():
            record = self.prechecker.precheck_row(row, idx)

            if self._should_judge_with_llm(record):
                judgment = self.llm_judge.judge(record)
                self._attach_llm_judgment(record, judgment)
            else:
                self._attach_empty_llm_judgment(record)

            final_record = self.merger.merge(record)
            records.append(final_record)

        annotated_df = pd.DataFrame(records)
        annotated_df_for_csv = self._serialize_for_csv(annotated_df)

        annotated_csv_path = output_dir / "stock_event_context_annotations.csv"
        jsonl_path = output_dir / "stock_event_context_annotations.jsonl"
        report_path = output_dir / "stock_event_context_annotation_report.md"

        annotated_df_for_csv.to_csv(
            annotated_csv_path,
            index=False,
            encoding=self.config.encoding,
        )

        self._write_jsonl(jsonl_path, records)
        self._write_report(report_path, annotated_df)

        print("[DONE] stock_event_context annotation completed")
        print(f"input: {input_path}")
        print(f"rows: {len(df):,}")
        print(f"use_llm_judge: {self.config.use_llm_judge}")
        print(f"model: {self.config.llm_model if self.config.use_llm_judge else '(not used)'}")
        print(f"annotated_csv: {annotated_csv_path}")
        print(f"jsonl: {jsonl_path}")
        print(f"report: {report_path}")

        self._print_summary(annotated_df)

    def _should_judge_with_llm(self, record: Dict[str, Any]) -> bool:
        if not self.config.use_llm_judge:
            return False

        if not record.get("llm_judge_required"):
            return False

        if self.config.judge_earnings_only and record.get("event_type") != "earnings":
            return False

        return True

    @staticmethod
    def _attach_llm_judgment(record: Dict[str, Any], judgment: Dict[str, Any]) -> None:
        record["llm_decision"] = judgment.get("decision")
        record["llm_specificity"] = judgment.get("specificity")
        record["llm_event_interpretation"] = judgment.get("event_interpretation")
        record["llm_confidence"] = judgment.get("confidence")
        record["llm_reason"] = judgment.get("reason")
        record["llm_allowed_claim_summary"] = judgment.get("allowed_claim_summary")
        record["llm_forbidden_claim_summary"] = judgment.get("forbidden_claim_summary")
        record["llm_evidence_used"] = judgment.get("evidence_used", [])
        record["llm_cache_hit"] = judgment.get("llm_cache_hit", False)
        record["llm_cache_key"] = judgment.get("llm_cache_key", "")
        record["llm_error"] = judgment.get("llm_error", "")

    @staticmethod
    def _attach_empty_llm_judgment(record: Dict[str, Any]) -> None:
        record["llm_decision"] = None
        record["llm_specificity"] = None
        record["llm_event_interpretation"] = None
        record["llm_confidence"] = None
        record["llm_reason"] = None
        record["llm_allowed_claim_summary"] = None
        record["llm_forbidden_claim_summary"] = None
        record["llm_evidence_used"] = []
        record["llm_cache_hit"] = False
        record["llm_cache_key"] = ""
        record["llm_error"] = ""

    def _validate_required_columns(self, df: pd.DataFrame) -> None:
        missing = [col for col in self.REQUIRED_COLUMNS if col not in df.columns]

        if missing:
            raise ValueError(f"Missing required columns: {missing}")

    def _serialize_for_csv(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        for col in self.JSON_COLUMNS:
            if col in out.columns:
                out[col] = out[col].apply(
                    lambda x: json.dumps(x, ensure_ascii=False, default=str)
                )

        return out

    @staticmethod
    def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _write_report(self, path: Path, annotated_df: pd.DataFrame) -> None:
        lines: List[str] = []

        lines.append("# stock_event_context annotation report")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- rows: {len(annotated_df):,}")
        lines.append(f"- use_llm_judge: {self.config.use_llm_judge}")
        lines.append(f"- llm_model: {self.config.llm_model if self.config.use_llm_judge else '(not used)'}")
        lines.append(
            f"- final_can_be_news_trigger=True: "
            f"{int(annotated_df['final_can_be_news_trigger'].sum()):,}"
        )
        lines.append(
            f"- can_be_market_cause_ceiling=True: "
            f"{int(annotated_df['can_be_market_cause_ceiling'].sum()):,}"
        )
        lines.append("")

        summary_columns = [
            "event_type",
            "stock_event_class",
            "evidence_role",
            "rule_news_trigger_precheck",
            "llm_judge_required",
            "llm_decision",
            "llm_specificity",
            "llm_event_interpretation",
            "final_event_interpretation",
            "final_allowed_usage",
            "final_can_be_news_trigger",
            "can_be_market_cause_ceiling",
            "source_market_claim_level",
            "bundle_market_claim_level_hint",
        ]

        for col in summary_columns:
            if col not in annotated_df.columns:
                continue

            lines.append(f"## {col} counts")
            lines.append("")
            lines.append(annotated_df[col].value_counts(dropna=False).to_string())
            lines.append("")

        lines.append("## Interpretation")
        lines.append("")
        lines.append(
            "This version separates source-level semantic trigger judgment from market-causality judgment."
        )
        lines.append(
            "LLM may decide whether a stock_event row is a company-specific news topic trigger."
        )
        lines.append(
            "The final_event_interpretation column applies deterministic correction to LLM event interpretation."
        )
        lines.append(
            "Group/peer contexts are split into earnings, non-earnings company-event, sector/macro, or generic group/peer classes."
        )
        lines.append(
            "Source-level stock_event rows still do not decide market causality."
        )
        lines.append(
            "Market impact should be judged later at evidence-bundle level using DART, price-volume, GDELT, macro, and stock_event together."
        )

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    @staticmethod
    def _print_summary(annotated_df: pd.DataFrame) -> None:
        print("\n[CLASS COUNTS]")
        print(annotated_df["stock_event_class"].value_counts(dropna=False).to_string())

        print("\n[ROLE COUNTS]")
        print(annotated_df["evidence_role"].value_counts(dropna=False).to_string())

        print("\n[LLM DECISION COUNTS]")
        print(annotated_df["llm_decision"].value_counts(dropna=False).to_string())

        print("\n[LLM EVENT INTERPRETATION COUNTS]")
        print(annotated_df["llm_event_interpretation"].value_counts(dropna=False).to_string())

        print("\n[FINAL EVENT INTERPRETATION COUNTS]")
        print(annotated_df["final_event_interpretation"].value_counts(dropna=False).to_string())

        print("\n[FINAL ALLOWED USAGE]")
        print(annotated_df["final_allowed_usage"].value_counts(dropna=False).to_string())

        print("\n[FINAL NEWS TRIGGER]")
        print(annotated_df["final_can_be_news_trigger"].value_counts(dropna=False).to_string())

        print("\n[MARKET CAUSE CEILING - SOURCE LEVEL]")
        print(annotated_df["can_be_market_cause_ceiling"].value_counts(dropna=False).to_string())

        print("\n[BUNDLE MARKET CLAIM HINT]")
        print(annotated_df["bundle_market_claim_level_hint"].value_counts(dropna=False).to_string())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Annotate stock_event_calendar with optional LLM semantic trigger judgment."
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

    parser.add_argument(
        "--use-llm-judge",
        action="store_true",
        help="Use OpenAI LLM judge for earnings rows.",
    )

    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI model for LLM judge.",
    )

    parser.add_argument(
        "--llm-sleep-sec",
        type=float,
        default=0.15,
        help="Sleep seconds between LLM calls.",
    )

    parser.add_argument(
        "--env-path",
        default=None,
        help="Optional explicit .env path.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    config = StockEventContextConfig(
        input_csv_path=args.input,
        output_dir=args.output_dir,
        use_llm_judge=args.use_llm_judge,
        llm_model=args.model,
        llm_sleep_sec=args.llm_sleep_sec,
        env_path=args.env_path,
    )

    pipeline = StockEventContextPipeline(config)
    pipeline.run()