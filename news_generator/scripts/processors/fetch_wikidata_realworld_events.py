#!/usr/bin/env python3
"""Fetch targeted real-world events from Wikidata using small cached queries."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests
import pandas as pd

from build_realworld_event_reaction_candidates import COUNTRY_EXPOSURES


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = BASE_DIR / "data/raw/realworld_events/wikidata"
SPARQL_URL = "https://query.wikidata.org/sparql"
ENTITY_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "IISE-CD-realworld-events/1.0 (research pipeline)"

EVENT_CLASSES = {
    "Q45382": ("coup_detat", 5),
    "Q2223653": ("terrorist_attack", 4),
    "Q8065": ("natural_disaster", 4),
    "Q171558": ("major_accident", 4),
    "Q43109": ("referendum", 3),
    "Q273120": ("protest", 3),
    "Q49776": ("strike", 3),
    "Q40231": ("election", 2),
}

COUNTRIES = {
    "Q884": "South Korea", "Q423": "North Korea", "Q148": "China",
    "Q865": "Taiwan", "Q17": "Japan", "Q159": "Russia (Soviet Union)",
    "Q212": "Ukraine", "Q801": "Israel", "Q219060": "Palestine",
    "Q822": "Lebanon", "Q858": "Syria", "Q796": "Iraq", "Q794": "Iran",
    "Q805": "Yemen (North Yemen)", "Q851": "Saudi Arabia", "Q1016": "Libya",
    "Q1033": "Nigeria", "Q79": "Egypt", "Q43": "Turkey",
    "Q30": "United States of America",
}


def request_json(session: requests.Session, url: str, params: dict, retries: int = 4) -> dict:
    error = None
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=90)
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(int(retry_after))
                raise requests.HTTPError(f"retryable status {response.status_code}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"request failed after {retries} attempts: {error}")


def query_class_year(session: requests.Session, class_qid: str, year: int, cache: Path) -> list[dict]:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    query = f"""
SELECT DISTINCT ?event ?date ?country WHERE {{
  ?event wdt:P31 wd:{class_qid}; wdt:P17 ?country.
  {{ ?event wdt:P585 ?date }} UNION {{ ?event wdt:P580 ?date }}
  FILTER(?date >= \"{year}-01-01T00:00:00Z\"^^xsd:dateTime &&
         ?date < \"{year + 1}-01-01T00:00:00Z\"^^xsd:dateTime)
}}
LIMIT 5000
"""
    payload = request_json(session, SPARQL_URL, {"query": query, "format": "json"})
    rows = payload["results"]["bindings"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.25)
    return rows


def labels(session: requests.Session, qids: list[str], cache: Path) -> dict[str, str]:
    stored = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    missing = [qid for qid in qids if qid not in stored]
    for offset in range(0, len(missing), 20):
        batch = missing[offset:offset + 20]
        payload = request_json(session, ENTITY_API, {
            "action": "wbgetentities", "format": "json", "ids": "|".join(batch),
            "props": "labels", "languages": "en",
        })
        for qid, entity in payload.get("entities", {}).items():
            stored[qid] = entity.get("labels", {}).get("en", {}).get("value", qid)
        cache.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(1.0)
    return stored


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-year", type=int, default=2013)
    parser.add_argument("--end-year", type=int, default=2023)
    parser.add_argument("--fetch-labels", action="store_true")
    args = parser.parse_args()

    cache_dir = args.output_dir / "cache"
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    raw = []
    for class_qid in EVENT_CLASSES:
        for year in range(args.start_year, args.end_year + 1):
            rows = query_class_year(
                session, class_qid, year, cache_dir / f"{class_qid}_{year}_global.json",
            )
            raw.extend({**row, "_class_qid": class_qid} for row in rows)

    event_qids = sorted({qid(row["event"]["value"]) for row in raw})
    country_qids = sorted({qid(row["country"]["value"]) for row in raw})
    event_label_cache = cache_dir / "event_labels_en.json"
    country_label_cache = cache_dir / "country_labels_en.json"
    if args.fetch_labels:
        event_labels = labels(session, event_qids, event_label_cache)
        country_labels = labels(session, country_qids, country_label_cache)
    else:
        event_labels = json.loads(event_label_cache.read_text(encoding="utf-8")) if event_label_cache.exists() else {}
        country_labels = json.loads(country_label_cache.read_text(encoding="utf-8")) if country_label_cache.exists() else {}
    seen = set()
    events = []
    for row in raw:
        event_qid = qid(row["event"]["value"])
        class_qid = row["_class_qid"]
        country_qid = qid(row["country"]["value"])
        country = COUNTRIES.get(country_qid, country_labels.get(country_qid, country_qid))
        event_date = row["date"]["value"][:10]
        key = (event_qid, event_date, country_qid, class_qid)
        if key in seen:
            continue
        seen.add(key)
        event_type, importance = EVENT_CLASSES[class_qid]
        exposure_rule, assets = COUNTRY_EXPOSURES.get(
            country, ("no_predeclared_asset_exposure", ()),
        )
        available = (pd.Timestamp(event_date) + pd.Timedelta(days=1)).date().isoformat()
        events.append({
            "event_id": f"wikidata_{event_qid}_{event_date.replace('-', '')}",
            "source": "Wikidata", "source_version": "live_snapshot",
            "source_url": f"https://www.wikidata.org/wiki/{event_qid}",
            "license": "CC0-1.0", "event_date": event_date,
            "available_date_conservative": available,
            "availability_rule": "event_date_plus_1_calendar_day",
            "event_type": event_type, "event_name_en": event_labels.get(event_qid, event_qid),
            "label_status": "resolved" if event_qid in event_labels else "pending",
            "country": country, "country_qid": country_qid, "wikidata_qid": event_qid,
            "exposure_rule": exposure_rule, "predeclared_assets": list(assets),
            "importance_score": importance, "retrospective_source": True,
            "generation_eligible": False,
            "verification_status": "requires_contemporaneous_source_verification",
        })
    events.sort(key=lambda event: (event["event_date"], event["event_id"]))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "normalized_events.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"raw_bindings={len(raw):,}")
    print(f"normalized_events={len(events):,}")
    print(f"output={out}")


if __name__ == "__main__":
    main()
