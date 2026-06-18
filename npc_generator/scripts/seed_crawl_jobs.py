import os
import math
from dataclasses import dataclass
from typing import List, Dict, Iterable

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DATA_RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
ENV_PATH = os.path.join(BASE_DIR, ".env")

@dataclass
class SeedConfig:
    supabase_url: str
    supabase_key: str
    csv_path: str = "../data/raw/dci_gallery_page_counts.csv"
    table_name: str = "crawl_jobs"
    shard_size: int = 500
    directions: tuple = ("forward", "reverse")
    max_galleries: int | None = None
    page_sort_ascending: bool = True
    batch_size: int = 500


class CrawlJobSeeder:
    REQUIRED_COLUMNS = {"gallery_name", "gall_id", "gall_type", "total_pages"}
    ALLOWED_DIRECTIONS = {"forward", "reverse", "top10_forward", "top10_reverse"}

    def __init__(self, config: SeedConfig):
        self.config = config
        self.client: Client = create_client(config.supabase_url, config.supabase_key)
        self._validate_config()

    def _validate_config(self) -> None:
        if not self.config.supabase_url:
            raise ValueError("SUPABASE_URL 이 비어 있습니다.")
        if not self.config.supabase_key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY 가 비어 있습니다.")
        if self.config.shard_size <= 0:
            raise ValueError("shard_size 는 1 이상이어야 합니다.")
        invalid = set(self.config.directions) - self.ALLOWED_DIRECTIONS
        if invalid:
            raise ValueError(f"허용되지 않은 direction 이 있습니다: {invalid}")

    def load_gallery_rows(self) -> List[Dict]:
        if not os.path.exists(self.config.csv_path):
            raise FileNotFoundError(f"CSV 파일이 없습니다: {self.config.csv_path}")

        df = pd.read_csv(self.config.csv_path)

        missing = self.REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"CSV 컬럼이 부족합니다: {missing}")

        df["total_pages"] = pd.to_numeric(df["total_pages"], errors="coerce").fillna(0).astype(int)
        df = df[df["total_pages"] > 0].copy()

        df = df.sort_values(
            ["total_pages", "gallery_name"],
            ascending=[self.config.page_sort_ascending, True]
        ).reset_index(drop=True)

        if self.config.max_galleries is not None:
            df = df.head(self.config.max_galleries)

        return df.to_dict("records")

    def build_shards_for_gallery(self, row: Dict) -> List[Dict]:
        gall_name = str(row["gallery_name"])
        gall_id = str(row["gall_id"])
        gall_type = str(row["gall_type"])
        total_pages = int(row["total_pages"])

        shard_count = math.ceil(total_pages / self.config.shard_size)
        jobs: List[Dict] = []

        for direction in self.config.directions:
            for shard_no in range(1, shard_count + 1):
                page_start = (shard_no - 1) * self.config.shard_size + 1
                page_end = min(shard_no * self.config.shard_size, total_pages)

                if direction in ("forward", "top10_forward"):
                    next_page = page_start
                else:
                    next_page = page_end

                jobs.append({
                    "gall_name": gall_name,
                    "gall_id": gall_id,
                    "gall_type": gall_type,
                    "total_pages": total_pages,
                    "shard_no": shard_no,
                    "page_start": page_start,
                    "page_end": page_end,
                    "direction": direction,
                    "status": "todo",
                    "claimed_by": None,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "last_page": None,
                    "next_page": next_page,
                    "started_at": None,
                    "finished_at": None,
                    "last_error": None,
                })

        return jobs

    def build_all_jobs(self, rows: List[Dict]) -> List[Dict]:
        all_jobs: List[Dict] = []
        for row in rows:
            all_jobs.extend(self.build_shards_for_gallery(row))
        return all_jobs

    def chunked(self, items: List[Dict], size: int) -> Iterable[List[Dict]]:
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def upsert_jobs(self, jobs: List[Dict]) -> None:
        if not jobs:
            print("적재할 job 이 없습니다.")
            return

        total = len(jobs)
        inserted = 0

        for batch in self.chunked(jobs, self.config.batch_size):
            self.client.table(self.config.table_name).upsert(
                batch,
                on_conflict="gall_name,direction,shard_no"
            ).execute()
            inserted += len(batch)
            print(f"[UPSERT] {inserted}/{total}")

    def run(self) -> None:
        rows = self.load_gallery_rows()
        print(f"[INFO] gallery rows: {len(rows)}")

        jobs = self.build_all_jobs(rows)
        print(f"[INFO] total shard jobs: {len(jobs)}")
        print(f"[INFO] shard_size={self.config.shard_size}, directions={self.config.directions}")

        self.upsert_jobs(jobs)
        print("[DONE] crawl_jobs 적재 완료")


def main():
    load_dotenv(ENV_PATH)

    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    print(f"[DEBUG] ENV_PATH={ENV_PATH}")
    print(f"[DEBUG] SUPABASE_URL exists={bool(supabase_url)}")
    print(f"[DEBUG] SUPABASE_SERVICE_ROLE_KEY exists={bool(supabase_key)}")

    if not supabase_url:
        raise ValueError(f"SUPABASE_URL 이 비어 있습니다. .env 경로 확인: {ENV_PATH}")
    if not supabase_key:
        raise ValueError(f"SUPABASE_SERVICE_ROLE_KEY 가 비어 있습니다. .env 경로 확인: {ENV_PATH}")

    config = SeedConfig(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        csv_path=os.path.join(DATA_RAW_DIR, "dci_gallery_page_counts.csv"),
        table_name="crawl_jobs",
        shard_size=1000,
        directions=("forward", "reverse"),
        max_galleries=None,
        page_sort_ascending=True,
        batch_size=500,
    )

    seeder = CrawlJobSeeder(config)
    seeder.run()


if __name__ == "__main__":
    main()