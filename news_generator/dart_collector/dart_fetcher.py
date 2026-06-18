"""
OpenDART 2013~2023 전체 공시 수집기

기능
- corpCode.xml ZIP을 로컬에 캐싱
- companies.txt의 회사 목록을 읽음
- aliases.json으로 DART 등록명 / stock_code / corp_code 강제 매칭
- 2013~2023년 공시 목록을 연도별로 수집
- 2013~2023년 사업보고서 재무제표를 연도별로 수집
- dart_results_2013_2023.json으로 저장

필요 파일
1. .env
   DART_API_KEY=본인_API_KEY

2. companies.txt
   삼성전자
   KT&G
   엔씨소프트
   F&F홀딩스
   F&F

3. aliases.json 예시
{
  "KT&G": "케이티앤지",
  "엔씨소프트": "NC",
  "한국전력": "한국전력공사",
  "현대차": "현대자동차",
  "스카이라이프": "케이티스카이라이프",
  "KCC": "케이씨씨",
  "F&F홀딩스": {
    "stock_code": "007700"
  },
  "F&F": {
    "stock_code": "383220"
  }
}
"""

from __future__ import annotations

import html
import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
load_dotenv()

API_KEY = os.getenv("DART_API_KEY")
if not API_KEY:
    raise EnvironmentError(".env 파일에 DART_API_KEY가 없습니다.")

BASE_URL = "https://opendart.fss.or.kr/api"

CORP_CODE_PATH = Path("corp_code_cache.xml")
COMPANY_LIST_PATH = Path("companies.txt")
ALIAS_PATH = Path("aliases.json")
RESULT_PATH = Path("dart_results_2013_2024.json")

START_YEAR = 2013
END_YEAR = 2024

REQUEST_SLEEP_SECONDS = 0.25
COMPANY_SLEEP_SECONDS = 0.5

# 013: 조회된 데이터가 없음
SILENT_STATUSES = {"013"}


# ──────────────────────────────────────────────
# 데이터 객체
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class CorpInfo:
    corp_name: str
    corp_code: str
    stock_code: str
    modify_date: str


@dataclass
class MatchResult:
    input_name: str
    dart_name: str
    corp_code: str
    stock_code: str
    modify_date: str
    match_type: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.input_name,
            "dart_name": self.dart_name,
            "corp_code": self.corp_code,
            "stock_code": self.stock_code,
            "modify_date": self.modify_date,
            "match_type": self.match_type,
        }


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────
def normalize_name(name: str) -> str:
    value = html.unescape(name or "")
    value = value.replace(" ", "").replace("\u3000", "")
    return value.upper()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def year_start_date(year: int) -> str:
    return f"{year}0101"


def year_end_date(year: int) -> str:
    return f"{year}1231"


