# ============================================================
# pr_dci00_filter_stock_thread_comments.py
#
# 로컬 실행 기준:
#   project_root/
#     processor/
#       pr_dci00_filter_stock_thread_comments.py
#     data/
#       processed/
#         dci_posts_ready.csv
#         dci_comments_ready.csv
#       raw/
#         stocklist.txt
#
# 목적:
#   1) dci_posts_ready.csv에서 종목 귀속 게시글 추출
#   2) dci_comments_ready.csv에서 해당 게시글 댓글만 chunk 필터링
#   3) 축소 댓글 파일 생성
# ============================================================

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd


# ============================================================
# 0. 설정
# ============================================================

@dataclass
class PipelineConfig:
    project_root: Path
    posts_path: Path
    comments_path: Path
    stocklist_path: Path
    output_dir: Path

    post_chunksize: int = 100_000
    comment_chunksize: int = 300_000
    encoding: str = "utf-8-sig"

    stock_posts_filename: str = "stock_attributed_posts.csv"
    stock_keys_filename: str = "stock_thread_keys.csv"
    filtered_comments_filename: str = "dci_comments_stock_thread_only.csv"
    report_filename: str = "filter_report.txt"

    min_stock_name_len: int = 3


# ============================================================
# 1. 경로 탐색
# ============================================================

class LocalPathResolver:
    @staticmethod
    def resolve_project_root() -> Path:
        script_path = Path(__file__).resolve()
        return script_path.parent.parent

    @staticmethod
    def find_first_existing(candidates: List[Path], label: str) -> Path:
        for path in candidates:
            if path.exists():
                return path

        msg = [f"[경로 오류] {label} 파일을 찾지 못했습니다."]
        msg.append("확인한 후보 경로:")
        for p in candidates:
            msg.append(f"  - {p}")
        raise FileNotFoundError("\n".join(msg))


# ============================================================
# 2. 종목 리스트 로더
# ============================================================

class StockListLoader:
    def __init__(self, stocklist_path: Path, encoding: str = "utf-8-sig"):
        self.stocklist_path = stocklist_path
        self.encoding = encoding

    def load(self) -> pd.DataFrame:
        rows = []

        with open(self.stocklist_path, "r", encoding=self.encoding) as f:
            for raw_line in f:
                line = raw_line.strip()

                if not line:
                    continue

                if line.startswith("#"):
                    continue

                lowered = line.lower()
                if "stock_code" in lowered and "stock_name" in lowered:
                    continue

                parsed = self._parse_line(line)
                if parsed is not None:
                    rows.append(parsed)

        if not rows:
            raise ValueError(f"종목 리스트를 읽지 못했습니다: {self.stocklist_path}")

        df = pd.DataFrame(rows)
        df["stock_code"] = (
            df["stock_code"]
            .fillna("")
            .astype(str)
            .str.replace("A", "", regex=False)
            .str.strip()
        )
        df["stock_name"] = df["stock_name"].fillna("").astype(str).str.strip()

        df = df[df["stock_name"].str.len() > 0].copy()
        df = df.drop_duplicates(subset=["stock_code", "stock_name"]).reset_index(drop=True)

        print(f"[StockListLoader] loaded: {len(df):,}")
        print(df.head(10).to_string(index=False))

        return df

    def _parse_line(self, line: str) -> Optional[Dict[str, str]]:
        # 예:
        # 005930,삼성전자
        # A005930,삼성전자
        # 005930 삼성전자
        # 삼성전자
        parts = re.split(r"[,\t]+|\s{2,}", line)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) == 1:
            m = re.match(r"^(A?\d{6})\s+(.+)$", parts[0])
            if m:
                return {
                    "stock_code": m.group(1).replace("A", ""),
                    "stock_name": m.group(2).strip(),
                }

            return {
                "stock_code": "",
                "stock_name": parts[0].strip(),
            }

        first = parts[0]
        second = parts[1]

        if re.fullmatch(r"A?\d{6}", first):
            return {
                "stock_code": first.replace("A", ""),
                "stock_name": second,
            }

        if re.fullmatch(r"A?\d{6}", second):
            return {
                "stock_code": second.replace("A", ""),
                "stock_name": first,
            }

        return {
            "stock_code": "",
            "stock_name": first,
        }


