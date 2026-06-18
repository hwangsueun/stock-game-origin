# ============================================================
# pr_dci04a_build_market_shock_features.py
#
# 입력:
#   /Users/hgs/Desktop/IISE CD/data/raw/stock/stock_price-volume_npq.xlsx
#   /Users/hgs/Desktop/IISE CD/stocklist.txt   # 선택. 종목명만 있어도 됨.
#
# 출력:
#   data/processed/market_shock_features/market_shock_features.csv
#   data/processed/market_shock_features/market_shock_report.txt
#
# 목적:
#   가격/거래량 shock feature 생성
# ============================================================

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class MarketShockConfig:
    project_root: Path
    excel_path: Path
    stocklist_path: Optional[Path]
    output_dir: Path

    encoding: str = "utf-8-sig"

    # 엑셀 구조: 1-based 기준
    code_row: int = 9
    field_row: int = 13
    data_start_row: int = 15

    lookback_days: int = 20
    min_periods: int = 10

    residual_z_cut: float = 2.5
    return_z_cut: float = 2.5
    volume_ratio_cut: float = 2.5


class StockCodeNormalizer:
    @staticmethod
    def normalize(value) -> str:
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


class StockNameLoader:
    def __init__(self, stocklist_path: Optional[Path], encoding: str = "utf-8-sig"):
        self.stocklist_path = stocklist_path
        self.encoding = encoding

    def load_name_list(self) -> List[str]:
        if self.stocklist_path is None:
            return []

        if not self.stocklist_path.exists():
            return []

        names = []

        with open(self.stocklist_path, "r", encoding=self.encoding) as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                if line.startswith("#"):
                    continue

                # stocklist.txt가 종목명만 있는 구조이므로 코드 포함 라인은 제외
                if re.search(r"A?\d{6}", line):
                    continue

                if any(x in line.lower() for x in ["stock_code", "stock_name", "종목코드", "종목명"]):
                    continue

                names.append(line)

        seen = set()
        result = []

        for name in names:
            if name in seen:
                continue

            seen.add(name)
            result.append(name)

        return result


