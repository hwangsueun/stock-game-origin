from __future__ import annotations

import argparse
import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tqdm import tqdm


# ============================================================
# 1. Config
# ============================================================

@dataclass(frozen=True)
class PipelineConfig:
    dart_api_key: str
    input_json_path: Path
    output_dir: Path
    start_year: int
    end_year: int
    request_sleep_seconds: float
    overwrite_zip: bool
    report_version: str  # "latest" or "original"

    @property
    def raw_document_dir(self) -> Path:
        return self.output_dir / "raw" / "dart" / "documents"

    @property
    def interim_text_dir(self) -> Path:
        return self.output_dir / "interim" / "dart" / "report_text"

    @property
    def processed_dir(self) -> Path:
        return self.output_dir / "processed"


# ============================================================
# 2. Data Objects
# ============================================================

@dataclass
class BusinessReportIndexRow:
    stock_code: str
    corp_code: str
    dart_name: str
    input_name: str

    business_year: int
    disclosure_year: int

    report_name: str
    rcept_no: str
    rcept_date: str
    flr_nm: str

    is_correction: bool
    source_year_bucket: str


@dataclass
class SectionExtractionResult:
    stock_code: str
    corp_code: str
    dart_name: str
    input_name: str

    business_year: int
    disclosure_year: int

    report_name: str
    rcept_no: str
    rcept_date: str

    company_overview_raw: str
    business_content_raw: str
    main_products_raw: str
    sales_order_raw: str
    risk_raw: str
    rnd_raw: str

    document_zip_path: str
    text_path: str

    extracted_at: str


@dataclass
class FailureRow:
    stock_code: str
    corp_code: str
    dart_name: str
    input_name: str
    business_year: Optional[int]
    rcept_no: str
    stage: str
    message: str


# ============================================================
# 3. Utility
# ============================================================