# ============================================================
# 3. 종목 매칭기
# ============================================================

class StockAliasMatcher:
    FORBIDDEN_SINGLE_NAMES = {
        "SK", "LG", "GS", "DL",
        "한미", "대한", "한국", "서울", "미래", "신라", "효성",
        "기아", "신한", "디오", "삼성", "한화", "현대",
    }

    CONFIRMED_ALIAS_TO_TARGET = {
        "하닉": "SK하이닉스",
        "삼바": "삼성바이오로직스",
        "셀트": "셀트리온",
        "엔씨": "엔씨소프트",
        "크아": "크래프톤",
        "카뱅": "카카오뱅크",
        "강랜": "강원랜드",
        "한전": "한국전력",
        "SKT": "SK텔레콤",
        "네이버": "NAVER",
    }

    def __init__(self, stock_df: pd.DataFrame, min_stock_name_len: int = 3):
        self.stock_df = stock_df.copy()
        self.min_stock_name_len = min_stock_name_len

        self.stock_records = self._build_stock_records()
        self.alias_records = self._build_alias_records()

        print(f"[StockAliasMatcher] stock_records: {len(self.stock_records):,}")
        print(f"[StockAliasMatcher] alias_records: {len(self.alias_records):,}")

    def match_text(self, text: str) -> List[Dict[str, str]]:
        if not isinstance(text, str):
            text = ""

        raw_text = text
        compact_text = self._compact(text)

        matched = []

        for rec in self.stock_records:
            code = rec["stock_code"]
            if code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", raw_text):
                matched.append({
                    "stock_code": rec["stock_code"],
                    "stock_name": rec["stock_name"],
                    "matched_by": "code",
                    "matched_term": code,
                })

        for rec in self.stock_records:
            name = rec["stock_name"]

            if not self._is_valid_stock_name_for_matching(name):
                continue

            if self._compact(name) in compact_text:
                matched.append({
                    "stock_code": rec["stock_code"],
                    "stock_name": rec["stock_name"],
                    "matched_by": "official_name",
                    "matched_term": name,
                })

        for alias_rec in self.alias_records:
            alias = alias_rec["alias"]

            if self._compact(alias) in compact_text:
                matched.append({
                    "stock_code": alias_rec["stock_code"],
                    "stock_name": alias_rec["stock_name"],
                    "matched_by": "confirmed_alias",
                    "matched_term": alias,
                })

        return self._deduplicate_matches(matched)

    def _build_stock_records(self) -> List[Dict[str, str]]:
        records = []

        for _, row in self.stock_df.iterrows():
            code = str(row.get("stock_code", "")).strip().replace("A", "")
            name = str(row.get("stock_name", "")).strip()

            if not name:
                continue

            records.append({
                "stock_code": code,
                "stock_name": name,
            })

        return records

    def _build_alias_records(self) -> List[Dict[str, str]]:
        alias_records = []

        for alias, target_name in self.CONFIRMED_ALIAS_TO_TARGET.items():
            target = self.stock_df[
                self.stock_df["stock_name"].astype(str).str.contains(
                    re.escape(target_name),
                    regex=True,
                    na=False,
                )
            ]

            if target.empty:
                continue

            row = target.iloc[0]

            alias_records.append({
                "alias": alias,
                "stock_code": str(row.get("stock_code", "")).strip().replace("A", ""),
                "stock_name": str(row.get("stock_name", "")).strip(),
            })

        return alias_records

    def _is_valid_stock_name_for_matching(self, name: str) -> bool:
        if not isinstance(name, str):
            return False

        name = name.strip()

        if not name:
            return False

        if name in self.FORBIDDEN_SINGLE_NAMES:
            return False

        if len(name) < self.min_stock_name_len:
            return False

        if re.fullmatch(r"[\d\W_]+", name):
            return False

        return True

    @staticmethod
    def _compact(text: str) -> str:
        text = str(text).lower()
        text = re.sub(r"\s+", "", text)
        return text

    @staticmethod
    def _deduplicate_matches(matches: List[Dict[str, str]]) -> List[Dict[str, str]]:
        seen = set()
        result = []

        for m in matches:
            key = (m["stock_code"], m["stock_name"])
            if key in seen:
                continue

            seen.add(key)
            result.append(m)

        return result


