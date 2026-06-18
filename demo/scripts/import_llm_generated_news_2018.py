from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from supabase import Client, create_client


ASSET_NAME_TO_ASSET_ID = {
    "삼성전자": "STOCK_SAMSUNG",
    "현대차": "STOCK_HYUNDAI",
    "현대자동차": "STOCK_HYUNDAI",
    "카카오": "STOCK_KAKAO",
    "비트코인": "COIN_BTC",
    "BTC": "COIN_BTC",
    "이더리움": "COIN_ETH",
    "ETH": "COIN_ETH",
    "국고채": "BOND_KTB",
    "채권": "BOND_KTB",
}


DIRECTION_TO_SENTIMENT = {
    "positive": "POSITIVE",
    "negative": "NEGATIVE",
    "neutral": "NEUTRAL",
}


@dataclass(frozen=True)
class ImportConfig:
    csv_path: Path
    env_path: Path
    source_file: str = "llm_generated_news_2018.csv"
    batch_size: int = 300


class JsonCellParser:
    @staticmethod
    def parse(value: Any) -> list[Any]:
        if value is None:
            return []

        if pd.isna(value):
            return []

        text = str(value).strip()

        if not text:
            return []

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except json.JSONDecodeError:
            return [text]


class AssetIdResolver:
    def resolve(self, related_assets: list[Any], asset_class: str | None) -> str | None:
        if asset_class in {"macro", "global", "market", "sector", "commodity"}:
            return None

        for raw_name in related_assets:
            name = str(raw_name).strip()

            if name in ASSET_NAME_TO_ASSET_ID:
                return ASSET_NAME_TO_ASSET_ID[name]

            for keyword, asset_id in ASSET_NAME_TO_ASSET_ID.items():
                if keyword in name:
                    return asset_id

        return None


class NewsTransformer:
    def __init__(self, calendar_rows: list[dict[str, Any]]):
        self.date_to_turn = {
            str(row["game_date"]): int(row["turn_index"])
            for row in calendar_rows
        }
        self.asset_resolver = AssetIdResolver()

    def transform(self, df: pd.DataFrame, source_file: str) -> list[dict[str, Any]]:
        required = [
            "date",
            "news_id",
            "news_order",
            "headline",
            "detail_news",
            "asset_class",
            "related_assets",
            "direction",
            "source_event_ids",
            "used_evidence",
            "news_style",
            "custom_id",
            "validation_errors",
        ]

        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"CSV 필수 컬럼 누락: {missing}")

        rows: list[dict[str, Any]] = []

        for source_row, row in df.reset_index(drop=True).iterrows():
            game_date = str(row["date"])

            if game_date not in self.date_to_turn:
                continue

            headline = self._safe_text(row["headline"])
            body = self._safe_text(row["detail_news"])

            if not headline or not body:
                continue

            related_assets = JsonCellParser.parse(row["related_assets"])
            source_event_ids = JsonCellParser.parse(row["source_event_ids"])
            used_evidence = JsonCellParser.parse(row["used_evidence"])

            asset_class = self._safe_text(row["asset_class"])
            direction = self._safe_text(row["direction"]).lower()

            asset_id = self.asset_resolver.resolve(
                related_assets=related_assets,
                asset_class=asset_class,
            )

            sentiment = DIRECTION_TO_SENTIMENT.get(direction, "NEUTRAL")

            news_order = int(row["news_order"])
            importance = max(1, 11 - news_order)

            rows.append(
                {
                    "turn_index": self.date_to_turn[game_date],
                    "game_date": game_date,

                    "news_id": self._safe_text(row["news_id"]),
                    "news_order": news_order,

                    "headline": headline,
                    "body": body,

                    "asset_class": asset_class,
                    "asset_id": asset_id,
                    "related_assets": related_assets,

                    "direction": direction,
                    "sentiment": sentiment,
                    "importance": importance,

                    "source_event_ids": source_event_ids,
                    "used_evidence": used_evidence,
                    "news_style": self._safe_text(row["news_style"]),
                    "custom_id": self._safe_text(row["custom_id"]),
                    "validation_errors": self._nullable_text(row["validation_errors"]),

                    "source_type": "LLM_GENERATED",
                    "source_file": source_file,
                    "source_row": int(source_row),
                }
            )

        return rows

    @staticmethod
    def _safe_text(value: Any) -> str:
        if value is None:
            return ""

        if pd.isna(value):
            return ""

        return str(value).strip()

    @staticmethod
    def _nullable_text(value: Any) -> str | None:
        text = NewsTransformer._safe_text(value)
        return text if text else None


class SupabaseNewsImporter:
    def __init__(self, client: Client, config: ImportConfig):
        self.client = client
        self.config = config

    def run(self) -> None:
        print("[1] CSV 로드")
        df = pd.read_csv(self.config.csv_path, encoding="utf-8-sig")
        print(f"CSV shape: {df.shape}")
        print(f"CSV columns: {list(df.columns)}")

        print("\n[2] game_calendar 로드")
        calendar_response = (
            self.client
            .table("game_calendar")
            .select("turn_index, game_date")
            .order("turn_index")
            .execute()
        )

        calendar_rows = calendar_response.data

        if not calendar_rows:
            raise RuntimeError("game_calendar가 비어 있습니다.")

        print(f"calendar rows: {len(calendar_rows)}")
        print(f"calendar date range: {calendar_rows[0]['game_date']} ~ {calendar_rows[-1]['game_date']}")

        print("\n[3] CSV date → turn_index 매핑")
        transformer = NewsTransformer(calendar_rows=calendar_rows)
        rows = transformer.transform(df=df, source_file=self.config.source_file)

        print(f"import target rows: {len(rows)}")

        if not rows:
            print("\n[중단] 매칭된 뉴스가 없습니다.")
            print("원인: CSV의 date와 game_calendar.game_date가 맞지 않습니다.")
            print("해결: game_calendar 날짜를 2018-07-01 ~ 2018-10-31 범위의 날짜로 맞추세요.")
            return

        print("\n[4] Supabase news upsert")
        self._upsert_batches(rows)

        print("\n[완료]")
        print(f"inserted_or_updated: {len(rows)}")

    def _upsert_batches(self, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), self.config.batch_size):
            batch = rows[start:start + self.config.batch_size]

            self.client.table("news").upsert(
                batch,
                on_conflict="source_file,source_row",
            ).execute()

            print(f"- upsert {start + len(batch)} / {len(rows)}")


class SupabaseClientFactory:
    @staticmethod
    def create(env_path: Path) -> Client:
        load_dotenv(env_path)

        supabase_url = os.getenv("VITE_SUPABASE_URL") or os.getenv("SUPABASE_URL")
        service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

        if not supabase_url:
            raise RuntimeError("VITE_SUPABASE_URL 또는 SUPABASE_URL이 없습니다.")

        if not service_role_key:
            raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY가 없습니다. 이 키는 로컬 import 스크립트에서만 사용하세요.")

        return create_client(supabase_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        required=True,
        help="llm_generated_news_2018.csv 경로",
    )
    parser.add_argument(
        "--env",
        default=".env.local",
        help="Supabase 환경변수 파일 경로",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = ImportConfig(
        csv_path=Path(args.csv).resolve(),
        env_path=Path(args.env).resolve(),
    )

    client = SupabaseClientFactory.create(config.env_path)
    importer = SupabaseNewsImporter(client=client, config=config)
    importer.run()


if __name__ == "__main__":
    main()