class CvExcelParser:
    """
    stock_price-volume_npq.xlsx를 long format으로 변환.

    전제:
      - 9행: 종목코드
      - 13행: 종가 / 거래량 구분
      - 15행부터 데이터
      - 1열: 날짜
    """

    def __init__(self, config: MarketShockConfig, stock_name_list: List[str]):
        self.config = config
        self.stock_name_list = stock_name_list

    def parse(self) -> pd.DataFrame:
        print("[ExcelParser] reading excel...")

        raw = pd.read_excel(
            self.config.excel_path,
            header=None,
            engine="openpyxl",
        )

        print(f"[ExcelParser] raw shape: {raw.shape}")

        code_row = raw.iloc[self.config.code_row - 1]
        field_row = raw.iloc[self.config.field_row - 1]
        data = raw.iloc[self.config.data_start_row - 1:].copy()

        specs = self._build_specs(code_row, field_row)

        if not specs:
            raise ValueError("엑셀에서 종가/거래량 컬럼을 찾지 못했습니다. code_row/field_row를 확인해야 합니다.")

        print(f"[ExcelParser] parsed column specs: {len(specs):,}")

        rows = []

        for idx, data_row in data.iterrows():
            date_value = pd.to_datetime(data_row.iloc[0], errors="coerce")

            if pd.isna(date_value):
                continue

            date_value = date_value.normalize()

            stock_bucket = {}

            for spec in specs:
                col_idx = spec["col_idx"]
                stock_code = spec["stock_code"]
                stock_name = spec["stock_name"]
                field = spec["field"]

                value = self._to_float(data_row.iloc[col_idx])

                if stock_code not in stock_bucket:
                    stock_bucket[stock_code] = {
                        "date": date_value,
                        "stock_code": stock_code,
                        "stock_name": stock_name,
                        "close": np.nan,
                        "volume": np.nan,
                    }

                if field == "close":
                    stock_bucket[stock_code]["close"] = value
                elif field == "volume":
                    stock_bucket[stock_code]["volume"] = value

            rows.extend(stock_bucket.values())

        out = pd.DataFrame(rows)

        if out.empty:
            raise ValueError("long format 변환 결과가 비었습니다.")

        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce")

        out = out[
            out["date"].notna()
            & out["stock_code"].astype(str).str.len().gt(0)
            & out["close"].notna()
        ].copy()

        out = out.drop_duplicates(["date", "stock_code"], keep="last")
        out = out.sort_values(["stock_code", "date"]).reset_index(drop=True)

        print(f"[ExcelParser] long rows: {len(out):,}")
        print(f"[ExcelParser] unique stocks: {out['stock_code'].nunique():,}")
        print(f"[ExcelParser] date range: {out['date'].min()} ~ {out['date'].max()}")

        return out

    def _build_specs(self, code_row: pd.Series, field_row: pd.Series) -> List[Dict]:
        specs = []

        current_code = ""
        ordered_codes = []

        # 0번 컬럼은 날짜 컬럼이므로 제외
        for col_idx in range(1, len(code_row)):
            raw_code = code_row.iloc[col_idx]
            code = StockCodeNormalizer.normalize(raw_code)

            # 병합셀/빈칸 대응: 종목코드 forward fill
            if code:
                current_code = code

                if current_code not in ordered_codes:
                    ordered_codes.append(current_code)

            if not current_code:
                continue

            field = self._normalize_field(field_row.iloc[col_idx])

            if field is None:
                continue

            specs.append({
                "col_idx": col_idx,
                "stock_code": current_code,
                "stock_name": "",
                "field": field,
            })

        # stocklist.txt가 종목명만 있는 경우, 엑셀 종목코드 순서와 stocklist 순서를 맞춰 매핑
        code_to_name = {}

        if len(self.stock_name_list) == len(ordered_codes):
            code_to_name = dict(zip(ordered_codes, self.stock_name_list))
            print(f"[ExcelParser] stock name order mapping applied: {len(code_to_name):,}")
        else:
            print("[ExcelParser] stock name order mapping skipped")
            print(f"  excel unique codes: {len(ordered_codes):,}")
            print(f"  stocklist names: {len(self.stock_name_list):,}")

        for spec in specs:
            spec["stock_name"] = code_to_name.get(spec["stock_code"], "")

        return specs

    @staticmethod
    def _normalize_field(value) -> Optional[str]:
        if pd.isna(value):
            return None

        s = str(value).strip().lower()

        if not s:
            return None

        if "종가" in s or "close" in s or "수정종가" in s:
            return "close"

        if "거래량" in s or "volume" in s or s == "vol":
            return "volume"

        return None

    @staticmethod
    def _to_float(value) -> float:
        if pd.isna(value):
            return np.nan

        if isinstance(value, (int, float, np.integer, np.floating)):
            return float(value)

        s = str(value).strip()
        s = s.replace(",", "")
        s = s.replace("+", "")
        s = s.replace("%", "")
        s = s.replace("−", "-")

        try:
            return float(s)
        except ValueError:
            return np.nan