# ============================================================
# 4. ID 정규화
# ============================================================

class ThreadKeyBuilder:
    @staticmethod
    def normalize_id(value: object) -> str:
        if pd.isna(value):
            return ""

        s = str(value).strip()

        # CSV에서 숫자형으로 읽혔던 흔적 방어
        if re.fullmatch(r"\d+\.0", s):
            s = s[:-2]

        return s

    @classmethod
    def make_key(cls, gall_id: object, post_id: object) -> str:
        gall = cls.normalize_id(gall_id)
        post = cls.normalize_id(post_id)
        return f"{gall}__{post}"


# ============================================================
# 5. 종목 귀속 게시글 추출
# ============================================================

class StockThreadPostExtractor:
    def __init__(self, config: PipelineConfig, matcher: StockAliasMatcher):
        self.config = config
        self.matcher = matcher

    def run(self) -> pd.DataFrame:
        output_path = self.config.output_dir / self.config.stock_posts_filename

        if output_path.exists():
            output_path.unlink()

        total_rows = 0
        matched_rows = 0
        first_write = True

        for chunk_idx, chunk in enumerate(pd.read_csv(
            self.config.posts_path,
            chunksize=self.config.post_chunksize,
            dtype=str,
            encoding=self.config.encoding,
            on_bad_lines="skip",
        )):
            t0 = time.time()
            total_rows += len(chunk)

            required = {"gall_id", "post_id", "title", "content"}
            missing = required - set(chunk.columns)

            if missing:
                raise ValueError(f"dci_posts_ready.csv 필수 컬럼 누락: {missing}")

            records = []

            for _, row in chunk.iterrows():
                title = row.get("title", "")
                content = row.get("content", "")
                text = f"{title}\n{content}"

                matches = self.matcher.match_text(text)

                if not matches:
                    continue

                stock_codes = [m["stock_code"] for m in matches]
                stock_names = [m["stock_name"] for m in matches]
                matched_terms = [m["matched_term"] for m in matches]
                matched_by = [m["matched_by"] for m in matches]

                out = row.to_dict()
                out["matched_stock_codes"] = "|".join(stock_codes)
                out["matched_stock_names"] = "|".join(stock_names)
                out["matched_terms"] = "|".join(matched_terms)
                out["matched_by"] = "|".join(matched_by)
                out["matched_stock_count"] = len(matches)
                out["multi_stock_thread"] = int(len(matches) >= 2)
                out["thread_key"] = ThreadKeyBuilder.make_key(
                    row.get("gall_id", ""),
                    row.get("post_id", ""),
                )

                records.append(out)

            if records:
                out_df = pd.DataFrame(records)
                matched_rows += len(out_df)

                out_df.to_csv(
                    output_path,
                    mode="w" if first_write else "a",
                    index=False,
                    header=first_write,
                    encoding=self.config.encoding,
                )

                first_write = False

            elapsed = time.time() - t0
            print(
                f"[PostExtractor] chunk={chunk_idx:,} "
                f"rows={len(chunk):,} "
                f"matched_chunk={len(records):,} "
                f"total_rows={total_rows:,} "
                f"total_matched={matched_rows:,} "
                f"elapsed={elapsed:.1f}s"
            )

        if not output_path.exists():
            raise ValueError("종목 귀속 게시글이 0건입니다. stocklist 또는 매칭 로직을 확인해야 합니다.")

        result = pd.read_csv(output_path, dtype=str, encoding=self.config.encoding)

        print("\n[PostExtractor DONE]")
        print(f"scanned_posts: {total_rows:,}")
        print(f"stock_attributed_posts: {len(result):,}")
        print(f"saved: {output_path}")

        return result

    def save_thread_keys(self, stock_posts_df: pd.DataFrame) -> Set[str]:
        key_path = self.config.output_dir / self.config.stock_keys_filename

        if "thread_key" not in stock_posts_df.columns:
            stock_posts_df["thread_key"] = stock_posts_df.apply(
                lambda r: ThreadKeyBuilder.make_key(r.get("gall_id", ""), r.get("post_id", "")),
                axis=1,
            )

        key_df = (
            stock_posts_df[["gall_id", "post_id", "thread_key"]]
            .drop_duplicates()
            .copy()
        )

        key_df.to_csv(key_path, index=False, encoding=self.config.encoding)

        key_set = set(key_df["thread_key"].astype(str).tolist())

        print(f"[ThreadKeys] unique_keys: {len(key_set):,}")
        print(f"[ThreadKeys] saved: {key_path}")

        return key_set


