# ============================================================
# pr05_generate_macro_news_from_llm.py
# pr04 JSONL → LLM 거시뉴스 생성 CSV
# ============================================================

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI


_DEFINITION_TOKENS = (
    "지표", "단위", "가늠하", "살피", "살필", "기준점", "성격의 자산",
    "비교하는 값", "요약해", "만기별 자금", "만기별 가격", "투자 분위기를 가늠",
)

_NUMBER = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
_UNSUPPORTED_FILLER_TOKENS = (
    "수 있다", "해석된다", "해석될", "것으로 보인다", "예상된다", "기대된다",
)


def _numbers(value: object) -> List[float]:
    found = []
    for token in _NUMBER.findall(json.dumps(value, ensure_ascii=False)):
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            pass
    return found


def _matches_source_number(value: float, allowed: List[float]) -> bool:
    tolerance = 0.51 if abs(value) >= 100 else 0.011
    return any(
        min(abs(value - source), abs(abs(value) - abs(source)))
        <= max(tolerance, abs(source) * 0.0001)
        for source in allowed
    )


def _matches_approximate_percent_band(
    value: float, text: str, allowed: List[float],
) -> bool:
    token = f"{value:g}%대"
    return token in text and any(value <= abs(source) < value + 1 for source in allowed)


def _strip_trailing_definition(text: str) -> str:
    """기사 끝에 붙은 교과서식 정의문(지표·단위·가늠하다 등 정의 어휘를 담은 꼬리 문장)만 제거한다.
    '이는 하루 전보다 N원 내린 수치다'처럼 정의 어휘가 없는 사실 문장은 보존한다."""
    text = text.strip()
    for _ in range(2):
        parts = re.split(r"(?<=[.。])\s+", text)
        if len(parts) < 2:
            break
        last = parts[-1]
        if any(token in last for token in _DEFINITION_TOKENS):
            text = " ".join(parts[:-1]).strip()
        else:
            break
    return text or text


def _strip_unsupported_filler_sentences(text: str) -> str:
    """가능성형·상투적 해석형 문장을 제거하고 단정적인 기사 문장을 보존한다."""
    parts = re.split(r"(?<=[.。!?])\s+", text.strip())
    kept = [
        part for part in parts
        if not any(token in part for token in _UNSUPPORTED_FILLER_TOKENS)
    ]
    return " ".join(kept).strip()


def _attribution_satisfied(detail: str, evidence: Dict[str, Any]) -> bool:
    """공식 기관 표기를 짧은형·정식명·괄호 약어 어느 것으로 써도 인정한다."""
    institution = str(evidence.get("institution") or "")
    candidates = [
        str(evidence.get("required_attribution") or ""),
        institution,
    ]
    acronym = re.search(r"\(([^)]+)\)", institution)
    if acronym:
        candidates.append(acronym.group(1))  # 예: BEA
    return any(c and c in detail for c in candidates)


# ============================================================
# 1. Config
# ============================================================

@dataclass
class MacroNewsGenerateConfig:
    input_jsonl: Path
    output_csv: Path
    fail_log_path: Path

    model: str = "gpt-4o"
    temperature: float = 0.2
    max_retries: int = 3
    sleep_sec: float = 1.0

    limit_days: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    max_news_per_day: int = 10
    detail_min_chars: int = 45
    detail_hard_min_chars: int = 30
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
All output text in the "headline", "detail_news", and "beginner_explanation" fields must be written in Korean.
All other instructions, rules, and examples in this prompt are in English for precision.

## Role
- Write articles based solely on the quantitative signals and event data provided.
- Do NOT invent institution names, person names, policy announcements, or meeting outcomes.
- For official_release_calendar events, name the institution using required_attribution OR its full official name (the institution field), and use one supplied allowed_release_verbs value.
- Write macro market briefs, sector-breadth briefs, and only the exceptional stock event explicitly supplied as a headline event.

## Korean writing style
- Write like a real Korean financial newspaper market article, NOT a data log or a list of stats.
- Register: newspaper plain-form past tense (신문 기사체 과거형). End sentences with: ~했다, ~됐다, ~나타났다, ~기록했다, ~집계됐다, ~밀렸다, ~올랐다, ~내렸다, ~좁혀졌다, ~벌어졌다.
- NEVER use polite endings: ~입니다, ~습니다, ~합니다, ~됩니다, ~보입니다, ~전망입니다, ~예상됩니다.
- Open with a news lead: the first sentence carries the date or subject and the single most important figure. Following sentences add supporting facts from the same event.
- Connect sentences so the article reads as one coherent piece (이날, 반면, 한편, ~에 그쳤다, 직전 거래일보다 등), while every clause stays strictly factual.
- Use ONLY the selected event's own figures and observations. You may review the event's supplied direction in firm Korean newsroom prose, but do not add causes, investor sentiment, forecasts, or downstream effects unless they appear in the selected event evidence.

## Korean particle (조사) agreement — match the final sound of the word the particle attaches to
- Pick the particle by whether the preceding syllable ends in a final consonant (받침): 받침 있음 → 이·은·을·과·으로 ; 받침 없음 → 가·는·를·와·로.
- NEVER write ambiguous placeholder forms such as "을(를)", "이(가)", "은(는)", "와(과)". Choose exactly one correct form. If the evidence text contains such a placeholder, resolve it.
- For names ending in a Latin letter or digit, judge by the Korean reading of the LAST character:
  - Final consonant sound → 이·은·을·과: F(에프)·L(엘)·M(엠)·N(엔)·R(알)·Z(제트), and digits 1(일)·3(삼)·6(육)·8(팔). 예: "HMM은", "DL이", "현대로템이".
  - No final consonant → 가·는·를·와: the other letters and digits. 예: "삼성SDI가", "삼성SDI는", "카카오가", "LG는", "SK가", "KT가", "엔씨소프트가".
- Examples: "삼성SDI가 기술자료를 유용했다"(O) / "삼성SDI이"(X); "골프존이 차별행위로 제재를 받았다"(O); "소득세법이 가결됐다"(O).

