# ============================================================
# pr_dci04b_build_dart_event_evidence.py
#
# 입력:
#   dart_results_2013_2024.json
#
# 출력:
#   data/processed/dart_event_evidence/
#     dart_event_evidence_detail.csv
#     dart_event_evidence_daily.csv
#     dart_event_evidence_report.txt
#
# 목적:
#   DART 공시 원본에서 event성 공시만 추출하여
#   date x stock 단위 factual evidence 생성
# ============================================================

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class DartEvidenceConfig:
    project_root: Path
    dart_path: Path
    output_dir: Path
    encoding: str = "utf-8-sig"


class StockCodeNormalizer:
    @staticmethod
    def normalize(value: object) -> str:
        if pd.isna(value):
            return ""

        s = str(value).strip()

        if re.fullmatch(r"\d+\.0", s):
            s = s[:-2]

        s = s.replace("A", "")
        s = s.replace(".KS", "")
        s = s.replace(".KQ", "")
        s = re.sub(r"\D", "", s)

        if not s:
            return ""

        if len(s) > 6:
            s = s[-6:]

        if len(s) < 6:
            s = s.zfill(6)

        return s


class DartRawLoader:
    def __init__(self, config: DartEvidenceConfig):
        self.config = config

    def load(self) -> pd.DataFrame:
        path = self.config.dart_path

        if not path.exists():
            raise FileNotFoundError(f"DART 파일 없음: {path}")

        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path, dtype=str, encoding=self.config.encoding)
            print(f"[DartRawLoader] csv rows: {len(df):,}")
            return df

        if path.suffix.lower() in [".xlsx", ".xls"]:
            df = pd.read_excel(path, dtype=str)
            print(f"[DartRawLoader] excel rows: {len(df):,}")
            return df

        rows = self._load_json_recursive(path)
        df = pd.DataFrame(rows)

        print(f"[DartRawLoader] json rows: {len(df):,}")

        if not df.empty:
            print("[DartRawLoader] columns:")
            print(df.columns.tolist())
            print("[DartRawLoader] sample:")
            print(df.head(3).to_string(index=False))

        return df

    def _load_json_recursive(self, path: Path) -> List[Dict[str, Any]]:
        text = path.read_text(encoding="utf-8")

        try:
            obj = json.loads(text)
        except Exception as e:
            raise ValueError(f"JSON 파싱 실패: {path} / {e}")

        rows = []
        self._collect_dart_rows(obj, rows)

        # 완전 중복 제거
        if rows:
            temp = pd.DataFrame(rows).drop_duplicates()
            rows = temp.to_dict("records")

        return rows

    def _collect_dart_rows(self, obj: Any, rows: List[Dict[str, Any]]) -> None:
        if obj is None:
            return

        if isinstance(obj, list):
            for item in obj:
                self._collect_dart_rows(item, rows)
            return

        if isinstance(obj, dict):
            # 현재 dict가 실제 공시 row인지 판정
            if self._looks_like_dart_row(obj):
                rows.append(obj)
                return

            # wrapper 또는 중첩 구조면 내부를 계속 탐색
            for value in obj.values():
                self._collect_dart_rows(value, rows)
            return

        return

    @staticmethod
    def _looks_like_dart_row(obj: Dict[str, Any]) -> bool:
        keys = {str(k).lower() for k in obj.keys()}

        report_keys = {
            "report_nm",
            "report_name",
            "reportname",
            "공시제목",
            "보고서명",
            "title",
        }

        date_keys = {
            "rcept_dt",
            "rcept_date",
            "접수일자",
            "date",
            "공시일자",
        }

        corp_keys = {
            "corp_name",
            "corpname",
            "stock_name",
            "종목명",
            "회사명",
            "기업명",
        }

        has_report = bool(keys & report_keys)
        has_date = bool(keys & date_keys)
        has_corp = bool(keys & corp_keys)

        return has_report and has_date and has_corp