class TextCleaner:
    @staticmethod
    def decode_bytes(raw: bytes) -> str:
        for enc in ["utf-8", "cp949", "euc-kr", "latin1"]:
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")

    @staticmethod
    def html_to_text(raw_text: str) -> str:
        soup = BeautifulSoup(raw_text, "lxml")

        for tag in soup(["script", "style", "head", "meta", "noscript"]):
            tag.decompose()

        text = soup.get_text("\n")
        return TextCleaner.clean_text(text)

    @staticmethod
    def clean_text(text: str) -> str:
        if not text:
            return ""

        text = text.replace("\r", "\n")
        text = re.sub(r"\u00a0", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        text = re.sub(r"[■◆◇●○▶▷▣□]+", " ", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n\s+", "\n", text)

        return text.strip()

    @staticmethod
    def compact(text: str, max_chars: int) -> str:
        text = TextCleaner.clean_text(text)

        if len(text) <= max_chars:
            return text

        return text[:max_chars].rstrip() + " ...[TRUNCATED]"


class JsonlWriter:
    @staticmethod
    def write(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


class CsvWriter:
    @staticmethod
    def write_dataframe(path: Path, df: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8-sig")


# ============================================================
# 4. Load existing dart_results_2013_2023.json
# ============================================================

class DartResultsLoader:
    def __init__(self, input_json_path: Path):
        self.input_json_path = input_json_path

    def load_results(self) -> dict[str, Any]:
        if not self.input_json_path.exists():
            raise FileNotFoundError(f"입력 JSON 파일이 없습니다: {self.input_json_path}")

        with self.input_json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        results = data.get("results", {})

        if not isinstance(results, dict) or not results:
            raise ValueError("입력 JSON에 results가 없거나 비어 있습니다.")

        return results


# ============================================================
# 5. Build business report index
# ============================================================

class BusinessReportIndexer:
    BUSINESS_REPORT_KEYWORD = "사업보고서"
    EXCLUDE_KEYWORDS = ["분기보고서", "반기보고서"]
    CORRECTION_KEYWORDS = ["정정", "첨부정정", "기재정정"]

    def __init__(self, start_year: int, end_year: int, report_version: str):
        if report_version not in {"latest", "original"}:
            raise ValueError("report_version은 latest 또는 original이어야 합니다.")

        self.start_year = start_year
        self.end_year = end_year
        self.report_version = report_version

    def build(self, company_results: dict[str, Any]) -> pd.DataFrame:
        rows: list[BusinessReportIndexRow] = []

        for input_name, company_data in company_results.items():
            stock_code = self._normalize_stock_code(company_data.get("stock_code", ""))
            corp_code = str(company_data.get("corp_code", "")).strip()
            dart_name = str(company_data.get("dart_name", "")).strip()
            disclosures_by_year = company_data.get("disclosures_by_year", {})

            if not stock_code or not corp_code:
                continue

            for year_bucket, disclosures in disclosures_by_year.items():
                if not isinstance(disclosures, list):
                    continue

                for item in disclosures:
                    report_name = self._get_report_name(item)

                    if not self._is_business_report(report_name):
                        continue

                    rcept_no = str(item.get("rcept_no", "")).strip()
                    rcept_date = str(item.get("rcept_dt", "")).strip()
                    flr_nm = str(item.get("flr_nm", "")).strip()

                    if not rcept_no:
                        continue

                    disclosure_year = self._parse_disclosure_year(
                        rcept_date=rcept_date,
                        year_bucket=str(year_bucket),
                    )

                    business_year = self._infer_business_year(
                        report_name=report_name,
                        rcept_date=rcept_date,
                        disclosure_year=disclosure_year,
                    )

                    if business_year is None:
                        continue

                    if not (self.start_year <= business_year <= self.end_year):
                        continue

                    rows.append(
                        BusinessReportIndexRow(
                            stock_code=stock_code,
                            corp_code=corp_code,
                            dart_name=dart_name,
                            input_name=str(input_name),
                            business_year=business_year,
                            disclosure_year=disclosure_year,
                            report_name=report_name,
                            rcept_no=rcept_no,
                            rcept_date=rcept_date,
                            flr_nm=flr_nm,
                            is_correction=self._is_correction(report_name),
                            source_year_bucket=str(year_bucket),
                        )
                    )

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([asdict(row) for row in rows])
        df = self._deduplicate(df)

        return df.sort_values(["stock_code", "business_year"]).reset_index(drop=True)

    def _normalize_stock_code(self, value: Any) -> str:
        code = str(value or "").strip()

        if not code:
            return ""

        code = code.replace(".0", "")

        if code.isdigit():
            return code.zfill(6)

        return code

    def _get_report_name(self, item: dict[str, Any]) -> str:
        return str(
            item.get("report_nm")
            or item.get("report_name")
            or item.get("reportNm")
            or ""
        ).strip()

    def _is_business_report(self, report_name: str) -> bool:
        if self.BUSINESS_REPORT_KEYWORD not in report_name:
            return False

        if any(keyword in report_name for keyword in self.EXCLUDE_KEYWORDS):
            return False

        return True

    def _is_correction(self, report_name: str) -> bool:
        return any(keyword in report_name for keyword in self.CORRECTION_KEYWORDS)

    def _parse_disclosure_year(self, rcept_date: str, year_bucket: str) -> int:
        if re.match(r"^\d{8}$", rcept_date):
            return int(rcept_date[:4])

        if year_bucket.isdigit():
            return int(year_bucket)

        return 0

    def _infer_business_year(
        self,
        report_name: str,
        rcept_date: str,
        disclosure_year: int,
    ) -> Optional[int]:
        # 예: 사업보고서 (2018.12)
        match = re.search(r"\((20\d{2})\.\d{2}\)", report_name)
        if match:
            return int(match.group(1))

        # 예: 사업보고서(2018년)
        match = re.search(r"\((20\d{2})\s*년", report_name)
        if match:
            return int(match.group(1))

        # 보통 사업보고서는 다음 해 3월 전후 공시
        if re.match(r"^\d{8}$", rcept_date):
            year = int(rcept_date[:4])
            month = int(rcept_date[4:6])

            if month <= 6:
                return year - 1

            return year

        if disclosure_year > 0:
            return disclosure_year - 1

        return None

    def _deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        temp = df.copy()
        temp["rcept_date_sort"] = temp["rcept_date"].fillna("")

        if self.report_version == "latest":
            # 정정 포함 최신본 우선
            temp = temp.sort_values(
                ["stock_code", "business_year", "rcept_date_sort"],
                ascending=[True, True, False],
            )
        else:
            # 원본 우선
            temp = temp.sort_values(
                ["stock_code", "business_year", "is_correction", "rcept_date_sort"],
                ascending=[True, True, True, True],
            )

        temp = temp.drop_duplicates(
            subset=["stock_code", "business_year"],
            keep="first",
        )

        return temp.drop(columns=["rcept_date_sort"])


# ============================================================
# 6. Download DART document ZIP
# ============================================================

class DartDocumentClient:
    BASE_URL = "https://opendart.fss.or.kr/api/document.xml"

    def __init__(self, api_key: str, sleep_seconds: float, timeout: int = 40):
        self.api_key = api_key
        self.sleep_seconds = sleep_seconds
        self.timeout = timeout

    def download(self, rcept_no: str) -> bytes:
        params = {
            "crtfc_key": self.api_key,
            "rcept_no": rcept_no,
        }

        response = requests.get(self.BASE_URL, params=params, timeout=self.timeout)
        response.raise_for_status()

        content = response.content

        if not zipfile.is_zipfile(io.BytesIO(content)):
            message = TextCleaner.decode_bytes(content)[:500]
            raise ValueError(f"ZIP 응답이 아닙니다: {message}")

        time.sleep(self.sleep_seconds)

        return content


class DartDocumentRepository:
    def __init__(self, raw_document_dir: Path, overwrite_zip: bool):
        self.raw_document_dir = raw_document_dir
        self.overwrite_zip = overwrite_zip

    def get_zip_path(self, stock_code: str, business_year: int, rcept_no: str) -> Path:
        return self.raw_document_dir / stock_code / f"{business_year}_{rcept_no}.zip"

    def get_or_save(
        self,
        stock_code: str,
        business_year: int,
        rcept_no: str,
        zip_bytes: bytes,
    ) -> Path:
        path = self.get_zip_path(stock_code, business_year, rcept_no)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists() and not self.overwrite_zip:
            return path

        path.write_bytes(zip_bytes)
        return path

    def exists(self, stock_code: str, business_year: int, rcept_no: str) -> bool:
        path = self.get_zip_path(stock_code, business_year, rcept_no)
        return path.exists() and not self.overwrite_zip


# ============================================================
# 7. Extract text from ZIP
# ============================================================

class DartReportTextExtractor:
    VALID_EXTENSIONS = {".xml", ".html", ".htm", ".txt"}

    def __init__(self, interim_text_dir: Path):
        self.interim_text_dir = interim_text_dir

    def extract(self, zip_path: Path, stock_code: str, business_year: int) -> tuple[str, Path]:
        text_path = self.interim_text_dir / f"{stock_code}_{business_year}.txt"
        text_path.parent.mkdir(parents=True, exist_ok=True)

        combined_parts: list[str] = []

        with zipfile.ZipFile(zip_path, "r") as zf:
            target_names = [
                name
                for name in zf.namelist()
                if Path(name).suffix.lower() in self.VALID_EXTENSIONS
            ]

            target_names = sorted(
                target_names,
                key=lambda name: zf.getinfo(name).file_size,
                reverse=True,
            )

            for name in target_names:
                raw = zf.read(name)
                decoded = TextCleaner.decode_bytes(raw)
                suffix = Path(name).suffix.lower()

                if suffix in {".xml", ".html", ".htm"}:
                    text = TextCleaner.html_to_text(decoded)
                else:
                    text = TextCleaner.clean_text(decoded)

                if text:
                    combined_parts.append(f"\n\n===== FILE: {name} =====\n\n{text}")

        combined_text = TextCleaner.clean_text("\n\n".join(combined_parts))
        text_path.write_text(combined_text, encoding="utf-8")

        return combined_text, text_path


# ============================================================
# 8. Extract report sections
# ============================================================

class ReportSectionExtractor:
    def extract_all(self, text: str) -> dict[str, str]:
        return {
            "company_overview_raw": self._extract_section(
                text=text,
                start_patterns=[
                    r"회사의\s*개요",
                    r"회사\s*개요",
                    r"기업의\s*개요",
                ],
                end_patterns=[
                    r"사업의\s*내용",
                    r"사업\s*내용",
                    r"재무에\s*관한\s*사항",
                ],
                fallback_keywords=["설립", "본사", "주요 사업"],
                max_chars=4000,
            ),
            "business_content_raw": self._extract_section(
                text=text,
                start_patterns=[
                    r"사업의\s*내용",
                    r"사업\s*내용",
                ],
                end_patterns=[
                    r"재무에\s*관한\s*사항",
                    r"이사의\s*경영진단",
                    r"감사인의\s*감사의견",
                    r"주주에\s*관한\s*사항",
                ],
                fallback_keywords=["주요 제품", "매출", "사업부문", "시장점유율"],
                max_chars=9000,
            ),
            "main_products_raw": self._extract_section(
                text=text,
                start_patterns=[
                    r"주요\s*제품\s*및\s*서비스",
                    r"주요\s*제품과\s*서비스",
                    r"주요\s*제품",
                    r"제품\s*및\s*서비스",
                ],
                end_patterns=[
                    r"원재료",
                    r"생산\s*및\s*설비",
                    r"매출\s*및\s*수주상황",
                    r"위험관리",
                    r"연구개발",
                ],
                fallback_keywords=["제품", "서비스", "상품", "매출"],
                max_chars=5000,
            ),
            "sales_order_raw": self._extract_section(
                text=text,
                start_patterns=[
                    r"매출\s*및\s*수주상황",
                    r"매출\s*및\s*수주\s*상황",
                    r"매출에\s*관한\s*사항",
                    r"매출\s*실적",
                ],
                end_patterns=[
                    r"위험관리",
                    r"파생거래",
                    r"연구개발",
                    r"그\s*밖에\s*투자의사결정",
                    r"기타\s*참고사항",
                ],
                fallback_keywords=["매출", "수주", "수출", "내수"],
                max_chars=5000,
            ),
            "risk_raw": self._extract_section(
                text=text,
                start_patterns=[
                    r"위험관리\s*및\s*파생거래",
                    r"위험관리",
                    r"시장위험",
                    r"신용위험",
                    r"유동성위험",
                ],
                end_patterns=[
                    r"연구개발",
                    r"그\s*밖에\s*투자의사결정",
                    r"기타\s*참고사항",
                    r"재무에\s*관한\s*사항",
                ],
                fallback_keywords=["위험", "환율", "이자율", "신용위험", "유동성위험"],
                max_chars=5000,
            ),
            "rnd_raw": self._extract_section(
                text=text,
                start_patterns=[
                    r"연구개발활동",
                    r"연구\s*개발\s*활동",
                    r"연구개발\s*실적",
                ],
                end_patterns=[
                    r"그\s*밖에\s*투자의사결정",
                    r"기타\s*참고사항",
                    r"재무에\s*관한\s*사항",
                ],
                fallback_keywords=["연구개발", "R&D", "특허"],
                max_chars=4000,
            ),
        }

    def _extract_section(
        self,
        text: str,
        start_patterns: list[str],
        end_patterns: list[str],
        fallback_keywords: list[str],
        max_chars: int,
    ) -> str:
        if not text:
            return ""

        start = self._find_first(text, start_patterns)

        if start is not None:
            end = self._find_next(text, end_patterns, start + 100)

            if end is None:
                section = text[start:start + max_chars]
            else:
                section = text[start:end]

            return TextCleaner.compact(section, max_chars=max_chars)

        fallback = self._fallback(text, fallback_keywords, max_chars)
        return TextCleaner.compact(fallback, max_chars=max_chars)

    def _find_first(self, text: str, patterns: list[str]) -> Optional[int]:
        positions: list[int] = []

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                positions.append(match.start())

        if not positions:
            return None

        return min(positions)

    def _find_next(self, text: str, patterns: list[str], start_pos: int) -> Optional[int]:
        positions: list[int] = []
        sub_text = text[start_pos:]

        for pattern in patterns:
            match = re.search(pattern, sub_text, flags=re.IGNORECASE)
            if match:
                positions.append(start_pos + match.start())

        if not positions:
            return None

        return min(positions)

    def _fallback(self, text: str, keywords: list[str], window: int) -> str:
        positions: list[int] = []

        for keyword in keywords:
            pos = text.find(keyword)
            if pos >= 0:
                positions.append(pos)

        if not positions:
            return ""

        center = min(positions)
        start = max(0, center - window // 4)
        end = min(len(text), start + window)

        return text[start:end]


# ============================================================
# 9. Build LLM JSONL rows
# ============================================================

class LlmInputBuilder:
    def build_rows(self, profile_df: pd.DataFrame) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for _, row in profile_df.iterrows():
            rows.append(
                {
                    "task": "generate_historical_stock_profile",
                    "asset_type": "stock",
                    "stock_code": str(row.get("stock_code", "")),
                    "corp_code": str(row.get("corp_code", "")),
                    "stock_name": str(row.get("dart_name", "")),
                    "business_year": int(row.get("business_year")),
                    "source": {
                        "name": "DART",
                        "report_name": str(row.get("report_name", "")),
                        "rcept_no": str(row.get("rcept_no", "")),
                        "rcept_date": str(row.get("rcept_date", "")),
                    },
                    "raw_facts": {
                        "company_overview": str(row.get("company_overview_raw", "")),
                        "business_content": str(row.get("business_content_raw", "")),
                        "main_products": str(row.get("main_products_raw", "")),
                        "sales_order": str(row.get("sales_order_raw", "")),
                        "risk": str(row.get("risk_raw", "")),
                        "rnd": str(row.get("rnd_raw", "")),
                    },
                    "output_schema": {
                        "business_summary_asof": "해당 연도 기준 기업이 무엇을 하는 회사인지 1~2문장",
                        "main_products_asof": ["주요 제품/서비스"],
                        "business_segments_asof": ["사업부문"],
                        "risk_keywords_asof": ["주요 리스크 키워드"],
                        "macro_sensitive_factors": ["환율", "금리", "유가", "경기", "수출", "소비심리 등"],
                        "news_reaction_tags": ["게임 뉴스와 연결할 반응 태그"],
                        "asset_personality": "성장형/가치형/경기민감형/방어형/배당형/테마형 중 복수 가능",
                        "beginner_description": "초보 투자자용 설명 2~3문장",
                    },
                    "constraints": [
                        "raw_facts에 없는 내용을 사실처럼 추가하지 말 것",
                        "해당 business_year 이후에 발생한 사건이나 사업 변화를 넣지 말 것",
                        "투자 추천 문구를 쓰지 말 것",
                        "게임 내 설명용으로 중립적이고 간결하게 작성할 것",
                    ],
                }
            )

        return rows


# ============================================================
# 10. Main Pipeline
# ============================================================

class DartLlmProfilePipeline:
    def __init__(self, config: PipelineConfig):
        self.config = config

        self.loader = DartResultsLoader(config.input_json_path)
        self.indexer = BusinessReportIndexer(
            start_year=config.start_year,
            end_year=config.end_year,
            report_version=config.report_version,
        )
        self.document_client = DartDocumentClient(
            api_key=config.dart_api_key,
            sleep_seconds=config.request_sleep_seconds,
        )
        self.document_repository = DartDocumentRepository(
            raw_document_dir=config.raw_document_dir,
            overwrite_zip=config.overwrite_zip,
        )
        self.text_extractor = DartReportTextExtractor(
            interim_text_dir=config.interim_text_dir,
        )
        self.section_extractor = ReportSectionExtractor()
        self.llm_input_builder = LlmInputBuilder()

    def run(self) -> None:
        self._prepare_dirs()

        company_results = self.loader.load_results()

        report_index_df = self.indexer.build(company_results)

        if report_index_df.empty:
            raise RuntimeError("사업보고서 index가 비었습니다. report_nm 필드와 입력 JSON 구조를 확인하세요.")

        index_path = self.config.processed_dir / "dart_business_report_index.csv"
        CsvWriter.write_dataframe(index_path, report_index_df)

        print(f"[저장] 사업보고서 index: {index_path}")
        print(f"[대상] 사업보고서 수: {len(report_index_df):,}")

        profile_rows: list[SectionExtractionResult] = []
        download_failures: list[FailureRow] = []
        section_failures: list[FailureRow] = []

        for row in tqdm(report_index_df.itertuples(index=False), total=len(report_index_df)):
            try:
                zip_path = self._get_document_zip(row)
            except Exception as e:
                download_failures.append(
                    FailureRow(
                        stock_code=row.stock_code,
                        corp_code=row.corp_code,
                        dart_name=row.dart_name,
                        input_name=row.input_name,
                        business_year=int(row.business_year),
                        rcept_no=row.rcept_no,
                        stage="download_zip",
                        message=str(e),
                    )
                )
                continue

            try:
                full_text, text_path = self.text_extractor.extract(
                    zip_path=zip_path,
                    stock_code=row.stock_code,
                    business_year=int(row.business_year),
                )

                sections = self.section_extractor.extract_all(full_text)

                if not sections.get("business_content_raw"):
                    section_failures.append(
                        FailureRow(
                            stock_code=row.stock_code,
                            corp_code=row.corp_code,
                            dart_name=row.dart_name,
                            input_name=row.input_name,
                            business_year=int(row.business_year),
                            rcept_no=row.rcept_no,
                            stage="extract_section",
                            message="business_content_raw가 비어 있음",
                        )
                    )

                profile_rows.append(
                    SectionExtractionResult(
                        stock_code=row.stock_code,
                        corp_code=row.corp_code,
                        dart_name=row.dart_name,
                        input_name=row.input_name,
                        business_year=int(row.business_year),
                        disclosure_year=int(row.disclosure_year),
                        report_name=row.report_name,
                        rcept_no=row.rcept_no,
                        rcept_date=row.rcept_date,
                        company_overview_raw=sections.get("company_overview_raw", ""),
                        business_content_raw=sections.get("business_content_raw", ""),
                        main_products_raw=sections.get("main_products_raw", ""),
                        sales_order_raw=sections.get("sales_order_raw", ""),
                        risk_raw=sections.get("risk_raw", ""),
                        rnd_raw=sections.get("rnd_raw", ""),
                        document_zip_path=str(zip_path),
                        text_path=str(text_path),
                        extracted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )

            except Exception as e:
                section_failures.append(
                    FailureRow(
                        stock_code=row.stock_code,
                        corp_code=row.corp_code,
                        dart_name=row.dart_name,
                        input_name=row.input_name,
                        business_year=int(row.business_year),
                        rcept_no=row.rcept_no,
                        stage="extract_text_or_section",
                        message=str(e),
                    )
                )

        profile_df = pd.DataFrame([asdict(row) for row in profile_rows])

        profile_csv_path = self.config.processed_dir / "stock_llm_profile_yearly_raw.csv"
        CsvWriter.write_dataframe(profile_csv_path, profile_df)

        jsonl_rows = self.llm_input_builder.build_rows(profile_df)
        jsonl_path = self.config.processed_dir / "stock_llm_input_yearly.jsonl"
        JsonlWriter.write(jsonl_path, jsonl_rows)

        download_failure_path = self.config.processed_dir / "dart_download_failures.csv"
        section_failure_path = self.config.processed_dir / "dart_section_failures.csv"

        CsvWriter.write_dataframe(
            download_failure_path,
            pd.DataFrame([asdict(row) for row in download_failures]),
        )
        CsvWriter.write_dataframe(
            section_failure_path,
            pd.DataFrame([asdict(row) for row in section_failures]),
        )

        print("\n[완료]")
        print(f"- profile CSV: {profile_csv_path}")
        print(f"- LLM input JSONL: {jsonl_path}")
        print(f"- download failures: {download_failure_path} ({len(download_failures):,}건)")
        print(f"- section failures: {section_failure_path} ({len(section_failures):,}건)")
        print(f"- 성공 profile rows: {len(profile_df):,}")

    def _prepare_dirs(self) -> None:
        self.config.raw_document_dir.mkdir(parents=True, exist_ok=True)
        self.config.interim_text_dir.mkdir(parents=True, exist_ok=True)
        self.config.processed_dir.mkdir(parents=True, exist_ok=True)

    def _get_document_zip(self, row: Any) -> Path:
        stock_code = row.stock_code
        business_year = int(row.business_year)
        rcept_no = row.rcept_no

        zip_path = self.document_repository.get_zip_path(
            stock_code=stock_code,
            business_year=business_year,
            rcept_no=rcept_no,
        )

        if zip_path.exists() and not self.config.overwrite_zip:
            return zip_path

        zip_bytes = self.document_client.download(rcept_no=rcept_no)

        return self.document_repository.get_or_save(
            stock_code=stock_code,
            business_year=business_year,
            rcept_no=rcept_no,
            zip_bytes=zip_bytes,
        )


# ============================================================
# 11. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="기존 dart_results_2013_2023.json에서 사업보고서 원문을 받아 LLM용 기업 profile을 생성합니다."
    )

    parser.add_argument(
        "--input",
        required=True,
        help="기존 dart_results_2013_2023.json 경로",
    )
    parser.add_argument(
        "--output-dir",
        default="dart_llm_profile_output",
        help="출력 폴더",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=2013,
        help="시작 사업연도",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=2023,
        help="종료 사업연도",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="DART 요청 간 sleep 초",
    )
    parser.add_argument(
        "--overwrite-zip",
        action="store_true",
        help="기존 다운로드 ZIP을 덮어쓸지 여부",
    )
    parser.add_argument(
        "--report-version",
        choices=["latest", "original"],
        default="latest",
        help="동일 사업연도에 정정 공시가 있을 때 latest=최신본, original=원본 우선",
    )

    return parser.parse_args()


def main() -> None:
    load_dotenv()

    args = parse_args()

    dart_api_key = os.getenv("DART_API_KEY")

    if not dart_api_key:
        raise EnvironmentError(".env 파일에 DART_API_KEY가 없습니다.")

    config = PipelineConfig(
        dart_api_key=dart_api_key,
        input_json_path=Path(args.input),
        output_dir=Path(args.output_dir),
        start_year=args.start_year,
        end_year=args.end_year,
        request_sleep_seconds=args.sleep,
        overwrite_zip=args.overwrite_zip,
        report_version=args.report_version,
    )

    pipeline = DartLlmProfilePipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()