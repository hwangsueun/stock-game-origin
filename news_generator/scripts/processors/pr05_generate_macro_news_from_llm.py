# ============================================================
# pr05_generate_macro_news_from_llm.py
# pr04 JSONL → LLM 거시뉴스 생성 CSV
# ============================================================

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI


# ============================================================
# 1. Config
# ============================================================

@dataclass
class MacroNewsGenerateConfig:
    input_jsonl: Path
    output_csv: Path
    fail_log_path: Path

    model: str = "gpt-4o"
    temperature: float = 0.85      # 다양성 확보: 0.3이면 표현이 수렴·반복됨
    max_retries: int = 3
    sleep_sec: float = 1.0

    limit_days: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    max_news_per_day: int = 5
    detail_min_chars: int = 160      # 프롬프트 지시 기준 (경고 임계값)
    detail_hard_min_chars: int = 80  # 이보다 짧으면 실제로 버림 (사실상 빈 응답 방어용)
    detail_max_chars: int = 260

    env_path: Optional[Path] = None


# ============================================================
# 2. Schema
# ============================================================

class MacroNewsSchemaFactory:
    @staticmethod
    def build_schema() -> Dict[str, Any]:
        # strict: True + json_schema 조합이 string 필드 길이를 억제하는 부작용이 있음
        # json_object 모드로 완화하고 필드 검증은 postprocess에서 직접 처리
        return {}  # json_object 모드에서는 schema 불필요

# ============================================================
# 3. Prompt Builder  (instructions in English, output in Korean)
# ============================================================