## Company / entity names — use the common market name, drop formal legal-form markers
- Remove formal corporate-form markers attached to a company name: (주), ㈜, 주식회사, (유), 유한회사, (재), (사), Co., Ltd., Inc., Corp. Write the name the way it is actually called in the market.
- 예: "(주)카카오" → "카카오", "(주)엔씨소프트" → "엔씨소프트", "(주)골프존" → "골프존", "한국지엠(주)" → "한국지엠", "삼성SDI" → "삼성SDI".
- For multiple firms, drop each marker and join naturally: "(주)카카오 및 (주)엔씨소프트" → "카카오와 엔씨소프트".
- Do NOT invent, translate, abbreviate, or swap in a different name than the evidence (do not turn "한국지엠" into "GM코리아" or "쉐보레"). Only strip the legal-form marker.
- When the event evidence supplies matched_stocks (market ticker names), use those exact names for the affected listed companies.

## Headline rules
- Length: 15–40 Korean characters (공백 포함).
- Write like a newspaper headline: include at least one indicator name AND a specific numeric value when one is available.
- No question marks. End with a verb or concise noun phrase. A mid-headline connective "…" is allowed (e.g., "코스닥 건설 15.7% 급락…시장 평균 크게 밑돌아").
- No two headlines in the same day's batch may overlap in meaning.

  GOOD examples:
    코스피 1.4% 하락, 2,400선 마감
    원달러 환율 1,340원으로 상승
    WTI 2% 하락, 배럴당 75달러
    장단기 금리차 50bp로 확대
    금 가격 0.8% 상승

  BAD examples (no numbers, too abstract):
    국내 증시 전반 투자심리 약화
    원화 약세로 대외 부담 확대
    물가 부담 완화 신호

## detail_news rules  (THIS FIELD IS THE NEWS ARTICLE BODY)
- Length: 50–220 Korean characters INCLUDING spaces.
- Write a complete short news article of 2 to 4 sentences: a lead sentence with the key figure, then supporting facts taken from the same event.
- Weave in as many of the event's supplied figures as read naturally (reference period, prior value, change amount, index level, daily change, etc.). Richer factual detail is good.
- State the selected event's facts and figures, then optionally summarize its supplied direction as a present or completed market move.
- Natural direction reviews are allowed: "약세 흐름을 보였다", "상승세가 두드러졌다", "하락 압력이 커졌다", "변동성이 확대됐다". Use them only when consistent with the event's direction and figures.
- Never turn a direction review into an invented cause or future effect. Do not use generic possibility or interpretation filler such as "~할 수 있다", "~영향을 미칠 수 있다", "~로 해석된다", or "~로 해석될 수 있다". If no grounded review is available, stop after the factual sentence.
- Do NOT put beginner definitions, "~란 ... 뜻이다", "~를 나타내는 단위다" or any textbook explanation in this field. That belongs ONLY in the separate beginner_explanation field.
- NEVER close an article with a definition/role sentence such as "이는 ~ 지표다", "~를 가늠하는 (중요한) 지표다", "~를 살피는 데 사용된다". If one market signal is too thin for even a short factual article, MERGE it with other market signals instead of padding with such a sentence. A clean one-sentence fact is acceptable; a padded definition is not.
- When saying an industry "앞섰다" or "뒤처졌다", write the relative gap as a positive magnitude, not a signed negative value.
- You MUST use at least one value from key_figures in the body text. An article with no numbers is incorrect.
- Use ONLY numbers that appear in the supplied evidence/key_figures. Do NOT bring in a prior/previous value, do NOT compute a difference, and do NOT add any outside number — even if you believe it is correct. If a prior value is not supplied, do not state one.
- Numeric historical names such as "COVID-19" are also outside numbers. Do not add any named historical event or background unless it appears in the selected evidence.
- Do NOT pad with filler or repeat the same figure twice.

## Policy / legal / political official releases (release_category = legislation, court_ruling, election, regulatory_action)
- These events are qualitative (no market figure). The "must use a number" rule is satisfied by the date, ordinal (제N대), or case number (예: 2016헌나1) already in the evidence — do NOT invent a market figure, vote count, or fine amount.
- Headline: name the actor and the action concisely (행위자+행위). GOOD: "헌재, 박근혜 대통령 파면 결정", "공정위, 골프존에 과징금·시정명령", "국회, 소득세법 개정안 가결". Keep 15–40 chars.
- Body lead: institution + dated action using one supplied allowed_release_verbs value, then the most concrete supplied facts (소관 위원회/제안자, 사건번호, 조치 유형, 대상 기업). When the evidence description lists 조치 유형(과징금·시정명령·고발 등), state them; do NOT add amounts or outcomes beyond what the description provides.
- Do NOT editorialize political meaning, winners, market reaction, or consequences that are not in the evidence.

## beginner_explanation rules  (SEPARATE field, shown in small text UNDER the article — it must NEVER appear inside detail_news)
- Write one additional Korean sentence of 35–100 characters for a player unfamiliar with economics.
- Paraphrase only the supplied beginner_context. Do not copy plain_meaning verbatim.
- variation_cue is mandatory. When two events share a concept, their explanations MUST use different sentence structures.
- Explain what the indicator means, not what will happen next. Add no new number, cause, prediction, or investment advice.
- Include both what the indicator means and why a player would look at it; do not stop at a bare definition.
- Include every token in beginner_context.required_terms exactly as written.

  GOOD example:
    bp는 금리 차이를 읽는 단위이며, 장단기 금리차는 만기가 다른 자금의 가격 차이를 보여준다.
    %p는 두 수익률의 차이를 나타내는 단위이며, 업종이 전체 시장보다 얼마나 강하거나 약했는지 비교할 때 쓴다.

  BAD example (prediction and advice):
    금리차가 커졌으므로 앞으로 주가가 내릴 가능성이 높아 매도해야 한다.

