import os
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
import pandas as pd
from dotenv import load_dotenv


class OpinetDubaiOilApiCollector:
    """
    오피넷 API 기반 Dubai 유가 수집기.

    주의:
    - OPINET_API_KEY 필요
    - endpoint는 오피넷 API 문서에서 국제유가 원유 API URL을 확인해서 넣어야 함
    - 현재 구조는 endpoint만 맞으면 바로 저장 가능하게 작성
    """

    def __init__(
        self,
        api_key: str,
        start_date: str = "2013-01-01",
        end_date: str = "2023-12-31",
        output_path: str = "data/raw/dubai_oil_price_20130101_20231231.csv",
        sleep_sec: float = 0.2,
    ):
        self.api_key = api_key
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date)
        self.output_path = Path(output_path)
        self.sleep_sec = sleep_sec

        # TODO:
        # 오피넷 API 문서에서 국제유가 원유 API endpoint 확인 후 여기에 넣기.
        #
        # 예시 형태:
        # self.base_url = "https://www.opinet.co.kr/api/국제유가원유API.do"
        #
        # 아직 정확한 endpoint를 모르면 DevTools Network에서
        # Dubai 조회 또는 CSV 저장 요청 URL을 확인해야 함.
        self.base_url = "PUT_OPINET_INTERNATIONAL_CRUDE_API_URL_HERE"

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.get(self.base_url, params=params, timeout=30)

        if response.status_code != 200:
            raise RuntimeError(
                f"API 요청 실패: status={response.status_code}, text={response.text[:500]}"
            )

        try:
            return response.json()
        except json.JSONDecodeError:
            raise RuntimeError(
                "JSON 파싱 실패. API가 JSON이 아니라 XML/HTML/CSV를 반환했을 가능성이 있습니다.\n"
                f"response preview:\n{response.text[:1000]}"
            )

    @staticmethod
    def _normalize_date(value) -> Optional[pd.Timestamp]:
        if pd.isna(value):
            return None

        text = str(value).strip()

        # 20130102
        if text.isdigit() and len(text) == 8:
            return pd.to_datetime(text, format="%Y%m%d", errors="coerce")

        # 2013-01-02, 2013.01.02, 13년01월02일 등
        return pd.to_datetime(text, errors="coerce")

    @staticmethod
    def _normalize_price(value) -> Optional[float]:
        if pd.isna(value):
            return None

        text = str(value).replace(",", "").strip()

        try:
            return float(text)
        except ValueError:
            return None

    def _extract_rows_from_json(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        오피넷 API 응답 구조가 endpoint마다 다를 수 있으므로
        흔한 구조들을 방어적으로 탐색.
        """

        candidates = []

        def walk(obj):
            if isinstance(obj, list):
                if obj and all(isinstance(x, dict) for x in obj):
                    candidates.append(obj)
                for item in obj:
                    walk(item)
            elif isinstance(obj, dict):
                for value in obj.values():
                    walk(value)

        walk(data)

        if not candidates:
            raise RuntimeError(f"응답에서 row list를 찾지 못했습니다: {data}")

        # 가장 긴 list를 데이터 본문으로 간주
        return max(candidates, key=len)

    def _rows_to_dataframe(self, rows: List[Dict[str, Any]]) -> pd.DataFrame:
        raw = pd.DataFrame(rows)

        if raw.empty:
            raise RuntimeError("API 응답 row가 비어 있습니다.")

        print("raw columns:", list(raw.columns))

        # 날짜 컬럼 후보
        date_candidates = [
            "DATE", "date", "TRD_DT", "BASE_DT", "YMD", "PERIOD", "기간", "일자"
        ]

        # Dubai 가격 컬럼 후보
        dubai_candidates = [
            "DUBAI", "Dubai", "dubai", "DUBAI_PRICE", "DUBAI_PRC",
            "PRICE", "price", "OIL_PRICE", "원유가격"
        ]

        date_col = None
        dubai_col = None

        for col in raw.columns:
            if str(col) in date_candidates:
                date_col = col
                break

        for col in raw.columns:
            if str(col) in dubai_candidates or "dubai" in str(col).lower():
                dubai_col = col
                break

        if date_col is None:
            raise RuntimeError(
                f"날짜 컬럼을 찾지 못했습니다. raw columns={list(raw.columns)}"
            )

        if dubai_col is None:
            raise RuntimeError(
                f"Dubai 가격 컬럼을 찾지 못했습니다. raw columns={list(raw.columns)}"
            )

        df = raw[[date_col, dubai_col]].copy()
        df.columns = ["date", "adj_close"]

        df["date"] = df["date"].apply(self._normalize_date)
        df["adj_close"] = df["adj_close"].apply(self._normalize_price)

        df = df.dropna(subset=["date", "adj_close"]).copy()

        # USD/Bbl 방어 필터
        # 원화 환산값이 같이 섞이면 900~1000대가 들어올 수 있음.
        df = df[(df["adj_close"] > 0) & (df["adj_close"] < 300)].copy()

        df = df[
            (df["date"] >= self.start_date)
            & (df["date"] <= self.end_date)
        ].copy()

        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        df["volume"] = pd.NA
        df = df[["date", "adj_close", "volume"]].copy()

        return df

    def collect(self) -> pd.DataFrame:
        if self.base_url == "PUT_OPINET_INTERNATIONAL_CRUDE_API_URL_HERE":
            raise RuntimeError(
                "오피넷 국제유가 원유 API endpoint가 아직 입력되지 않았습니다.\n"
                "오피넷 API 문서 또는 DevTools Network에서 Dubai 조회 요청 URL을 확인한 뒤 "
                "self.base_url에 넣으세요."
            )

        # endpoint별 파라미터명이 다를 수 있음.
        # 아래는 가장 흔한 구조의 예시.
        params = {
            "certkey": self.api_key,
            "out": "json",
            "start": self.start_date.strftime("%Y%m%d"),
            "end": self.end_date.strftime("%Y%m%d"),
            "prodcd": "DUBAI",
        }

        data = self._request(params)
        rows = self._extract_rows_from_json(data)
        df = self._rows_to_dataframe(rows)

        return df

    def save(self, df: pd.DataFrame) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.output_path, index=False, encoding="utf-8-sig")

        print(df.head())
        print(df.tail())
        print(f"rows: {len(df)}")
        print(f"saved: {self.output_path}")


def main():
    load_dotenv()

    api_key = os.getenv("OPINET_API_KEY")

    if not api_key:
        raise RuntimeError("환경변수 OPINET_API_KEY가 없습니다. .env에 추가하세요.")

    collector = OpinetDubaiOilApiCollector(
        api_key=api_key,
        start_date="2013-01-01",
        end_date="2023-12-31",
        output_path="data/raw/dubai_oil_price_20130101_20231231.csv",
    )

    df = collector.collect()
    collector.save(df)


if __name__ == "__main__":
    main()