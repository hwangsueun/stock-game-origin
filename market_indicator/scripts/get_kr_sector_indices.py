import os
import re
import time
from pathlib import Path
from typing import Dict, List, Set

from dotenv import load_dotenv

# scripts 폴더 기준으로 프로젝트 루트의 .env를 명시적으로 로드
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

krx_id = os.getenv("KRX_ID")
krx_pw = os.getenv("KRX_PW")

if not krx_id or not krx_pw:
    raise RuntimeError(
        f"KRX_ID 또는 KRX_PW를 읽지 못했습니다. 확인 경로: {ENV_PATH}"
    )

print(f"[환경변수 확인] KRX_ID loaded: {krx_id[:2]}***")
print(f"[환경변수 확인] KRX_PW loaded: {bool(krx_pw)}")

import pandas as pd
from pykrx import stock


class KrxSectorIndexCollector:
    """
    KRX 업종지수 자동 수집기.

    수집 대상:
    - KOSPI 업종지수 후보
    - KOSDAQ 업종지수 후보

    저장 파일:
    - data/raw/kr_index_master_all.csv
    - data/raw/kr_index_master_sector_candidates.csv
    - data/raw/kr_sector_indices_long_20130101_20231231.csv
    - data/raw/kr_sector_indices_close_wide_20130101_20231231.csv
    """

    def __init__(
        self,
        start_date: str = "20130101",
        end_date: str = "20231231",
        output_dir: str = "data/raw",
        sleep_sec: float = 0.4,
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sleep_sec = sleep_sec

        self.markets = ["KOSPI", "KOSDAQ"]

        # 지수 목록 수집 기준일 후보.
        # 시점별로 지수 구성이 다를 수 있어서 여러 날짜의 union을 사용.
        self.discovery_dates = [
            "20130102",
            "20151230",
            "20181228",
            "20201230",
            "20231228",
        ]

        self.exclude_keywords = [
            "200",
            "100",
            "150",
            "50",
            "고배당",
            "배당",
            "가치",
            "성장",
            "모멘텀",
            "퀄리티",
            "로우볼",
            "저변동",
            "레버리지",
            "인버스",
            "ESG",
            "TOP",
            "KRX",
            "MSCI",
            "코스피지수",
            "코스닥지수",
            "대형주",
            "중형주",
            "소형주",
            "우선주",
            "리츠",
            "IPO",
        ]

        self.kospi_sector_keywords = [
            "음식료",
            "섬유",
            "의복",
            "종이",
            "목재",
            "화학",
            "의약품",
            "비금속",
            "철강",
            "금속",
            "기계",
            "전기",
            "전자",
            "의료정밀",
            "운수장비",
            "유통",
            "전기가스",
            "건설",
            "운수창고",
            "통신",
            "금융",
            "증권",
            "보험",
            "서비스",
            "제조",
        ]

        self.kosdaq_sector_keywords = [
            "제조",
            "건설",
            "유통",
            "운송",
            "금융",
            "오락",
            "문화",
            "통신",
            "방송",
            "IT",
            "소프트웨어",
            "하드웨어",
            "음식료",
            "담배",
            "섬유",
            "의류",
            "종이",
            "목재",
            "출판",
            "매체",
            "화학",
            "제약",
            "비금속",
            "금속",
            "기계",
            "장비",
            "전기",
            "전자",
            "의료",
            "정밀",
            "운송장비",
            "부품",
            "기타제조",
        ]

    @staticmethod
    def _normalize_name(name: str) -> str:
        if pd.isna(name):
            return ""
        return re.sub(r"\s+", "", str(name)).upper()

    @staticmethod
    def _safe_slug(value: str) -> str:
        value = str(value).strip()
        value = re.sub(r"[^\w가-힣]+", "_", value)
        value = re.sub(r"_+", "_", value)
        return value.strip("_")

    def _contains_any(self, text: str, keywords: List[str]) -> bool:
        text_norm = self._normalize_name(text)
        return any(self._normalize_name(keyword) in text_norm for keyword in keywords)

    def _is_sector_index(self, market: str, index_name: str) -> bool:
        name_norm = self._normalize_name(index_name)

        if not name_norm:
            return False

        # 대표지수, 규모지수, 전략지수 제외
        if self._contains_any(index_name, self.exclude_keywords):
            return False

        if market == "KOSPI":
            return self._contains_any(index_name, self.kospi_sector_keywords)

        if market == "KOSDAQ":
            return self._contains_any(index_name, self.kosdaq_sector_keywords)

        return False

    def discover_indices(self) -> pd.DataFrame:
        records: List[Dict] = []
        seen: Set[tuple] = set()

        for market in self.markets:
            for date in self.discovery_dates:
                print(f"[지수 목록 조회] market={market}, date={date}")

                try:
                    tickers = stock.get_index_ticker_list(date, market=market)
                except Exception as e:
                    print(f"  실패: market={market}, date={date}, error={e}")
                    continue

                for ticker in tickers:
                    key = (market, ticker)
                    if key in seen:
                        continue

                    try:
                        name = stock.get_index_ticker_name(ticker)
                    except Exception:
                        name = ""

                    seen.add(key)

                    records.append(
                        {
                            "market": market,
                            "index_code": ticker,
                            "index_name": name,
                            "discovered_from": date,
                        }
                    )

                time.sleep(self.sleep_sec)

        master = pd.DataFrame(records)

        if master.empty:
            raise RuntimeError("지수 목록을 하나도 가져오지 못했습니다.")

        master["is_sector_candidate"] = master.apply(
            lambda row: self._is_sector_index(
                market=row["market"],
                index_name=row["index_name"],
            ),
            axis=1,
        )

        master = master.sort_values(["market", "index_code"]).reset_index(drop=True)

        all_path = self.output_dir / "kr_index_master_all.csv"
        sector_path = self.output_dir / "kr_index_master_sector_candidates.csv"

        master.to_csv(all_path, index=False, encoding="utf-8-sig")
        master[master["is_sector_candidate"]].to_csv(
            sector_path,
            index=False,
            encoding="utf-8-sig",
        )

        print("\n[전체 지수 목록 저장]")
        print(f"saved: {all_path}")
        print(f"rows: {len(master)}")

        print("\n[업종지수 후보]")
        sector_master = master[master["is_sector_candidate"]].copy()
        print(sector_master[["market", "index_code", "index_name"]].to_string(index=False))
        print(f"saved: {sector_path}")
        print(f"sector candidate rows: {len(sector_master)}")

        if sector_master.empty:
            raise RuntimeError(
                "업종지수 후보가 비었습니다. kr_index_master_all.csv를 보고 필터 키워드를 수정해야 합니다."
            )

        return sector_master

    def _fetch_one_index(self, market: str, index_code: str, index_name: str) -> pd.DataFrame:
        print(f"[수집] {market} / {index_code} / {index_name}")

        df = stock.get_index_ohlcv(
            self.start_date,
            self.end_date,
            index_code,
        )

        if df is None or df.empty:
            print(f"  빈 데이터: {market} / {index_code} / {index_name}")
            return pd.DataFrame()

        df = df.reset_index()

        # pykrx는 날짜 컬럼명이 보통 '날짜' 또는 index name으로 들어옴
        date_col = df.columns[0]
        df = df.rename(columns={date_col: "date"})

        rename_map = {
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
            "상장시가총액": "market_cap",
        }

        df = df.rename(columns=rename_map)

        keep_cols = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "market_cap",
        ]

        for col in keep_cols:
            if col not in df.columns:
                df[col] = pd.NA

        df = df[keep_cols].copy()

        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        numeric_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trading_value",
            "market_cap",
        ]

        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["date"]).copy()
        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        df["market"] = market
        df["index_code"] = index_code
        df["index_name"] = index_name
        df["index_slug"] = self._safe_slug(f"{market}_{index_name}_{index_code}")

        df = df[
            [
                "date",
                "market",
                "index_code",
                "index_name",
                "index_slug",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trading_value",
                "market_cap",
            ]
        ].copy()

        return df

    def collect_sector_indices(self, sector_master: pd.DataFrame) -> pd.DataFrame:
        dfs = []

        for _, row in sector_master.iterrows():
            try:
                df = self._fetch_one_index(
                    market=row["market"],
                    index_code=row["index_code"],
                    index_name=row["index_name"],
                )

                if not df.empty:
                    dfs.append(df)

            except Exception as e:
                print(
                    f"  실패: market={row['market']}, "
                    f"code={row['index_code']}, name={row['index_name']}, error={e}"
                )

            time.sleep(self.sleep_sec)

        if not dfs:
            raise RuntimeError("업종지수 데이터를 하나도 수집하지 못했습니다.")

        result = pd.concat(dfs, ignore_index=True)
        result = result.sort_values(["market", "index_code", "date"]).reset_index(drop=True)

        return result

    def save_outputs(self, long_df: pd.DataFrame) -> None:
        long_path = self.output_dir / "kr_sector_indices_long_20130101_20231231.csv"
        long_df.to_csv(long_path, index=False, encoding="utf-8-sig")

        print("\n[LONG 저장]")
        print(f"saved: {long_path}")
        print(long_df.head())
        print(long_df.tail())
        print(f"rows: {len(long_df)}")

        close_wide = long_df.pivot_table(
            index="date",
            columns="index_slug",
            values="close",
            aggfunc="last",
        ).reset_index()

        wide_path = self.output_dir / "kr_sector_indices_close_wide_20130101_20231231.csv"
        close_wide.to_csv(wide_path, index=False, encoding="utf-8-sig")

        print("\n[WIDE 저장]")
        print(f"saved: {wide_path}")
        print(close_wide.head())
        print(close_wide.tail())
        print(f"rows: {len(close_wide)}")
        print(f"columns: {len(close_wide.columns)}")

    def run(self) -> pd.DataFrame:
        sector_master = self.discover_indices()
        long_df = self.collect_sector_indices(sector_master)
        self.save_outputs(long_df)
        return long_df


def main():
    collector = KrxSectorIndexCollector(
        start_date="20130101",
        end_date="20231231",
        output_dir="data/raw",
        sleep_sec=0.4,
    )

    collector.run()


if __name__ == "__main__":
    main()