class DartColumnStandardizer:
    STOCK_CODE_CANDIDATES = [
        "stock_code",
        "종목코드",
        "code",
        "ticker",
    ]

    STOCK_NAME_CANDIDATES = [
        "stock_name",
        "corp_name",
        "corpName",
        "종목명",
        "회사명",
        "기업명",
    ]

    DATE_CANDIDATES = [
        "rcept_dt",
        "rcept_date",
        "접수일자",
        "date",
        "공시일자",
    ]

    REPORT_CANDIDATES = [
        "report_nm",
        "report_name",
        "공시제목",
        "보고서명",
        "title",
    ]

    RCEPT_NO_CANDIDATES = [
        "rcept_no",
        "접수번호",
        "receipt_no",
    ]

    CORP_CODE_CANDIDATES = [
        "corp_code",
        "고유번호",
    ]

    def standardize(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            raise ValueError("DART 원본이 비어 있습니다.")

        cols = list(df.columns)

        stock_code_col = self._find_col(cols, self.STOCK_CODE_CANDIDATES)
        stock_name_col = self._find_col(cols, self.STOCK_NAME_CANDIDATES)
        date_col = self._find_col(cols, self.DATE_CANDIDATES)
        report_col = self._find_col(cols, self.REPORT_CANDIDATES)
        rcept_no_col = self._find_col(cols, self.RCEPT_NO_CANDIDATES)
        corp_code_col = self._find_col(cols, self.CORP_CODE_CANDIDATES)

        if date_col is None:
            raise ValueError(f"DART 날짜 컬럼을 찾지 못했습니다. columns={cols}")

        if report_col is None:
            raise ValueError(f"DART 보고서명 컬럼을 찾지 못했습니다. columns={cols}")

        out = pd.DataFrame()

        out["dart_date"] = df[date_col].map(self._parse_date)

        if stock_code_col is not None:
            out["stock_code"] = df[stock_code_col].map(StockCodeNormalizer.normalize)
        else:
            out["stock_code"] = ""

        if stock_name_col is not None:
            out["stock_name"] = df[stock_name_col].fillna("").astype(str).str.strip()
        else:
            out["stock_name"] = ""

        if corp_code_col is not None:
            out["corp_code"] = df[corp_code_col].fillna("").astype(str).str.strip()
        else:
            out["corp_code"] = ""

        out["report_name"] = df[report_col].fillna("").astype(str).str.strip()

        if rcept_no_col is not None:
            out["rcept_no"] = df[rcept_no_col].fillna("").astype(str).str.strip()
        else:
            out["rcept_no"] = ""

        out = out[
            out["dart_date"].notna()
            & out["report_name"].astype(str).str.len().gt(0)
            & (
                out["stock_code"].astype(str).str.len().gt(0)
                | out["stock_name"].astype(str).str.len().gt(0)
            )
        ].copy()

        out["dart_date"] = pd.to_datetime(out["dart_date"]).dt.normalize()

        print("[DartColumnStandardizer]")
        print(f"  date_col: {date_col}")
        print(f"  stock_code_col: {stock_code_col}")
        print(f"  stock_name_col: {stock_name_col}")
        print(f"  report_col: {report_col}")
        print(f"  rcept_no_col: {rcept_no_col}")
        print(f"  standardized rows: {len(out):,}")

        return out

    @staticmethod
    def _find_col(cols: List[str], candidates: List[str]) -> Optional[str]:
        norm = {str(c).strip().lower(): c for c in cols}

        for cand in candidates:
            if cand.lower() in norm:
                return norm[cand.lower()]

        for col in cols:
            low = str(col).strip().lower()
            for cand in candidates:
                if cand.lower() in low:
                    return col

        return None

    @staticmethod
    def _parse_date(value: object) -> pd.Timestamp:
        if pd.isna(value):
            return pd.NaT

        s = str(value).strip()

        if re.fullmatch(r"\d{8}", s):
            return pd.to_datetime(s, format="%Y%m%d", errors="coerce")

        return pd.to_datetime(s, errors="coerce")


class DartEventClassifier:
    """
    게임 뉴스 factual evidence용 DART 필터.

    원칙:
      - 투자설명서, 정기보고서, 대량보유보고서 등 반복/행정성 공시는 제외
      - 단일판매공급계약, 실적변동, 배당, 증자, 자사주, 시설투자, 합병/분할, 리스크 공시 중심
      - '투자', '취득', '처분' 같은 너무 넓은 단어 단독 매칭 금지
    """

    EXCLUDE_KEYWORDS = [
        # 정기/행정성 보고서
        "사업보고서",
        "반기보고서",
        "분기보고서",
        "감사보고서",
        "연결감사보고서",
        "기업설명회",

        # 발행/신고 문서 자체. 사건 뉴스 근거로 쓰기 부적절
        "투자설명서",
        "증권신고서",
        "일괄신고서",
        "일괄신고추가서류",

        # 지분 보고 반복성이 강함
        "주식등의대량보유상황보고서",
        "임원ㆍ주요주주특정증권등소유상황보고서",
        "최대주주등소유주식변동신고서",

        # 내부거래/계열거래 반복성이 강함
        "동일인등출자계열회사와의상품ㆍ용역거래",
        "특수관계인에대한출자",
        "특수관계인에대한부동산거래",
        "특수관계인과의내부거래",
    ]

    EVENT_PATTERNS = {
        "contract": [
            "단일판매ㆍ공급계약체결",
            "단일판매·공급계약체결",
            "단일판매공급계약체결",
            "공급계약체결",
            "수주",
        ],
        "earnings_or_dividend": [
            "매출액또는손익구조30%",
            "매출액또는손익구조",
            "영업실적",
            "잠정실적",
            "영업이익",
            "당기순이익",
            "현금ㆍ현물배당결정",
            "현금·현물배당결정",
            "배당결정",
        ],
        "capital": [
            "유상증자결정",
            "무상증자결정",
            "전환사채권발행결정",
            "신주인수권부사채권발행결정",
            "교환사채권발행결정",
            "감자결정",
        ],
        "buyback": [
            "자기주식취득결정",
            "자기주식처분결정",
            "자기주식취득신탁계약체결결정",
            "자기주식취득결과보고서",
            "자기주식처분결과보고서",
        ],
        "investment": [
            "신규시설투자",
            "시설투자",
            "타법인주식및출자증권취득결정",
            "타법인주식및출자증권처분결정",
            "유형자산취득결정",
            "유형자산처분결정",
            "영업양수결정",
            "영업양도결정",
        ],
        "corporate_action": [
            "회사합병결정",
            "회사분할결정",
            "회사분할합병결정",
            "합병등종료보고서",
            "대표이사변경",
            "최대주주변경",
            "주요사항보고서",
            "투자판단관련주요경영사항",
        ],
        "risk": [
            "횡령",
            "배임",
            "소송",
            "부도",
            "파산",
            "회생절차",
            "거래정지",
            "불성실공시",
            "상장폐지",
            "관리종목",
            "감사의견",
        ],
    }

    GROUP_SCORE = {
        "risk": 5,
        "contract": 4,
        "capital": 4,
        "buyback": 3,
        "earnings_or_dividend": 3,
        "investment": 3,
        "corporate_action": 2,
    }

    def classify(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        out["report_name_clean"] = (
            out["report_name"]
            .fillna("")
            .astype(str)
            .str.replace("[기재정정]", "", regex=False)
            .str.replace("(정정)", "", regex=False)
            .str.strip()
        )

        out["is_excluded_routine"] = out["report_name_clean"].map(self._is_excluded).astype(int)
        out["dart_event_group"] = out["report_name_clean"].map(self._classify_group)

        out["is_dart_event"] = (
            (out["is_excluded_routine"] == 0)
            & out["dart_event_group"].notna()
        ).astype(int)

        out = out[out["is_dart_event"] == 1].copy()

        out["dart_materiality_score"] = (
            out["dart_event_group"]
            .map(self.GROUP_SCORE)
            .fillna(1)
            .astype(int)
        )

        return out

    def _is_excluded(self, report_name: str) -> bool:
        text = str(report_name)

        return any(k in text for k in self.EXCLUDE_KEYWORDS)

    def _classify_group(self, report_name: str) -> Optional[str]:
        text = str(report_name)

        for group, patterns in self.EVENT_PATTERNS.items():
            if any(p in text for p in patterns):
                return group

        return None
    
    
class DartEvidenceAggregator:
    def aggregate(self, detail: pd.DataFrame) -> pd.DataFrame:
        if detail.empty:
            return pd.DataFrame(columns=[
                "dart_date",
                "stock_code",
                "stock_name",
                "has_dart_event",
                "dart_event_count",
                "dart_max_materiality_score",
                "dart_event_groups",
                "dart_report_names",
                "dart_rcept_nos",
            ])

        df = detail.copy()

        df["stock_code"] = df["stock_code"].fillna("").astype(str)
        df["stock_name"] = df["stock_name"].fillna("").astype(str)

        grouped = (
            df.groupby(["dart_date", "stock_code", "stock_name"], dropna=False)
            .agg(
                dart_event_count=("report_name", "count"),
                dart_max_materiality_score=("dart_materiality_score", "max"),
                dart_event_groups=("dart_event_group", lambda x: self._join_unique(x, 10)),
                dart_report_names=("report_name", lambda x: self._join_unique(x, 10)),
                dart_rcept_nos=("rcept_no", lambda x: self._join_unique(x, 10)),
            )
            .reset_index()
        )

        grouped["has_dart_event"] = 1

        grouped = grouped[
            [
                "dart_date",
                "stock_code",
                "stock_name",
                "has_dart_event",
                "dart_event_count",
                "dart_max_materiality_score",
                "dart_event_groups",
                "dart_report_names",
                "dart_rcept_nos",
            ]
        ].copy()

        return grouped

    @staticmethod
    def _join_unique(values, limit: int = 10) -> str:
        result = []

        for v in values:
            s = str(v).strip()

            if not s or s == "nan":
                continue

            if s not in result:
                result.append(s)

            if len(result) >= limit:
                break

        return " | ".join(result)


class DartEvidenceWriter:
    def __init__(self, config: DartEvidenceConfig):
        self.config = config

    def write(self, raw: pd.DataFrame, detail: pd.DataFrame, daily: pd.DataFrame) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        detail_path = self.config.output_dir / "dart_event_evidence_detail.csv"
        daily_path = self.config.output_dir / "dart_event_evidence_daily.csv"
        report_path = self.config.output_dir / "dart_event_evidence_report.txt"

        detail_out = detail.copy()
        daily_out = daily.copy()

        if not detail_out.empty:
            detail_out["dart_date"] = pd.to_datetime(detail_out["dart_date"]).dt.strftime("%Y-%m-%d")

        if not daily_out.empty:
            daily_out["dart_date"] = pd.to_datetime(daily_out["dart_date"]).dt.strftime("%Y-%m-%d")

        detail_out.to_csv(detail_path, index=False, encoding=self.config.encoding)
        daily_out.to_csv(daily_path, index=False, encoding=self.config.encoding)

        self._write_report(raw, detail, daily, report_path)

        print(f"[SAVE] {detail_path}")
        print(f"[SAVE] {daily_path}")
        print(f"[SAVE] {report_path}")

    def _write_report(
        self,
        raw: pd.DataFrame,
        detail: pd.DataFrame,
        daily: pd.DataFrame,
        report_path: Path,
    ) -> None:
        lines = []

        lines.append("# DART Event Evidence Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- dart_path: {self.config.dart_path}")
        lines.append("")
        lines.append("## Counts")
        lines.append(f"- raw_rows: {len(raw):,}")
        lines.append(f"- event_detail_rows: {len(detail):,}")
        lines.append(f"- event_daily_rows: {len(daily):,}")

        if not detail.empty:
            lines.append(f"- unique_stocks_detail: {detail['stock_code'].nunique():,}")
            lines.append(f"- date_min: {detail['dart_date'].min()}")
            lines.append(f"- date_max: {detail['dart_date'].max()}")

            lines.append("")
            lines.append("## By dart_event_group")
            for k, v in detail["dart_event_group"].value_counts().items():
                lines.append(f"- {k}: {int(v):,}")

            lines.append("")
            lines.append("## Top report names")
            for k, v in detail["report_name"].value_counts().head(30).items():
                lines.append(f"- {k}: {int(v):,}")

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))