## Diversity rules
- Do NOT write all articles from the same angle (e.g., all about equity decline).
- Cover different angles across articles: equities, sectors, FX, rates, oil, and real activity.
- Do NOT start every article with "코스피가~". Vary the subject: "원달러 환율이~", "WTI 선물이~", "장단기 금리차가~", etc.
- Do NOT repeat the same Korean expression or sentence frame across articles.

## Source-event coverage
- You are an editor choosing the day's stories, not a logger of every signal.
- MUST-COVER events — every official release, preview, review, projection, and headline/major-stock event MUST be
  covered by some article. Never drop these.
- The remaining market signals (지수·업종 확산도·섹터·환율·유가·금·미국 증시/금리·신용스프레드 등) are raw
  material. Use them to build the day's articles, merging related ones; you do NOT have to cite every
  minor signal, just as a real desk does not write a separate story for each tick.
- Write each official release / preview / review / projection / major-stock event as its OWN standalone article;
  do not merge those into a market-snapshot article. This keeps the institution name and figure exact.
- You MAY merge closely related market signals into ONE richer article when they tell one story
  (예: 같은 시장의 지수·업종 확산도, 또는 환율·유가·미국 증시·미국 금리 같은 대외 신호 묶음).
- Each article lists EVERY event it draws from in source_event_ids (one or more), and uses only
  those events' evidence and key_figures. Never mix in figures from an event you did not cite.
- A thin single-number event (공식 발표, 단일 지수 등) may stand alone as a short 1–2 sentence article.
  Do NOT pad it with a textbook definition to make it longer.
- For an event_role="preview" event, write a forward-looking schedule notice
  ("오는 OO일 ~ 발표가 예정돼 있다"). State only the scheduled date, institution, and indicator.
  A preview has NO result figures — never invent a result number for it.
- For an event_role="review" event, recap the already-released figure and how it compares to the prior reading.
- For an event_role="projection" event, report the institution's PUBLISHED forecast as fact using the supplied
  projection figures ("연준은 올해 말 기준금리 중앙값을 2.1%로 제시했다. 실질 GDP는 2018년 2.7%, 2019년 2.4%로 전망했다").
  Use 전망했다/예상했다/내다봤다/제시했다. Use only the supplied projection numbers; never add your own forecast.

## Article count and mix (produce EXACTLY 5 articles)
- Output EXACTLY 5 articles for the day — no more, no fewer.
- The 5 must be DIFFERENT KINDS of articles. Never write 5 near-identical market-snapshot blurbs.
- Each must-cover event (official release / preview / review / projection / major-stock) takes one of the 5 slots as its own standalone article.
- Fill the remaining slots by picking the most newsworthy angle from DIFFERENT buckets below — at most one article per bucket — and merging that bucket's minor signals into its single article:
  · 국내 증시 종합: 코스피·코스닥 지수와 등락 업종 분포를 한 기사로.
  · 업종 하이라이트: 그날 가장 두드러진 강세 또는 약세 업종.
  · 환율·원자재: 원달러 환율, WTI, 금 중 움직임이 큰 것.
  · 글로벌 시장: 미국 증시·미국 금리·신용스프레드.
  · 국내 금리·채권: 장단기 금리차 등.
- Choose the 5 angles that best capture THIS day; do not repeat a bucket and do not pad with a sixth.
- Vary the framing across the 5 (straight report / comparison vs prior / recap), not one repeated sentence shape.

## Global market coverage
- When global_market_context or events with angle="external_pressure" or "risk_sentiment" include
  sp500, nasdaq, us_10y_yield, us_term_spread_10y_2y data, write at least one article covering
  the US market or US rates angle.
- Do not connect global signals to Korean flows, FX, or sentiment unless the selected event contains that evidence.
- Do NOT write US market articles if the global_market_context is missing or has no signal.

