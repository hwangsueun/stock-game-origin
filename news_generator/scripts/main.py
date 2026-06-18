"""
GDELT 뉴스 수집 메인 실행 스크립트
=====================================
사용법:
    # BigQuery (권장 - 2014~2023 전체)
    python main.py --method bigquery --start 2014-01-01 --end 2023-12-31

    # BigQuery 특정 기간만
    python main.py --method bigquery --start 2022-01-01 --end 2022-12-31

    # DOC API (테스트 / 최근 데이터)
    python main.py --method docapi --start 2023-10-01 --end 2023-12-31

    # 직접 다운로드 (BigQuery 없을 때, 단기간만 권장)
    python main.py --method bulk --start 2020-01-01 --end 2020-03-31

    # 수집 후 처리만 실행
    python main.py --method process --input-dir ./output --output-dir ./output/processed
"""

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────────────────────────────

def setup_logging(log_dir: str = "./logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"collect_{datetime.now():%Y%m%d_%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────
# 파이프라인 실행
# ──────────────────────────────────────────────────────────────

def run_bigquery(start: date, end: date, project_id: str | None) -> None:
    """BigQuery 수집 + 처리 파이프라인."""
    from collectors.bigquery_collector import GDELTBigQueryCollector
    from processors.dedup_processor import process_parquet_files

    logger = logging.getLogger("main.bigquery")

    collector = GDELTBigQueryCollector(project_id=project_id)
    logger.info("=== GKG 수집 시작 ===")
    collector.collect_gkg(start=start, end=end)

    logger.info("=== Events 수집 시작 ===")
    collector.collect_events(start=start, end=end)

    logger.info("=== 후처리 (중복 제거 + 연관성 판정) ===")
    process_parquet_files(
        input_dir="./output",
        output_dir="./output/processed",
        pattern="gkg_*.parquet",
    )
    logger.info("완료!")


def run_docapi(start: date, end: date, lang_en: bool) -> None:
    """DOC API 수집 파이프라인."""
    from collectors.docapi_collector import GDELTDocAPICollector, jsonl_to_parquet
    from processors.dedup_processor import process_parquet_files

    logger = logging.getLogger("main.docapi")

    collector = GDELTDocAPICollector()
    collector.collect_all_groups(
        include_english=lang_en,
        start=start,
        end=end,
    )

    # JSONL → Parquet 변환
    docapi_dir = Path("./output/docapi")
    for jl in docapi_dir.glob("*.jsonl"):
        jsonl_to_parquet(jl)

    # 후처리
    process_parquet_files(
        input_dir=docapi_dir,
        output_dir="./output/processed",
        pattern="*.parquet",
    )
    logger.info("DOC API 수집 완료!")


def run_bulk(start: date, end: date) -> None:
    """직접 다운로드 수집 파이프라인."""
    from collectors.bulk_downloader import GDELTBulkDownloader
    from processors.dedup_processor import process_parquet_files

    logger = logging.getLogger("main.bulk")
    days = (end - start).days
    if days > 90:
        logger.warning(
            "직접 다운로드 방식은 90일 이상 수집 시 처리량이 매우 큽니다. "
            "BigQuery 사용을 강력히 권장합니다. (현재: %d일)", days
        )

    downloader = GDELTBulkDownloader()
    downloader.collect(start=start, end=end)

    process_parquet_files(
        input_dir="./output/bulk",
        output_dir="./output/processed",
        pattern="gkg_bulk_*.parquet",
    )
    logger.info("직접 다운로드 수집 완료!")


def run_naver(start: date, end: date) -> None:
    """네이버 뉴스 API 수집 파이프라인."""
    from collectors.naver_collector import NaverNewsCollector, merge_naver_gdelt
    logger = logging.getLogger("main.naver")
    collector = NaverNewsCollector()
    collector.collect_period(start=start, end=end)
    logger.info("=== GDELT + 네이버 병합 ===")
    merge_naver_gdelt(
        gdelt_dir="./output/processed",
        naver_dir="./output/naver",
        output_dir="./output/merged",
    )
    logger.info("완료!")


def run_process_only(input_dir: str, output_dir: str) -> None:
    """수집 완료된 parquet만 후처리."""
    from processors.dedup_processor import process_parquet_files
    process_parquet_files(input_dir=input_dir, output_dir=output_dir)


# ──────────────────────────────────────────────────────────────
# 요약 리포트
# ──────────────────────────────────────────────────────────────

def print_summary(output_dir: str = "./output/processed") -> None:
    """수집 결과 요약 출력."""
    import pandas as pd
    logger = logging.getLogger("main.summary")
    out = Path(output_dir)

    files = sorted(out.glob("*.parquet"))
    if not files:
        logger.info("처리된 파일 없음: %s", out)
        return

    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_parquet(f))
        except Exception:
            pass

    if not dfs:
        return

    df = pd.concat(dfs, ignore_index=True)
    total = len(df)

    print("\n" + "="*60)
    print("  GDELT 수집 결과 요약")
    print("="*60)
    print(f"  총 기사 수:    {total:,}건")

    if "relevance" in df.columns:
        rel = df["relevance"].value_counts()
        print(f"  직접 연관:     {rel.get('direct', 0):,}건")
        print(f"  간접 연관:     {rel.get('indirect', 0):,}건")

    if "lang_code" in df.columns:
        lang = df["lang_code"].value_counts()
        print(f"\n  언어 분포:")
        for l, cnt in lang.items():
            print(f"    {l}: {cnt:,}건")

    if "ref_date" in df.columns:
        years = df["ref_date"].str[:4].value_counts().sort_index()
        print(f"\n  연도별 분포:")
        for yr, cnt in years.items():
            print(f"    {yr}: {cnt:,}건")

    if "domain" in df.columns:
        top_domains = df["domain"].value_counts().head(10)
        print(f"\n  상위 10개 도메인:")
        for d, cnt in top_domains.items():
            print(f"    {d}: {cnt:,}건")

    print("="*60 + "\n")


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="GDELT 뉴스 수집 파이프라인 (2014~2023)"
    )
    p.add_argument(
        "--method",
        choices=["bigquery", "docapi", "bulk", "process", "naver", "gkgv1"],
        default="bigquery",
        help="수집 방법 선택 (기본: bigquery)",
    )
    p.add_argument("--start", default="2014-01-01", help="시작일 (YYYY-MM-DD)")
    p.add_argument("--end",   default="2023-12-31", help="종료일 (YYYY-MM-DD)")
    p.add_argument("--project-id", default=None,  help="GCP 프로젝트 ID (bigquery 전용)")
    p.add_argument("--include-english", action="store_true", default=True,
                   help="영어 기사 포함 여부 (기본: True)")
    p.add_argument("--input-dir",  default="./output",           help="처리 입력 디렉터리")
    p.add_argument("--output-dir", default="./output/processed", help="처리 출력 디렉터리")
    p.add_argument("--log-dir",    default="./logs",             help="로그 디렉터리")
    return p.parse_args()


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    args = parse_args()
    setup_logging(args.log_dir)
    logger = logging.getLogger("main")

    start = parse_date(args.start)
    end   = parse_date(args.end)

    logger.info(
        "GDELT 수집 시작 | 방법=%s | 기간=%s ~ %s",
        args.method, start, end
    )

    if args.method == "bigquery":
        run_bigquery(start, end, args.project_id)
    elif args.method == "docapi":
        run_docapi(start, end, args.include_english)
    elif args.method == "bulk":
        run_bulk(start, end)
    elif args.method == "gkgv1":
        run_gkgv1(start, end)
    elif args.method == "naver":
        run_naver(start, end)
    elif args.method == "process":
        run_process_only(args.input_dir, args.output_dir)

    print_summary(args.output_dir)


def run_gkgv1(start: date, end: date) -> None:
    """GKG v1 수집 파이프라인 (2014년 전용)."""
    from collectors.gkg_v1_collector import GDELTGKGV1Collector
    from processors.dedup_processor import process_parquet_files

    logger = logging.getLogger("main.gkgv1")
    collector = GDELTGKGV1Collector()
    collector.collect(start=start, end=end)

    logger.info("=== 후처리 (중복 제거 + 연관성 판정) ===")
    process_parquet_files(
        input_dir="./output",
        output_dir="./output/processed",
        pattern="gkg_2014*.parquet",
    )
    logger.info("GKG v1 수집 완료!")


if __name__ == "__main__":
    main()