class MacroNewsPromptBuilder:
    def __init__(self, config: MacroNewsGenerateConfig):
        self.config = config

    # --------------------------------------------------------
    # System Prompt  — English instructions, Korean output
    # --------------------------------------------------------

    def build_system_prompt(self) -> str:
        return """
You are a Korean financial newspaper reporter writing daily macro market brief articles.
All output text in the "headline" and "detail_news" fields must be written in Korean.
All other instructions, rules, and examples in this prompt are in English for precision.

## Role
- Write articles based solely on the quantitative signals and event data provided.
- Do NOT invent institution names, person names, policy announcements, or meeting outcomes.
- Write macro market briefs — NOT individual stock or sector analysis pieces.

## Korean writing style
- Register: plain-form past tense (반말체 과거형). End sentences with: ~했다, ~됐다, ~나타났다, ~확산됐다, ~밀렸다, ~올랐다, ~내렸다, ~좁혀졌다, ~벌어졌다.
- NEVER use polite endings: ~입니다, ~습니다, ~합니다, ~됩니다, ~보입니다, ~전망입니다, ~예상됩니다.
- One fact per sentence. Subject first. Keep sentences short and concrete.
- Use causal connectors: ~하며, ~속에, ~를 배경으로, ~에 따라, ~을 앞두고.

## Headline rules
- Length: 15–35 Korean characters (공백 포함).
- Must include at least one indicator name OR a specific numeric value.
- No question marks. End with a verb or noun phrase.
- No two headlines in the same day's batch may overlap in meaning.

  GOOD examples:
    코스피 1.4% 하락…외국인 사흘째 순매도
    원달러 1,340원 돌파, 수입물가 압력 재확대
    WTI 2% 급락에 유가 부담 완화 기대
    장단기 금리차 역전 심화, 경기 경보 신호 강화
    금 0.8% 오르며 안전자산 선호 재부상

  BAD examples (no numbers, too abstract):
    국내 증시 전반 투자심리 약화
    원화 약세로 대외 부담 확대
    물가 부담 완화 신호

## detail_news rules
- Length: 180–260 Korean characters INCLUDING spaces. This is a hard requirement.
- Write AT LEAST 3 sentences. Two sentences will almost always fall short of 180 chars.
- Structure: (1) core fact + numeric value, (2) cause or related indicator, (3) market reaction or downstream effect. Add a 4th sentence if needed to reach the minimum length.
- You MUST use at least one value from key_figures in the body text. An article with no numbers is incorrect.
- Do NOT pad with filler sentences. Expand on related indicator linkages within the given data to reach the required length.

  GOOD example (~210 chars):
    코스피가 1.4% 하락하며 사흘 만에 반락했다. 외국인이 현선물에서 동반 순매도에 나선 가운데 원달러 환율도 1,340원대로 올라서며 위험자산 회피 심리가 겹쳤다. 코스닥 역시 1.1% 내리며 중소형주까지 약세가 확산됐다. 장중 낙폭이 일시 확대되며 투자자들의 경계감이 높아졌다.

  BAD example (too short + wrong register):
    코스피가 약세를 보이며 국내 증시 전반에 대한 경계감이 커진 모습입니다. 투자심리가 위축되며 위험자산에 대한 회피 심리가 나타나고 있습니다.
    → Only 2 sentences, no numbers, polite endings — all wrong.

## Diversity rules
- Do NOT write all articles from the same angle (e.g., all about equity decline).
- Cover different angles across articles: equities, FX, rates, oil, sentiment, real activity.
- Do NOT start every article with "코스피가~". Vary the subject: "원달러 환율이~", "WTI 선물이~", "장단기 금리차가~", etc.
- Do NOT repeat the same Korean expression across articles. Use "위험자산 회피" at most once per day, "투자심리 위축" at most once per day.

## Global market coverage
- When global_market_context or events with angle="external_pressure" or "risk_sentiment" include
  sp500, nasdaq, us_10y_yield, us_term_spread_10y_2y data, write at least one article covering
  the US market or US rates angle.
- Connect global signals to Korean market impact naturally:
  "S&P500이 X% 하락하며 글로벌 위험회피 심리가 확산됐다. 이는 외국인 수급 압력으로 이어지며..."
  "미국 국채 10년물 금리가 X%로 상승하며 달러 강세 압력이 커졌다. 원달러 환율에..."
- Do NOT write US market articles if the global_market_context is missing or has no signal.

## Hard prohibitions
- No sector-specific reviews (반도체, 화학, 운송, 은행 등) unless explicitly named in the event data.
- No individual company or ticker names.
- No future predictions: ~할 것으로 보인다, ~될 전망이다, ~기대된다 (as a statement of fact).
- No fabricated events beyond the provided input data.

## Output format
- Return a single JSON object. Top-level key is "news" only.
- No markdown code blocks, no explanatory text — JSON only.
- asset_class: always "macro". news_style: always "macro_market_news".
""".strip()

    # --------------------------------------------------------
    # User Prompt
    # --------------------------------------------------------

    def build_user_prompt(self, daily_record: Dict[str, Any]) -> str:
        date = daily_record.get("date", "")
        news_count = min(
            self.config.max_news_per_day,
            int(daily_record.get("news_count_target", self.config.max_news_per_day)),
        )

        events_for_prompt = self._extract_events_for_prompt(
            daily_record.get("macro_events", [])
        )

        rules = daily_record.get("generation_rules", {})
        scope = rules.get("scope_constraints", {})
        forbidden_topics = scope.get("forbidden_topics", [])
        angle_allocation = {
            k: v for k, v in rules.get("angle_allocation", {}).items()
            if isinstance(v, int)
        }

        # angle 할당 텍스트 구성
        if angle_allocation:
            alloc_lines = [
                f"  - {angle}: {count} article(s)"
                for angle, count in angle_allocation.items()
            ]
            alloc_text = "\n".join(alloc_lines)
        else:
            alloc_text = "  - Cover all angles evenly"

        prompt_parts = [
            f"Date: {date}",
            f"Articles to generate: {news_count}",
            "",
            "## Today's market events (priority order)",
            json.dumps(events_for_prompt, ensure_ascii=False, indent=2),
            "",
            "## Market snapshot (use for additional numeric context if needed)",
            json.dumps(
                self._extract_snapshot_for_prompt(daily_record.get("daily_market_snapshot", {})),
                ensure_ascii=False,
                indent=2,
            ),
            "",
            "## Article angle allocation (STRICT — follow this distribution)",
            "Write exactly this many articles per angle. Do NOT write more market_breadth",
            "(equity index) articles than allocated — this is the most common mistake.",
            alloc_text,
            "If an angle has insufficient events, use the closest related angle instead.",
            "",
            "## Additional prohibited topics",
            "\n".join(f"- {t}" for t in forbidden_topics) if forbidden_topics else "None",
            "",
            "## Output requirements",
            f"- Return a JSON object with a 'news' array containing exactly {news_count} article objects.",
            "- Each article must have all of these fields:",
            "  date, news_id, headline, detail_news, asset_class, related_assets,",
            "  direction, source_event_ids, used_evidence, news_style",
            f"- date: fixed as {date}",
            f"- news_id format: {date}_01, {date}_02, ... (zero-padded index)",
            "- detail_news: Korean text, 180–260 chars INCLUDING spaces. MINIMUM 3 sentences.",
            "  Two sentences will almost always be too short — write at least 3.",
            "- headline: Korean text, 15–35 chars, must contain an indicator name or numeric value.",
            "- source_event_ids: array of event_id strings used for this article.",
            "- used_evidence: array of short strings showing the actual figures used.",
            '  Example: ["kospi_ret_1d: -1.4%", "usdkrw: 1340"]',
            "- direction: one of positive / negative / neutral / mixed",
        ]

        return "\n".join(prompt_parts)

    def _extract_events_for_prompt(
        self, macro_events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        result = []

        for e in macro_events:
            key_figures = {
                k: v for k, v in e.get("key_figures", {}).items()
                if v is not None
            }

            item: Dict[str, Any] = {
                "event_id": e.get("event_id", ""),
                "role": e.get("event_role", "core"),
                "angle": e.get("macro_angle", ""),
                "label": e.get("angle_label", ""),
                "implication": e.get("market_implication", ""),
                "direction": e.get("direction", ""),
                "severity": e.get("severity", "normal"),
            }

            if key_figures:
                item["key_figures"] = key_figures

            result.append(item)

        return result

    def _extract_snapshot_for_prompt(
        self, snapshot: Dict[str, Any]
    ) -> Dict[str, Any]:
        condensed: Dict[str, Any] = {}

        for context_name, context_data in snapshot.items():
            if not isinstance(context_data, dict):
                continue

            section: Dict[str, Any] = {}
            for col, col_data in context_data.items():
                if not isinstance(col_data, dict):
                    continue

                value = col_data.get("value")
                signal = col_data.get("signal")

                if value is None:
                    continue

                entry: Dict[str, Any] = {"value": value}
                if signal and signal not in ("normal",):
                    entry["signal"] = signal

                section[col] = entry

            if section:
                condensed[context_name] = section

        return condensed

# ============================================================
# 4. Generator
# ============================================================

class MacroNewsGenerator:
    def __init__(self, config: MacroNewsGenerateConfig):
        self.config = config
        self.client = self._build_client()
        self.prompt_builder = MacroNewsPromptBuilder(config)
        self.schema_factory = MacroNewsSchemaFactory()

    def run(self) -> None:
        records = self._load_jsonl()
        records = self._filter_records(records)

        self.config.output_csv.parent.mkdir(parents=True, exist_ok=True)
        self.config.fail_log_path.parent.mkdir(parents=True, exist_ok=True)

        all_news: List[Dict[str, Any]] = []
        fail_rows: List[Dict[str, Any]] = []

        print("=" * 100)
        print("[pr05 시작] LLM 거시뉴스 생성")
        print(f"input_jsonl : {self.config.input_jsonl}")
        print(f"output_csv  : {self.config.output_csv}")
        print(f"model       : {self.config.model}")
        print(f"temperature : {self.config.temperature}")
        print(f"days        : {len(records)}")
        print("=" * 100)

        for idx, record in enumerate(records, start=1):
            date = record.get("date", "UNKNOWN")
            print(f"[{idx}/{len(records)}] {date} 생성 중...")

            try:
                news_items = self._generate_one_day(record)
                news_items = self._postprocess_news_items(news_items, record)

                all_news.extend(news_items)
                print(f"  -> 성공: {len(news_items)}개")

            except Exception as e:
                print(f"  -> 실패: {date} / {e}")
                fail_rows.append({
                    "date": date,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                })

            time.sleep(self.config.sleep_sec)

        self._save_news_csv(all_news)
        self._save_fail_log(fail_rows)

        print("=" * 100)
        print("[pr05 완료]")
        print(f"생성 뉴스 수 : {len(all_news)}")
        print(f"실패 일수    : {len(fail_rows)}")
        print(f"output_csv   : {self.config.output_csv}")
        print(f"fail_log     : {self.config.fail_log_path}")
        print("=" * 100)

    # --------------------------------------------------------
    # Client
    # --------------------------------------------------------

    def _build_client(self) -> OpenAI:
        if self.config.env_path is not None:
            load_dotenv(self.config.env_path)
        else:
            load_dotenv()

        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY가 없습니다. .env에 OPENAI_API_KEY=... 형태로 넣으세요."
            )

        return OpenAI(api_key=api_key)

    # --------------------------------------------------------
    # Load / Filter
    # --------------------------------------------------------

    def _load_jsonl(self) -> List[Dict[str, Any]]:
        if not self.config.input_jsonl.exists():
            raise FileNotFoundError(f"입력 JSONL 없음: {self.config.input_jsonl}")

        records = []

        with open(self.config.input_jsonl, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"JSONL 파싱 실패 line={line_no}: {e}")

        return records

    def _filter_records(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        df = pd.DataFrame([{"idx": i, "date": r.get("date")} for i, r in enumerate(records)])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        if self.config.start_date is not None:
            start = pd.to_datetime(self.config.start_date)
            df = df[df["date"] >= start]

        if self.config.end_date is not None:
            end = pd.to_datetime(self.config.end_date)
            df = df[df["date"] <= end]

        df = df.sort_values("date")

        if self.config.limit_days is not None:
            df = df.head(self.config.limit_days)

        return [records[i] for i in df["idx"].tolist()]

    # --------------------------------------------------------
    # Generate
    # --------------------------------------------------------

    def _generate_one_day(self, daily_record: Dict[str, Any]) -> List[Dict[str, Any]]:
        system_prompt = self.prompt_builder.build_system_prompt()
        user_prompt = self.prompt_builder.build_user_prompt(daily_record)

        last_error = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    temperature=self.config.temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )

                content = response.choices[0].message.content

                if content is None:
                    raise ValueError("LLM 응답 content가 None입니다.")

                parsed = json.loads(content)

                if "news" not in parsed:
                    raise ValueError("응답에 news 키가 없습니다.")

                if not isinstance(parsed["news"], list):
                    raise ValueError("news가 list가 아닙니다.")

                return parsed["news"]

            except Exception as e:
                last_error = e
                print(f"    재시도 {attempt}/{self.config.max_retries}: {e}")
                time.sleep(self.config.sleep_sec * attempt)

        raise RuntimeError(f"LLM 생성 최종 실패: {last_error}")

    # --------------------------------------------------------
    # Postprocess
    # --------------------------------------------------------

    def _postprocess_news_items(
        self,
        news_items: List[Dict[str, Any]],
        daily_record: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        date = daily_record.get("date", "")
        macro_events = daily_record.get("macro_events", [])
        valid_event_ids = {e.get("event_id") for e in macro_events}

        cleaned = []

        for i, item in enumerate(news_items, start=1):
            headline = str(item.get("headline", "")).strip()
            detail_news = str(item.get("detail_news", "")).strip()

            # 헤드라인·본문 비어있으면 버림
            if not headline or not detail_news:
                continue

            # 본문 길이 검증
            detail_char_count = len(detail_news)

            # hard_min 미만은 사실상 빈 응답 — 버림
            if detail_char_count < self.config.detail_hard_min_chars:
                print(
                    f"    [탈락] {date}_{i:02d} 본문 너무 짧음 ({detail_char_count}자 < "
                    f"{self.config.detail_hard_min_chars}자)"
                )
                continue

            # detail_min_chars 미만은 경고만 — 버리지 않음
            # (LLM이 프롬프트 지시를 못 지킨 경우, 어조가 나쁘더라도 데이터는 보존)
            if detail_char_count < self.config.detail_min_chars:
                print(
                    f"    [경고] {date}_{i:02d} 본문 짧음 ({detail_char_count}자 < "
                    f"{self.config.detail_min_chars}자), 경고만"
                )

            # 금지 어미 감지 (소프트 경고만, 버리지 않음)
            forbidden_endings = [
                "모습입니다", "흐름입니다", "전망입니다", "예상됩니다",
                "주목됩니다", "보입니다", "있습니다", "됩니다",
            ]
            found_endings = [e for e in forbidden_endings if e in detail_news]
            if found_endings:
                print(
                    f"    [경고] {date}_{i:02d} 금지 어미 감지: {found_endings}"
                )

            # source_event_ids 정제
            source_event_ids = item.get("source_event_ids", [])
            if not isinstance(source_event_ids, list):
                source_event_ids = []
            source_event_ids = [
                str(eid) for eid in source_event_ids if eid in valid_event_ids
            ]

            # related_assets 정제
            related_assets = item.get("related_assets", [])
            if not isinstance(related_assets, list):
                related_assets = []

            # used_evidence 정제
            used_evidence = item.get("used_evidence", [])
            if not isinstance(used_evidence, list):
                used_evidence = []

            # news_id 보정
            news_id = item.get("news_id")
            if not news_id:
                news_id = f"{date}_{i:02d}"

            cleaned.append({
                "date": date,
                "news_id": str(news_id),
                "headline": headline,
                "detail_news": detail_news,
                "asset_class": "macro",
                "related_assets": json.dumps(related_assets, ensure_ascii=False),
                "direction": self._normalize_direction(item.get("direction")),
                "source_event_ids": json.dumps(source_event_ids, ensure_ascii=False),
                "used_evidence": json.dumps(used_evidence, ensure_ascii=False),
                "news_style": "macro_market_news",
            })

        return cleaned[: self.config.max_news_per_day]

    def _normalize_direction(self, direction: Any) -> str:
        value = str(direction).strip().lower()
        allowed = {"positive", "negative", "neutral", "mixed"}
        return value if value in allowed else "mixed"

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    def _save_news_csv(self, rows: List[Dict[str, Any]]) -> None:
        columns = [
            "date", "news_id", "headline", "detail_news",
            "asset_class", "related_assets", "direction",
            "source_event_ids", "used_evidence", "news_style",
        ]

        with open(self.config.output_csv, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in columns})

    def _save_fail_log(self, rows: List[Dict[str, Any]]) -> None:
        columns = ["date", "error", "traceback"]

        with open(self.config.fail_log_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in columns})