## Hard prohibitions
- No sector-specific reviews (반도체, 화학, 운송, 은행 등) unless explicitly named in the event data.
- Do not INVENT individual company, ticker, coin, or bond names. You MAY name a specific stock, coin, or bond — regardless of asset class — only when that name appears in the supplied event evidence. For a genuinely large move you may feature that named security prominently.
- For a named security, restate only the figures supplied for it. Never claim it caused the overall market or sector move.
- Never invent foreign selling, investor concern, dollar strength, recovery expectations, technical-stock weakness, or causality.
- Do not use these speculative words as if they were fact unless they occur in the selected evidence: 투자자, 외국인, 기대, 우려, 심리, 가능성, 견인. (Plain factual connectives such as 영향, 반영, 배경 are allowed when they describe a figure that is actually in the evidence, but never use them to assert an unsupported cause.)
- Do NOT invent a forecast (오를 전망이다, ~할 것으로 예상된다 as your own guess). EXCEPTION: for an event_role="projection" article you MUST report the institution's PUBLISHED forecast as fact using 전망했다/예상했다/내다봤다 — those figures are supplied in the event.
- A value ending in %p is a market-relative percentage-point gap, not the sector's own return. Write "시장 대비 N%p 앞섰다/뒤처졌다".
- No future predictions about outcomes: ~할 것으로 보인다, ~될 전망이다, ~기대된다 (as a statement of fact). A scheduled event ("~ 발표가 예정돼 있다") is allowed only for event_role="preview".
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
        event_count = len(events_for_prompt)
        prompt_parts = [
            f"Date: {date}",
            f"Supplied events: {event_count}. Cover every event at least once; merge closely related events into fewer, richer articles.",
            "",
            "## Today's market events (priority order)",
            json.dumps(events_for_prompt, ensure_ascii=False, indent=2),
            "",
            "## Additional prohibited topics",
            "\n".join(f"- {t}" for t in forbidden_topics) if forbidden_topics else "None",
            "",
            "## Output requirements",
            "- Return a JSON object with a 'news' array of EXACTLY 5 article objects (no more, no fewer).",
            "- Make the 5 articles different kinds (see the Article mix section). Each official release / preview / review / projection / major-stock event is its own article; merge the remaining market signals into the other slots, one bucket per article.",
            "- Every must-cover event (official release, preview, review, projection, major-stock) must appear in some article's source_event_ids.",
            "- Each article must have all of these fields:",
            "  date, news_id, headline, detail_news, beginner_explanation, asset_class, related_assets,",
            "  direction, source_event_ids, used_evidence, news_style",
            f"- date: fixed as {date}",
            f"- news_id format: {date}_01, {date}_02, ... (zero-padded index)",
            "- detail_news: Korean NEWS ARTICLE body, 50–220 chars, 2 to 4 factual sentences with a news lead. No beginner definitions here.",
            "- beginner_explanation: Korean text, 35–100 chars, one sentence paraphrased only from beginner_context. Separate field, never inside detail_news.",
            "- beginner_explanation MUST contain every token in that event's beginner_context.required_terms exactly as written. When an article merges several events, base the explanation on the most important event.",
            "- headline: Korean text, 15–40 chars, must contain an indicator name or numeric value.",
            "- source_event_ids: a list of one or more event_ids this article draws from. Every supplied event must appear in some article's source_event_ids at least once.",
            "- used_evidence: array of short strings showing the actual figures used.",
            '  Example: ["kospi_ret_1d: -1.4%", "usdkrw: 1340"]',
            "- direction: one of positive / negative / neutral / mixed",
        ]

        return "\n".join(prompt_parts)

    def _extract_events_for_prompt(
        self, macro_events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        result = []

        variation_cues = (
            "개념의 정의부터 설명한다.",
            "이 지표를 어디에 쓰는지부터 설명한다.",
            "비교 대상과 읽는 방법부터 설명한다.",
            "초보자가 헷갈리기 쉬운 점부터 설명한다.",
        )
        used_meanings: set = set()
        for index, e in enumerate(macro_events):
            event_id = str(e.get("event_id") or "")
            variation_index = (index + sum(map(ord, event_id))) % len(variation_cues)
            key_figures = {
                k: v for k, v in e.get("key_figures", {}).items()
                if v is not None
            }

            # 같은 날 plain_meaning이 겹치면 변형 인덱스를 밀어 다른 문구를 고른다.
            beginner_ctx = self._beginner_context(e, index)
            for bump in range(1, 4):
                if beginner_ctx["plain_meaning"] not in used_meanings:
                    break
                beginner_ctx = self._beginner_context(e, index + bump)
            used_meanings.add(beginner_ctx["plain_meaning"])

            item: Dict[str, Any] = {
                "event_id": e.get("event_id", ""),
                "role": e.get("event_role", "core"),
                "angle": e.get("macro_angle", ""),
                "label": e.get("angle_label", ""),
                "direction": e.get("direction", ""),
                "severity": e.get("severity", "normal"),
                "evidence": e.get("evidence", {}),
                "beginner_context": {
                    **beginner_ctx,
                    "variation_cue": variation_cues[variation_index],
                },
            }

            if key_figures:
                item["key_figures"] = key_figures

            result.append(item)

        return result

    def _beginner_context(self, event: Dict[str, Any], variant: int = 0) -> Dict[str, str]:
        event_id = str(event.get("event_id") or "")
        direction = str(event.get("direction") or "mixed")
        sources = set(event.get("source_columns") or [])
        evidence = event.get("evidence") or {}
        role = str(event.get("event_role") or "")

        if role == "preview" or "preview_release" in event_id:
            return {
                "concept": "예정된 공식 발표 일정",
                "plain_meaning": "예정된 공식 발표는 아직 결과가 나오지 않은 일정으로, 발표 당일에 수치가 공개된다.",
                "boundary": "발표 전에는 결과 수치나 시장 방향을 단정하지 않는다.",
            }

        if role == "projection" or "projection" in event_id:
            return {
                "concept": "공식 경제전망",
                "plain_meaning": "경제전망은 중앙은행이나 국제기구가 앞으로의 성장률·물가 등을 어떻게 보는지 공식 수치로 제시한 것이다.",
                "boundary": "전망은 약속이 아니라 발표 시점의 판단이며, 실제 결과와 다를 수 있다.",
            }

        if "official_release_calendar" in sources:
            category = str(evidence.get("release_category") or "")
            if category == "monetary_policy":
                return {
                    "concept": "중앙은행 통화정책 발표",
                    "plain_meaning": "중앙은행의 정책금리 결정은 예금·대출과 채권 금리가 움직일 때 기준이 되는 공식 결정이다.",
                    "boundary": "정책금리 결정만으로 이후 주가나 경기 방향을 단정하지 않는다.",
                }
            if category == "growth":
                return {
                    "concept": "국내총생산 발표",
                    "plain_meaning": "실질 GDP는 한 나라가 물가 변화를 제외하고 생산한 재화와 서비스의 규모가 얼마나 변했는지 보여준다.",
                    "boundary": "한 분기 수치만으로 장기 경기 방향을 단정하지 않는다.",
                }
            if category == "inflation":
                return {
                    "concept": "PCE 물가지수 발표",
                    "plain_meaning": "PCE 물가지수는 미국 가계가 소비한 상품과 서비스의 가격 변화를 보여주는 물가 지표다.",
                    "boundary": "한 달 수치만으로 향후 금리 결정을 단정하지 않는다.",
                }
            if category == "trade":
                return {
                    "concept": "상품·서비스 무역수지 발표",
                    "plain_meaning": "무역적자는 한 나라가 상품과 서비스를 해외에 판 금액보다 사들인 금액이 더 큰 상태다.",
                    "boundary": "적자 규모만으로 경제 전체의 좋고 나쁨을 단정하지 않는다.",
                }
            # 같은 날 같은 카테고리가 여러 건일 때 plain_meaning이 겹치지 않도록 variant로 회전.
            def rotate(options: tuple) -> str:
                return options[variant % len(options)]

            if category == "legislation":
                return {
                    "concept": "국회 본회의 의결",
                    "plain_meaning": rotate((
                        "국회 본회의 의결은 법률안을 국회의원 표결로 확정하는 입법 절차다.",
                        "본회의 의결은 상임위를 거친 법안을 국회의원 다수결로 최종 확정하는 단계다.",
                        "국회 의결은 법안이 본회의 표결을 통과해 법률로 확정되는 절차를 뜻한다.",
                    )),
                    "boundary": "법안 통과 사실만 전하고 시행 효과나 정치적 해석은 덧붙이지 않는다.",
                }
            if category == "court_ruling":
                return {
                    "concept": "헌법재판소 결정",
                    "plain_meaning": rotate((
                        "헌법재판소 결정은 법률의 위헌 여부나 탄핵·정당해산 심판을 최종 판단하는 절차다.",
                        "헌재 결정은 헌법에 따라 법률의 효력이나 탄핵·정당해산 여부를 가리는 최종 심판이다.",
                    )),
                    "boundary": "결정 주문 사실만 전하고 향후 정국이나 시장 영향은 단정하지 않는다.",
                }
            if category == "election":
                return {
                    "concept": "공직선거 실시",
                    "plain_meaning": rotate((
                        "공직선거는 유권자 투표로 대통령·국회의원·지방자치단체장 등을 선출하는 절차다.",
                        "공직선거는 정해진 선거일에 국민이 투표로 대표자를 뽑는 제도다.",
                    )),
                    "boundary": "선거가 실시된 사실만 전하고 결과 해석이나 정책 전망은 덧붙이지 않는다.",
                }
            if category == "regulatory_action":
                return {
                    "concept": "규제기관 조치",
                    "plain_meaning": rotate((
                        "공정거래위원회·금융감독원의 조치는 법 위반 여부를 조사해 과징금·시정명령 등을 부과하는 절차다.",
                        "규제기관 조치는 기업의 법규 위반을 심의해 제재나 시정을 결정하는 절차다.",
                        "공정위·금감원의 조치는 위반 사실을 조사해 과징금·제재 등 행정 처분을 내리는 절차다.",
                    )),
                    "boundary": "조치 사실만 전하고 기업 실적이나 주가 영향은 단정하지 않는다.",
                }
            return {
                "concept": "공식 경제지표 발표",
                "plain_meaning": "정부나 공공기관이 정해진 기준으로 집계해 공개한 경제 수치다.",
                "boundary": "발표 수치 밖의 원인이나 전망을 덧붙이지 않는다.",
            }

        # 같은 개념이 한 날에 여러 번 나와도 서로 다른 설명을 쓰도록,
        # 이벤트 위치(variant)와 event_id를 함께 섞어 문구를 회전한다.
        def pick(options: tuple) -> str:
            return options[(variant + sum(map(ord, event_id))) % len(options)]

        if "major_stock" in event_id:
            return {
                "concept": "개별 종목의 큰 주가 반응",
                "plain_meaning": pick((
                    "발표 전후 주가 변화는 시장 반응을 함께 살펴보기 위한 정보다.",
                    "개별 종목의 큰 주가 변동은 그 종목에 대한 시장 평가가 빠르게 달라졌음을 보여준다.",
                )),
                "boundary": "발표가 주가 움직임의 유일한 원인이라고 단정하지 않는다.",
            }
        if "sector_breadth" in event_id:
            market = "코스피" if "kospi" in event_id else "코스닥" if "kosdaq" in event_id else "국내 증시"
            movement = "강세" if direction == "positive" else "약세" if direction == "negative" else "엇갈린 흐름"
            meanings = (
                f"{market} 시장에서 오른 업종과 내린 업종의 수를 비교하면 그날 강세가 얼마나 넓게 퍼졌는지 알 수 있고, 이날은 {movement}였다.",
                f"{market}의 상승·하락 업종 분포는 지수 움직임이 일부 종목에 그쳤는지 여러 업종에 퍼졌는지 보여주며, 이날은 {movement}였다.",
            )
            return {
                "concept": f"{market} 업종 확산도",
                "plain_meaning": pick(meanings),
                "boundary": "개별 업종의 향후 방향을 예측하지 않는다.",
            }
        if "sector_dislocation" in event_id:
            comparison = "더 강했다" if direction == "positive" else "더 약했다"
            meanings = (
                f"%p는 업종과 전체 시장의 수익률 차이를 나타내며, 해당 업종이 같은 시장보다 {comparison}는 뜻이다.",
                f"시장 대비 수익률은 업종의 등락을 시장 전체와 비교하는 값으로, 해당 업종이 상대적으로 {comparison}는 뜻이다.",
            )
            return {
                "concept": "시장 대비 업종 수익률",
                "plain_meaning": pick(meanings),
                "boundary": "업종 자체 수익률과 시장 대비 차이를 구분한다.",
            }
        if "credit_spread" in event_id:
            return {
                "concept": "회사채 신용스프레드",
                "plain_meaning": pick((
                    "신용스프레드는 회사채가 국채보다 추가로 부담하는 금리 차이로, 클수록 기업의 상대적 조달 부담이 높다.",
                    "신용스프레드는 기업이 돈을 빌릴 때 안전한 국채보다 얼마나 더 높은 금리를 무는지를 보여주는 간격이다.",
                )),
                "boundary": "한 수치만으로 기업 부실을 단정하지 않는다.",
            }
        if "rate_spread" in event_id:
            meanings = (
                "bp는 금리 차이를 나타내는 단위다. 한국의 장단기 금리차는 장기 국채금리에서 단기 국채금리를 뺀 값이다.",
                "bp는 금리 간격을 읽는 단위다. 한국 장기와 단기 국채 금리의 차이로 국내 채권시장의 만기별 자금 가격을 비교한다.",
            )
            return {
                "concept": "한국 장단기 금리차",
                "plain_meaning": pick(meanings),
                "boundary": "금리차만으로 경기 방향을 단정하지 않는다.",
                "required_terms": ["bp", "단위"],
            }
        if "global_us_rates" in event_id:
            meanings = (
                "bp는 금리 차이를 나타내는 단위다. 미국 장단기 금리차는 미국 채권시장이 만기별 자금 가격을 얼마나 다르게 매기는지 보여준다.",
                "bp는 금리 간격을 읽는 단위다. 미국 장기와 단기 국채 금리의 차이로 미국 채권시장의 만기별 가격 차이를 가늠한다.",
            )
            return {
                "concept": "미국 장단기 금리차",
                "plain_meaning": pick(meanings),
                "boundary": "금리차만으로 미국 경기 방향을 단정하지 않는다.",
                "required_terms": ["bp", "단위"],
            }
        if "us_rate" in event_id:
            return {
                "concept": "미국 시장금리",
                "plain_meaning": pick((
                    "미국 시장금리 수준은 미국 채권 가격과 글로벌 자금 흐름을 가늠할 때 바탕이 되는 값이다.",
                    "미국 국채금리는 세계 금리의 기준점 역할을 해 국내 채권시장도 함께 참고한다.",
                )),
                "boundary": "하루 금리 변동만으로 통화정책 방향을 단정하지 않는다.",
            }
        if "us_equity" in event_id:
            return {
                "concept": "미국 주가지수",
                "plain_meaning": pick((
                    "미국 대표 주가지수는 미국 증시 전반의 가격 수준을 요약해 보여준다.",
                    "미국 주가지수는 글로벌 증시의 기준점 역할을 해 국내 시장도 함께 참고한다.",
                    "미국 대표 지수의 등락은 세계 위험자산 투자 분위기를 가늠하는 잣대가 된다.",
                )),
                "boundary": "미국 지수 움직임이 국내 시장과 항상 같은 방향은 아니다.",
            }
        if "usdkrw" in sources or "fx" in event_id:
            return {
                "concept": "원달러 환율",
                "plain_meaning": pick((
                    "원달러 환율 상승은 달러를 사는 데 더 많은 원화가 필요해 원화 가치가 낮아졌다는 뜻이다.",
                    "원달러 환율은 외환시장에서 원화와 달러가 교환되는 비율을 나타낸다.",
                    "환율 변동은 수출입 가격과 해외 자금 흐름을 가늠할 때 함께 참고하는 값이다.",
                )),
                "boundary": "모든 기업에 같은 방향으로 작용하지 않는다.",
            }
        if "wti" in sources or "oil" in event_id:
            return {
                "concept": "WTI 국제유가",
                "plain_meaning": pick((
                    "WTI는 국제 원유가격의 대표 기준 중 하나로 에너지 비용 흐름을 살필 때 쓰인다.",
                    "국제유가는 휘발유·난방비 같은 생활 물가와 기업 원가에 두루 연결되는 값이다.",
                    "원유 가격은 에너지를 많이 쓰는 산업의 비용 환경을 가늠하는 잣대가 된다.",
                )),
                "boundary": "하루 등락만으로 물가나 기업 실적을 단정하지 않는다.",
            }
        if "policy_rate" in event_id:
            return {
                "concept": "정책금리",
                "plain_meaning": pick((
                    "정책금리는 중앙은행이 정하는 기준 금리로 예금·대출과 채권 금리의 출발점 역할을 한다.",
                    "정책금리는 한 나라 금리의 기준점으로, 시장금리가 움직일 때 바탕이 되는 값이다.",
                )),
                "boundary": "시장금리가 항상 같은 폭으로 움직이는 것은 아니다.",
            }
        if "gold" in sources or "gold" in event_id:
            return {
                "concept": "금 가격",
                "plain_meaning": pick((
                    "금은 주식·채권과 다른 성격의 자산이라 시장의 자금 이동을 함께 살필 때 쓰인다.",
                    "금 가격은 안전자산 선호가 높아질 때 흔히 함께 살펴보는 지표다.",
                    "금은 통화 가치나 물가와 함께 움직이기도 해 자산 배분의 참고가 된다.",
                )),
                "boundary": "금값 상승만으로 시장 불안을 단정하지 않는다.",
            }
        if "kospi" in event_id or "kospi" in sources:
            return {
                "concept": "코스피 지수",
                "plain_meaning": pick((
                    "코스피 등락률은 유가증권시장에 상장된 종목 전체의 가격 흐름을 한데 모아 보여준다.",
                    "코스피는 대형주 중심의 국내 대표 지수로, 시장 전반의 분위기를 가늠하는 기준이 된다.",
                    "코스피 지수 수준은 국내 주식시장이 그날 전반적으로 어느 정도였는지를 요약한다.",
                )),
                "boundary": "모든 종목이 같은 폭으로 움직였다는 뜻은 아니다.",
            }
        if "kosdaq" in event_id or "kosdaq" in sources:
            return {
                "concept": "코스닥 지수",
                "plain_meaning": pick((
                    "코스닥 등락률은 중소·벤처기업이 많은 코스닥 시장의 전반적인 가격 흐름을 보여준다.",
                    "코스닥은 성장기업 비중이 큰 시장이라 코스피와 움직임이 다르게 나타나기도 한다.",
                    "코스닥 지수 수준은 기술·성장주가 몰린 시장이 그날 어느 정도였는지를 요약한다.",
                )),
                "boundary": "모든 종목이 같은 폭으로 움직였다는 뜻은 아니다.",
            }
        if "market_breadth" in event_id:
            return {
                "concept": "시장 등락 분포",
                "plain_meaning": pick((
                    "시장 등락 분포는 오른 종목과 내린 종목의 수를 비교해 상승세가 얼마나 넓었는지 보여준다.",
                    "오른 종목과 내린 종목의 비율은 지수 움직임이 일부에 쏠렸는지 전반적이었는지 구분한다.",
                )),
                "boundary": "모든 종목이 같은 폭으로 움직였다는 뜻은 아니다.",
            }
        if "rate" in event_id:
            return {
                "concept": "국내 시장금리",
                "plain_meaning": pick((
                    "국내 금리 수준은 예금·대출과 채권 가격이 정해질 때 바탕이 되는 값이다.",
                    "시장금리는 자금을 빌리고 빌려줄 때의 가격으로, 채권시장 분위기를 보여준다.",
                )),
                "boundary": "하루 금리 변동만으로 경기 방향을 단정하지 않는다.",
            }
        return {
            "concept": "시장 지표",
            "plain_meaning": pick((
                "이 지표는 해당 시장의 가격이나 금리 수준을 한눈에 요약해 보여준다.",
                "이 수치는 그날 시장이 전반적으로 어느 방향이었는지 가늠하는 참고값이다.",
                "시장 지표는 여러 자산의 흐름을 숫자로 요약해 비교를 쉽게 해준다.",
            )),
            "boundary": "한 지표만으로 투자 결정을 내리지 않는다.",
        }

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
                retry_feedback = ""
                if last_error is not None:
                    retry_feedback = (
                        "\n\n## REQUIRED CORRECTION FROM THE PREVIOUS ATTEMPT\n"
                        f"The previous response failed validation: {last_error}\n"
                        "Correct this exact issue while still returning the full batch."
                    )
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    temperature=self.config.temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt + retry_feedback},
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

                self._validate_batch_event_coverage(parsed["news"], daily_record)
                return parsed["news"]

            except Exception as e:
                last_error = e
                print(f"    재시도 {attempt}/{self.config.max_retries}: {e}")
                time.sleep(self.config.sleep_sec * attempt)

        raise RuntimeError(f"LLM 생성 최종 실패: {last_error}")

    def _validate_batch_event_coverage(
        self,
        news_items: List[Dict[str, Any]],
        daily_record: Dict[str, Any],
    ) -> None:
        events_list = [
            e for e in daily_record.get("macro_events", []) if e.get("event_id")
        ]
        expected_ids = [str(e.get("event_id")) for e in events_list]
        event_by_id = {str(e.get("event_id")): e for e in events_list}

        if not news_items:
            raise ValueError("기사가 한 건도 생성되지 않았습니다.")
        # 하루 정확히 5건(이벤트가 5건 미만인 예외적 날에는 이벤트 수만큼).
        target_count = 5 if len(expected_ids) >= 5 else len(expected_ids)
        if len(news_items) != target_count:
            raise ValueError(
                f"기사 수는 정확히 {target_count}건이어야 합니다: 현재 {len(news_items)}건"
            )

        # 추측·심리·수급·전망을 단정하는 표현만 금지(영향·반영·배경 등 사실 연결어는 허용).
        forbidden_inference = (
            "투자자", "외국인", "기대", "전망", "우려", "심리", "가능성", "견인",
            "것으로 보인다", "예상된다", "기대된다", "수 있다", "해석된다", "해석될",
        )

        used_ids: List[str] = []
        for item in news_items:
            detail = _strip_unsupported_filler_sentences(
                str(item.get("detail_news") or "")
            )
            item["detail_news"] = detail

            source_ids = item.get("source_event_ids")
            if not isinstance(source_ids, list) or not source_ids:
                raise ValueError("각 기사는 source_event_ids에 이벤트를 하나 이상 담아야 합니다.")
            cited = [str(s) for s in source_ids]
            if len(cited) != len(set(cited)):
                raise ValueError(f"한 기사가 같은 이벤트를 중복 인용했습니다: {cited}")
            unknown = [s for s in cited if s not in event_by_id]
            if unknown:
                raise ValueError(f"입력에 없는 event_id 인용: {unknown}")
            used_ids.extend(cited)
            cited_events = [event_by_id[s] for s in cited]

            allowed_numbers = _numbers([
                {"evidence": e.get("evidence"), "key_figures": e.get("key_figures")}
                for e in cited_events
            ])
            allowed_numbers += [
                float(part) for part in str(daily_record.get("date") or "").split("-")
                if part.isdigit()
            ]
            output_numbers = _numbers({
                "headline": item.get("headline"),
                "detail_news": item.get("detail_news"),
                "used_evidence": item.get("used_evidence"),
            })
            output_text = f"{item.get('headline') or ''} {item.get('detail_news') or ''}"
            unsupported = [
                value for value in output_numbers
                if not _matches_source_number(value, allowed_numbers)
                and not _matches_approximate_percent_band(
                    value, output_text, allowed_numbers,
                )
            ]
            if unsupported:
                raise ValueError(f"evidence 밖 숫자 감지: {unsupported}")

            # 공식 전망(SEP 등) 기사는 '전망했다/예상했다'가 필수 동사이므로,
            # 인용 이벤트의 허용 동사에 들어 있는 단어는 금지어 검사에서 제외한다.
            allowed_verbs = {
                v for e in cited_events
                for v in ((e.get("evidence") or {}).get("allowed_release_verbs") or [])
            }
            found = [
                word for word in forbidden_inference
                if word in detail and not any(word in v for v in allowed_verbs)
            ]
            if found:
                raise ValueError(f"근거 없는 추측 표현 감지: {found}")
            if re.search(r"-\s*\d[\d,.]*\s*%p\s*(?:앞섰|뒤처)", detail):
                raise ValueError("시장 대비 격차에 음수 부호와 방향 표현을 함께 쓸 수 없습니다.")
            # 후처리에서 본문 길이로 기사를 조용히 버려 커버리지가 깨지지 않도록,
            # 게이트 단계에서 같은 길이 기준을 강제해 재시도로 다시 쓰게 한다.
            if not self.config.detail_hard_min_chars <= len(detail) <= self.config.detail_max_chars:
                raise ValueError(
                    f"본문 길이 오류: {cited[0]} / {len(detail)}자 "
                    f"(허용 {self.config.detail_hard_min_chars}~{self.config.detail_max_chars}자)"
                )

            has_figures = any(e.get("key_figures") for e in cited_events)
            used_evidence = item.get("used_evidence")
            if has_figures and (not isinstance(used_evidence, list) or not used_evidence):
                raise ValueError("used_evidence가 비어 있습니다.")

            explanation = str(item.get("beginner_explanation") or "").strip()
            # LLM이 설명을 너무 짧게(예: 금리차 기사 18자) 또는 부정확하게 쓰면, 프롬프트에
            # 공급한 beginner_context.plain_meaning(검증된 정의문, 필수어 포함)으로 결정론 폴백.
            is_rate = len(cited) == 1 and ("rate_spread" in cited[0] or "global_us_rates" in cited[0])
            rate_bad = is_rate and not (
                ("bp" in explanation or "베이시스포인트" in explanation) and "단위" in explanation
            )
            needs_fallback = (
                not (25 <= len(explanation) <= 120)
                or any(ch.isdigit() for ch in explanation)
                or rate_bad
            )
            if needs_fallback and cited[0] in event_by_id:
                fallback = str(
                    self.prompt_builder._beginner_context(event_by_id[cited[0]]).get("plain_meaning") or ""
                ).strip()
                if 25 <= len(fallback) <= 120 and not any(ch.isdigit() for ch in fallback):
                    explanation = fallback
                    item["beginner_explanation"] = fallback
            if not 25 <= len(explanation) <= 120:
                raise ValueError(
                    f"beginner_explanation 길이 오류: {cited[0]} / {len(explanation)}자"
                )
            if any(char.isdigit() for char in explanation):
                raise ValueError("beginner_explanation에는 새 숫자를 넣을 수 없습니다.")

            # 인용한 공식 발표/프리뷰/리뷰 이벤트는 기관명과 허용 동사를 본문에 정확히 써야 한다.
            for ev_obj in cited_events:
                is_attributed_event = (
                    "official_release_calendar" in set(ev_obj.get("source_columns") or [])
                    or str(ev_obj.get("event_role") or "") == "projection"
                )
                if not is_attributed_event:
                    continue
                evi = ev_obj.get("evidence") or {}
                verbs = list(evi.get("allowed_release_verbs") or [])
                if not _attribution_satisfied(detail, evi):
                    raise ValueError(
                        f"공식 기관 표기 누락: {ev_obj.get('event_id')} / "
                        f"{evi.get('required_attribution') or evi.get('institution')}"
                    )
                if verbs and not any(v in detail for v in verbs):
                    raise ValueError(f"공식 발표/예정 동사 누락: {ev_obj.get('event_id')} / {verbs}")

            # 단독 금리차 기사만 bp 단위 설명을 강제(병합 기사는 핵심 이벤트 기준이라 제외).
            if len(cited) == 1 and ("rate_spread" in cited[0] or "global_us_rates" in cited[0]):
                rate_unit = "bp" in explanation or "베이시스포인트" in explanation
                if not (rate_unit and "단위" in explanation):
                    raise ValueError(f"금리차 단위 풀이 누락: {cited[0]} / {explanation}")

        # 프리뷰('내일 ~ 예정')는 보조 색채라 must-cover에서 제외(뉴스성 큰 건은 LLM이 자발 작성,
        # 모호한 예정 안건이 빠져도 그날 생성이 실패하지 않게 함). 프리뷰 이벤트는 source_columns가
        # official_release_calendar라 명시 제외가 필요하다.
        must_cover = {
            str(e.get("event_id"))
            for e in events_list
            if str(e.get("event_role") or "") != "preview"
            and (
                "official_release_calendar" in set(e.get("source_columns") or [])
                or str(e.get("event_role") or "") in ("review", "headline", "projection")
                or "major_stock" in str(e.get("event_id") or "")
            )
        }
        missing = must_cover - set(used_ids)
        if missing:
            raise ValueError(f"필수 커버 이벤트가 누락됐습니다: {sorted(missing)}")

        explanations = [
            str(item.get("beginner_explanation") or "").strip()
            for item in news_items
        ]
        if len(explanations) != len(set(explanations)):
            duplicates = sorted(
                explanation for explanation in set(explanations)
                if explanations.count(explanation) > 1
            )
            raise ValueError(f"같은 날짜에 beginner_explanation 문장이 중복됐습니다: {duplicates}")

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
            beginner_explanation = str(item.get("beginner_explanation", "")).strip()

            # 본문 끝에 붙은 교과서식 정의문 꼬리만 결정론적으로 제거(사실 문장은 보존)
            detail_news = _strip_trailing_definition(detail_news)
            detail_news = _strip_unsupported_filler_sentences(detail_news)

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

            if detail_char_count > self.config.detail_max_chars:
                print(
                    f"    [탈락] {date}_{i:02d} 본문 너무 김 ({detail_char_count}자 > "
                    f"{self.config.detail_max_chars}자)"
                )
                continue

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
                "fact_news": detail_news,
                "beginner_explanation": beginner_explanation,
                # 본문은 순수 기사문단만 — 초보 설명은 별도 컬럼으로만 보존(기사 아래 작게 표시)
                "detail_news": detail_news,
                "asset_class": "macro",
                "related_assets": json.dumps(related_assets, ensure_ascii=False),
                "direction": self._normalize_direction(item.get("direction")),
                "source_event_ids": json.dumps(source_event_ids, ensure_ascii=False),
                "used_evidence": json.dumps(used_evidence, ensure_ascii=False),
                "news_style": "macro_market_news",
            })

        # 유연 병합 후에는 기사 수가 이벤트 수보다 적어 자르면 커버리지가 깨진다.
        # 게이트가 이미 기사 수 ≤ 이벤트 수와 전수 커버리지를 보장하므로 그대로 반환한다.
        return cleaned

    def _normalize_direction(self, direction: Any) -> str:
        value = str(direction).strip().lower()
        allowed = {"positive", "negative", "neutral", "mixed"}
        return value if value in allowed else "mixed"

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    def _save_news_csv(self, rows: List[Dict[str, Any]]) -> None:
        columns = [
            "date", "news_id", "headline", "fact_news", "beginner_explanation", "detail_news",
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
        default=0.2,
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
        default=10,
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
