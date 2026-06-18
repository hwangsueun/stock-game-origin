#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extract detail facts from exact DART disclosure documents.

This is intentionally stricter than annual-report context:
- candidates come from pr05e dart_evidence rcept_no values;
- downloaded/extracted facts keep the same rcept_no;
- pr05f can attach these facts only to the matching DART evidence item.

Network is optional. Without DART_API_KEY, the script still writes the candidate
CSV and extracts facts from any already-downloaded ZIP files.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


DEFAULT_BUNDLES_JSONL = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05e_stock_evidence_bundles/stock_evidence_bundles.jsonl"
)
DEFAULT_OUTPUT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/data/interim/pr05f_dart_disclosure_detail_facts"
)
DEFAULT_DOCUMENT_DIR = Path(
    "/Users/hgs/Desktop/IISE CD/news_generator/data/raw/dart/disclosure_documents"
)
DETAIL_REPORT_KEYWORDS = [
    "매출액또는손익구조",
    "현금ㆍ현물배당결정",
    "현금·현물배당결정",
    "신규시설투자",
    "유형자산취득결정",
    "유형자산처분결정",
    "타법인주식및출자증권취득결정",
    "타법인주식및출자증권처분결정",
    "단일판매ㆍ공급계약",
    "단일판매·공급계약",
]


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def stock_code(value: Any) -> str:
    digits = re.sub(r"[^0-9]", "", clean(value))
    return digits.zfill(6)[-6:] if digits else ""


