# news_generator/scripts/processors/pr_gdelt01_build_gdelt_context_summary.py
# -*- coding: utf-8 -*-

"""
pr_gdelt01_build_gdelt_context_summary.py

목적:
    GDELT GKG / EVENTS parquet를 읽어서 개별 주식 뉴스 생성(pr05a)에
    사용할 수 있는 일별 context signal을 생성한다.

핵심 원칙:
    - 이 파일은 뉴스를 생성하지 않는다.
    - GDELT만으로 특정 종목의 직접 원인을 단정하지 않는다.
    - GKG는 themes_json / orgs_json taxonomy 중심으로 처리한다.
    - EVENTS는 actor / country / CAMEO 중심으로 처리한다.
    - URL, domain, source_name은 matching evidence로 쓰지 않는다.
      진단 및 source quality 정보로만 보관한다.
    - co-occurrence는 반드시 record 단위에서 판정한 뒤 집계한다.
    - CAMEO는 sector_linkable로 쓰지 않는다.
    - direct_stock은 stock alias universe가 생기기 전까지 구현하지 않는다.

입력:
    gkg_YYYYMM.parquet
    events_YYYYMM.parquet

출력:
    gdelt_context_summary.csv              # 사용 가능한 전체 후보 pool
    gdelt_context_quarantine.csv           # 실제 제외 후보
    gdelt_context_ranked_preview.csv       # 사람이 확인하기 위한 날짜별 top-N preview
    gdelt_context_diagnostics.txt

주요 출력 컬럼:
    date, theme, evidence_class, confidence_level,
    stock_link_score, specificity_score,
    sector_tags, factor_tags, exposure_tags, macro_tags,
    hard_match_tags, modifier_tags, profile_factor_tags,
    matched_tokens, strong_tokens, generic_tokens, cooccurrence_rules,
    org_anchor_detected, korea_related, source,
    raw_count, weighted_count, avg_tone,
    forbidden_as_standalone_evidence,
    requires_profile_match, requires_corroboration,
    usage_rule, reason_code
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd


# =============================================================================
# Basic utils
# =============================================================================


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>", "nat"}:
        return ""
    return text


def safe_float(value: Any) -> Optional[float]:
    text = clean_text(value)
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalize_date_value(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""

    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) >= 8:
        yyyy, mm, dd = digits[:4], digits[4:6], digits[6:8]
        try:
            return pd.Timestamp(f"{yyyy}-{mm}-{dd}").strftime("%Y-%m-%d")
        except Exception:
            pass

    try:
        dt = pd.to_datetime(text, errors="coerce")
        if pd.isna(dt):
            return ""
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def normalize_date_series(series: pd.Series) -> pd.Series:
    s = series.astype("string").fillna("")
    digits = s.str.replace(r"[^0-9]", "", regex=True).str.slice(0, 8)
    valid = digits.str.len().eq(8)

    out = pd.Series("", index=series.index, dtype="string")
    out.loc[valid] = (
        digits.loc[valid].str.slice(0, 4)
        + "-"
        + digits.loc[valid].str.slice(4, 6)
        + "-"
        + digits.loc[valid].str.slice(6, 8)
    )
    return out


def normalize_cameo_root(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\.0$", "", text)
    m = re.search(r"(\d+)", text)
    if not m:
        return ""
    return m.group(1).zfill(2)[-2:]


def normalize_cameo_root_series(series: pd.Series) -> pd.Series:
    s = series.astype("string").fillna("")
    s = s.str.replace(r"\.0$", "", regex=True)
    digits = s.str.extract(r"(\d+)", expand=False).fillna("")
    return digits.str.zfill(2).str[-2:]


def row_get(row: pd.Series, *names: str) -> Any:
    index_map = {str(c).lower(): c for c in row.index}
    for name in names:
        if name in row.index:
            return row.get(name)
        lower = name.lower()
        if lower in index_map:
            return row.get(index_map[lower])
    return None


def stable_join(values: Iterable[str], sep: str = "|") -> str:
    seen: Set[str] = set()
    out: List[str] = []
    for value in values:
        text = clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return sep.join(out)


def counter_join(counter: Counter, max_items: int = 12) -> str:
    return "|".join([k for k, _ in counter.most_common(max_items) if clean_text(k)])


def import_pyarrow_parquet():
    try:
        import pyarrow.parquet as pq
        return pq
    except Exception:
        return None


def available_columns(path: Path) -> List[str]:
    pq = import_pyarrow_parquet()
    if pq is not None:
        schema = pq.read_schema(path)
        return list(schema.names)
    df = pd.read_parquet(path)
    return list(df.columns)


def read_parquet_batches(path: Path, columns: Sequence[str], batch_size: int) -> Iterator[pd.DataFrame]:
    pq = import_pyarrow_parquet()
    existing = available_columns(path)
    use_cols = [c for c in columns if c in existing]

    if pq is None:
        df = pd.read_parquet(path, columns=use_cols if use_cols else None)
        yield df
        return

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size, columns=use_cols if use_cols else None):
        yield batch.to_pandas()


# =============================================================================
# JSON parsing for GKG
# =============================================================================


class JsonFieldParser:
    """GKG themes_json / orgs_json / persons_json을 유연하게 파싱한다."""

    VALUE_KEYS = (
        "name", "Name", "text", "Text", "value", "Value",
        "theme", "Theme", "code", "Code", "organization", "person",
        "Organization", "Person", "word", "Word", "label", "Label",
    )

    @classmethod
    def parse_items(cls, value: Any) -> List[str]:
        if value is None:
            return []

        if isinstance(value, float) and math.isnan(value):
            return []

        if isinstance(value, (list, tuple, set)):
            return cls._flatten(value)

        if isinstance(value, dict):
            return cls._flatten(value)

        text = clean_text(value)
        if not text:
            return []

        # JSON string 우선 처리
        if text[:1] in {"[", "{"}:
            try:
                loaded = json.loads(text)
                return cls._flatten(loaded)
            except Exception:
                pass

        # fallback: 기존 raw GKG처럼 ;, | 로 연결된 경우
        parts = re.split(r"[;|]", text)
        out: List[str] = []
        for part in parts:
            item = clean_text(part)
            if not item:
                continue
            # GKG raw: THEME,offset 형태 방어
            item = item.split(",")[0].strip()
            if item:
                out.append(item)
        return cls._dedupe(out)

    @classmethod
    def _flatten(cls, obj: Any) -> List[str]:
        out: List[str] = []

        def walk(x: Any) -> None:
            if x is None:
                return
            if isinstance(x, float) and math.isnan(x):
                return
            if isinstance(x, str):
                text = clean_text(x)
                if text:
                    out.append(text)
                return
            if isinstance(x, (int, np.integer)):
                out.append(str(x))
                return
            if isinstance(x, (float, np.floating)):
                if not math.isnan(float(x)):
                    out.append(str(x))
                return
            if isinstance(x, (list, tuple, set)):
                for item in x:
                    walk(item)
                return
            if isinstance(x, dict):
                picked = False
                for key in cls.VALUE_KEYS:
                    if key in x:
                        walk(x.get(key))
                        picked = True
                if not picked:
                    for value in x.values():
                        walk(value)
                return
            text = clean_text(x)
            if text:
                out.append(text)

        walk(obj)
        return cls._dedupe(out)

    @staticmethod
    def _dedupe(values: Iterable[str]) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        for value in values:
            text = clean_text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out


# =============================================================================
# Token metadata
# =============================================================================


SECTOR_THEME: Dict[str, str] = {
    "SEMICONDUCTOR": "반도체 업황 관련 보도 증가",
    "BATTERY": "배터리 업황 관련 보도 증가",
    "SHIPPING": "해운·물류 관련 보도 증가",
    "AIRLINE": "항공 업황 관련 보도 증가",
    "AUTO": "자동차 업황 관련 보도 증가",
    "CHEMICAL": "화학 업황 관련 보도 증가",
    "STEEL": "철강·금속 관련 보도 증가",
    "BIO_PHARMA": "제약·바이오 관련 보도 증가",
    "CONTENT": "콘텐츠·엔터 업황 관련 보도 증가",
    "CONSTRUCTION": "건설·부동산 관련 보도 증가",
    "FINANCIAL_SECTOR": "금융업 관련 보도 증가",
    "CONSUMER_TOURISM": "소비·관광 관련 보도 증가",
    "TELECOM": "통신 업황 관련 보도 증가",
    "TECHNOLOGY": "기술 업황 관련 보도 증가",
}

SECTOR_TAG: Dict[str, str] = {
    "SEMICONDUCTOR": "sector_semiconductor",
    "BATTERY": "sector_battery",
    "SHIPPING": "sector_shipping",
    "AIRLINE": "sector_airline",
    "AUTO": "sector_auto",
    "CHEMICAL": "sector_chemical",
    "STEEL": "sector_steel",
    "BIO_PHARMA": "sector_bio_pharma",
    "CONTENT": "sector_content",
    "CONSTRUCTION": "sector_construction",
    "FINANCIAL_SECTOR": "sector_financial",
    "CONSUMER_TOURISM": "sector_consumer",
    "TELECOM": "sector_telecom",
    "TECHNOLOGY": "sector_technology",
}

SECTOR_SPECIFICITY: Dict[str, float] = {
    "SEMICONDUCTOR": 0.78,
    "BATTERY": 0.78,
    "SHIPPING": 0.74,
    "AIRLINE": 0.74,
    "AUTO": 0.72,
    "CHEMICAL": 0.72,
    "STEEL": 0.72,
    "BIO_PHARMA": 0.72,
    "CONTENT": 0.70,
    "CONSTRUCTION": 0.66,
    "FINANCIAL_SECTOR": 0.64,
    "CONSUMER_TOURISM": 0.62,
    "TELECOM": 0.62,
    "TECHNOLOGY": 0.58,
}

SECTOR_PRIORITY: List[str] = [
    "SEMICONDUCTOR", "BATTERY", "SHIPPING", "AIRLINE", "AUTO",
    "CHEMICAL", "STEEL", "BIO_PHARMA", "CONTENT", "CONSTRUCTION",
    "FINANCIAL_SECTOR", "CONSUMER_TOURISM", "TELECOM", "TECHNOLOGY",
]

FACTOR_THEME: Dict[str, str] = {
    "CHINA": "중국 경기·정책 관련 보도 증가",
    "TRADE": "교역·수출입 관련 보도 증가",
    "EXPORT": "수출 관련 보도 증가",
    "IMPORT": "수입 관련 보도 증가",
    "OIL": "유가·에너지 관련 보도 증가",
    "RATE": "금리·통화정책 관련 보도 증가",
    "INFLATION_PRICE": "물가 관련 보도 증가",
    "RAW_MATERIAL": "원자재 관련 보도 증가",
}

FACTOR_PROFILE_TAG: Dict[str, str] = {
    "CHINA": "exposure_china",
    "TRADE": "exposure_export",
    "EXPORT": "exposure_export",
    "IMPORT": "exposure_import",
    "OIL": "commodity_oil",
    "RATE": "rate_sensitive",
    "INFLATION_PRICE": "inflation_sensitive",
    "RAW_MATERIAL": "commodity_raw_material",
}

FACTOR_TAG: Dict[str, str] = {
    "CHINA": "factor_china",
    "TRADE": "factor_trade",
    "EXPORT": "factor_export",
    "IMPORT": "factor_import",
    "OIL": "factor_oil",
    "RATE": "factor_rate",
    "INFLATION_PRICE": "factor_inflation_price",
    "RAW_MATERIAL": "factor_raw_material",
}

FACTOR_SPECIFICITY: Dict[str, float] = {
    "CHINA": 0.42,
    "TRADE": 0.42,
    "EXPORT": 0.44,
    "IMPORT": 0.40,
    "OIL": 0.45,
    "RATE": 0.40,
    "INFLATION_PRICE": 0.38,
    "RAW_MATERIAL": 0.44,
}

FACTOR_PRIORITY: List[str] = [
    "OIL", "RAW_MATERIAL", "RATE", "INFLATION_PRICE",
    "TRADE", "EXPORT", "IMPORT", "CHINA",
]

MACRO_THEME: Dict[str, str] = {
    "BROAD_MACRO": "글로벌 경기 관련 보도 증가",
    "MARKET_GENERAL": "금융시장 전반 관련 보도 증가",
    "GEOPOLITICAL": "지정학적 리스크 관련 보도 증가",
    "POLICY_DIPLOMACY": "정책·외교 관련 보도 증가",
    "CONFLICT_SECURITY": "안보·충돌 리스크 관련 보도 증가",
    "SOCIAL_UNREST": "시위·사회 불안 관련 보도 증가",
    "SANCTION_PRESSURE": "제재·대외 압박 관련 보도 증가",
    "CAMEO_BROAD": "대외 이벤트 관련 보도 증가",
}

MACRO_TAG: Dict[str, str] = {
    "BROAD_MACRO": "macro_global_growth",
    "MARKET_GENERAL": "macro_market",
    "GEOPOLITICAL": "macro_geopolitical",
    "POLICY_DIPLOMACY": "macro_policy",
    "CONFLICT_SECURITY": "macro_geopolitical",
    "SOCIAL_UNREST": "macro_geopolitical",
    "SANCTION_PRESSURE": "macro_geopolitical",
    "CAMEO_BROAD": "macro_policy",
}

GENERIC_TOKENS: Set[str] = {
    "BUSINESS", "MARKET", "TECHNOLOGY_GENERIC", "POLICY_GENERIC", "ECONOMY_GENERIC",
}


# =============================================================================
# Mappers
# =============================================================================


@dataclass(frozen=True)
class RegexRule:
    pattern: str
    token: str
    flags: int = re.IGNORECASE

    def matches(self, text: str) -> bool:
        return bool(re.search(self.pattern, text, flags=self.flags))


class GkgThemeMapper:
    """GKG theme code를 canonical token으로 매핑한다."""

    @classmethod
    def map_theme_code(cls, code: str) -> Set[str]:
        c = clean_text(code).upper()
        if not c:
            return set()

        tokens: Set[str] = set()

        # rate / inflation / energy / trade
        if c in {"ECON_INTEREST_RATES", "EPU_CATS_MONETARY_POLICY", "EPU_POLICY_INTEREST_RATES"}:
            tokens.add("RATE")
        if "INTEREST_RATES" in c or "MONETARY_POLICY" in c or c.endswith("_RATE"):
            tokens.add("RATE")

        if c in {"TAX_ECON_PRICE"} or "INFLATION" in c or c.endswith("_CPI") or c.endswith("_PPI"):
            tokens.add("INFLATION_PRICE")

        if c in {"ENV_OIL", "WB_507_ENERGY_AND_EXTRACTIVES"} or "PETROLEUM" in c or "OIL" in c:
            tokens.add("OIL")

        if c in {"WB_698_TRADE"} or "TRADE" in c or "EXPORT" in c or "IMPORT" in c:
            tokens.add("TRADE")

        # sector-ish taxonomy
        if c in {"WB_1920_FINANCIAL_SECTOR_DEVELOPMENT", "EPU_CATS_FINANCIAL_REGULATION"}:
            tokens.add("FINANCIAL_SECTOR")

        if c in {"WB_895_MINING_SYSTEMS", "WB_1699_METAL_ORE_MINING", "WB_2937_SILVER", "ENV_COAL"}:
            tokens.add("RAW_MATERIAL")

        if c in {"WB_135_TRANSPORT"}:
            tokens.add("TRADE")

        if c in {"ECON_HOUSING_PRICES"} or "REAL_ESTATE" in c or "HOUSING" in c:
            tokens.add("CONSTRUCTION")

        if c in {"GENERAL_HEALTH", "MEDICAL", "UNGP_HEALTHCARE"} or "HEALTH" in c or "MEDICAL" in c:
            tokens.add("BIO_PHARMA")

        if c in {"WB_133_INFORMATION_AND_COMMUNICATION_TECHNOLOGIES"}:
            tokens.add("TECHNOLOGY")

        # country / exposure
        if c in {"TAX_ETHNICITY_CHINESE", "TAX_WORLDLANGUAGES_CHINESE"}:
            tokens.add("CHINA")
        if "CHINA" in c or "CHINESE" in c:
            tokens.add("CHINA")

        # broad macro
        if c in {"EPU_ECONOMY", "EPU_ECONOMY_HISTORIC", "WB_1104_MACROECONOMIC_VULNERABILITY_AND_DEBT"}:
            tokens.add("BROAD_MACRO")
        if c in {"ECON_STOCKMARKET"}:
            tokens.add("MARKET_GENERAL")
        if c in {"WB_2432_FRAGILITY_CONFLICT_AND_VIOLENCE", "WB_2433_CONFLICT_AND_VIOLENCE", "TERROR"}:
            tokens.add("GEOPOLITICAL")
        if "CONFLICT" in c or "VIOLENCE" in c or "TERROR" in c or "WAR" in c:
            tokens.add("GEOPOLITICAL")

        # explicit generic quarantine candidates
        if c in {"ECON", "BUSINESS", "TAX_FNCACT_BUSINESS", "MEDIA_MSM", "WB_678_DIGITAL_DEVELOPMENT"}:
            tokens.add("BUSINESS")

        return tokens

    @classmethod
    def map_many(cls, codes: Iterable[str]) -> Set[str]:
        out: Set[str] = set()
        for code in codes:
            out.update(cls.map_theme_code(code))
        return out


class OrgAnchorMapper:
    """GKG orgs_json을 sector confidence 보조 anchor로 사용한다. direct_stock은 만들지 않는다."""

    ORG_RULES: List[RegexRule] = [
        RegexRule(r"\b(SAMSUNG ELECTRONICS|SAMSUNG SEMICONDUCTOR|SK HYNIX|HYNIX|MICRON|TSMC|INTEL|NVIDIA)\b", "SEMICONDUCTOR"),
        RegexRule(r"\b(SAMSUNG SDI|LG ENERGY SOLUTION|LGES|SK ON|PANASONIC|CATL|LITHIUM)\b", "BATTERY"),
        RegexRule(r"\b(HMM|HANJIN|HYUNDAI MERCHANT MARINE|KOREA LINE|PAN OCEAN|MAERSK|COSCO)\b", "SHIPPING"),
        RegexRule(r"\b(ASIANA AIRLINES|KOREAN AIR|JEJU AIR|TWAY|JIN AIR|AIR BUSAN|DELTA AIR|UNITED AIRLINES)\b", "AIRLINE"),
        RegexRule(r"\b(HYUNDAI MOTOR|KIA|GM KOREA|RENAULT KOREA|TESLA|TOYOTA|VOLKSWAGEN|FORD)\b", "AUTO"),
        RegexRule(r"\b(LG CHEM|LOTTE CHEMICAL|KUMHO PETROCHEMICAL|HANWHA SOLUTIONS|SK INNOVATION|S-OIL|GS CALTEX)\b", "CHEMICAL"),
        RegexRule(r"\b(POSCO|POHANG IRON|HYUNDAI STEEL|DONGKUK STEEL|KISCO|NIPPON STEEL)\b", "STEEL"),
        RegexRule(r"\b(CELLTRION|SAMSUNG BIOLOGICS|SK BIOSCIENCE|YUHAN|HANMI PHARM|GREEN CROSS|GC PHARMA)\b", "BIO_PHARMA"),
        RegexRule(r"\b(HYBE|SM ENTERTAINMENT|YG ENTERTAINMENT|JYP|CJ ENM|NETMARBLE|NCSOFT|KAKAO GAMES)\b", "CONTENT"),
        RegexRule(r"\b(HYUNDAI ENGINEERING|GS ENGINEERING|DAEWOO E&C|SAMSUNG C&T|DL E&C|HDC HYUNDAI|LOTTE ENGINEERING)\b", "CONSTRUCTION"),
        RegexRule(r"\b(SHINHAN BANK|WOORI BANK|HANA BANK|KB KOOKMIN|KOOKMIN BANK|KAKAO BANK|MIRAE ASSET|SAMSUNG SECURITIES)\b", "FINANCIAL_SECTOR"),
        RegexRule(r"\b(HOTEL SHILLA|SHILLA|LOTTE SHOPPING|SHINSEGAE|HYUNDAI DEPARTMENT|AMOREPACIFIC|LG HOUSEHOLD|DUTY FREE)\b", "CONSUMER_TOURISM"),
        RegexRule(r"\b(SK TELECOM|KT CORP|LG UPLUS|LG U\+|KOREA TELECOM)\b", "TELECOM"),
        RegexRule(r"\b(NAVER|KAKAO|GOOGLE|MICROSOFT|APPLE|AMAZON|META)\b", "TECHNOLOGY"),
    ]

    @classmethod
    def map_orgs(cls, org_items: Iterable[str]) -> Set[str]:
        text = " ".join(clean_text(x) for x in org_items if clean_text(x)).upper()
        if not text:
            return set()
        tokens: Set[str] = set()
        for rule in cls.ORG_RULES:
            if rule.matches(text):
                tokens.add(rule.token)
        return tokens


class StrictKeywordMapper:
    """부분 문자열 오탐을 줄이기 위한 strict keyword mapper."""

    RULES: List[RegexRule] = [
        RegexRule(r"\bSEMICONDUCTOR(S)?\b|\bCHIP(S)?\b|\bMEMORY CHIP(S)?\b", "SEMICONDUCTOR"),
        RegexRule(r"\bBATTERY\b|\bBATTERIES\b|\bLITHIUM\b|\bSECONDARY BATTERY\b", "BATTERY"),
        RegexRule(r"\bSHIPPING\b|\bMARITIME\b|\bFREIGHT\b|\bCARGO\b|\bCONTAINER(S)?\b|\bPORTS?\b", "SHIPPING"),
        RegexRule(r"\bAIRLINE(S)?\b|\bAVIATION\b|\bAIRPORT(S)?\b|\bAIRCRAFT\b", "AIRLINE"),
        RegexRule(r"\bAUTO\b|\bAUTOMOBILE(S)?\b|\bVEHICLE(S)?\b|\bEVS?\b|\bELECTRIC VEHICLE(S)?\b", "AUTO"),
        RegexRule(r"\bCHEMICAL(S)?\b|\bPETROCHEMICAL(S)?\b|\bNAPHTHA\b|\bPLASTIC(S)?\b", "CHEMICAL"),
        RegexRule(r"\bSTEEL\b|\bIRON ORE\b|\bCOPPER\b|\bALUMINUM\b|\bMETAL(S)?\b", "STEEL"),
        RegexRule(r"\bBIO\b|\bPHARMA\b|\bPHARMACEUTICAL(S)?\b|\bDRUG(S)?\b|\bVACCINE(S)?\b", "BIO_PHARMA"),
        RegexRule(r"\bGAME(S)?\b|\bMEDIA\b|\bENTERTAINMENT\b|\bCONTENT(S)?\b|\bK-?POP\b|\bFILM(S)?\b", "CONTENT"),
        RegexRule(r"\bCONSTRUCTION\b|\bREAL ESTATE\b|\bHOUSING\b|\bPROPERTY\b|\bINFRASTRUCTURE\b", "CONSTRUCTION"),
        RegexRule(r"\bBANK(S)?\b|\bBANKING\b|\bINSURANCE\b|\bSECURITIES\b|\bBROKERAGE\b", "FINANCIAL_SECTOR"),
        RegexRule(r"\bCONSUMER\b|\bRETAIL\b|\bTOURISM\b|\bTRAVEL\b|\bCOSMETIC(S)?\b|\bDUTY FREE\b|\bHOTEL(S)?\b", "CONSUMER_TOURISM"),
        RegexRule(r"\bTELECOM\b|\bTELECOMMUNICATION(S)?\b|\b5G\b|\bMOBILE CARRIER(S)?\b", "TELECOM"),
        RegexRule(r"\bTECHNOLOGY\b|\bICT\b|\bSOFTWARE\b|\bAI\b|\bCLOUD\b", "TECHNOLOGY"),
        RegexRule(r"\bCHINA\b|\bCHINESE\b|\bBEIJING\b|\bSHANGHAI\b", "CHINA"),
        RegexRule(r"\bTRADE\b|\bTARIFF(S)?\b|\bCUSTOMS\b|\bSUPPLY CHAIN\b", "TRADE"),
        RegexRule(r"\bEXPORT(S)?\b|\bEXPORTED\b|\bEXPORTING\b", "EXPORT"),
        RegexRule(r"\bIMPORT(S)?\b|\bIMPORTED\b|\bIMPORTING\b", "IMPORT"),
        RegexRule(r"\bOIL\b|\bWTI\b|\bBRENT\b|\bPETROLEUM\b|\bCRUDE\b|\bFUEL\b", "OIL"),
        RegexRule(r"\bINTEREST RATE(S)?\b|\bMONETARY POLICY\b|\bFOMC\b|\bFED\b|\bCENTRAL BANK\b|\bBOND(S)?\b", "RATE"),
        RegexRule(r"\bINFLATION\b|\bCPI\b|\bPPI\b|\bCONSUMER PRICE(S)?\b|\bPRICE(S)?\b", "INFLATION_PRICE"),
        RegexRule(r"\bRAW MATERIAL(S)?\b|\bCOMMODITY\b|\bCOMMODITIES\b|\bCOAL\b|\bMINING\b", "RAW_MATERIAL"),
        RegexRule(r"\bGEOPOLITICAL\b|\bCONFLICT\b|\bWAR\b|\bSANCTION(S)?\b|\bTENSION(S)?\b", "GEOPOLITICAL"),
        RegexRule(r"\bPOLICY\b|\bDIPLOMACY\b|\bDIPLOMATIC\b|\bGOVERNMENT\b", "POLICY_DIPLOMACY"),
    ]

    @classmethod
    def map_text(cls, text: str) -> Set[str]:
        t = clean_text(text).upper()
        if not t:
            return set()
        tokens: Set[str] = set()
        for rule in cls.RULES:
            if rule.matches(t):
                tokens.add(rule.token)
        return tokens


class CameoMapper:
    """CAMEO는 broad_macro 전용. roots 01~05는 drop한다."""

    DROP_ROOTS: Set[str] = {"01", "02", "03", "04", "05"}

    ROOT_META: Dict[str, Tuple[str, str]] = {
        "06": ("POLICY_DIPLOMACY", "cameo_cooperation_material"),
        "07": ("POLICY_DIPLOMACY", "cameo_aid"),
        "08": ("POLICY_DIPLOMACY", "cameo_yield"),
        "09": ("GEOPOLITICAL", "cameo_investigate"),
        "10": ("SANCTION_PRESSURE", "cameo_demand"),
        "11": ("SANCTION_PRESSURE", "cameo_disapprove"),
        "12": ("SOCIAL_UNREST", "cameo_reject"),
        "13": ("CONFLICT_SECURITY", "cameo_threaten"),
        "14": ("GEOPOLITICAL", "cameo_protest"),
        "15": ("CONFLICT_SECURITY", "cameo_exhibit_force"),
        "16": ("SANCTION_PRESSURE", "cameo_reduce_relations"),
        "17": ("CONFLICT_SECURITY", "cameo_coerce"),
        "18": ("CONFLICT_SECURITY", "cameo_assault"),
        "19": ("CONFLICT_SECURITY", "cameo_fight"),
        "20": ("CONFLICT_SECURITY", "cameo_mass_violence"),
    }

    @classmethod
    def map_root(cls, root: str) -> Tuple[Set[str], str]:
        r = normalize_cameo_root(root)
        if not r or r in cls.DROP_ROOTS:
            return set(), "cameo_root_dropped_01_05"
        token, reason = cls.ROOT_META.get(r, ("CAMEO_BROAD", "cameo_broad_other"))
        return {token}, reason


class KoreaRelevance:
    KOREA_CODES = {"KS", "KR", "KOR", "ROK"}
    CHINA_CODES = {"CH", "CN", "CHN", "PRC"}
    KOREA_TEXT = r"\b(?:KOREA|SOUTH KOREA|SEOUL|KOREAN)\b"
    @classmethod
    def is_korea_text(cls, text: str) -> bool:
        return bool(cls.KOREA_TEXT.search(clean_text(text)))

    @classmethod
    def is_korea_code(cls, value: Any) -> bool:
        return clean_text(value).upper() in cls.KOREA_CODES

    @classmethod
    def is_china_code(cls, value: Any) -> bool:
        return clean_text(value).upper() in cls.CHINA_CODES


# =============================================================================
# Record-level classification
# =============================================================================


@dataclass
class ClassifiedRecord:
    date: str
    theme: str
    evidence_class: str
    source: str
    scope: str
    tone: Optional[float]
    weight: float

    sector_tags: List[str]
    factor_tags: List[str]
    exposure_tags: List[str]
    macro_tags: List[str]
    hard_match_tags: List[str]
    modifier_tags: List[str]
    profile_factor_tags: List[str]

    matched_tokens: List[str]
    strong_tokens: List[str]
    generic_tokens: List[str]
    cooccurrence_rules: List[str]

    org_anchor_detected: bool
    korea_related: bool

    specificity_score: float
    stock_link_score: float
    confidence_level: str

    forbidden_as_standalone_evidence: bool
    requires_profile_match: bool
    requires_corroboration: bool
    usage_rule: str
    reason_code: str


class RecordClassifier:
    def classify(
        self,
        date: str,
        source: str,
        tokens: Set[str],
        tone: Optional[float],
        weight: float,
        korea_related: bool,
        org_anchor_detected: bool,
        reason_hints: Optional[Sequence[str]] = None,
        scope: Optional[str] = None,
    ) -> Optional[ClassifiedRecord]:
        date = clean_text(date)
        if not date:
            return None

        reason_parts: List[str] = [r for r in (reason_hints or []) if clean_text(r)]
        cleaned_tokens = {clean_text(t).upper() for t in tokens if clean_text(t)}

        sector_tokens = cleaned_tokens & set(SECTOR_THEME.keys())
        factor_tokens = cleaned_tokens & set(FACTOR_THEME.keys())
        macro_tokens = cleaned_tokens & set(MACRO_THEME.keys())
        generic_tokens = cleaned_tokens & GENERIC_TOKENS

        # generic alias가 canonical dict에는 없으므로 보정
        if cleaned_tokens & {"BUSINESS", "MARKET"}:
            generic_tokens.update(cleaned_tokens & {"BUSINESS", "MARKET"})

        co_rules = self._build_cooccurrence_rules(sector_tokens, factor_tokens, macro_tokens)

        # tie-break: sector anchor가 있으면 sector_linkable
        if sector_tokens:
            evidence_class = "sector_linkable"
            selected = self._pick_first(SECTOR_PRIORITY, sector_tokens)
            theme = SECTOR_THEME[selected]
            reason_parts.append("hard_sector_anchor")
            if factor_tokens:
                reason_parts.append("sector_with_factor_modifier")

        # sector 없이 factor만 있으면 company_profile_dependent
        elif factor_tokens:
            # CHINA + diplomacy/conflict/policy only는 broad_macro로 내린다.
            if macro_tokens and factor_tokens == {"CHINA"}:
                evidence_class = "broad_macro"
                selected_macro = self._pick_macro(macro_tokens)
                theme = MACRO_THEME[selected_macro]
                reason_parts.append("china_with_macro_only_demoted_to_broad")
            else:
                evidence_class = "company_profile_dependent"
                selected_factor = self._pick_first(FACTOR_PRIORITY, factor_tokens)
                theme = FACTOR_THEME[selected_factor]
                reason_parts.append("profile_factor_without_sector_anchor")

        elif macro_tokens:
            evidence_class = "broad_macro"
            selected_macro = self._pick_macro(macro_tokens)
            theme = MACRO_THEME[selected_macro]
            reason_parts.append("macro_only")

        elif generic_tokens:
            evidence_class = "noisy"
            theme = "일반 경제·시장 관련 저신뢰 보도"
            reason_parts.append("generic_only")

        else:
            evidence_class = "noisy"
            theme = "분류 불가 저신뢰 보도"
            reason_parts.append("no_useful_token")

        sector_tags = [SECTOR_TAG[t] for t in self._ordered(SECTOR_PRIORITY, sector_tokens) if t in SECTOR_TAG]
        factor_tags = [FACTOR_TAG[t] for t in self._ordered(FACTOR_PRIORITY, factor_tokens) if t in FACTOR_TAG]
        profile_factor_tags = [FACTOR_PROFILE_TAG[t] for t in self._ordered(FACTOR_PRIORITY, factor_tokens) if t in FACTOR_PROFILE_TAG]
        exposure_tags = [tag for tag in profile_factor_tags if tag.startswith("exposure_") or tag.startswith("commodity_")]
        macro_tags = [MACRO_TAG[t] for t in self._ordered(list(MACRO_THEME.keys()), macro_tokens) if t in MACRO_TAG]

        hard_match_tags = list(sector_tags)
        modifier_tags = profile_factor_tags if evidence_class == "sector_linkable" else []

        strong_tokens = self._ordered(SECTOR_PRIORITY + FACTOR_PRIORITY, sector_tokens | factor_tokens)
        matched_tokens = self._ordered(SECTOR_PRIORITY + FACTOR_PRIORITY + list(MACRO_THEME.keys()), cleaned_tokens)

        specificity = self._specificity(evidence_class, sector_tokens, factor_tokens, macro_tokens, generic_tokens, org_anchor_detected, co_rules, korea_related)
        stock_score = self._stock_link_score(evidence_class, specificity, weight, org_anchor_detected, co_rules, korea_related, generic_tokens)
        confidence = self._confidence(stock_score)

        forbidden = evidence_class in {"company_profile_dependent", "broad_macro", "noisy"}
        requires_profile = evidence_class == "company_profile_dependent"
        requires_corroboration = evidence_class in {"company_profile_dependent", "broad_macro"} or confidence in {"low", "medium"}

        usage_rule = self._usage_rule(evidence_class)

        if evidence_class == "noisy" and not reason_parts:
            reason_parts.append("noisy")

        return ClassifiedRecord(
            date=date,
            theme=theme,
            evidence_class=evidence_class,
            source=source,
            scope=scope or ("korea_related_macro" if korea_related else "global_macro_background"),
            tone=tone,
            weight=float(weight),
            sector_tags=sector_tags,
            factor_tags=factor_tags,
            exposure_tags=exposure_tags,
            macro_tags=macro_tags,
            hard_match_tags=hard_match_tags,
            modifier_tags=modifier_tags,
            profile_factor_tags=profile_factor_tags,
            matched_tokens=matched_tokens,
            strong_tokens=strong_tokens,
            generic_tokens=self._ordered(list(GENERIC_TOKENS), generic_tokens),
            cooccurrence_rules=co_rules,
            org_anchor_detected=org_anchor_detected,
            korea_related=korea_related,
            specificity_score=round(specificity, 4),
            stock_link_score=round(stock_score, 4),
            confidence_level=confidence,
            forbidden_as_standalone_evidence=forbidden,
            requires_profile_match=requires_profile,
            requires_corroboration=requires_corroboration,
            usage_rule=usage_rule,
            reason_code=stable_join(reason_parts),
        )

    @staticmethod
    def _pick_first(priority: Sequence[str], values: Set[str]) -> str:
        for p in priority:
            if p in values:
                return p
        return sorted(values)[0]

    @staticmethod
    def _pick_macro(values: Set[str]) -> str:
        priority = ["CONFLICT_SECURITY", "SANCTION_PRESSURE", "SOCIAL_UNREST", "GEOPOLITICAL", "POLICY_DIPLOMACY", "MARKET_GENERAL", "BROAD_MACRO", "CAMEO_BROAD"]
        for p in priority:
            if p in values:
                return p
        return sorted(values)[0]

    @staticmethod
    def _ordered(priority: Sequence[str], values: Set[str]) -> List[str]:
        out = [p for p in priority if p in values]
        out.extend(sorted(values - set(out)))
        return out

    @staticmethod
    def _build_cooccurrence_rules(sector_tokens: Set[str], factor_tokens: Set[str], macro_tokens: Set[str]) -> List[str]:
        rules: List[str] = []
        for sector in sorted(sector_tokens):
            for factor in sorted(factor_tokens):
                rules.append(f"{factor}+{sector}")
        if sector_tokens:
            rules.append("hard_sector_anchor_present")
        if factor_tokens and not sector_tokens:
            rules.append("profile_factor_only")
        if macro_tokens and not sector_tokens and not factor_tokens:
            rules.append("macro_only")
        return rules

    @staticmethod
    def _specificity(
        evidence_class: str,
        sector_tokens: Set[str],
        factor_tokens: Set[str],
        macro_tokens: Set[str],
        generic_tokens: Set[str],
        org_anchor_detected: bool,
        co_rules: List[str],
        korea_related: bool,
    ) -> float:
        if evidence_class == "sector_linkable":
            base = max(SECTOR_SPECIFICITY.get(t, 0.58) for t in sector_tokens)
        elif evidence_class == "company_profile_dependent":
            base = max(FACTOR_SPECIFICITY.get(t, 0.38) for t in factor_tokens)
        elif evidence_class == "broad_macro":
            base = 0.24
        else:
            base = 0.08

        if org_anchor_detected:
            base += 0.08
        if any("+" in r for r in co_rules):
            base += 0.06
        if korea_related:
            base += 0.03
        if generic_tokens and evidence_class != "sector_linkable":
            base -= 0.12

        cap = {
            "sector_linkable": 0.92,
            "company_profile_dependent": 0.52,
            "broad_macro": 0.34,
            "noisy": 0.16,
        }.get(evidence_class, 0.20)

        return clamp(base, 0.0, cap)

    @staticmethod
    def _stock_link_score(
        evidence_class: str,
        specificity: float,
        weight: float,
        org_anchor_detected: bool,
        co_rules: List[str],
        korea_related: bool,
        generic_tokens: Set[str],
    ) -> float:
        # record-level에서는 volume bonus를 작게만 준다. 최종 집계에서 다시 보정한다.
        volume_bonus = min(0.04, 0.012 * math.log1p(max(weight, 0.0)))
        score = specificity + volume_bonus

        if org_anchor_detected:
            score += 0.03
        if any("+" in r for r in co_rules):
            score += 0.03
        if korea_related:
            score += 0.02
        if generic_tokens and evidence_class != "sector_linkable":
            score -= 0.10

        cap = {
            # record-level score is only a GDELT prior.
            # Final stock attachment must be decided in pr05a with profile/DART/price/community evidence.
            "sector_linkable": 0.72,
            "company_profile_dependent": 0.50,
            "broad_macro": 0.32,
            "noisy": 0.15,
        }.get(evidence_class, 0.20)

        return clamp(score, 0.0, cap)

    @staticmethod
    def _confidence(score: float) -> str:
        if score >= 0.72:
            return "high"
        if score >= 0.42:
            return "medium"
        return "low"

    @staticmethod
    def _usage_rule(evidence_class: str) -> str:
        if evidence_class == "sector_linkable":
            return "attach_only_if_hard_match_tags_intersect_stock_sensitivity_tags"
        if evidence_class == "company_profile_dependent":
            return "profile_match_required_and_never_standalone"
        if evidence_class == "broad_macro":
            return "background_only_no_direct_causation"
        return "do_not_use_quarantine_review_only"


# =============================================================================
# Accumulator and output split
# =============================================================================


@dataclass
class AggregateBucket:
    date: str
    theme: str
    evidence_class: str

    source_counter: Counter = field(default_factory=Counter)
    scope_counter: Counter = field(default_factory=Counter)
    raw_count: int = 0
    weighted_count: float = 0.0
    tone_sum: float = 0.0
    tone_count: int = 0

    sector_tags: Counter = field(default_factory=Counter)
    factor_tags: Counter = field(default_factory=Counter)
    exposure_tags: Counter = field(default_factory=Counter)
    macro_tags: Counter = field(default_factory=Counter)
    hard_match_tags: Counter = field(default_factory=Counter)
    modifier_tags: Counter = field(default_factory=Counter)
    profile_factor_tags: Counter = field(default_factory=Counter)
    matched_tokens: Counter = field(default_factory=Counter)
    strong_tokens: Counter = field(default_factory=Counter)
    generic_tokens: Counter = field(default_factory=Counter)
    cooccurrence_rules: Counter = field(default_factory=Counter)
    reason_code: Counter = field(default_factory=Counter)

    org_anchor_count: int = 0
    korea_related_count: int = 0

    specificity_weighted_sum: float = 0.0
    stock_score_weighted_sum: float = 0.0

    forbidden_any: bool = False
    requires_profile_any: bool = False
    requires_corroboration_any: bool = False

    def add(self, record: ClassifiedRecord) -> None:
        self.raw_count += 1
        self.weighted_count += record.weight
        self.source_counter[record.source] += 1
        self.scope_counter[record.scope] += 1

        if record.tone is not None and not pd.isna(record.tone):
            self.tone_sum += float(record.tone)
            self.tone_count += 1

        self._add_many(self.sector_tags, record.sector_tags)
        self._add_many(self.factor_tags, record.factor_tags)
        self._add_many(self.exposure_tags, record.exposure_tags)
        self._add_many(self.macro_tags, record.macro_tags)
        self._add_many(self.hard_match_tags, record.hard_match_tags)
        self._add_many(self.modifier_tags, record.modifier_tags)
        self._add_many(self.profile_factor_tags, record.profile_factor_tags)
        self._add_many(self.matched_tokens, record.matched_tokens)
        self._add_many(self.strong_tokens, record.strong_tokens)
        self._add_many(self.generic_tokens, record.generic_tokens)
        self._add_many(self.cooccurrence_rules, record.cooccurrence_rules)
        self._add_many(self.reason_code, record.reason_code.split("|") if record.reason_code else [])

        self.org_anchor_count += int(record.org_anchor_detected)
        self.korea_related_count += int(record.korea_related)

        w = max(record.weight, 1e-9)
        self.specificity_weighted_sum += record.specificity_score * w
        self.stock_score_weighted_sum += record.stock_link_score * w

        self.forbidden_any = self.forbidden_any or record.forbidden_as_standalone_evidence
        self.requires_profile_any = self.requires_profile_any or record.requires_profile_match
        self.requires_corroboration_any = self.requires_corroboration_any or record.requires_corroboration

    @staticmethod
    def _add_many(counter: Counter, values: Iterable[str]) -> None:
        for value in values:
            text = clean_text(value)
            if text:
                counter[text] += 1

    def to_row(self) -> Dict[str, Any]:
        avg_tone: Any = ""
        if self.tone_count > 0:
            avg_tone = round(self.tone_sum / self.tone_count, 5)

        base_specificity = self.specificity_weighted_sum / max(self.weighted_count, 1e-9)
        base_stock_score = self.stock_score_weighted_sum / max(self.weighted_count, 1e-9)

        # Aggregated scores are still GDELT-only priors, not final stock causality.
        # Keep three meanings separate:
        # - context_specificity_score: how specific the GDELT context is.
        # - gdelt_signal_strength: how repeatedly/strongly it appears in GDELT.
        # - stock_attach_prior: conservative prior before pr05a profile/DART/price/community gates.
        # v3 calibration:
        # v1 was too aggressive: single-record sector signals became high.
        # v2 was too conservative: even repeated sector signals mostly stayed low.
        # This version keeps raw_count=1 capped, but lets repeated + anchored sector context rise to medium/high.
        volume_bonus = min(0.10, 0.020 * math.log1p(self.raw_count) + 0.008 * math.log1p(self.weighted_count))
        repeat_bonus = min(0.16, 0.055 * math.log1p(self.raw_count))
        source_bonus = min(0.04, 0.01 * max(0, len(self.source_counter) - 1))
        org_bonus = 0.055 if self.org_anchor_count > 0 else 0.0
        korea_bonus = 0.025 if self.korea_related_count > 0 else 0.0
        cooccur_bonus = 0.035 if self.cooccurrence_rules else 0.0

        context_specificity_score = clamp(base_specificity + min(0.08, volume_bonus * 0.5), 0.0, 0.95)
        gdelt_signal_strength = clamp(
            0.62 * base_stock_score
            + volume_bonus
            + repeat_bonus
            + source_bonus
            + org_bonus
            + korea_bonus
            + cooccur_bonus,
            0.0,
            0.95,
        )

        score = gdelt_signal_strength

        if self.evidence_class == "sector_linkable":
            if self.raw_count <= 1:
                # Single GDELT record is a useful candidate, not a confirmed strong stock context.
                cap = 0.52 if self.org_anchor_count > 0 else 0.47
            elif self.raw_count == 2:
                cap = 0.62 if self.org_anchor_count > 0 else 0.57
            elif self.raw_count < 5:
                cap = 0.70 if self.org_anchor_count > 0 else 0.64
            elif self.raw_count < 10:
                cap = 0.78 if self.org_anchor_count > 0 else 0.70
            else:
                cap = 0.84 if self.org_anchor_count > 0 else 0.76
        elif self.evidence_class == "company_profile_dependent":
            cap = 0.55
        elif self.evidence_class == "broad_macro":
            cap = 0.35
        else:
            cap = 0.15

        score = clamp(score, 0.0, cap)

        if self.evidence_class == "sector_linkable":
            if (score >= 0.68 and self.raw_count >= 10) or (score >= 0.66 and self.raw_count >= 5 and self.org_anchor_count > 0):
                confidence = "high"
            elif score >= 0.50 or self.raw_count >= 2:
                confidence = "medium"
            else:
                confidence = "low"
        else:
            confidence = "high" if score >= 0.72 else "medium" if score >= 0.42 else "low"

        # GDELT-only evidence always needs downstream confirmation for actual stock-news attachment.
        forbidden = self.evidence_class in {"company_profile_dependent", "broad_macro", "noisy"}
        requires_profile = self.evidence_class == "company_profile_dependent"
        requires_corroboration = self.evidence_class in {"sector_linkable", "company_profile_dependent", "broad_macro"}

        return {
            "date": self.date,
            "theme": self.theme,
            "evidence_class": self.evidence_class,
            "confidence_level": confidence,
            # Backward-compatible aliases. Prefer stock_attach_prior/context_specificity_score in new downstream code.
            "stock_link_score": round(score, 4),
            "specificity_score": round(context_specificity_score, 4),
            "context_specificity_score": round(context_specificity_score, 4),
            "gdelt_signal_strength": round(gdelt_signal_strength, 4),
            "stock_attach_prior": round(score, 4),
            "sector_tags": counter_join(self.sector_tags),
            "factor_tags": counter_join(self.factor_tags),
            "exposure_tags": counter_join(self.exposure_tags),
            "macro_tags": counter_join(self.macro_tags),
            "hard_match_tags": counter_join(self.hard_match_tags),
            "modifier_tags": counter_join(self.modifier_tags),
            "profile_factor_tags": counter_join(self.profile_factor_tags),
            "matched_tokens": counter_join(self.matched_tokens, 20),
            "strong_tokens": counter_join(self.strong_tokens, 20),
            "generic_tokens": counter_join(self.generic_tokens, 20),
            "cooccurrence_rules": counter_join(self.cooccurrence_rules, 20),
            "org_anchor_detected": bool(self.org_anchor_count > 0),
            "korea_related": bool(self.korea_related_count > 0),
            "scope": self.scope_counter.most_common(1)[0][0] if self.scope_counter else "global_macro_background",
            "source": "+".join([src for src, _ in self.source_counter.most_common()]),
            "raw_count": int(self.raw_count),
            "weighted_count": round(float(self.weighted_count), 3),
            "avg_tone": avg_tone,
            "forbidden_as_standalone_evidence": forbidden,
            "requires_profile_match": requires_profile,
            "requires_corroboration": requires_corroboration,
            "usage_rule": RecordClassifier._usage_rule(self.evidence_class),
            "reason_code": counter_join(self.reason_code, 20),
        }


class DailyContextAccumulator:
    OUTPUT_COLUMNS = [
        "date", "theme", "evidence_class", "confidence_level",
        "stock_link_score", "specificity_score",
        "context_specificity_score", "gdelt_signal_strength", "stock_attach_prior",
        "sector_tags", "factor_tags", "exposure_tags", "macro_tags",
        "hard_match_tags", "modifier_tags", "profile_factor_tags",
        "matched_tokens", "strong_tokens", "generic_tokens", "cooccurrence_rules",
        "org_anchor_detected", "korea_related", "scope", "source",
        "raw_count", "weighted_count", "avg_tone",
        "forbidden_as_standalone_evidence", "requires_profile_match", "requires_corroboration",
        "usage_rule", "reason_code",
    ]

    def __init__(self):
        self.buckets: Dict[Tuple[str, str, str, str, str, str], AggregateBucket] = {}
        self.record_counter: Counter = Counter()

    def add(self, record: ClassifiedRecord) -> None:
        key = (
            record.date,
            record.theme,
            record.evidence_class,
            stable_join(record.hard_match_tags),
            stable_join(record.profile_factor_tags),
            stable_join(record.macro_tags),
        )
        if key not in self.buckets:
            self.buckets[key] = AggregateBucket(record.date, record.theme, record.evidence_class)
        self.buckets[key].add(record)
        self.record_counter[record.evidence_class] += 1

    def to_dataframe(self) -> pd.DataFrame:
        rows = [bucket.to_row() for bucket in self.buckets.values()]
        if not rows:
            return pd.DataFrame(columns=self.OUTPUT_COLUMNS)
        df = pd.DataFrame(rows)
        for col in self.OUTPUT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[self.OUTPUT_COLUMNS]


class OutputSplitter:
    def __init__(
        self,
        score_threshold: float,
        max_contexts_per_day: int,
        max_broad_per_day: int,
        keep_broad_macro: bool,
    ):
        self.score_threshold = score_threshold
        self.max_contexts_per_day = max_contexts_per_day
        self.max_broad_per_day = max_broad_per_day
        self.keep_broad_macro = keep_broad_macro

    def split(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        v4 원칙:
            - summary(main)는 사용 가능한 전체 후보 pool이다.
            - max_contexts_per_day는 summary를 자르지 않는다.
            - quarantine은 실제로 제외할 후보만 담는다.
            - 날짜별 top-N은 사람이 검수하기 위한 ranked_preview로만 별도 생성한다.
        """
        if df.empty:
            empty = df.copy()
            return empty, empty, empty

        df = df.copy()
        df["_priority"] = df["evidence_class"].map({
            "sector_linkable": 0,
            "company_profile_dependent": 1,
            "broad_macro": 2,
            "noisy": 9,
        }).fillna(9)

        quarantine_reasons: List[str] = []
        keep_mask: List[bool] = []

        for _, row in df.iterrows():
            keep, reason = self._keep_decision(row)
            keep_mask.append(keep)
            quarantine_reasons.append(reason)

        df["quarantine_reason"] = quarantine_reasons
        main = df.loc[keep_mask].copy()
        quarantine = df.loc[[not x for x in keep_mask]].copy()

        main = main.sort_values(
            ["date", "_priority", "stock_link_score", "weighted_count", "raw_count"],
            ascending=[True, True, False, False, False],
        )

        ranked_preview = self._build_ranked_preview(main)

        main = main.drop(columns=["_priority", "quarantine_reason"], errors="ignore")

        quarantine = quarantine.sort_values(
            ["date", "_priority", "stock_link_score", "weighted_count", "raw_count"],
            ascending=[True, True, False, False, False],
        ).drop(columns=["_priority"], errors="ignore")

        ranked_preview = ranked_preview.drop(columns=["_priority", "quarantine_reason"], errors="ignore")

        return main.reset_index(drop=True), quarantine.reset_index(drop=True), ranked_preview.reset_index(drop=True)

    def _keep_decision(self, row: pd.Series) -> Tuple[bool, str]:
        evidence_class = clean_text(row.get("evidence_class"))
        score = float(row.get("stock_link_score") or 0.0)
        raw_count = int(row.get("raw_count") or 0)
        hard_tags = clean_text(row.get("hard_match_tags"))
        profile_tags = clean_text(row.get("profile_factor_tags"))
        reason = clean_text(row.get("reason_code"))

        if evidence_class == "noisy":
            return False, "noisy"

        if "generic_only" in reason:
            return False, "generic_only"

        if evidence_class == "sector_linkable":
            if not hard_tags:
                return False, "sector_linkable_without_hard_match_tags"
            if score < self.score_threshold and raw_count <= 1:
                # raw_count=1 + strong sector anchor는 보존한다.
                return True, "kept_single_strong_sector_anchor"
            return True, "kept_sector_linkable"

        if evidence_class == "company_profile_dependent":
            if not profile_tags:
                return False, "profile_dependent_without_profile_factor_tags"
            if raw_count <= 1:
                return False, "single_weak_profile_factor"
            if score < self.score_threshold:
                return False, "low_score_profile_factor"
            return True, "kept_profile_dependent"

        if evidence_class == "broad_macro":
            if not self.keep_broad_macro:
                return False, "broad_macro_not_kept"
            # broad는 score가 낮아도 background로 제한 보관한다.
            return True, "kept_broad_background_only"

        if score < self.score_threshold:
            return False, "low_stock_link_score"

        return True, "kept_other"

    def _build_ranked_preview(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df.copy()

        # 0 이하이면 preview도 자르지 않는다.
        if self.max_contexts_per_day <= 0:
            return df.copy()

        selected: List[pd.DataFrame] = []
        sort_cols = ["_priority", "stock_link_score", "weighted_count", "raw_count"]

        for _, part in df.groupby("date", sort=True):
            part = part.sort_values(sort_cols, ascending=[True, False, False, False])

            if self.max_broad_per_day >= 0:
                broad = part[part["evidence_class"].eq("broad_macro")].head(self.max_broad_per_day)
                non_broad = part[~part["evidence_class"].eq("broad_macro")]
                merged = pd.concat([non_broad, broad], ignore_index=False)
            else:
                merged = part

            merged = merged.sort_values(sort_cols, ascending=[True, False, False, False]).head(self.max_contexts_per_day)
            selected.append(merged)

        if not selected:
            return df.iloc[0:0].copy()

        return pd.concat(selected, ignore_index=False)


# =============================================================================
# GKG processor
# =============================================================================


class GkgProcessor:
    COLUMNS = [
        "ref_date", "published_at", "lang_code", "source_name", "domain", "url",
        "themes_json", "persons_json", "orgs_json", "tone_score",
        # legacy fallback
        "DATE", "SourceCommonName", "DocumentIdentifier", "V2Themes", "Themes",
        "Organizations", "Persons", "V2Tone", "Tone", "themes_raw", "orgs_raw", "persons_raw",
    ]

    def __init__(self, accumulator: DailyContextAccumulator, classifier: RecordClassifier):
        self.accumulator = accumulator
        self.classifier = classifier

    def process_file(self, path: Path, batch_size: int, max_rows_per_file: Optional[int]) -> int:
        processed = 0
        for batch_df in read_parquet_batches(path, self.COLUMNS, batch_size=batch_size):
            if max_rows_per_file is not None:
                remaining = max_rows_per_file - processed
                if remaining <= 0:
                    break
                batch_df = batch_df.head(remaining)
            processed += len(batch_df)
            self._process_batch(batch_df)
        return processed

    def _process_batch(self, df: pd.DataFrame) -> None:
        if df.empty:
            return

        for _, row in df.iterrows():
            date = normalize_date_value(row_get(row, "ref_date", "DATE", "published_at"))
            if not date:
                continue

            theme_items = JsonFieldParser.parse_items(row_get(row, "themes_json", "themes_raw", "V2Themes", "Themes"))
            org_items = JsonFieldParser.parse_items(row_get(row, "orgs_json", "orgs_raw", "Organizations"))
            person_items = JsonFieldParser.parse_items(row_get(row, "persons_json", "persons_raw", "Persons"))

            if not theme_items and not org_items:
                continue

            tokens: Set[str] = set()
            tokens.update(GkgThemeMapper.map_many(theme_items))

            org_tokens = OrgAnchorMapper.map_orgs(org_items)
            tokens.update(org_tokens)

            # GKG keyword는 URL/domain 제외. theme/org/person text만 보조로 사용한다.
            keyword_text = " ".join(theme_items + org_items + person_items)
            tokens.update(StrictKeywordMapper.map_text(keyword_text))

            korea_related = (
                any("KOREA" in clean_text(x).upper() or "KOREAN" in clean_text(x).upper() or "SEOUL" in clean_text(x).upper() for x in theme_items + org_items + person_items)
                or KoreaRelevance.is_korea_text(keyword_text)
            )

            tone = safe_float(row_get(row, "tone_score", "V2Tone", "Tone"))
            org_anchor_detected = bool(org_tokens)

            if not tokens:
                tokens = {"BUSINESS"}

            record = self.classifier.classify(
                date=date,
                source="GDELT_GKG",
                tokens=tokens,
                tone=tone,
                weight=1.0,
                korea_related=korea_related,
                org_anchor_detected=org_anchor_detected,
                reason_hints=["gkg_theme_json" if theme_items else "gkg_no_theme", "gkg_org_anchor" if org_anchor_detected else ""],
            )
            if record is not None:
                self.accumulator.add(record)


# =============================================================================
# EVENTS processor
# =============================================================================


class EventsProcessor:
    COLUMNS = [
        "event_id", "event_date", "actor1_name", "actor1_country", "actor1_type",
        "actor2_name", "actor2_country", "actor2_type",
        "actor1_geo_country", "actor2_geo_country", "action_geo_country",
        "cameo_code", "cameo_base", "cameo_root",
        "goldstein_scale", "num_mentions", "num_sources", "num_articles", "avg_tone", "source_url",
        # raw fallback
        "SQLDATE", "Actor1Name", "Actor1CountryCode", "Actor1Type1Code",
        "Actor2Name", "Actor2CountryCode", "Actor2Type1Code",
        "EventRootCode", "EventCode", "GoldsteinScale",
        "NumMentions", "NumSources", "NumArticles", "AvgTone", "SOURCEURL",
        "Actor1Geo_CountryCode", "Actor2Geo_CountryCode", "ActionGeo_CountryCode",
    ]

    def __init__(
        self,
        accumulator: DailyContextAccumulator,
        classifier: RecordClassifier,
        event_filter_mode: str,
        min_event_mentions: float,
        min_event_articles: float,
        min_abs_goldstein: float,
        include_korea_related_events: bool,
    ):
        self.accumulator = accumulator
        self.classifier = classifier
        self.event_filter_mode = event_filter_mode
        self.min_event_mentions = min_event_mentions
        self.min_event_articles = min_event_articles
        self.min_abs_goldstein = min_abs_goldstein
        self.include_korea_related_events = include_korea_related_events

    def process_file(self, path: Path, batch_size: int, max_rows_per_file: Optional[int]) -> int:
        processed = 0
        for batch_df in read_parquet_batches(path, self.COLUMNS, batch_size=batch_size):
            if max_rows_per_file is not None:
                remaining = max_rows_per_file - processed
                if remaining <= 0:
                    break
                batch_df = batch_df.head(remaining)
            processed += len(batch_df)
            self._process_batch(batch_df)
        return processed

    def _process_batch(self, df: pd.DataFrame) -> None:
        if df.empty:
            return

        df = self._standardize_columns(df)
        if df.empty:
            return

        df["date"] = normalize_date_series(df["event_date"])
        df["cameo_root_norm"] = normalize_cameo_root_series(df["cameo_root"])
        df["goldstein"] = pd.to_numeric(df["goldstein_scale"], errors="coerce").fillna(0.0)
        df["mentions"] = pd.to_numeric(df["num_mentions"], errors="coerce").fillna(0.0)
        df["articles"] = pd.to_numeric(df["num_articles"], errors="coerce").fillna(0.0)
        df["tone"] = pd.to_numeric(df["avg_tone"], errors="coerce")

        korea_related = self._korea_related_mask(df)
        high_impact = (
            df["mentions"].ge(self.min_event_mentions)
            | df["articles"].ge(self.min_event_articles)
            | df["goldstein"].abs().ge(self.min_abs_goldstein)
        )

        if self.event_filter_mode == "high_impact":
            mask = high_impact
            if self.include_korea_related_events:
                mask = mask | korea_related
            df = df.loc[mask].copy()
            korea_related = korea_related.loc[df.index]
        elif self.event_filter_mode == "korea_or_high_impact":
            mask = korea_related | high_impact
            df = df.loc[mask].copy()
            korea_related = korea_related.loc[df.index]
        else:
            df = df.copy()
            korea_related = korea_related.loc[df.index]

        if df.empty:
            return

        weights = self._event_weight(df, korea_related)

        for idx, row in df.iterrows():
            date = clean_text(row.get("date"))
            if not date:
                continue

            actor_text = " ".join([
                clean_text(row.get("actor1_name")),
                clean_text(row.get("actor2_name")),
                clean_text(row.get("actor1_type")),
                clean_text(row.get("actor2_type")),
            ])

            tokens: Set[str] = set()
            tokens.update(StrictKeywordMapper.map_text(actor_text))

            # country code는 CHINA/KOREA 노출 factor로만 사용한다.
            country_values = [
                row.get("actor1_country"), row.get("actor2_country"),
                row.get("actor1_geo_country"), row.get("actor2_geo_country"), row.get("action_geo_country"),
            ]
            if any(KoreaRelevance.is_china_code(x) for x in country_values):
                tokens.add("CHINA")

            # CAMEO는 항상 broad macro로만 추가. sector token과 섞지 않는다.
            cameo_tokens, cameo_reason = CameoMapper.map_root(row.get("cameo_root_norm"))
            if cameo_tokens:
                cameo_record = self.classifier.classify(
                    date=date,
                    source="GDELT_EVENTS_CAMEO",
                    tokens=cameo_tokens,
                    tone=safe_float(row.get("tone")),
                    weight=float(weights.loc[idx]) * 0.12,
                    korea_related=bool(korea_related.loc[idx]),
                    org_anchor_detected=False,
                    reason_hints=[cameo_reason, "cameo_broad_only"],
                )
                if cameo_record is not None:
                    # CAMEO root는 classifier상 broad_macro로만 나오도록 보정
                    if cameo_record.evidence_class != "broad_macro":
                        continue
                    self.accumulator.add(cameo_record)

            # actor strict keyword context. source_url은 matching에서 제외한다.
            if tokens:
                keyword_record = self.classifier.classify(
                    date=date,
                    source="GDELT_EVENTS_KEYWORD",
                    tokens=tokens,
                    tone=safe_float(row.get("tone")),
                    weight=float(weights.loc[idx]),
                    korea_related=bool(korea_related.loc[idx]),
                    org_anchor_detected=False,
                    reason_hints=["events_actor_strict_keyword"],
                )
                if keyword_record is not None:
                    self.accumulator.add(keyword_record)

    @staticmethod
    def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
        colmap = {str(c).lower(): c for c in df.columns}

        def pick(*names: str) -> pd.Series:
            for name in names:
                if name in df.columns:
                    return df[name]
                lower = name.lower()
                if lower in colmap:
                    return df[colmap[lower]]
            return pd.Series("", index=df.index)

        out = pd.DataFrame(index=df.index)
        out["event_date"] = pick("event_date", "SQLDATE")
        out["actor1_name"] = pick("actor1_name", "Actor1Name")
        out["actor2_name"] = pick("actor2_name", "Actor2Name")
        out["actor1_type"] = pick("actor1_type", "Actor1Type1Code")
        out["actor2_type"] = pick("actor2_type", "Actor2Type1Code")
        out["actor1_country"] = pick("actor1_country", "Actor1CountryCode")
        out["actor2_country"] = pick("actor2_country", "Actor2CountryCode")
        out["actor1_geo_country"] = pick("actor1_geo_country", "Actor1Geo_CountryCode")
        out["actor2_geo_country"] = pick("actor2_geo_country", "Actor2Geo_CountryCode")
        out["action_geo_country"] = pick("action_geo_country", "ActionGeo_CountryCode")
        out["cameo_root"] = pick("cameo_root", "EventRootCode")
        out["goldstein_scale"] = pick("goldstein_scale", "GoldsteinScale")
        out["num_mentions"] = pick("num_mentions", "NumMentions")
        out["num_articles"] = pick("num_articles", "NumArticles")
        out["avg_tone"] = pick("avg_tone", "AvgTone")
        out["source_url"] = pick("source_url", "SOURCEURL")
        return out

    @staticmethod
    def _korea_related_mask(df: pd.DataFrame) -> pd.Series:
        mask = pd.Series(False, index=df.index)
        for col in ["actor1_country", "actor2_country", "actor1_geo_country", "actor2_geo_country", "action_geo_country"]:
            normalized = df[col].astype("string").str.upper().fillna("")
            mask = mask | normalized.isin(KoreaRelevance.KOREA_CODES)

        text = (
            df["actor1_name"].astype("string").fillna("") + " "
            + df["actor2_name"].astype("string").fillna("")
        )
        mask = mask | text.str.contains(KoreaRelevance.KOREA_TEXT, regex=True, na=False)
        return mask

    @staticmethod
    def _event_weight(df: pd.DataFrame, korea_related: pd.Series) -> pd.Series:
        mention_score = np.log1p(df["mentions"].clip(lower=0))
        article_score = np.log1p(df["articles"].clip(lower=0))
        goldstein_score = df["goldstein"].abs().clip(lower=0, upper=10) / 10.0
        tone_score = df["tone"].abs().fillna(0).clip(lower=0, upper=10) / 10.0

        weight = 1.0 + mention_score + 0.7 * article_score + 2.0 * goldstein_score + 0.5 * tone_score
        weight = pd.Series(weight, index=df.index)
        weight = weight.where(~korea_related, weight * 1.4)
        return weight.clip(lower=1.0, upper=50.0)


# =============================================================================
# Diagnostics
# =============================================================================


class DiagnosticsWriter:
    @staticmethod
    def write(path: Path, main_df: pd.DataFrame, quarantine_df: pd.DataFrame, preview_df: pd.DataFrame, processed_info: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        lines.append("# GDELT Context Summary Diagnostics")
        lines.append("")
        lines.append("## Processed")
        for key, value in processed_info.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

        DiagnosticsWriter._append_df_summary(lines, "Main", main_df)
        DiagnosticsWriter._append_df_summary(lines, "Ranked Preview", preview_df)
        DiagnosticsWriter._append_df_summary(lines, "Quarantine", quarantine_df)

        if not main_df.empty and "date" in main_df.columns:
            lines.append("## Main daily row count")
            desc = main_df.groupby("date").size().describe().to_dict()
            lines.append(f"- rows_per_day: {desc}")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _append_df_summary(lines: List[str], name: str, df: pd.DataFrame) -> None:
        lines.append(f"## {name}")
        lines.append(f"rows: {len(df):,}")
        if df.empty:
            lines.append("")
            return

        for col in ["evidence_class", "confidence_level", "source", "quarantine_reason", "reason_code"]:
            if col not in df.columns:
                continue
            lines.append(f"\n### {col}")
            vc = df[col].value_counts(dropna=False).head(30)
            lines.extend([f"- {idx}: {val}" for idx, val in vc.items()])

        lines.append("\n### score describe")
        for col in ["stock_link_score", "specificity_score", "raw_count", "weighted_count"]:
            if col in df.columns:
                desc = pd.to_numeric(df[col], errors="coerce").describe().to_dict()
                lines.append(f"- {col}: {desc}")
        lines.append("")


# =============================================================================
# Pipeline
# =============================================================================


@dataclass
class PipelineConfig:
    gdelt_dir: Path
    output_csv: Path
    quarantine_csv: Path
    preview_csv: Path
    diagnostics_txt: Path
    pattern: str
    mode: str
    batch_size: int
    max_rows_per_file: Optional[int]
    event_filter_mode: str
    min_event_mentions: float
    min_event_articles: float
    min_abs_goldstein: float
    include_korea_related_events: bool
    score_threshold: float
    max_contexts_per_day: int
    max_broad_per_day: int
    keep_broad_macro: bool


class GdeltContextSummaryPipeline:
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.classifier = RecordClassifier()
        self.accumulator = DailyContextAccumulator()
        self.gkg_processor = GkgProcessor(self.accumulator, self.classifier)
        self.events_processor = EventsProcessor(
            accumulator=self.accumulator,
            classifier=self.classifier,
            event_filter_mode=config.event_filter_mode,
            min_event_mentions=config.min_event_mentions,
            min_event_articles=config.min_event_articles,
            min_abs_goldstein=config.min_abs_goldstein,
            include_korea_related_events=config.include_korea_related_events,
        )
        self.splitter = OutputSplitter(
            score_threshold=config.score_threshold,
            max_contexts_per_day=config.max_contexts_per_day,
            max_broad_per_day=config.max_broad_per_day,
            keep_broad_macro=config.keep_broad_macro,
        )

    def run(self) -> None:
        paths = self._collect_paths()
        if not paths:
            raise FileNotFoundError(f"parquet 파일 없음: {self.config.gdelt_dir} / pattern={self.config.pattern}")

        print("=" * 100)
        print("[GDELT context summary 생성 시작]")
        print(f"gdelt_dir: {self.config.gdelt_dir}")
        print(f"pattern: {self.config.pattern}")
        print(f"mode: {self.config.mode}")
        print(f"files: {len(paths)}")
        print(f"output_csv: {self.config.output_csv}")
        print(f"quarantine_csv: {self.config.quarantine_csv}")
        print(f"preview_csv: {self.config.preview_csv}")
        print("=" * 100)

        file_counts: Counter = Counter()
        row_counts: Counter = Counter()

        for idx, path in enumerate(paths, start=1):
            kind = self._kind(path)
            print(f"[{idx}/{len(paths)}] {kind.upper()} {path.name}")
            try:
                if kind == "gkg":
                    n = self.gkg_processor.process_file(path, self.config.batch_size, self.config.max_rows_per_file)
                elif kind == "events":
                    n = self.events_processor.process_file(path, self.config.batch_size, self.config.max_rows_per_file)
                else:
                    print("  [SKIP] unknown type")
                    continue
                file_counts[kind] += 1
                row_counts[kind] += n
                print(f"  processed_rows: {n:,}")
            except Exception as exc:
                print(f"  [SKIP] reason={exc}")

        all_df = self.accumulator.to_dataframe()
        main_df, quarantine_df, preview_df = self.splitter.split(all_df)

        self.config.output_csv.parent.mkdir(parents=True, exist_ok=True)
        self.config.quarantine_csv.parent.mkdir(parents=True, exist_ok=True)
        self.config.preview_csv.parent.mkdir(parents=True, exist_ok=True)
        main_df.to_csv(self.config.output_csv, index=False, encoding="utf-8-sig")
        quarantine_df.to_csv(self.config.quarantine_csv, index=False, encoding="utf-8-sig")
        preview_df.to_csv(self.config.preview_csv, index=False, encoding="utf-8-sig")

        processed_info = {
            "processed_files": dict(file_counts),
            "processed_rows": dict(row_counts),
            "record_level_evidence_class": dict(self.accumulator.record_counter),
            "all_aggregated_rows": len(all_df),
            "main_rows": len(main_df),
            "quarantine_rows": len(quarantine_df),
            "ranked_preview_rows": len(preview_df),
            "daily_context_cap_applied_to_summary": False,
            "daily_context_cap_applied_to_preview_only": True,
        }
        DiagnosticsWriter.write(self.config.diagnostics_txt, main_df, quarantine_df, preview_df, processed_info)

        self._print_summary(main_df, quarantine_df, preview_df, processed_info)

    def _collect_paths(self) -> List[Path]:
        all_paths = sorted(self.config.gdelt_dir.glob(self.config.pattern))
        if self.config.mode == "gkg":
            return [p for p in all_paths if p.name.startswith("gkg_")]
        if self.config.mode == "events":
            return [p for p in all_paths if p.name.startswith("events_")]
        return [p for p in all_paths if p.name.startswith("gkg_") or p.name.startswith("events_")]

    @staticmethod
    def _kind(path: Path) -> str:
        if path.name.startswith("gkg_"):
            return "gkg"
        if path.name.startswith("events_"):
            return "events"
        return "unknown"

    def _print_summary(self, main_df: pd.DataFrame, quarantine_df: pd.DataFrame, preview_df: pd.DataFrame, processed_info: Dict[str, Any]) -> None:
        print("=" * 100)
        print("[GDELT context summary 생성 완료]")
        print(f"processed_files: {processed_info['processed_files']}")
        print(f"processed_rows: {processed_info['processed_rows']}")
        print(f"main_rows: {len(main_df):,}")
        print(f"quarantine_rows: {len(quarantine_df):,}")
        print(f"ranked_preview_rows: {len(preview_df):,}")
        print(f"output_csv: {self.config.output_csv}")
        print(f"quarantine_csv: {self.config.quarantine_csv}")
        print(f"preview_csv: {self.config.preview_csv}")
        print(f"diagnostics_txt: {self.config.diagnostics_txt}")

        if not main_df.empty:
            print("\n[evidence_class - main]")
            print(main_df["evidence_class"].value_counts(dropna=False).to_string())
            print("\n[confidence_level - main]")
            print(main_df["confidence_level"].value_counts(dropna=False).to_string())
            print("\n[preview - main]")
            preview_cols = [
                "date", "theme", "evidence_class", "confidence_level", "stock_link_score",
                "hard_match_tags", "profile_factor_tags", "source", "raw_count", "reason_code",
            ]
            print(main_df[preview_cols].head(30).to_string(index=False))

        if not quarantine_df.empty:
            print("\n[quarantine_reason]")
            print(quarantine_df["quarantine_reason"].value_counts(dropna=False).head(30).to_string())
        print("=" * 100)


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    parser.add_argument("--gdelt-dir", type=str, required=True, help="GDELT parquet 디렉토리")
    parser.add_argument("--output-csv", type=str, required=True, help="main context summary CSV 경로")
    parser.add_argument("--quarantine-csv", type=str, default="", help="quarantine CSV 경로. 미지정 시 output stem 기준 자동 생성")
    parser.add_argument("--preview-csv", type=str, default="", help="ranked preview CSV 경로. 미지정 시 output stem 기준 자동 생성")
    parser.add_argument("--diagnostics-txt", type=str, default="", help="diagnostics txt 경로. 미지정 시 output stem 기준 자동 생성")
    parser.add_argument("--pattern", type=str, default="*.parquet", help="parquet glob pattern")
    parser.add_argument("--mode", type=str, choices=["gkg", "events", "all"], default="all", help="처리 대상")
    parser.add_argument("--batch-size", type=int, default=200_000, help="parquet batch size")
    parser.add_argument("--max-rows-per-file", type=int, default=None, help="디버깅용 파일별 최대 처리 row 수")

    parser.add_argument(
        "--event-filter-mode",
        type=str,
        choices=["all", "korea_or_high_impact", "high_impact"],
        default="all",
        help="EVENTS 처리 방식",
    )
    parser.add_argument("--min-event-mentions", type=float, default=30, help="EVENTS high-impact 기준 num_mentions")
    parser.add_argument("--min-event-articles", type=float, default=20, help="EVENTS high-impact 기준 num_articles")
    parser.add_argument("--min-abs-goldstein", type=float, default=5.0, help="EVENTS high-impact 기준 abs(goldstein_scale)")
    parser.add_argument("--include-korea-related-events", action="store_true", help="high-impact가 아니어도 한국 관련 EVENTS 포함")

    parser.add_argument("--score-threshold", type=float, default=0.35, help="main/quarantine 분리 기준")
    parser.add_argument(
        "--max-contexts-per-day",
        type=int,
        default=20,
        help="날짜별 ranked preview 최대 수. summary는 자르지 않음. 0 이하이면 preview도 무제한",
    )
    parser.add_argument("--max-broad-per-day", type=int, default=2, help="날짜별 ranked preview broad_macro 최대 수. summary는 자르지 않음")
    parser.add_argument("--drop-broad-macro", action="store_true", help="broad_macro를 main output에서 제외")

    return parser


def build_config(args: argparse.Namespace) -> PipelineConfig:
    output_csv = Path(args.output_csv).expanduser().resolve()

    if clean_text(args.quarantine_csv):
        quarantine_csv = Path(args.quarantine_csv).expanduser().resolve()
    else:
        quarantine_csv = output_csv.with_name(output_csv.stem.replace("_summary", "") + "_quarantine.csv")

    if clean_text(args.preview_csv):
        preview_csv = Path(args.preview_csv).expanduser().resolve()
    else:
        preview_csv = output_csv.with_name(output_csv.stem.replace("_summary", "") + "_ranked_preview.csv")

    if clean_text(args.diagnostics_txt):
        diagnostics_txt = Path(args.diagnostics_txt).expanduser().resolve()
    else:
        diagnostics_txt = output_csv.with_name(output_csv.stem.replace("_summary", "") + "_diagnostics.txt")

    return PipelineConfig(
        gdelt_dir=Path(args.gdelt_dir).expanduser().resolve(),
        output_csv=output_csv,
        quarantine_csv=quarantine_csv,
        preview_csv=preview_csv,
        diagnostics_txt=diagnostics_txt,
        pattern=args.pattern,
        mode=args.mode,
        batch_size=args.batch_size,
        max_rows_per_file=args.max_rows_per_file,
        event_filter_mode=args.event_filter_mode,
        min_event_mentions=args.min_event_mentions,
        min_event_articles=args.min_event_articles,
        min_abs_goldstein=args.min_abs_goldstein,
        include_korea_related_events=args.include_korea_related_events,
        score_threshold=args.score_threshold,
        max_contexts_per_day=args.max_contexts_per_day,
        max_broad_per_day=args.max_broad_per_day,
        keep_broad_macro=not args.drop_broad_macro,
    )


def main() -> None:
    args = build_arg_parser().parse_args()
    config = build_config(args)
    pipeline = GdeltContextSummaryPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