# ──────────────────────────────────────────────
# DART corp_code 저장소
# ──────────────────────────────────────────────
class DartCorpCodeRepository:
    def __init__(self, api_key: str, cache_path: Path = CORP_CODE_PATH):
        self.api_key = api_key
        self.cache_path = cache_path

        self.corp_map: dict[str, CorpInfo] = {}
        self.normalized_map: dict[str, str] = {}
        self.stock_map: dict[str, str] = {}
        self.corp_code_map: dict[str, str] = {}

    def download_if_needed(self, force: bool = False) -> None:
        if self.cache_path.exists() and not force:
            print(f"[캐시] {self.cache_path} 이미 존재 → 다운로드 스킵")
            return

        print("[다운로드] 전체 corp_code ZIP 수신 중...")

        response = requests.get(
            f"{BASE_URL}/corpCode.xml",
            params={"crtfc_key": self.api_key},
            timeout=30,
        )
        response.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            xml_name = next(
                name for name in zf.namelist()
                if name.lower().endswith(".xml")
            )
            self.cache_path.write_bytes(zf.read(xml_name))

        print(f"[완료] corp_code 저장 → {self.cache_path}")

    def build_indexes(self) -> None:
        if not self.cache_path.exists():
            raise FileNotFoundError(
                f"{self.cache_path} 파일이 없습니다. corp_code 다운로드가 필요합니다."
            )

        tree = ET.parse(self.cache_path)
        root = tree.getroot()

        self.corp_map.clear()
        self.normalized_map.clear()
        self.stock_map.clear()
        self.corp_code_map.clear()

        for item in root.iter("list"):
            corp_name = (item.findtext("corp_name") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            stock_code = (item.findtext("stock_code") or "").strip()
            modify_date = (item.findtext("modify_date") or "").strip()

            if not corp_name or not corp_code:
                continue

            info = CorpInfo(
                corp_name=corp_name,
                corp_code=corp_code,
                stock_code=stock_code,
                modify_date=modify_date,
            )

            self.corp_map[corp_name] = info
            self.normalized_map[normalize_name(corp_name)] = corp_name
            self.corp_code_map[corp_code] = corp_name

            if stock_code:
                self.stock_map[stock_code] = corp_name

        print(f"[파싱] 총 {len(self.corp_map):,}개 회사 로드 완료")
        print(f"[파싱] 상장 종목코드 {len(self.stock_map):,}개 로드 완료")

    def get_by_name(self, corp_name: str) -> CorpInfo | None:
        return self.corp_map.get(corp_name)

    def get_by_normalized_name(self, corp_name: str) -> CorpInfo | None:
        original_name = self.normalized_map.get(normalize_name(corp_name))
        if not original_name:
            return None
        return self.corp_map.get(original_name)

    def get_by_stock_code(self, stock_code: str) -> CorpInfo | None:
        original_name = self.stock_map.get(stock_code)
        if not original_name:
            return None
        return self.corp_map.get(original_name)

    def get_by_corp_code(self, corp_code: str) -> CorpInfo | None:
        original_name = self.corp_code_map.get(corp_code)
        if not original_name:
            return None
        return self.corp_map.get(original_name)

    def find_partial_candidates(self, search_name: str) -> list[CorpInfo]:
        candidates: list[CorpInfo] = []

        for corp_name, info in self.corp_map.items():
            if search_name in corp_name or corp_name in search_name:
                candidates.append(info)

        return candidates


# ──────────────────────────────────────────────
# 회사 목록 / 별칭 로더
# ──────────────────────────────────────────────
class CompanyConfigLoader:
    def __init__(
        self,
        company_list_path: Path = COMPANY_LIST_PATH,
        alias_path: Path = ALIAS_PATH,
    ):
        self.company_list_path = company_list_path
        self.alias_path = alias_path

    def load_company_names(self) -> list[str]:
        if not self.company_list_path.exists():
            raise FileNotFoundError(f"{self.company_list_path} 파일이 없습니다.")

        names = [
            line.strip()
            for line in self.company_list_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        print(f"[로드] {self.company_list_path} → {len(names)}개 회사")
        return names

    def load_aliases(self) -> dict[str, Any]:
        if not self.alias_path.exists():
            print(f"[별칭] {self.alias_path} 없음 → 별칭 없이 진행")
            return {}

        aliases = read_json(self.alias_path)
        print(f"[별칭] {self.alias_path} → {len(aliases)}개 로드")
        return aliases


# ──────────────────────────────────────────────
# 회사명 매칭
# ──────────────────────────────────────────────
class CompanyMatcher:
    def __init__(
        self,
        repository: DartCorpCodeRepository,
        aliases: dict[str, Any],
    ):
        self.repository = repository
        self.aliases = aliases

    def match_all(self, company_names: list[str]) -> tuple[list[dict], list[str]]:
        matched: list[dict] = []
        unmatched: list[str] = []

        for input_name in company_names:
            result = self._match_one(input_name)

            if result is None:
                unmatched.append(input_name)
            else:
                matched.append(result.to_dict())

        print(f"\n[매핑 결과] 성공: {len(matched)}개 / 실패: {len(unmatched)}개")

        if unmatched:
            print("[미매칭 회사]")
            for name in unmatched:
                print(f"  - {name}")

        return matched, unmatched

    def _match_one(self, input_name: str) -> MatchResult | None:
        alias_value = self.aliases.get(input_name)

        if isinstance(alias_value, dict):
            return self._match_by_alias_dict(input_name, alias_value)

        search_name = input_name

        if isinstance(alias_value, str):
            search_name = alias_value.strip()
            print(f"  [별칭] '{input_name}' → '{search_name}'")

        elif alias_value is not None:
            print(f"  [별칭오류] '{input_name}' → alias 값 형식 오류: {alias_value}")
            return None

        info = self.repository.get_by_name(search_name)
        if info:
            return self._make_result(
                input_name=input_name,
                info=info,
                match_type="정확매칭",
            )

        info = self.repository.get_by_normalized_name(search_name)
        if info:
            return self._make_result(
                input_name=input_name,
                info=info,
                match_type="정규화매칭",
            )

        candidates = self.repository.find_partial_candidates(search_name)

        if candidates:
            print(
                f"  [부분후보] '{input_name}' → 자동 매칭하지 않음. "
                f"aliases.json에 정확한 corp_name, stock_code, corp_code 중 하나를 추가해야 함."
            )

            for candidate in candidates[:10]:
                print(
                    f"             '{candidate.corp_name}' "
                    f"(corp_code={candidate.corp_code}, "
                    f"stock_code={candidate.stock_code or '-'})"
                )

            if len(candidates) > 10:
                print(f"             ... 외 {len(candidates) - 10}건")

            return None

        print(f"  [미매칭] '{input_name}' → 검색명 '{search_name}'")
        return None

    def _match_by_alias_dict(
        self,
        input_name: str,
        alias_value: dict[str, Any],
    ) -> MatchResult | None:
        stock_code = str(alias_value.get("stock_code", "")).strip()
        corp_code = str(alias_value.get("corp_code", "")).strip()
        corp_name = str(
            alias_value.get("corp_name")
            or alias_value.get("dart_name")
            or alias_value.get("name")
            or ""
        ).strip()

        if stock_code:
            info = self.repository.get_by_stock_code(stock_code)

            if info:
                return self._make_result(
                    input_name=input_name,
                    info=info,
                    match_type="종목코드매칭",
                )

            print(f"  [미매칭] '{input_name}' → stock_code={stock_code} 없음")
            return None

        if corp_code:
            info = self.repository.get_by_corp_code(corp_code)

            if info:
                return self._make_result(
                    input_name=input_name,
                    info=info,
                    match_type="corp_code매칭",
                )

            print(f"  [미매칭] '{input_name}' → corp_code={corp_code} 없음")
            return None

        if corp_name:
            info = self.repository.get_by_name(corp_name)

            if info:
                return self._make_result(
                    input_name=input_name,
                    info=info,
                    match_type="별칭회사명매칭",
                )

            info = self.repository.get_by_normalized_name(corp_name)

            if info:
                return self._make_result(
                    input_name=input_name,
                    info=info,
                    match_type="별칭정규화매칭",
                )

            print(f"  [미매칭] '{input_name}' → alias corp_name='{corp_name}' 없음")
            return None

        print(
            f"  [별칭오류] '{input_name}' → dict alias에는 "
            f"stock_code, corp_code, corp_name 중 하나가 필요함"
        )
        return None

    def _make_result(
        self,
        input_name: str,
        info: CorpInfo,
        match_type: str,
    ) -> MatchResult:
        print(
            f"  [{match_type}] '{input_name}' → '{info.corp_name}' "
            f"(corp_code={info.corp_code}, stock_code={info.stock_code or '-'})"
        )

        return MatchResult(
            input_name=input_name,
            dart_name=info.corp_name,
            corp_code=info.corp_code,
            stock_code=info.stock_code,
            modify_date=info.modify_date,
            match_type=match_type,
        )


# ──────────────────────────────────────────────
# DART API 클라이언트
# ──────────────────────────────────────────────
class DartApiClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        self.api_key = api_key
        self.base_url = base_url

    def get_disclosures(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_ty: str = "",
        page_count: int = 100,
    ) -> list[dict]:
        all_items: list[dict] = []
        page_no = 1

        while True:
            params: dict[str, Any] = {
                "crtfc_key": self.api_key,
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_no": page_no,
                "page_count": page_count,
            }

            if pblntf_ty:
                params["pblntf_ty"] = pblntf_ty

            data = self._get_json(
                endpoint="list.json",
                params=params,
                timeout=20,
            )

            status = data.get("status", "")

            if status == "000":
                items = data.get("list", [])
                all_items.extend(items)

                total_page = int(data.get("total_page", 1) or 1)

                if page_no >= total_page:
                    break

                page_no += 1
                time.sleep(REQUEST_SLEEP_SECONDS)
                continue

            if status in SILENT_STATUSES:
                break

            print(
                f"    ⚠ 공시 API 오류 "
                f"status={status} msg={data.get('message')}"
            )
            break

        return all_items

    def get_annual_financial_report(
        self,
        corp_code: str,
        year: int,
        reprt_code: str = "11011",
        fs_div: str = "CFS",
    ) -> list[dict]:
        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        }

        data = self._get_json(
            endpoint="fnlttSinglAcntAll.json",
            params=params,
            timeout=20,
        )

        status = data.get("status", "")

        if status == "000":
            return data.get("list", [])

        if status in SILENT_STATUSES:
            return []

        print(
            f"    ⚠ 재무제표 API 오류 "
            f"year={year} status={status} msg={data.get('message')}"
        )
        return []

    def _get_json(
        self,
        endpoint: str,
        params: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"

        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()

        try:
            return response.json()
        except json.JSONDecodeError:
            print(f"    ⚠ JSON 파싱 실패 endpoint={endpoint}")
            return {
                "status": "JSON_ERROR",
                "message": response.text[:500],
            }


# ──────────────────────────────────────────────
# 전체 수집 파이프라인
# ──────────────────────────────────────────────
class DartDisclosureCollector:
    def __init__(
        self,
        corp_repository: DartCorpCodeRepository,
        config_loader: CompanyConfigLoader,
        api_client: DartApiClient,
        start_year: int = START_YEAR,
        end_year: int = END_YEAR,
    ):
        self.corp_repository = corp_repository
        self.config_loader = config_loader
        self.api_client = api_client
        self.start_year = start_year
        self.end_year = end_year

    def run(self) -> dict[str, Any]:
        self.corp_repository.download_if_needed(force=False)
        self.corp_repository.build_indexes()

        company_names = self.config_loader.load_company_names()
        aliases = self.config_loader.load_aliases()

        matcher = CompanyMatcher(
            repository=self.corp_repository,
            aliases=aliases,
        )

        matched_companies, unmatched = matcher.match_all(company_names)

        results: dict[str, Any] = {}

        for idx, company in enumerate(matched_companies, start=1):
            input_name = company["name"]
            dart_name = company["dart_name"]
            corp_code = company["corp_code"]
            stock_code = company.get("stock_code", "")

            print(
                f"\n[{idx}/{len(matched_companies)}] [수집] "
                f"{input_name} → {dart_name} "
                f"(corp_code={corp_code}, stock_code={stock_code or '-'})"
            )

            disclosures_by_year = self._collect_disclosures_by_year(
                corp_code=corp_code,
            )

            financials_by_year = self._collect_financials_by_year(
                corp_code=corp_code,
                stock_code=stock_code,
            )

            total_disclosures = sum(
                len(items) for items in disclosures_by_year.values()
            )
            total_financial_items = sum(
                len(items) for items in financials_by_year.values()
            )

            results[input_name] = {
                "input_name": input_name,
                "dart_name": dart_name,
                "corp_code": corp_code,
                "stock_code": stock_code,
                "modify_date": company.get("modify_date", ""),
                "match_type": company.get("match_type", ""),
                "period": {
                    "start_year": self.start_year,
                    "end_year": self.end_year,
                },
                "summary": {
                    "total_disclosures": total_disclosures,
                    "total_financial_items": total_financial_items,
                },
                "disclosures_by_year": disclosures_by_year,
                "financials_by_year": financials_by_year,
            }

            time.sleep(COMPANY_SLEEP_SECONDS)

        output = {
            "collected_at": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
            "period": {
                "start_year": self.start_year,
                "end_year": self.end_year,
                "bgn_de": year_start_date(self.start_year),
                "end_de": year_end_date(self.end_year),
            },
            "count": {
                "input": len(company_names),
                "matched": len(matched_companies),
                "unmatched": len(unmatched),
                "collected": len(results),
            },
            "unmatched": unmatched,
            "results": results,
        }

        write_json(RESULT_PATH, output)

        print(f"\n[완료] 수집 회사 수: {len(results)}개")
        print(f"[기간] {self.start_year}년 ~ {self.end_year}년")
        print(f"[저장] {RESULT_PATH}")

        if unmatched:
            print("\n[미매칭 회사]")
            for name in unmatched:
                print(f"  - {name}")

        return output

    def _collect_disclosures_by_year(self, corp_code: str) -> dict[str, list[dict]]:
        disclosures_by_year: dict[str, list[dict]] = {}

        for year in range(self.start_year, self.end_year + 1):
            bgn_de = year_start_date(year)
            end_de = year_end_date(year)

            disclosures = self.api_client.get_disclosures(
                corp_code=corp_code,
                bgn_de=bgn_de,
                end_de=end_de,
            )

            disclosures_by_year[str(year)] = disclosures

            print(f"  공시 {year}: {len(disclosures)}건")

            time.sleep(REQUEST_SLEEP_SECONDS)

        return disclosures_by_year

    def _collect_financials_by_year(
        self,
        corp_code: str,
        stock_code: str,
    ) -> dict[str, list[dict]]:
        financials_by_year: dict[str, list[dict]] = {}

        if not stock_code:
            print("  재무제표: stock_code 없음 → 스킵")
            return financials_by_year

        for year in range(self.start_year, self.end_year + 1):
            financials = self.api_client.get_annual_financial_report(
                corp_code=corp_code,
                year=year,
                reprt_code="11011",
                fs_div="CFS",
            )

            # 연결재무제표가 없으면 별도재무제표로 한 번 더 시도
            if not financials:
                financials = self.api_client.get_annual_financial_report(
                    corp_code=corp_code,
                    year=year,
                    reprt_code="11011",
                    fs_div="OFS",
                )

                if financials:
                    print(f"  재무제표 {year}: {len(financials)}개 항목 (OFS)")

            else:
                print(f"  재무제표 {year}: {len(financials)}개 항목 (CFS)")

            if not financials:
                print(f"  재무제표 {year}: 0개 항목")

            financials_by_year[str(year)] = financials

            time.sleep(REQUEST_SLEEP_SECONDS)

        return financials_by_year


# ──────────────────────────────────────────────
# 실행부
# ──────────────────────────────────────────────
def main() -> None:
    corp_repository = DartCorpCodeRepository(
        api_key=API_KEY,
        cache_path=CORP_CODE_PATH,
    )

    config_loader = CompanyConfigLoader(
        company_list_path=COMPANY_LIST_PATH,
        alias_path=ALIAS_PATH,
    )

    api_client = DartApiClient(
        api_key=API_KEY,
        base_url=BASE_URL,
    )

    collector = DartDisclosureCollector(
        corp_repository=corp_repository,
        config_loader=config_loader,
        api_client=api_client,
        start_year=START_YEAR,
        end_year=END_YEAR,
    )

    collector.run()


if __name__ == "__main__":
    main()