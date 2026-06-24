#!/usr/bin/env python3
"""Rebuild Korean domestic event candidates from the raw GDELT GKG cache.

Handoff item 1 + item 3 (and the verification signal for item 4).

This does NOT reuse the old coverage-volume summary. It reads the raw per-article
GKG parquet rows and clusters them into candidate events keyed by
``(ref_date, salient organization)``. The number of *independent source domains*
covering a cluster is the contemporaneous corroboration signal (item 4); price
reaction is never consulted (item 5 stays manual / downstream).

Only metadata is preserved per the no-body rule (item 3): URL, source domain,
publication day, GKG theme codes, tone, and the structured cluster key. Article
text is never stored.

Date basis follows the date-consistency audit: GKG ``ref_date`` (== the
publication day, look-ahead-safe). ``available_date_conservative`` = ref_date + 1
calendar day, matching the UCDP / Wikidata convention.

Output schema is kept compatible with the existing normalized event ledgers so
the events can flow into ``build_realworld_event_news_candidates.py`` later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq


# --- Entities that must never be a cluster key ----------------------------
# Countries / regions / supranationals: too generic to be "an event".
_GEO_STOP = {
    "united states", "china", "japan", "north korea", "south korea", "korea",
    "europe", "european union", "united nations", "asia", "russia", "india",
    "united kingdom", "germany", "france", "vietnam", "taiwan", "hong kong",
    "middle east", "africa", "america", "washington", "beijing", "tokyo",
}
# Wire services / outlets that appear as orgs/persons via bylines.
_WIRE_STOP = {
    "reuters", "yonhap", "yonhap news", "associated press", "bloomberg",
    "joongang ilbo", "chosun ilbo", "dong-a ilbo", "hankyoreh", "afp",
    "korea herald", "korea times", "maeil business", "newsis",
}
# Foreign mega-entities: out of scope for a *domestic* ledger.
_FOREIGN_STOP = {
    "facebook", "google", "apple", "amazon", "twitter", "youtube", "microsoft",
    "nasdaq", "dow jones", "wall street", "boeing", "toyota", "sony", "tesla",
    "white house", "federal reserve", "imf", "world bank", "opec",
}
_STOP = _GEO_STOP | _WIRE_STOP | _FOREIGN_STOP

# Garbled / vacuous one-token entities seen in GKG extraction.
_JUNK_TOKENS = {"young", "old", "new", "market committee", "group", "company",
                "ministry", "court", "office", "center", "association"}

# High-confidence Korean institution / chaebol signals (English GKG forms).
_KOREAN_HINTS = (
    "samsung", "hyundai", "kia", " lg", "lg ", "sk ", " sk", "lotte", "posco",
    "hanwha", "doosan", "kakao", "naver", "celltrion", "korean air", "asiana",
    "national assembly", "fair trade commission", "financial supervisory",
    "bank of korea", "kospi", "kosdaq", "korea exchange", "blue house",
    "ministry of", "supreme court", "constitutional court", "prosecutor",
    "seoul", "incheon", "busan", "gyeonggi", "saenuri", "democratic party",
)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:40] or "x"


def _norm_org(org: str) -> str:
    # GDELT gives title-case English orgs; collapse internal whitespace.
    return re.sub(r"\s+", " ", org.strip())


def _parse_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    s = str(value).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return [str(x) for x in json.loads(s)]
        except Exception:
            return []
    out = []
    for tok in s.split(";"):
        tok = tok.strip()
        if tok:
            out.append(tok.split(",")[0].strip())
    return out


def _is_korean(org_lower: str) -> bool:
    return any(h in org_lower for h in _KOREAN_HINTS)


def _column(names, base: str) -> str | None:
    for suffix in ("_json", "_raw"):
        if base + suffix in names:
            return base + suffix
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parquet-dir", type=Path, default=Path("scripts/output"))
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("data/interim/realworld_event_news/gdelt_gkg"))
    parser.add_argument("--start-date", default="2013-01-01")
    parser.add_argument("--end-date", default="2023-12-31")
    parser.add_argument("--min-domains", type=int, default=3,
                        help="keep clusters with at least this many distinct domains")
    parser.add_argument("--corroborated-domains", type=int, default=4,
                        help="domains needed to mark verification_status corroborated")
    parser.add_argument("--evidence-cap", type=int, default=12)
    parser.add_argument("--theme-topk", type=int, default=8)
    parser.add_argument("--theme-articles-cap", type=int, default=40,
                        help="articles per cluster scanned for theme signature")
    args = parser.parse_args()

    start = args.start_date.replace("-", "")
    end = args.end_date.replace("-", "")

    # accumulator keyed by (ref_date, org)
    domains: dict[tuple, set] = defaultdict(set)
    n_articles: dict[tuple, int] = defaultdict(int)
    tone_sum: dict[tuple, float] = defaultdict(float)
    tone_n: dict[tuple, int] = defaultdict(int)
    themes: dict[tuple, Counter] = defaultdict(Counter)
    evidence: dict[tuple, list] = defaultdict(list)

    files = sorted(args.parquet_dir.glob("gkg_*.parquet"))
    for f in files:
        names = pq.ParquetFile(f).schema_arrow.names
        ocol = _column(names, "orgs")
        tcol = _column(names, "themes")
        cols = ["ref_date", "published_at", "domain", "url", "tone_score"]
        if ocol:
            cols.append(ocol)
        if tcol:
            cols.append(tcol)
        df = pq.read_table(f, columns=cols).to_pandas()
        rds = df["ref_date"].astype(str)
        for i in range(len(df)):
            rd = rds.iat[i]
            if rd < start or rd > end:
                continue
            orgs = _parse_list(df[ocol].iat[i]) if ocol else []
            if not orgs:
                continue
            dom = df["domain"].iat[i]
            dom = "" if dom is None else str(dom).strip().lower()
            url = df["url"].iat[i]
            url = "" if url is None else str(url)
            pub = df["published_at"].iat[i]
            pub = "" if pub is None else str(pub)[:10]
            tone = df["tone_score"].iat[i]
            theme_codes = _parse_list(df[tcol].iat[i]) if tcol else []
            seen = set()
            for raw in orgs:
                org = _norm_org(raw)
                ol = org.lower()
                if (not org or len(org) < 2 or ol in _STOP or ol in _JUNK_TOKENS):
                    continue
                if org in seen:
                    continue
                seen.add(org)
                key = (rd, org)
                if dom:
                    domains[key].add(dom)
                n_articles[key] += 1
                if tone is not None:
                    try:
                        tone_sum[key] += float(tone)
                        tone_n[key] += 1
                    except (TypeError, ValueError):
                        pass
                if n_articles[key] <= args.theme_articles_cap:
                    themes[key].update(theme_codes)
                if len(evidence[key]) < args.evidence_cap and url:
                    evidence[key].append({"url": url, "domain": dom, "published_at": pub})

    # build candidates
    candidates = []
    for key, doms in domains.items():
        nd = len(doms)
        if nd < args.min_domains:
            continue
        rd, org = key
        ref_iso = f"{rd[:4]}-{rd[4:6]}-{rd[6:8]}"
        avail = (datetime.strptime(ref_iso, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        ol = org.lower()
        domestic_conf = "high" if _is_korean(ol) else "press_context"
        corroborated = nd >= args.corroborated_domains
        event_id = "gdelt_gkg_" + rd + "_" + _slug(org) + "_" + \
            hashlib.sha1(f"{rd}|{org}".encode()).hexdigest()[:8]
        candidates.append({
            "event_id": event_id,
            "source": "GDELT_GKG",
            "source_version": "gkg_korean_cache_v1",
            "license": "GDELT-open (metadata only; article bodies not stored)",
            "event_date": ref_iso,
            "ref_date": ref_iso,
            "available_date_conservative": avail,
            "availability_rule": "ref_date_plus_1_calendar_day",
            "date_basis": "gkg_ref_date_capture_day",
            "event_type": "media_coverage_spike",
            "salient_entity": org,
            "entity_kind": "organization",
            "country": "South Korea",
            "is_domestic": True,
            "domestic_confidence": domestic_conf,
            "theme_signature": [t for t, _ in themes[key].most_common(args.theme_topk)],
            "n_articles": n_articles[key],
            "n_distinct_domains": nd,
            "source_domains": sorted(doms),
            "avg_tone": round(tone_sum[key] / tone_n[key], 3) if tone_n[key] else None,
            "evidence": evidence[key],
            "exposure_rule": "no_predeclared_asset_exposure",
            "predeclared_assets": [],
            "retrospective_source": False,
            "contemporaneous_source": True,
            "verification_status": ("multi_domain_corroborated" if corroborated
                                    else "multi_domain_unverified"),
            "generation_eligible": False,
        })

    candidates.sort(key=lambda c: (c["ref_date"], -c["n_distinct_domains"], c["salient_entity"]))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "normalized_events.jsonl").open("w", encoding="utf-8") as h:
        for c in candidates:
            h.write(json.dumps(c, ensure_ascii=False) + "\n")

    # report
    corr = sum(1 for c in candidates if c["verification_status"] == "multi_domain_corroborated")
    high = sum(1 for c in candidates if c["domestic_confidence"] == "high")
    dates = {c["ref_date"] for c in candidates}
    years = Counter(c["ref_date"][:4] for c in candidates)
    top = sorted(candidates, key=lambda c: -c["n_distinct_domains"])[:25]
    report = [
        "# GDELT GKG Korean domestic event candidates", "",
        "Rebuilt from raw per-article GKG rows (no reuse of the old coverage "
        "summary). Clusters keyed by `(ref_date, salient organization)`; the "
        "distinct-domain count is the contemporaneous corroboration signal.", "",
        f"- gkg files read: {len(files)}",
        f"- candidates (>= {args.min_domains} distinct domains): {len(candidates):,}",
        f"- corroborated (>= {args.corroborated_domains} domains): {corr:,}",
        f"- high domestic-confidence entity: {high:,}",
        f"- distinct dates covered: {len(dates):,}",
        "- all candidates: generation_eligible=false, contemporaneous_source=true",
        "- price reaction was NOT consulted in selection", "",
        "## Candidates per year", "",
    ]
    report.extend(f"- {y}: {years[y]:,}" for y in sorted(years))
    report += ["", "## Top 25 clusters by independent-domain coverage", ""]
    report.extend(
        f"- {c['ref_date']}  {c['salient_entity']}  "
        f"(domains={c['n_distinct_domains']}, articles={c['n_articles']}, "
        f"{c['domestic_confidence']})"
        for c in top
    )
    (args.output_dir / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    print(f"gkg_files={len(files)} candidates={len(candidates):,} "
          f"corroborated={corr:,} high_domestic={high:,} dates={len(dates):,}")
    print(f"-> {args.output_dir / 'normalized_events.jsonl'}")


if __name__ == "__main__":
    main()
