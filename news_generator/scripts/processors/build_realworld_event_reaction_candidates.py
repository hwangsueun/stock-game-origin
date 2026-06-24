#!/usr/bin/env python3
"""Build price-linked real-world event candidates from structured event data.

The first supported source is UCDP GED. UCDP records are retrospective research
data, so this script never sends them directly to the news writer. It creates a
review queue whose events have both a predeclared asset exposure and a material
post-event market move. A separate source-verification step must promote a row
before it can enter macro-news generation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BASE_DIR.parent
DEFAULT_UCDP = BASE_DIR / "data/raw/realworld_events/ucdp/GEDEvent_v26_1.csv"
DEFAULT_MARKET_DIR = PROJECT_DIR / "market_indicator/data/raw"
DEFAULT_OUTPUT_DIR = BASE_DIR / "data/interim/realworld_event_reactions/ucdp_v26_1"

UCDP_SOURCE_URL = "https://ucdp.uu.se/downloads/"
UCDP_LICENSE = "CC-BY-4.0"


@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    file_name: str
    min_abs_return_pct: float
    min_abs_zscore: float = 2.0


ASSETS = {
    "kospi": AssetSpec("kospi", "kospi_20130101_20231231.csv", 1.5),
    "kosdaq": AssetSpec("kosdaq", "kosdaq_20130101_20231231.csv", 2.0),
    "sp500": AssetSpec("sp500", "sp500_20130101_20231231.csv", 1.5),
    "nasdaq": AssetSpec("nasdaq", "nasdaq_20130101_20231231.csv", 2.0),
    "usdkrw": AssetSpec("usdkrw", "usdkrw_20130101_20231231.csv", 0.8),
    "wti": AssetSpec("wti", "wti_price_20130101_20231231.csv", 2.5),
    "gold": AssetSpec("gold", "gold_price_20130101_20231231.csv", 1.0),
}

# Countries are mapped before any return is observed. This prevents choosing an
# asset after seeing which one happened to move on an event date.
COUNTRY_EXPOSURES = {
    "South Korea": ("east_asia_security", ("kospi", "kosdaq", "usdkrw", "gold")),
    "North Korea": ("east_asia_security", ("kospi", "kosdaq", "usdkrw", "gold")),
    "China": ("east_asia_security", ("kospi", "kosdaq", "usdkrw", "gold")),
    "Taiwan": ("east_asia_security", ("kospi", "kosdaq", "usdkrw", "gold")),
    "Japan": ("east_asia_security", ("kospi", "usdkrw", "gold")),
    "Russia (Soviet Union)": ("europe_security", ("sp500", "nasdaq", "wti", "gold", "usdkrw")),
    "Ukraine": ("europe_security", ("sp500", "nasdaq", "wti", "gold", "usdkrw")),
    "Israel": ("middle_east_security", ("sp500", "wti", "gold", "usdkrw")),
    "Palestine": ("middle_east_security", ("sp500", "wti", "gold", "usdkrw")),
    "Lebanon": ("middle_east_security", ("sp500", "wti", "gold")),
    "Syria": ("middle_east_security", ("sp500", "wti", "gold")),
    "Iraq": ("oil_supply_security", ("wti", "gold", "sp500")),
    "Iran": ("oil_supply_security", ("wti", "gold", "sp500", "usdkrw")),
    "Yemen (North Yemen)": ("oil_supply_security", ("wti", "gold", "sp500")),
    "Saudi Arabia": ("oil_supply_security", ("wti", "gold", "sp500")),
    "Libya": ("oil_supply_security", ("wti", "gold", "sp500")),
    "Nigeria": ("oil_supply_security", ("wti", "gold")),
    "Egypt": ("shipping_energy_security", ("wti", "gold", "sp500")),
    "Turkey": ("europe_security", ("sp500", "wti", "gold")),
    "United States of America": ("us_domestic_security", ("sp500", "nasdaq", "gold")),
}

VIOLENCE_LABELS = {
    1: "state_based_conflict",
    2: "non_state_conflict",
    3: "one_sided_violence",
}

UCDP_COLUMNS = [
    "id", "year", "code_status", "type_of_violence", "conflict_name",
    "side_a", "side_b", "number_of_sources", "country", "region",
    "date_prec", "date_start", "date_end", "best", "high", "low",
    "deaths_civilians", "where_coordinates", "where_description",
]


def stable_id(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_asset_series(market_dir: Path) -> dict[str, pd.DataFrame]:
    result = {}
    for asset_id, spec in ASSETS.items():
        path = market_dir / spec.file_name
        frame = pd.read_csv(path, encoding="utf-8-sig")
        if "adj_close" not in frame.columns:
            raise ValueError(f"adj_close column missing: {path}")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["adj_close"] = pd.to_numeric(frame["adj_close"], errors="coerce")
        frame = frame.dropna(subset=["date", "adj_close"]).sort_values("date")
        frame = frame.drop_duplicates("date", keep="last").set_index("date")
        previous = frame["adj_close"].shift(1)
        frame["return_pct"] = frame["adj_close"].pct_change() * 100
        # Ratio returns are undefined across zero/negative futures prices.
        frame.loc[(frame["adj_close"] <= 0) | (previous <= 0), "return_pct"] = pd.NA
        frame["prior_60d_vol_pct"] = frame["return_pct"].rolling(60).std().shift(1)
        result[asset_id] = frame
    return result


def load_ucdp_events(
    path: Path,
    start_date: str,
    end_date: str,
    min_fatalities: int,
    availability_lag_days: int,
) -> list[dict]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    frame = pd.read_csv(path, usecols=UCDP_COLUMNS, low_memory=False)
    frame["date_start"] = pd.to_datetime(frame["date_start"], errors="coerce")
    frame["date_end"] = pd.to_datetime(frame["date_end"], errors="coerce")
    for column in ("best", "high", "low", "deaths_civilians", "date_prec", "type_of_violence"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame[
        frame["date_start"].between(start, end)
        & frame["date_prec"].eq(1)
        & frame["code_status"].eq("Clear")
    ].copy()
    frame["best"] = frame["best"].fillna(0)
    frame["deaths_civilians"] = frame["deaths_civilians"].fillna(0)

    group_columns = [
        "date_start", "date_end", "country", "region", "type_of_violence",
        "conflict_name", "side_a", "side_b",
    ]
    grouped = frame.groupby(group_columns, dropna=False, as_index=False).agg(
        ucdp_record_count=("id", "size"),
        ucdp_ids=("id", lambda values: [str(value) for value in values]),
        fatalities_best=("best", "sum"),
        fatalities_high=("high", "sum"),
        fatalities_low=("low", "sum"),
        civilian_fatalities=("deaths_civilians", "sum"),
    )
    grouped = grouped[grouped["fatalities_best"] >= min_fatalities].copy()

    events = []
    for row in grouped.to_dict("records"):
        country = str(row["country"])
        exposure_rule, assets = COUNTRY_EXPOSURES.get(country, ("no_predeclared_asset_exposure", ()))
        event_date = pd.Timestamp(row["date_start"]).normalize()
        available_date = event_date + pd.Timedelta(days=availability_lag_days)
        violence_type = int(row["type_of_violence"])
        event_id = "ucdp_" + stable_id(
            event_date.date(), country, row["conflict_name"], row["side_a"], row["side_b"],
        )
        events.append({
            "event_id": event_id,
            "source": "UCDP_GED",
            "source_version": "26.1",
            "source_url": UCDP_SOURCE_URL,
            "license": UCDP_LICENSE,
            "event_date": event_date.date().isoformat(),
            "available_date_conservative": available_date.date().isoformat(),
            "availability_rule": f"event_date_plus_{availability_lag_days}_calendar_day",
            "event_type": VIOLENCE_LABELS.get(violence_type, "organized_violence"),
            "country": country,
            "region": str(row["region"]),
            "conflict_name": str(row["conflict_name"]),
            "side_a": str(row["side_a"]),
            "side_b": str(row["side_b"]),
            "fatalities_best": int(row["fatalities_best"]),
            "fatalities_low": int(row["fatalities_low"]),
            "fatalities_high": int(row["fatalities_high"]),
            "civilian_fatalities": int(row["civilian_fatalities"]),
            "ucdp_record_count": int(row["ucdp_record_count"]),
            "ucdp_ids": row["ucdp_ids"],
            "exposure_rule": exposure_rule,
            "predeclared_assets": list(assets),
            "retrospective_source": True,
            "generation_eligible": False,
            "verification_status": "requires_contemporaneous_source_verification",
        })
    return sorted(events, key=lambda event: (event["event_date"], event["event_id"]))


def first_reaction(series: pd.DataFrame, available_date: str) -> dict | None:
    available = pd.Timestamp(available_date)
    pos = series.index.searchsorted(available, side="left")
    if pos <= 0 or pos >= len(series):
        return None
    row = series.iloc[pos]
    ret = row["return_pct"]
    vol = row["prior_60d_vol_pct"]
    if pd.isna(ret) or pd.isna(vol) or vol <= 0:
        return None
    return {
        "reaction_date": series.index[pos].date().isoformat(),
        "baseline_date": series.index[pos - 1].date().isoformat(),
        "return_1d_pct": round(float(ret), 4),
        "prior_60d_vol_pct": round(float(vol), 4),
        "return_zscore": round(float(ret / vol), 4),
    }


def build_reactions(
    events: list[dict],
    series_by_asset: dict[str, pd.DataFrame],
) -> list[dict]:
    rows = []
    for event in events:
        for asset_id in event["predeclared_assets"]:
            spec = ASSETS[asset_id]
            reaction = first_reaction(
                series_by_asset[asset_id], event["available_date_conservative"],
            )
            if reaction is None:
                continue
            material = (
                abs(reaction["return_1d_pct"]) >= spec.min_abs_return_pct
                and abs(reaction["return_zscore"]) >= spec.min_abs_zscore
            )
            rows.append({
                "event_id": event["event_id"],
                "event_date": event["event_date"],
                "available_date_conservative": event["available_date_conservative"],
                "country": event["country"],
                "event_type": event["event_type"],
                "event_name": event.get("conflict_name") or event.get("event_name_en") or event["event_id"],
                "dominance_score": event.get("fatalities_best", event.get("importance_score", 1)),
                "exposure_rule": event["exposure_rule"],
                "asset_id": asset_id,
                **reaction,
                "min_abs_return_pct": spec.min_abs_return_pct,
                "min_abs_zscore": spec.min_abs_zscore,
                "material_reaction": material,
                "linkage_class": "event_window_reaction" if material else "below_threshold",
                "causal_claim_allowed": False,
                "generation_eligible": False,
                "verification_status": event["verification_status"],
            })
    return rows


def annotate_review_candidates(rows: list[dict]) -> list[dict]:
    frame = pd.DataFrame(rows)
    frame["same_window_candidate_count"] = frame.groupby(
        ["reaction_date", "asset_id"]
    )["event_id"].transform("count")
    frame["candidate_rank"] = frame.groupby(
        ["reaction_date", "asset_id"]
    )["dominance_score"].rank(method="first", ascending=False).astype(int)

    second_by_window = {}
    for key, group in frame.groupby(["reaction_date", "asset_id"]):
        values = sorted(group["dominance_score"].tolist(), reverse=True)
        second_by_window[key] = values[1] if len(values) > 1 else 0

    dominance = []
    review = []
    for row in frame.to_dict("records"):
        second = second_by_window[(row["reaction_date"], row["asset_id"])]
        ratio = None if second <= 0 else row["dominance_score"] / second
        dominant = row["candidate_rank"] == 1 and (second <= 0 or ratio >= 2.0)
        dominance.append(round(ratio, 4) if ratio is not None else "")
        review.append(bool(row["material_reaction"] and dominant))
    frame["dominance_ratio"] = dominance
    frame["review_candidate"] = review
    frame.loc[frame["review_candidate"], "linkage_class"] = "event_window_reaction_candidate"
    frame.loc[
        frame["material_reaction"] & ~frame["review_candidate"], "linkage_class"
    ] = "confounded_event_window"
    return frame.to_dict("records")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("no reaction rows")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, events: list[dict], reactions: list[dict]) -> None:
    material = [row for row in reactions if row["material_reaction"]]
    review = [row for row in reactions if row["review_candidate"]]
    material_events = {row["event_id"] for row in material}
    review_events = {row["event_id"] for row in review}
    by_asset = pd.Series([row["asset_id"] for row in review]).value_counts().to_dict()
    by_country = pd.Series([
        row["country"] for row in review
    ]).value_counts().head(20).to_dict()
    lines = [
        "# UCDP real-world event reaction gate",
        "",
        f"- normalized events: {len(events):,}",
        f"- tested event-asset pairs: {len(reactions):,}",
        f"- material reaction pairs: {len(material):,}",
        f"- events with material reaction: {len(material_events):,}",
        f"- unconfounded review pairs: {len(review):,}",
        f"- unconfounded review events: {len(review_events):,}",
        "- linkage: event-window association only; causal wording is prohibited",
        "- generation eligibility: false until contemporaneous-source verification",
        "",
        "## Review pairs by asset",
        "",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in by_asset.items())
    lines += ["", "## Top countries by review pairs", ""]
    lines.extend(f"- {key}: {value:,}" for key, value in by_country.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ucdp-csv", type=Path, default=DEFAULT_UCDP)
    parser.add_argument("--market-data-dir", type=Path, default=DEFAULT_MARKET_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-date", default="2013-01-01")
    parser.add_argument("--end-date", default="2023-12-31")
    parser.add_argument("--min-fatalities", type=int, default=25)
    parser.add_argument("--availability-lag-days", type=int, default=1)
    parser.add_argument("--normalized-events-jsonl", type=Path)
    args = parser.parse_args()

    if args.normalized_events_jsonl:
        events = [
            json.loads(line) for line in args.normalized_events_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        events = load_ucdp_events(
            args.ucdp_csv,
            args.start_date,
            args.end_date,
            args.min_fatalities,
            args.availability_lag_days,
        )
    series = load_asset_series(args.market_data_dir)
    reactions = build_reactions(events, series)
    reactions = annotate_review_candidates(reactions)

    write_jsonl(args.output_dir / "normalized_events.jsonl", events)
    write_csv(args.output_dir / "event_asset_reactions.csv", reactions)
    write_csv(
        args.output_dir / "material_event_asset_reactions.csv",
        [row for row in reactions if row["material_reaction"]],
    )
    write_csv(
        args.output_dir / "review_candidates.csv",
        [row for row in reactions if row["review_candidate"]],
    )
    write_report(args.output_dir / "REPORT.md", events, reactions)
    print(f"events={len(events):,}")
    print(f"reaction_pairs={len(reactions):,}")
    print(f"material_pairs={sum(row['material_reaction'] for row in reactions):,}")
    print(f"review_candidates={sum(row['review_candidate'] for row in reactions):,}")
    print(f"output_dir={args.output_dir}")


if __name__ == "__main__":
    main()