# ============================================================
# 6. 대용량 댓글 필터링
# ============================================================

class LargeCommentFilter:
    def __init__(self, config: PipelineConfig, stock_thread_keys: Set[str]):
        self.config = config
        self.stock_thread_keys = stock_thread_keys

    def run(self) -> Dict[str, int]:
        output_path = self.config.output_dir / self.config.filtered_comments_filename

        if output_path.exists():
            output_path.unlink()

        total_rows = 0
        kept_rows = 0
        kept_thread_keys = set()
        first_write = True

        for chunk_idx, chunk in enumerate(pd.read_csv(
            self.config.comments_path,
            chunksize=self.config.comment_chunksize,
            dtype=str,
            encoding=self.config.encoding,
            on_bad_lines="skip",
        )):
            t0 = time.time()
            total_rows += len(chunk)

            required = {"gall_id", "post_id"}
            missing = required - set(chunk.columns)

            if missing:
                raise ValueError(f"dci_comments_ready.csv 필수 컬럼 누락: {missing}")

            thread_keys = (
                chunk["gall_id"].map(ThreadKeyBuilder.normalize_id)
                + "__"
                + chunk["post_id"].map(ThreadKeyBuilder.normalize_id)
            )

            mask = thread_keys.isin(self.stock_thread_keys)
            kept = chunk.loc[mask].copy()

            if not kept.empty:
                kept["thread_key"] = thread_keys.loc[mask].values
                kept_thread_keys.update(kept["thread_key"].astype(str).unique().tolist())

                kept.to_csv(
                    output_path,
                    mode="w" if first_write else "a",
                    index=False,
                    header=first_write,
                    encoding=self.config.encoding,
                )

                kept_rows += len(kept)
                first_write = False

            elapsed = time.time() - t0

            print(
                f"[CommentFilter] chunk={chunk_idx:,} "
                f"rows={len(chunk):,} "
                f"kept_chunk={len(kept):,} "
                f"total_rows={total_rows:,} "
                f"total_kept={kept_rows:,} "
                f"kept_threads={len(kept_thread_keys):,} "
                f"elapsed={elapsed:.1f}s"
            )

        report = {
            "total_comment_rows_scanned": total_rows,
            "kept_comment_rows": kept_rows,
            "stock_thread_keys_input": len(self.stock_thread_keys),
            "stock_thread_keys_with_comments": len(kept_thread_keys),
        }

        print("\n[CommentFilter DONE]")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"saved: {output_path}")

        return report


# ============================================================
# 7. 리포트
# ============================================================

class ReportWriter:
    def __init__(self, config: PipelineConfig):
        self.config = config

    def write(self, stock_posts_df: pd.DataFrame, comment_report: Dict[str, int]) -> None:
        report_path = self.config.output_dir / self.config.report_filename

        total_stock_posts = len(stock_posts_df)

        multi_stock_posts = int(
            pd.to_numeric(
                stock_posts_df.get("multi_stock_thread", 0),
                errors="coerce",
            ).fillna(0).sum()
        )

        single_stock_posts = total_stock_posts - multi_stock_posts

        top_stocks = (
            stock_posts_df["matched_stock_names"]
            .fillna("")
            .str.split("|")
            .explode()
            .loc[lambda s: s.astype(str).str.len() > 0]
            .value_counts()
            .head(50)
        )

        lines = []
        lines.append("# DCI Stock Thread Comment Filter Report")
        lines.append("")
        lines.append("## Paths")
        lines.append(f"- posts_path: {self.config.posts_path}")
        lines.append(f"- comments_path: {self.config.comments_path}")
        lines.append(f"- stocklist_path: {self.config.stocklist_path}")
        lines.append(f"- output_dir: {self.config.output_dir}")
        lines.append("")
        lines.append("## Post Extraction")
        lines.append(f"- stock_attributed_posts: {total_stock_posts:,}")
        lines.append(f"- single_stock_posts: {single_stock_posts:,}")
        lines.append(f"- multi_stock_posts: {multi_stock_posts:,}")
        lines.append("")
        lines.append("## Comment Filtering")

        for k, v in comment_report.items():
            lines.append(f"- {k}: {v:,}")

        lines.append("")
        lines.append("## Top Matched Stocks")

        for stock_name, count in top_stocks.items():
            lines.append(f"- {stock_name}: {count:,}")

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))

        print(f"[ReportWriter] saved: {report_path}")