def cap(text: str, limit: int = 180) -> str:
    text = clean(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_title(title: str) -> str:
    title = clean(title)
    title = re.sub(r"^\[기재정정\]\s*", "", title)
    return title


@dataclass
class Candidate:
    bundle_id: str
    anchor_date: str
    stock_code: str
    stock_name: str
    report_name: str
    rcept_no: str


class CandidateBuilder:
    def __init__(self, bundles_jsonl: Path) -> None:
        self.bundles_jsonl = bundles_jsonl

    def build(self) -> list[Candidate]:
        out: list[Candidate] = []
        seen: set[str] = set()
        with self.bundles_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                bundle = json.loads(line)
                for item in bundle.get("dart_evidence") or []:
                    report_name = clean(item.get("title") or item.get("report_name") or item.get("report_nm"))
                    rcept_no = clean(item.get("rcept_no") or item.get("receipt_no") or "")
                    if not rcept_no:
                        evidence_id = clean(item.get("evidence_id"))
                        if evidence_id.startswith("DART_"):
                            rcept_no = evidence_id.replace("DART_", "", 1)
                    if not rcept_no:
                        continue
                    if not any(keyword in report_name for keyword in DETAIL_REPORT_KEYWORDS):
                        continue
                    if rcept_no in seen:
                        continue
                    seen.add(rcept_no)
                    out.append(
                        Candidate(
                            bundle_id=clean(bundle.get("bundle_id")),
                            anchor_date=clean(bundle.get("anchor_date")),
                            stock_code=stock_code(bundle.get("stock_code") or item.get("stock_code")),
                            stock_name=clean(bundle.get("stock_name") or item.get("stock_name")),
                            report_name=report_name,
                            rcept_no=rcept_no,
                        )
                    )
        return out


class DartDocumentClient:
    URL = "https://opendart.fss.or.kr/api/document.xml"

    def __init__(self, api_key: str, sleep_seconds: float = 0.25, timeout: int = 40) -> None:
        self.api_key = api_key
        self.sleep_seconds = sleep_seconds
        self.timeout = timeout

    def download(self, rcept_no: str) -> bytes:
        response = requests.get(
            self.URL,
            params={"crtfc_key": self.api_key, "rcept_no": rcept_no},
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.content
        if not zipfile.is_zipfile(io.BytesIO(content)):
            message = content[:500].decode("utf-8", "ignore")
            raise ValueError(f"DART response is not a ZIP: {message}")
        time.sleep(self.sleep_seconds)
        return content


class DisclosureDetailExtractor:
    def __init__(self, document_dir: Path) -> None:
        self.document_dir = document_dir

    def zip_path(self, candidate: Candidate) -> Path:
        return self.document_dir / candidate.stock_code / f"{candidate.rcept_no}.zip"

    def find_zip_path(self, candidate: Candidate) -> Path:
        direct = self.zip_path(candidate)
        if direct.exists():
            return direct
        stock_dir = self.document_dir / candidate.stock_code
        if stock_dir.exists():
            matches = sorted(stock_dir.glob(f"*_{candidate.rcept_no}.zip"))
            if matches:
                return matches[0]
        return direct

    def save_zip(self, candidate: Candidate, data: bytes, overwrite: bool = False) -> Path:
        path = self.zip_path(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            return path
        path.write_bytes(data)
        return path

    def extract(self, candidate: Candidate) -> tuple[str, list[dict[str, str]]]:
        path = self.find_zip_path(candidate)
        if not path.exists():
            return "missing_zip", []
        text = self._zip_text(path)
        if not text:
            return "empty_text", []
        facts = self._extract_by_report_name(candidate, text)
        return "ok" if facts else "no_facts_extracted", facts

    def _zip_text(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as zf:
                chunks: list[str] = []
                for name in zf.namelist():
                    if not name.lower().endswith((".xml", ".html", ".htm", ".txt")):
                        continue
                    data = self._decode_bytes(zf.read(name))
                    soup = BeautifulSoup(data, "lxml")
                    chunks.append(soup.get_text(" ", strip=True))
                return clean(" ".join(chunks))
        except (OSError, zipfile.BadZipFile):
            return ""

    @staticmethod
    def _decode_bytes(data: bytes) -> str:
        for encoding in ["utf-8", "cp949", "euc-kr"]:
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", "ignore")

    def _extract_by_report_name(self, candidate: Candidate, text: str) -> list[dict[str, str]]:
        title = normalize_title(candidate.report_name)
        facts: list[dict[str, str]] = []
        if "배당" in title:
            facts.extend(self._extract_dividend(candidate, text))
        if "매출액또는손익구조" in title:
            facts.extend(self._extract_sales_profit_change(candidate, text))
        if "신규시설투자" in title or "유형자산" in title:
            facts.extend(self._extract_investment_or_asset(candidate, text))
        if "타법인주식" in title:
            facts.extend(self._extract_equity_transaction(candidate, text))
        if "단일판매" in title or "공급계약" in title:
            facts.extend(self._extract_contract(candidate, text))

        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for fact in facts:
            text_ko = clean(fact.get("text_ko"))
            if not text_ko or text_ko in seen:
                continue
            seen.add(text_ko)
            fact["text_ko"] = text_ko
            deduped.append(fact)
        return deduped[:8]

    def _fact(self, fact_type: str, text_ko: str, source_text: str = "") -> dict[str, str]:
        return {
            "fact_type": fact_type,
            "relation_scope": "same_dart_rcept_no",
            "text_ko": cap(text_ko, 170),
            "source_text_ko": cap(source_text or text_ko, 220),
        }

    def _extract_dividend(self, candidate: Candidate, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        per_share = self._near_value(text, ["1주당", "배당금"], r"(\d[\d,]*)\s*원")
        total = self._near_amount(text, ["배당금총액"])
        record_date = self._near_value(text, ["배당기준일"], r"(\d{4}[-./년]\s*\d{1,2}[-./월]\s*\d{1,2})")
        if per_share:
            facts.append(self._fact("dividend_per_share", f"{self._subject(candidate.stock_name)} 1주당 배당금을 {per_share}원으로 결정했다."))
        if total:
            facts.append(self._fact("dividend_total_amount", f"{candidate.stock_name}의 배당금 총액은 {total}으로 공시됐다."))
        if record_date:
            facts.append(self._fact("record_date", f"{candidate.stock_name}의 배당 기준일은 {clean(record_date)}로 공시됐다."))
        return facts

    def _extract_sales_profit_change(self, candidate: Candidate, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        for label, fact_type in [("매출액", "sales"), ("영업이익", "operating_profit"), ("당기순이익", "net_income")]:
            value = self._financial_statement_row_value(text, label)
            if value:
                amount_text = self._format_thousand_krw(value)
                if value.startswith("-") and label == "영업이익":
                    facts.append(self._fact(fact_type, f"{candidate.stock_name}의 영업손실은 {amount_text.lstrip('-').replace('약 -', '약 ')}으로 공시됐다."))
                elif value.startswith("-") and label == "당기순이익":
                    facts.append(self._fact(fact_type, f"{candidate.stock_name}의 당기순손실은 {amount_text.lstrip('-').replace('약 -', '약 ')}으로 공시됐다."))
                else:
                    facts.append(self._fact(fact_type, f"{candidate.stock_name}의 {label}은 {amount_text}으로 공시됐다."))
        for reason in self._extract_change_reasons(text):
            facts.append(self._fact("earnings_change_reason", reason))
        return facts

    def _extract_change_reasons(self, text: str) -> list[str]:
        """실적변경 공시의 주요원인·변동원인 섹션에서 원인 문구를 추출한다."""
        reasons: list[str] = []
        for kw in ["주요원인", "변동원인", "변동 원인"]:
            idx = text.find(kw)
            if idx < 0:
                continue
            # 다음 섹션 헤더까지만 추출
            window = text[idx + len(kw): idx + len(kw) + 600]
            end_markers = ["이사회결의일", "결정일", "사외이사", "기타 투자판단", "대규모법인"]
            end = len(window)
            for marker in end_markers:
                pos = window.find(marker)
                if 0 < pos < end:
                    end = pos
            raw = window[:end].strip(" :：-\n")
            # 불릿(-) 단위로 분리해 각 원인을 개별 팩트로
            items = re.split(r"\s*-\s+", raw)
            for item in items:
                item = clean(item)
                item = re.sub(r"^\d+\.\s*", "", item)
                item = re.sub(r"\s+\d+\.$", "", item).strip()
                if len(item) < 8 or len(item) > 150:
                    continue
                # 불필요한 법적·회계 문구 제거
                if any(skip in item for skip in ["K-IFRS", "외부감사", "감사위원", "사외이사", "회계감사"]):
                    continue
                reasons.append(f"{item}")
            break
        return reasons[:3]

    def _extract_investment_or_asset(self, candidate: Candidate, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        amount = self._near_amount(text, ["투자금액", "취득금액", "양수금액"])
        purpose = self._near_text(text, ["투자목적", "취득목적", "양수목적"], max_chars=120)
        detail = self._extract_investment_detail(text)
        if amount:
            facts.append(self._fact("investment_amount", f"{candidate.stock_name}의 관련 투자·취득 금액은 {amount}으로 공시됐다."))
        if purpose:
            facts.append(self._fact("transaction_purpose", f"{self._subject(candidate.stock_name)} 목적을 '{purpose}'로 공시했다."))
        if detail:
            facts.append(self._fact("investment_detail", f"{candidate.stock_name}의 투자 세부내용은 다음과 같이 공시됐다: {detail}"))
        return facts

    def _extract_investment_detail(self, text: str) -> str:
        """기타 투자판단과 관련한 중요사항에서 사업 맥락 문구를 추출한다."""
        return self._extract_other_important_matters(text, window_chars=600)

    # 회계·법률 보일러플레이트 문장을 식별하는 프리픽스/키워드
    _BOILERPLATE_PREFIXES = re.compile(
        r"^(?:"
        r"상기\s*[\d’’’\"‘’]"   # 상기 2항, 상기 ‘2.
        r"|[)）]\s*상기"                    # ) 상기
        r"|\)\s*\d+"                        # ) 1) 형식
        r"|총\s*선가\s*USD"                 # 총 선가 USD
        r"|총\s*용선료\s*USD"               # 총 용선료 USD
        r"|총\s*투자금액은\s*USD"           # 총 투자금액은 USD
        r"|총\s*계약금액\(USD"              # 총 계약금액(USD
        r"|총\s*대선수익금액\(USD"          # 총 대선수익금액(USD
        r"|List\s*가격"                     # List 가격
        r"|투자기간.{0,3}의?\s*시작일"      # 투자기간의 시작일
        r"|투자기간.{0,3}\s*은\s*용선기간"  # 투자기간"은 용선기간
        r"|투자기간.{0,3}\s*은\s*이사회"    # 투자기간"은 이사회 결의일 이후
        r"|계약기간.{0,3}\s*[은의]\s*시작일" # 계약기간"의 시작일은
        r"|계약기간.{0,3}\s*은\s*향후"      # 계약기간"은 향후
        r"|계약금액.{0,3}\s*은\s*총"        # 계약금액"은 총
        r"|계약금액\s*대비"                  # 계약금액 대비 5% 이상
        r"|계약내역\s*계약금액"              # 계약내역 계약금액(원) 순수 숫자 나열
        r"|주요계약조건의\s*계약제품\s*중"  # 계약기간 설명 반복 레이블
        r"|유가\s*및\s*환율"                # 유가 및 환율상승
        r"|판매[ㆍ·]\s*공급계약\s*구분"    # 판매·공급계약 구분 (양식 레이블)
        r"|투자입지\s*[:：]"                # 투자입지: 경기도 파주
        r"|투자금액\s*및\s*투자기간은\s*집행" # 투자금액 및 투자기간은 집행과정에서
        r"|취득대금\s*및\s*지급일자"        # 취득대금 및 지급일자 (지급 일정표)
        r")",
        re.IGNORECASE,
    )
    _BOILERPLATE_KEYWORDS = [
        "K-IFRS", "재무제표", "자기자본", "참여비율", "감사위원",
        "잔금지급", "최초고시환율", "최근매출액은", "외부감사",
        "변동될 수 있음", "변경될 수 있음", "변동 가능성",
        "변동될 수 있습니다", "변경될 수 있습니다",  # ‘음’ 대신 ‘습니다’ 형태
        "정정공시", "USD환율을 적용", "USD환율(", "환율은 계약",
        "공시유보", "유보기간 종료 후 공시",
        "조건부로 결의", "금융기관의 동의",      # 조건부 투자 문구
        "IFRS 16 리스회계",                       # 회계처리 기준 언급
        "리스계약 구조의 투자임",                  # HMM 리스 구조 설명
        "시작일은 이사회 결의일",                  # 투자기간 기산일 설명
        "투자금액USD", "투자금액 USD",             # HMM USD 계산 항목
        "확정공시입니다",                           # 풍문해명 확정공시 표현
        "List 가격 총 금액",                       # 대한항공 항공기 가격 총액
        "최근매출액(원)",                           # 매출액 대비 % 테이블 잔여물
        "제세공과금 및 기타 부대비용",             # 취득가액 주석
        "상기 투자금액은 총 용선료",               # HMM 환율 계산 항목
        "상기 투자금액은 총 선가",                 # HMM 환율 계산 항목
    ]

    def _is_boilerplate(self, sentence: str) -> bool:
        s = sentence.strip()
        if self._BOILERPLATE_PREFIXES.match(s):
            return True
        if any(kw in s for kw in self._BOILERPLATE_KEYWORDS):
            return True
        # "자산총액은 '13년말 연결재무제표..." 같은 기준 주석
        if re.search(r"자산총액은\s*['\'\"]?\d{2}년", s):
            return True
        # 의미 없는 "※" 단독 혹은 "※ 이하 동일" 수준 짧은 주석
        if re.match(r"^[※*]", s) and len(s) < 30:
            return True
        # 문장 끝이 "※"로만 끝나는 경우 (표 주석 잔여물)
        stripped_note = re.sub(r"\s*[※*]\s*$", "", s)
        if stripped_note != s and len(stripped_note) < 15:
            return True
        # 동사(다/됩니다/임)가 전혀 없는 짧은 레이블형 문장
        if len(s) < 30 and not re.search(r"[다됩임했]", s):
            return True
        return False

    def _extract_other_important_matters(self, text: str, window_chars: int = 600) -> str:
        """'기타 투자판단과 관련한 중요사항' 섹션에서 사업 맥락 문장을 골라 반환한다."""
        for kw in ["기타 투자판단과 관련한 중요사항", "기타 투자판단관련 중요사항"]:
            idx = text.find(kw)
            if idx < 0:
                continue
            window = text[idx + len(kw): idx + len(kw) + window_chars]
            window = window.strip(" :：-\n0123456789.")
            end_markers = ["이사회결의일", "결정일", "사외이사", "대규모법인", "관련공시"]
            end = len(window)
            for marker in end_markers:
                pos = window.find(marker)
                if 0 < pos < end:
                    end = pos
            raw = clean(window[:end])

            # 불릿(-) 또는 번호(1) 2)) 단위로 분리, 각 항목을 개별 평가
            items = re.split(r"\s*(?:-\s+|\d+[).]\s+)", raw)
            good: list[str] = []
            for item in items:
                item = clean(item)
                item = re.sub(r"^\d+\.\s*", "", item).strip()
                if len(item) < 10:
                    continue
                if self._is_boilerplate(item):
                    continue
                good.append(item)
                if len(good) >= 2:
                    break

            if good:
                return cap(" ".join(good), 200)
        return ""

    def _extract_equity_transaction(self, candidate: Candidate, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        target = self._near_text(text, ["회사명", "발행회사"], max_chars=60)
        amount = self._near_amount(text, ["취득금액", "처분금액"])
        stake = self._near_value(text, ["취득후", "소유주식비율", "지분비율"], r"(\d[\d,]*(?:\.\d+)?)\s*%")
        if target:
            facts.append(self._fact("target_company", f"{candidate.stock_name}의 거래 대상 회사는 {target}로 공시됐다."))
        if amount:
            facts.append(self._fact("acquisition_amount", f"{candidate.stock_name}의 지분 거래 금액은 {amount}으로 공시됐다."))
        if stake:
            facts.append(self._fact("stake_ratio", f"{candidate.stock_name}의 거래 후 지분율은 {stake}%로 공시됐다."))
        return facts

    def _extract_contract(self, candidate: Candidate, text: str) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        amount = self._near_amount(text, ["계약금액"])
        counterparty = self._near_text(text, ["계약상대", "상대방"], max_chars=70)
        item = self._near_text(text, ["계약품목", "품목", "계약내용", "공급내용"], max_chars=100)
        detail = self._extract_contract_detail(text)
        if amount:
            facts.append(self._fact("contract_amount", f"{candidate.stock_name}의 계약금액은 {amount}으로 공시됐다."))
        if counterparty:
            facts.append(self._fact("counterparty", f"{candidate.stock_name}의 계약 상대방은 {counterparty}로 공시됐다."))
        if item and item not in {"-", "해당없음"}:
            facts.append(self._fact("contract_item", f"{candidate.stock_name}의 계약 품목 또는 내용은 '{item}'으로 공시됐다."))
        if detail:
            facts.append(self._fact("contract_detail", f"{candidate.stock_name}의 계약 관련 주요사항: {detail}"))
        return facts

    def _extract_contract_detail(self, text: str) -> str:
        """기타 투자판단과 관련한 중요사항에서 계약 목적·배경 문구를 추출한다."""
        return self._extract_other_important_matters(text, window_chars=700)

    @staticmethod
    def _near_value(text: str, labels: list[str], value_pattern: str) -> str:
        for label in labels:
            idx = text.find(label)
            if idx < 0:
                continue
            window = text[idx: idx + 600]
            match = re.search(value_pattern, window)
            if match:
                return clean(match.group(1))
        return ""

    @staticmethod
    def _near_amount(text: str, labels: list[str]) -> str:
        for label in labels:
            idx = text.find(label)
            if idx < 0:
                continue
            window = text[idx: idx + 600]
            match = re.search(
                rf"{re.escape(label)}(?:\([^)]*(원|백만원|억원)[^)]*\))?[^\d-]{{0,80}}(-?\d[\d,]*(?:\.\d+)?)\s*(원|백만원|억원)?",
                window,
            )
            if match:
                unit = clean(match.group(1) or match.group(3))
                value = clean(match.group(2))
                if unit:
                    return DisclosureDetailExtractor._format_amount(value, unit)
        return ""

    @staticmethod
    def _format_amount(value: str, unit: str) -> str:
        raw = clean(value).replace(",", "")
        try:
            number = float(raw)
        except ValueError:
            return f"{clean(value)}{unit}"

        if unit == "억원":
            if number.is_integer():
                return f"{int(number):,}억원"
            return f"{number:,.2f}".rstrip("0").rstrip(".") + "억원"
        if unit == "백만원":
            eok = round(number / 100)
            if eok >= 1:
                jo, rem_eok = divmod(eok, 10000)
                if jo > 0 and rem_eok > 0:
                    return f"약 {jo}조{rem_eok:,}억원"
                if jo > 0:
                    return f"약 {jo}조원"
                return f"약 {rem_eok:,}억원"
        if unit == "원" and number >= 100000000:
            eok = round(number / 100000000)
            return f"약 {eok:,}억원"
        if unit == "원" and number < 100000000:
            return ""

        if number.is_integer():
            return f"{int(number):,}{unit}"
        return f"{number:,.2f}".rstrip("0").rstrip(".") + unit

    @staticmethod
    def _financial_statement_row_value(text: str, label: str) -> str:
        section = text
        start = section.find("변동내용")
        if start >= 0:
            section = section[start:]
        end = section.find("대규모법인여부")
        if end >= 0:
            section = section[:end]

        if label == "매출액":
            pattern = r"-\s*매출액(?:\([^)]*\))*[^\-]{0,120}?\s(-?\d[\d,]{5,})\s"
        else:
            pattern = rf"-\s*{re.escape(label)}\s+(-?\d[\d,]{{5,}})\s"
        match = re.search(pattern, section)
        return clean(match.group(1)) if match else ""

    @staticmethod
    def _near_text(text: str, labels: list[str], max_chars: int = 80) -> str:
        for label in labels:
            idx = text.find(label)
            if idx < 0:
                continue
            window = text[idx + len(label): idx + len(label) + 260]
            window = re.sub(r"^[\s:：\-ㆍ·|]+", "", window)
            window = re.split(
                r"\s+(?:국적|대표자|자본금|회사와 관계|발행주식총수|주요사업|취득내역|취득금액|"
                r"자기자본|지분비율|취득방법|취득목적|취득예정일자|이사회결의일|대규모법인여부)\s+",
                window,
            )[0]
            window = re.split(r"\s{2,}|(?<=다\.)|(?<=\. )|(?<=\))", window)[0]
            value = clean(window).strip(" :：-ㆍ·|")
            value = re.sub(r"\s+\d+\.$", "", value)
            value = re.sub(r"^미정\s+", "", value)
            value = re.sub(r"^(?:대표자\s+)?회사명\s*[:：]\s*", "", value)
            value = re.sub(r"^대표자\s+", "", value)
            value = re.sub(r"^회사명\s+", "", value)
            if value == "미정":
                return ""
            compact = re.sub(r"\s+", "", value)
            invalid_values = {
                "(주)",
                "회사명(주)",
                "대표자자본금(원)",
                "대표자회사명",
                ",발행회사명,발행회사",
            }
            if compact in invalid_values:
                return ""
            if compact.startswith("대표자자본금") or "자본금" in compact or "발행회사명" in compact:
                return ""
            if 2 <= len(value) <= max_chars:
                return value
        return ""

    @staticmethod
    def _subject(stock_name: str) -> str:
        if not stock_name:
            return "해당 회사는"
        last = stock_name[-1]
        code = ord(last)
        if not (0xAC00 <= code <= 0xD7A3):
            return f"{stock_name}은"
        has_batchim = 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28 != 0
        return f"{stock_name}{'은' if has_batchim else '는'}"

    @staticmethod
    def _format_thousand_krw(value: str) -> str:
        raw = clean(value).replace(",", "")
        sign = ""
        if raw.startswith("-"):
            sign = "-"
            raw = raw[1:]
        try:
            thousand_krw = int(float(raw))
        except ValueError:
            return clean(value) + "천원"

        eok = round(thousand_krw / 100000)
        if eok <= 0:
            return sign + f"{thousand_krw:,}천원"

        jo, rem_eok = divmod(eok, 10000)
        prefix = "약 "
        if jo > 0 and rem_eok > 0:
            return f"{prefix}{sign}{jo}조{rem_eok:,}억원"
        if jo > 0:
            return f"{prefix}{sign}{jo}조원"
        return f"{prefix}{sign}{rem_eok:,}억원"


def write_candidates(path: Path, candidates: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["bundle_id", "anchor_date", "stock_code", "stock_name", "report_name", "rcept_no"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in candidates:
            writer.writerow({k: getattr(c, k) for k in fields})


def write_facts(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["rcept_no", "stock_code", "stock_name", "report_name", "status", "zip_path", "fact_count", "facts_json"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download/extract exact DART disclosure detail facts for pr05f.")
    parser.add_argument("--bundles-jsonl", type=Path, default=DEFAULT_BUNDLES_JSONL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--document-dir", type=Path, default=DEFAULT_DOCUMENT_DIR)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--max-docs", type=int, default=None)
    parser.add_argument("--sleep-sec", type=float, default=0.25)
    parser.add_argument("--overwrite-zip", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv("/Users/hgs/Desktop/IISE CD/news_generator/.env")
    load_dotenv("/Users/hgs/Desktop/IISE CD/news_generator/dart_collector/.env", override=False)
    candidates = CandidateBuilder(args.bundles_jsonl).build()
    if args.max_docs is not None:
        candidates = candidates[: args.max_docs]

    candidate_csv = args.output_dir / "dart_disclosure_detail_candidates.csv"
    facts_csv = args.output_dir / "dart_disclosure_detail_facts.csv"
    write_candidates(candidate_csv, candidates)

    api_key = os.getenv("DART_API_KEY", "")
    client = DartDocumentClient(api_key=api_key, sleep_seconds=args.sleep_sec) if args.download and api_key else None
    extractor = DisclosureDetailExtractor(args.document_dir)
    rows: list[dict[str, Any]] = []

    for c in candidates:
        status = ""
        zip_path = extractor.zip_path(c)
        if client and (args.overwrite_zip or not zip_path.exists()):
            try:
                data = client.download(c.rcept_no)
                zip_path = extractor.save_zip(c, data, overwrite=args.overwrite_zip)
                status = "downloaded"
            except Exception as e:  # noqa: BLE001 - report and continue batch extraction
                rows.append({
                    "rcept_no": c.rcept_no,
                    "stock_code": c.stock_code,
                    "stock_name": c.stock_name,
                    "report_name": c.report_name,
                    "status": f"download_error:{type(e).__name__}",
                    "zip_path": str(zip_path),
                    "fact_count": 0,
                    "facts_json": "[]",
                })
                continue
        elif args.download and not api_key:
            status = "download_skipped_missing_dart_api_key"

        extract_status, facts = extractor.extract(c)
        if not status:
            status = extract_status
        elif extract_status != "ok":
            status = f"{status};{extract_status}"

        rows.append({
            "rcept_no": c.rcept_no,
            "stock_code": c.stock_code,
            "stock_name": c.stock_name,
            "report_name": c.report_name,
            "status": status,
            "zip_path": str(zip_path),
            "fact_count": len(facts),
            "facts_json": json.dumps(facts, ensure_ascii=False),
        })

    write_facts(facts_csv, rows)
    print(f"[candidates] {len(candidates)} -> {candidate_csv}")
    print(f"[facts] {sum(1 for r in rows if int(r.get('fact_count') or 0) > 0)} docs_with_facts / {len(rows)} -> {facts_csv}")
    if args.download and not api_key:
        print("[download] skipped: DART_API_KEY is not set")


if __name__ == "__main__":
    main()