class DartEvidencePipeline:
    def __init__(self, config: DartEvidenceConfig):
        self.config = config

    def run(self) -> None:
        raw = DartRawLoader(self.config).load()
        standardized = DartColumnStandardizer().standardize(raw)
        detail = DartEventClassifier().classify(standardized)
        daily = DartEvidenceAggregator().aggregate(detail)

        DartEvidenceWriter(self.config).write(
            raw=raw,
            detail=detail,
            daily=daily,
        )

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")


def find_default_dart_path(project_root: Path) -> Optional[Path]:
    candidates = [
        project_root / "data" / "raw" / "dart_results_2013_2024.json",
        project_root / "data" / "raw" / "dart" / "dart_results_2013_2024.json",
        project_root.parent / "dart_results_2013_2024.json",
        project_root.parent / "data" / "raw" / "dart_results_2013_2024.json",
        project_root.parent / "data" / "raw" / "dart" / "dart_results_2013_2024.json",
    ]

    for path in candidates:
        if path.exists():
            return path

    return None


def build_config_from_args() -> DartEvidenceConfig:
    project_root = Path(__file__).resolve().parent.parent
    default_dart = find_default_dart_path(project_root)

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dart-json",
        type=str,
        default=str(default_dart) if default_dart is not None else "",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(project_root / "data" / "processed" / "dart_event_evidence"),
    )

    args = parser.parse_args()

    if not args.dart_json:
        raise FileNotFoundError("DART 파일을 자동으로 찾지 못했습니다. --dart-json으로 직접 지정하세요.")

    return DartEvidenceConfig(
        project_root=project_root,
        dart_path=Path(args.dart_json).expanduser().resolve(),
        output_dir=Path(args.output_dir).expanduser().resolve(),
    )


def main() -> None:
    config = build_config_from_args()
    DartEvidencePipeline(config).run()


if __name__ == "__main__":
    main()