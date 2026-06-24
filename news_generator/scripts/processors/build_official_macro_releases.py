#!/usr/bin/env python3
"""Collect dated macro announcements from official institution pages.

Only records with an official source URL and an extracted numeric result are
written. The output is intentionally separate from the hand-written macro
event calendar.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


FED_BASE = "https://www.federalreserve.gov"
FED_CURRENT = f"{FED_BASE}/monetarypolicy/fomccalendars.htm"
BOK_RATE_URL = "https://www.bok.or.kr/portal/singl/baseRate/progress.do?dataSeCd=01&menuNo=200643"
BEA_ARCHIVE = "https://www.bea.gov/news/archive"


@dataclass(frozen=True)
class OfficialRelease:
    event_date: str
    source_release_date: str
    event_type: str
    release_category: str
    region: str
    institution: str
    title: str
    description: str
    reference_period: str
    affected_markets: str
    direction: str
    severity: str
    source_url: str
    key_figures_json: str
    verification_status: str = "official_source_verified"


class OfficialReleaseCollector:
    def __init__(self, start_year: int, end_year: int, timeout: int = 30):
        self.start_year = start_year
        self.end_year = end_year
        self.timeout = timeout

    def get(self, url: str, params: dict[str, str] | None = None) -> str:
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "data-pipeline official-release collector/1.0"})
        with urlopen(request, timeout=self.timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    @staticmethod
    def text(html_text: str) -> str:
        clean = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
        clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.I | re.S)
        clean = re.sub(r"<[^>]+>", " ", clean)
        return re.sub(r"\s+", " ", html.unescape(clean)).strip()

    @staticmethod
    def rate_number(raw: str) -> float:
        raw = re.sub(r"[‐‑‒–—]", "-", raw.strip())
        mixed = re.fullmatch(r"(\d+)-(\d+)/(\d+)", raw)
        if mixed:
            return float(mixed.group(1)) + float(mixed.group(2)) / float(mixed.group(3))
        fraction = re.fullmatch(r"(\d+)/(\d+)", raw)
        if fraction:
            return float(fraction.group(1)) / float(fraction.group(2))
        return float(raw)

    @staticmethod
    def next_weekday(raw_date: str) -> str:
        result = date.fromisoformat(raw_date) + timedelta(days=1)
        while result.weekday() >= 5:
            result += timedelta(days=1)
        return result.isoformat()

    def collect_fomc(self) -> list[OfficialRelease]:
        links: dict[str, str] = {}
        for year in range(self.start_year, self.end_year + 1):
            page_url = (
                f"{FED_BASE}/monetarypolicy/fomchistorical{year}.htm"
                if year <= 2020 else FED_CURRENT
            )
            page = self.get(page_url)
            pattern = rf'href="([^"]*monetary({year}\d{{4}})a\.htm)"'
            for href, yyyymmdd in re.findall(pattern, page, flags=re.I):
                links[yyyymmdd] = urljoin(FED_BASE, href)

        releases = []
        decision_pattern = re.compile(
            r"Committee decided(?: today)? to (raise|lower|maintain|keep) the target range "
            r"for the federal funds rate.{0,60}?(?:to|at) "
            r"(\d+(?:[‐‑‒–—-]\d+/\d+)?|\d+/\d+)\s+to\s+"
            r"(\d+(?:[‐‑‒–—-]\d+/\d+)?|\d+/\d+) percent",
            flags=re.I,
        )
        reaffirm_pattern = re.compile(
            r"(?:current|maintain the)\s+"
            r"(\d+(?:[‐‑‒–—-]\d+/\d+)?|\d+/\d+)\s+to\s+"
            r"(\d+(?:[‐‑‒–—-]\d+/\d+)?|\d+/\d+) percent target range "
            r"for the federal funds rate",
            flags=re.I,
        )
        for yyyymmdd, url in sorted(links.items()):
            statement = self.text(self.get(url))
            match = decision_pattern.search(statement)
            if match:
                decision = match.group(1).lower()
                lower = self.rate_number(match.group(2))
                upper = self.rate_number(match.group(3))
            else:
                reaffirmed = reaffirm_pattern.search(statement)
                if not reaffirmed:
                    continue
                decision = "maintain"
                lower = self.rate_number(reaffirmed.group(1))
                upper = self.rate_number(reaffirmed.group(2))
            action = {"raise": "인상", "lower": "인하", "maintain": "동결", "keep": "동결"}[decision]
            direction = {"raise": "negative", "lower": "positive"}.get(decision, "neutral")
            source_date = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
            releases.append(OfficialRelease(
                event_date=self.next_weekday(source_date),
                source_release_date=source_date,
                event_type="official_release",
                release_category="monetary_policy",
                region="us",
                institution="미국 연방준비제도 연방공개시장위원회(FOMC)",
                title=f"FOMC 연방기금금리 목표 범위 {action}",
                description=f"FOMC가 연방기금금리 목표 범위를 {lower:g}~{upper:g}%로 {action}했다.",
                reference_period=source_date,
                affected_markets="us_rates/global_equity/usd",
                direction=direction,
                severity="high" if decision in {"raise", "lower"} else "moderate",
                source_url=url,
                key_figures_json=json.dumps({"target_lower_pct": lower, "target_upper_pct": upper, "action": action}, ensure_ascii=False),
            ))
        return releases

    def collect_bok_rate_changes(self) -> list[OfficialRelease]:
        page = self.get(BOK_RATE_URL)
        pairs = re.findall(r'\["(\d{4})/(\d{2})/(\d{2})\s*",\s*([0-9.]+)\]', page)
        releases = []
        previous_rate = None
        for year, month, day, raw_rate in pairs:
            rate = float(raw_rate)
            year_num = int(year)
            if previous_rate is None:
                previous_rate = rate
                continue
            change_bp = round((rate - previous_rate) * 100, 6)
            previous_rate = rate
            if not self.start_year <= year_num <= self.end_year:
                continue
            action = "인상" if change_bp > 0 else "인하"
            direction = "negative" if change_bp > 0 else "positive"
            date = f"{year}-{month}-{day}"
            releases.append(OfficialRelease(
                event_date=date,
                source_release_date=date,
                event_type="official_release",
                release_category="monetary_policy",
                region="kr",
                institution="한국은행 금융통화위원회",
                title=f"한국은행 기준금리 {action}",
                description=f"한국은행 금융통화위원회가 기준금리를 {abs(change_bp):g}bp {action}해 {rate:g}%로 결정했다.",
                reference_period=date,
                affected_markets="kr_rates/kospi/usdkrw",
                direction=direction,
                severity="high" if abs(change_bp) >= 50 else "moderate",
                source_url=BOK_RATE_URL,
                key_figures_json=json.dumps({"base_rate_pct": rate, "change_bp": change_bp, "action": action}, ensure_ascii=False),
            ))
        return releases

    def collect_bea_gdp_advance(self) -> list[OfficialRelease]:
        releases = []
        year_to_filter = {year: 2025 - year for year in range(2013, 2024)}
        row_pattern = re.compile(
            r'<tr class="release-row">.*?href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<time datetime="(\d{4}-\d{2}-\d{2})T',
            flags=re.I | re.S,
        )
        gdp_pattern = re.compile(
            r"Real gross domestic product.*?(increased|decreased) at an annual rate of "
            r"([0-9.]+) percent in the ([^.]+?)(?:\s*\(|\.)",
            flags=re.I,
        )
        for year in range(self.start_year, self.end_year + 1):
            filter_id = year_to_filter.get(year)
            if filter_id is None:
                continue
            archive = self.get(BEA_ARCHIVE, params={
                "field_related_product_target_id": "451",
                "created_1": str(filter_id),
            })
            for href, raw_title, date in row_pattern.findall(archive):
                title_en = self.text(raw_title)
                lowered = title_en.lower()
                if "advance estimate" not in lowered and "initial estimate" not in lowered:
                    continue
                url = urljoin("https://www.bea.gov", href)
                release_text = self.text(self.get(url))
                match = gdp_pattern.search(release_text)
                if not match:
                    continue
                movement, raw_rate, reference_period = match.groups()
                rate = float(raw_rate) * (1 if movement.lower() == "increased" else -1)
                direction = "positive" if rate > 0 else "negative"
                verb = "증가" if rate > 0 else "감소"
                reference_period = reference_period.strip()
                quarter_match = re.search(
                    r"(first|second|third|fourth) quarter of (\d{4})",
                    reference_period,
                    flags=re.I,
                )
                if quarter_match:
                    quarter = {"first": 1, "second": 2, "third": 3, "fourth": 4}[
                        quarter_match.group(1).lower()
                    ]
                    reference_period = f"{quarter_match.group(2)}년 {quarter}분기"
                releases.append(OfficialRelease(
                    event_date=self.next_weekday(date),
                    source_release_date=date,
                    event_type="official_release",
                    release_category="growth",
                    region="us",
                    institution="미국 상무부 경제분석국(BEA)",
                    title="미국 분기 GDP 속보치 발표",
                    description=f"미국 BEA가 {reference_period} 실질 GDP가 연율 {abs(rate):g}% {verb}했다고 발표했다.",
                    reference_period=reference_period,
                    affected_markets="us_equity/us_rates/usd/global_equity",
                    direction=direction,
                    severity="high" if abs(rate) >= 5 else "moderate",
                    source_url=url,
                    key_figures_json=json.dumps({"real_gdp_annualized_pct": rate}, ensure_ascii=False),
                ))
        return releases

    def bea_archive_rows(self, product_id: int, year: int) -> list[tuple[str, str, str]]:
        filter_id = 2025 - year
        archive = self.get(BEA_ARCHIVE, params={
            "field_related_product_target_id": str(product_id),
            "created_1": str(filter_id),
        })
        row_pattern = re.compile(
            r'<tr class="release-row">.*?href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<time datetime="(\d{4}-\d{2}-\d{2})T',
            flags=re.I | re.S,
        )
        return [
            (urljoin("https://www.bea.gov", href), self.text(title), release_date)
            for href, title, release_date in row_pattern.findall(archive)
        ]

    @staticmethod
    def korean_month_period(title: str) -> str:
        match = re.search(
            r",\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
            title,
            flags=re.I,
        )
        if not match:
            return ""
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        return f"{match.group(2)}년 {months[match.group(1).lower()]}월"

    def collect_bea_pce_prices(self) -> list[OfficialRelease]:
        releases = []
        pattern = re.compile(
            r"The PCE price index (increased|decreased) ([0-9.]+) percent",
            flags=re.I,
        )
        for year in range(self.start_year, self.end_year + 1):
            for url, title, source_date in self.bea_archive_rows(476, year):
                reference_period = self.korean_month_period(title)
                if not reference_period:
                    continue
                match = pattern.search(self.text(self.get(url)))
                if not match:
                    continue
                movement, raw_rate = match.groups()
                rate = float(raw_rate) * (1 if movement.lower() == "increased" else -1)
                verb = "상승" if rate > 0 else "하락"
                direction = "negative" if rate > 0 else "positive"
                releases.append(OfficialRelease(
                    event_date=self.next_weekday(source_date),
                    source_release_date=source_date,
                    event_type="official_release",
                    release_category="inflation",
                    region="us",
                    institution="미국 상무부 경제분석국(BEA)",
                    title="미국 PCE 물가지수 발표",
                    description=f"미국 BEA가 {reference_period} PCE 물가지수가 전월 대비 {abs(rate):g}% {verb}했다고 발표했다.",
                    reference_period=reference_period,
                    affected_markets="us_rates/usd/global_equity",
                    direction=direction,
                    severity="high" if abs(rate) >= 0.5 else "moderate",
                    source_url=url,
                    key_figures_json=json.dumps({"pce_price_mom_pct": rate}, ensure_ascii=False),
                ))
        return releases

    def collect_bea_trade(self) -> list[OfficialRelease]:
        releases = []
        pattern = re.compile(
            r"goods and services deficit was \$([0-9.]+) billion in ([A-Za-z]+), "
            r"(up|down) \$([0-9.]+) billion",
            flags=re.I,
        )
        for year in range(self.start_year, self.end_year + 1):
            for url, title, source_date in self.bea_archive_rows(496, year):
                reference_period = self.korean_month_period(title)
                if not reference_period or "annual update" in title.lower():
                    continue
                match = pattern.search(self.text(self.get(url)))
                if not match:
                    continue
                raw_deficit, _, movement, raw_change = match.groups()
                deficit = float(raw_deficit)
                change = float(raw_change) * (1 if movement.lower() == "up" else -1)
                deficit_100m = deficit * 10
                change_100m = change * 10
                verb = "증가" if change > 0 else "감소"
                direction = "negative" if change > 0 else "positive"
                releases.append(OfficialRelease(
                    event_date=self.next_weekday(source_date),
                    source_release_date=source_date,
                    event_type="official_release",
                    release_category="trade",
                    region="us",
                    institution="미국 인구조사국·상무부 경제분석국(BEA)",
                    title="미국 상품·서비스 무역수지 발표",
                    description=f"미국 인구조사국과 BEA가 {reference_period} 상품·서비스 무역적자가 {deficit_100m:g}억달러로 전월보다 {abs(change_100m):g}억달러 {verb}했다고 발표했다.",
                    reference_period=reference_period,
                    affected_markets="usd/us_rates/global_equity",
                    direction=direction,
                    severity="high" if abs(change) >= 10 else "moderate",
                    source_url=url,
                    key_figures_json=json.dumps({
                        "trade_deficit_billion_usd": deficit,
                        "change_billion_usd": change,
                        "trade_deficit_100m_usd": deficit_100m,
                        "change_100m_usd": change_100m,
                    }, ensure_ascii=False),
                ))
        return releases

    def collect(self) -> list[OfficialRelease]:
        collectors = (
            self.collect_fomc,
            self.collect_bok_rate_changes,
            self.collect_bea_gdp_advance,
            self.collect_bea_pce_prices,
            self.collect_bea_trade,
        )
        with ThreadPoolExecutor(max_workers=len(collectors)) as executor:
            records = [record for batch in executor.map(lambda fn: fn(), collectors) for record in batch]
        unique = {(record.event_date, record.institution, record.title): record for record in records}
        return sorted(unique.values(), key=lambda item: (item.event_date, item.institution, item.title))


def write_csv(path: Path, records: Iterable[OfficialRelease]) -> None:
    rows = [asdict(record) for record in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OfficialRelease.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2013)
    parser.add_argument("--end-year", type=int, default=2023)
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("news_generator/data/raw/official_macro_releases_2013_2023.csv"),
    )
    args = parser.parse_args()
    collector = OfficialReleaseCollector(args.start_year, args.end_year)
    records = collector.collect()
    write_csv(args.output_csv, records)
    by_category: dict[str, int] = {}
    for record in records:
        by_category[record.release_category] = by_category.get(record.release_category, 0) + 1
    print(json.dumps({"rows": len(records), "by_category": by_category, "output": str(args.output_csv)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