# ============================================================
# 8. 파이프라인
# ============================================================

class DciStockCommentFilterPipeline:
    def __init__(self, config: PipelineConfig):
        self.config = config

    def run(self) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._validate_inputs()

        stock_df = StockListLoader(
            stocklist_path=self.config.stocklist_path,
            encoding=self.config.encoding,
        ).load()

        matcher = StockAliasMatcher(
            stock_df=stock_df,
            min_stock_name_len=self.config.min_stock_name_len,
        )

        post_extractor = StockThreadPostExtractor(
            config=self.config,
            matcher=matcher,
        )

        stock_posts_df = post_extractor.run()
        stock_thread_keys = post_extractor.save_thread_keys(stock_posts_df)

        comment_filter = LargeCommentFilter(
            config=self.config,
            stock_thread_keys=stock_thread_keys,
        )

        comment_report = comment_filter.run()

        ReportWriter(self.config).write(
            stock_posts_df=stock_posts_df,
            comment_report=comment_report,
        )

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")

    def _validate_inputs(self) -> None:
        paths = {
            "posts_path": self.config.posts_path,
            "comments_path": self.config.comments_path,
            "stocklist_path": self.config.stocklist_path,
        }

        print("[Input Check]")

        for name, path in paths.items():
            if not path.exists():
                raise FileNotFoundError(f"{name} 없음: {path}")

            size_mb = path.stat().st_size / 1024 / 1024
            print(f"  {name}: {path} ({size_mb:,.1f} MB)")


# ============================================================
# 9. CLI
# ============================================================

def build_config_from_args() -> PipelineConfig:
    project_root = LocalPathResolver.resolve_project_root()
    data_dir = project_root / "data"
    processed_dir = data_dir / "processed"
    raw_dir = data_dir / "raw"

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--posts",
        type=str,
        default=str(processed_dir / "dci_posts_ready.csv"),
    )

    parser.add_argument(
        "--comments",
        type=str,
        default=str(processed_dir / "dci_comments_ready.csv"),
    )

    parser.add_argument(
        "--stocklist",
        type=str,
        default="",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(processed_dir / "dci_stock_thread_comment_subset"),
    )

    parser.add_argument(
        "--post-chunksize",
        type=int,
        default=100_000,
    )

    parser.add_argument(
        "--comment-chunksize",
        type=int,
        default=300_000,
    )

    args = parser.parse_args()

    if args.stocklist:
        stocklist_path = Path(args.stocklist).expanduser().resolve()
    else:
        stocklist_path = LocalPathResolver.find_first_existing(
            candidates=[
                raw_dir / "stocklist.txt",
                processed_dir / "stocklist.txt",
                project_root / "stocklist.txt",
                project_root.parent / "stocklist.txt",  # /Users/hgs/Desktop/IISE CD/stocklist.txt
            ],
            label="stocklist.txt",
        )

    return PipelineConfig(
        project_root=project_root,
        posts_path=Path(args.posts).expanduser().resolve(),
        comments_path=Path(args.comments).expanduser().resolve(),
        stocklist_path=stocklist_path,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        post_chunksize=args.post_chunksize,
        comment_chunksize=args.comment_chunksize,
    )


def main() -> None:
    config = build_config_from_args()
    pipeline = DciStockCommentFilterPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()