class MarketShockFeatureBuilder:
    def __init__(self, config: MarketShockConfig):
        self.config = config

    def build(self, price_df: pd.DataFrame) -> pd.DataFrame:
        df = price_df.copy()

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

        df = df.dropna(subset=["date", "stock_code", "close"]).copy()
        df = df[df["close"] > 0].copy()

        df = df.sort_values(["stock_code", "date"]).reset_index(drop=True)

        # 1. 개별 수익률
        df["return_pct"] = df.groupby("stock_code")["close"].pct_change()

        # 2. 시장 평균 수익률
        market_return = (
            df.groupby("date")
            .agg(
                market_return_pct=("return_pct", "mean"),
                market_stock_count=("return_pct", "count"),
            )
            .reset_index()
        )

        df = df.merge(market_return, on="date", how="left")

        # 3. 시장 제거 수익률
        df["residual_return"] = df["return_pct"] - df["market_return_pct"]

        # 4. rolling feature
        parts = []

        for stock_code, g in df.groupby("stock_code"):
            g = g.sort_values("date").copy()

            ret = g["return_pct"]
            residual = g["residual_return"]
            volume = g["volume"]

            ret_mean = ret.shift(1).rolling(
                window=self.config.lookback_days,
                min_periods=self.config.min_periods,
            ).mean()

            ret_std = ret.shift(1).rolling(
                window=self.config.lookback_days,
                min_periods=self.config.min_periods,
            ).std()

            residual_mean = residual.shift(1).rolling(
                window=self.config.lookback_days,
                min_periods=self.config.min_periods,
            ).mean()

            residual_std = residual.shift(1).rolling(
                window=self.config.lookback_days,
                min_periods=self.config.min_periods,
            ).std()

            volume_mean = volume.shift(1).rolling(
                window=self.config.lookback_days,
                min_periods=self.config.min_periods,
            ).mean()

            g["return_prev20_mean"] = ret_mean
            g["return_prev20_std"] = ret_std
            g["return_z"] = self._safe_z(ret, ret_mean, ret_std)

            g["residual_prev20_mean"] = residual_mean
            g["residual_prev20_std"] = residual_std
            g["residual_z"] = self._safe_z(residual, residual_mean, residual_std)

            g["volume_prev20_mean"] = volume_mean
            g["volume_ratio"] = (volume + 1.0) / (volume_mean + 1.0)
            g["volume_ratio"] = g["volume_ratio"].replace([np.inf, -np.inf], np.nan).fillna(0)

            parts.append(g)

        out = pd.concat(parts, ignore_index=True)

        out["return_z_abs"] = out["return_z"].abs()
        out["residual_z_abs"] = out["residual_z"].abs()

        out["has_price_shock"] = (
            (out["residual_z_abs"] >= self.config.residual_z_cut)
            | (out["return_z_abs"] >= self.config.return_z_cut)
        ).astype(int)

        out["has_volume_shock"] = (
            out["volume_ratio"] >= self.config.volume_ratio_cut
        ).astype(int)

        out["has_market_shock"] = (
            (out["has_price_shock"] == 1)
            | (out["has_volume_shock"] == 1)
        ).astype(int)

        keep_cols = [
            "date",
            "stock_code",
            "stock_name",
            "close",
            "volume",
            "return_pct",
            "market_return_pct",
            "market_stock_count",
            "residual_return",
            "return_prev20_mean",
            "return_prev20_std",
            "return_z",
            "return_z_abs",
            "residual_prev20_mean",
            "residual_prev20_std",
            "residual_z",
            "residual_z_abs",
            "volume_prev20_mean",
            "volume_ratio",
            "has_price_shock",
            "has_volume_shock",
            "has_market_shock",
        ]

        out = out[keep_cols].copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")

        return out

    @staticmethod
    def _safe_z(value: pd.Series, mean: pd.Series, std: pd.Series) -> pd.Series:
        std_safe = std.where(std > 1e-9, np.nan)

        z = (value - mean) / std_safe

        return z.replace([np.inf, -np.inf], np.nan).fillna(0)