# ============================================================
# 5. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input-jsonl",
        type=str,
        default="data/raw/news_generation_input_2018_test.jsonl",
    )

    parser.add_argument(
        "--output-csv",
        type=str,
        default="data/processed/llm_generated_macro_news_test.csv",
    )

    parser.add_argument(
        "--fail-log-path",
        type=str,
        default="data/processed/llm_generated_macro_news_fail_log.csv",
    )

    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
    )

    parser.add_argument(
        "--temperature",
        type=float,
        default=0.85,
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
    )

    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=1.0,
    )

    parser.add_argument(
        "--limit-days",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
    )

    parser.add_argument(
        "--max-news-per-day",
        type=int,
        default=5,
    )

    parser.add_argument(
        "--env-path",
        type=str,
        default=None,
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = MacroNewsGenerateConfig(
        input_jsonl=Path(args.input_jsonl),
        output_csv=Path(args.output_csv),
        fail_log_path=Path(args.fail_log_path),
        model=args.model,
        temperature=args.temperature,
        max_retries=args.max_retries,
        sleep_sec=args.sleep_sec,
        limit_days=args.limit_days,
        start_date=args.start_date,
        end_date=args.end_date,
        max_news_per_day=args.max_news_per_day,
        env_path=Path(args.env_path) if args.env_path else None,
    )

    generator = MacroNewsGenerator(config)
    generator.run()


if __name__ == "__main__":
    main()