class MarketShockOutputWriter:
    def __init__(self, config: MarketShockConfig):
        self.config = config

    def write(self, features: pd.DataFrame) -> None:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        feature_path = self.config.output_dir / "market_shock_features.csv"
        top_path = self.config.output_dir / "market_shock_top_events.csv"
        report_path = self.config.output_dir / "market_shock_report.txt"

        features.to_csv(feature_path, index=False, encoding=self.config.encoding)

        top = features[features["has_market_shock"] == 1].copy()
        top["shock_score"] = (
            0.5 * top["residual_z_abs"]
            + 0.25 * top["return_z_abs"]
            + 0.25 * np.log1p(top["volume_ratio"])
        )

        top = top.sort_values(
            ["shock_score", "residual_z_abs", "volume_ratio"],
            ascending=[False, False, False],
        )

        top.to_csv(top_path, index=False, encoding=self.config.encoding)

        self._write_report(features, report_path)

        print(f"[SAVE] {feature_path}")
        print(f"[SAVE] {top_path}")
        print(f"[SAVE] {report_path}")

    def _write_report(self, features: pd.DataFrame, report_path: Path) -> None:
        lines = []

        lines.append("# Market Shock Feature Report")
        lines.append("")
        lines.append("## Input")
        lines.append(f"- excel_path: {self.config.excel_path}")
        lines.append(f"- stocklist_path: {self.config.stocklist_path}")
        lines.append("")
        lines.append("## Rows")
        lines.append(f"- total_rows: {len(features):,}")
        lines.append(f"- unique_stocks: {features['stock_code'].nunique():,}")
        lines.append(f"- date_min: {features['date'].min()}")
        lines.append(f"- date_max: {features['date'].max()}")
        lines.append("")
        lines.append("## Shock Counts")
        lines.append(f"- has_price_shock: {int((features['has_price_shock'] == 1).sum()):,}")
        lines.append(f"- has_volume_shock: {int((features['has_volume_shock'] == 1).sum()):,}")
        lines.append(f"- has_market_shock: {int((features['has_market_shock'] == 1).sum()):,}")
        lines.append("")

        for metric in ["return_z_abs", "residual_z_abs", "volume_ratio"]:
            s = pd.to_numeric(features[metric], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()

            lines.append(f"## {metric}")
            lines.append(f"- p50: {s.quantile(0.50):.6f}")
            lines.append(f"- p90: {s.quantile(0.90):.6f}")
            lines.append(f"- p95: {s.quantile(0.95):.6f}")
            lines.append(f"- p99: {s.quantile(0.99):.6f}")
            lines.append(f"- max: {s.max():.6f}")
            lines.append("")

        with open(report_path, "w", encoding=self.config.encoding) as f:
            f.write("\n".join(lines))


class MarketShockPipeline:
    def __init__(self, config: MarketShockConfig):
        self.config = config

    def run(self) -> None:
        self._validate_inputs()

        stock_names = StockNameLoader(
            stocklist_path=self.config.stocklist_path,
            encoding=self.config.encoding,
        ).load_name_list()

        print(f"[StockNameLoader] stock names: {len(stock_names):,}")

        price_df = CvExcelParser(
            config=self.config,
            stock_name_list=stock_names,
        ).parse()

        features = MarketShockFeatureBuilder(
            config=self.config,
        ).build(price_df)

        MarketShockOutputWriter(
            config=self.config,
        ).write(features)

        print("\n[DONE]")
        print(f"output_dir: {self.config.output_dir}")

    def _validate_inputs(self) -> None:
        if not self.config.excel_path.exists():
            raise FileNotFoundError(f"excel_path 없음: {self.config.excel_path}")

        print("[Input Check]")
        print(f"  excel_path: {self.config.excel_path} ({self.config.excel_path.stat().st_size / 1024 / 1024:,.1f} MB)")

        if self.config.stocklist_path is not None:
            print(f"  stocklist_path: {self.config.stocklist_path}")


def build_config_from_args() -> MarketShockConfig:
    project_root = Path(__file__).resolve().parent.parent
    processed_dir = project_root / "data" / "processed"

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--excel",
        type=str,
        required=True,
    )

    parser.add_argument(
        "--stocklist",
        type=str,
        default="",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(processed_dir / "market_shock_features"),
    )

    parser.add_argument("--lookback-days", type=int, default=20)
    parser.add_argument("--min-periods", type=int, default=10)

    parser.add_argument("--residual-z-cut", type=float, default=2.5)
    parser.add_argument("--return-z-cut", type=float, default=2.5)
    parser.add_argument("--volume-ratio-cut", type=float, default=2.5)

    args = parser.parse_args()

    stocklist_path = None

    if args.stocklist.strip():
        stocklist_path = Path(args.stocklist).expanduser().resolve()

    return MarketShockConfig(
        project_root=project_root,
        excel_path=Path(args.excel).expanduser().resolve(),
        stocklist_path=stocklist_path,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        lookback_days=args.lookback_days,
        min_periods=args.min_periods,
        residual_z_cut=args.residual_z_cut,
        return_z_cut=args.return_z_cut,
        volume_ratio_cut=args.volume_ratio_cut,
    )


def main() -> None:
    config = build_config_from_args()
    MarketShockPipeline(config).run()


if __name__ == "__main__":
    main()