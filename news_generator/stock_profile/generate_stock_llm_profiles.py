from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from tqdm import tqdm


# ============================================================
# 0. Project Paths
# ============================================================

class ProjectPaths:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    ENV_PATH = PROJECT_ROOT / ".env"

    @classmethod
    def resolve_from_root(cls, path_value: str | Path) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path
        return cls.PROJECT_ROOT / path


# ============================================================
# 1. Structured Output Schema
# ============================================================

class StockProfileOutput(BaseModel):
    business_summary_asof: str = Field(
        description=(
            "기업 개요를 명사구로 작성. 문장형 종결 금지. "
            "'기업이다', '수행한다', '운영한다', '제공한다', '생산한다'로 끝내지 말 것. "
            "예: '토목·건축·플랜트 공사와 석유화학 제품 생산 중심의 건설·제조 계열'"
        )
    )
    main_products_asof: list[str] = Field(
        description="주요 제품/서비스. 짧은 명사구만 사용."
    )
    business_segments_asof: list[str] = Field(
        description="주요 사업부문. 짧은 명사구만 사용."
    )
    risk_keywords_asof: list[str] = Field(
        description="주요 리스크 키워드. 짧은 명사구만 사용."
    )
    macro_sensitive_factors: list[str] = Field(
        description="거시 민감 요인. 짧은 명사구만 사용."
    )
    news_reaction_tags: list[str] = Field(
        description="뉴스 연결 태그. 짧은 명사구만 사용."
    )
    asset_personality: list[str] = Field(
        description=(
            "자산 성격 태그. 예: 경기민감, 금리민감, 수출주, 내수주, "
            "배당주, 원자재민감, 방어주, 성장주, 가치주"
        )
    )
    beginner_description: str = Field(
        description=(
            "게임 화면에 들어갈 쉬운 설명. 문장형 종결 금지. "
            "예: '건설 경기, 유가, 환율 변화의 영향을 받는 경기민감형 자산'"
        )
    )
    data_quality_note: str = Field(
        description="근거 부족, 추출 오류, 정보 부족이 있을 때만 짧게 작성. 문제 없으면 빈 문자열."
    )


# ============================================================
# 2. Config
# ============================================================

@dataclass(frozen=True)
class GenerateConfig:
    input_jsonl_path: Path
    output_dir: Path
    model: str
    limit: int | None
    sleep_seconds: float
    max_retries: int
    overwrite: bool

    @property
    def output_jsonl_path(self) -> Path:
        return self.output_dir / "stock_llm_profile_yearly_clean.jsonl"

    @property
    def output_csv_path(self) -> Path:
        return self.output_dir / "stock_llm_profile_yearly_clean.csv"

    @property
    def failure_csv_path(self) -> Path:
        return self.output_dir / "stock_llm_generation_failures.csv"


@dataclass
class FailureRow:
    stock_code: str
    corp_code: str
    stock_name: str
    business_year: str
    rcept_no: str
    stage: str
    message: str


# ============================================================
# 3. Utility
# ============================================================

class JsonlReader:
    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise FileNotFoundError(f"입력 JSONL 파일이 없습니다: {path}")

        rows: list[dict[str, Any]] = []

        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"JSONL 파싱 실패 line={line_no}: {e}") from e

        return rows


class JsonlAppender:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class ExistingResultLoader:
    @staticmethod
    def load_done_keys(path: Path) -> set[tuple[str, int]]:
        if not path.exists():
            return set()

        done: set[tuple[str, int]] = set()

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    row = json.loads(line)
                    stock_code = str(row.get("stock_code", "")).zfill(6)
                    business_year = int(row.get("business_year"))
                    done.add((stock_code, business_year))
                except Exception:
                    continue

        return done


class TextLimiter:
    @staticmethod
    def limit(text: Any, max_chars: int) -> str:
        value = str(text or "").strip()

        if len(value) <= max_chars:
            return value

        return value[:max_chars].rstrip() + " ...[TRUNCATED]"


class OutputCleaner:
    FORBIDDEN_PATTERNS = [
        r"\b20\d{2}년\s*기준(?:으로|의)?\s*",
        r"해당\s*연도\s*기준(?:으로|의)?\s*",
        r"당시\s*기준(?:으로|의)?\s*",
        r"이\s*시점\s*기준(?:으로|의)?\s*",
        r"초보\s*투자자(?:용)?\s*",
        r"게임\s*내\s*",
        r"교육용\s*",
        r"DART\s*사업보고서(?:에서|에)?\s*",
        r"raw_facts(?:에서|에)?\s*",
    ]

    PHRASE_REPLACEMENTS = {
        "다양한 사업": "여러 사업",
        "여러 사업을 운영하는": "여러 사업",
        "사업을 운영하는 기업입니다": "사업 중심",
        "사업을 운영하는 기업이다": "사업 중심",
        "사업을 운영하고 있습니다": "사업 중심",
        "사업을 운영한다": "사업 운영",
        "사업을 전개한다": "사업 전개",
        "사업을 영위한다": "사업 영위",
        "영위하고 있습니다": "영위",
        "생산합니다": "생산",
        "생산한다": "생산",
        "제공합니다": "제공",
        "제공한다": "제공",
        "수행합니다": "수행",
        "수행한다": "수행",
        "운영합니다": "운영",
        "운영한다": "운영",
        "담당합니다": "담당",
        "담당한다": "담당",
        "포함합니다": "포함",
        "포함한다": "포함",
        "민감하게 반응할 수 있습니다": "영향",
        "민감하게 반응합니다": "영향",
        "영향을 받을 수 있습니다": "영향",
        "영향을 받습니다": "영향",
        "영향을 받는다": "영향",
        "기업입니다": "기업",
        "기업이다": "기업",
        "회사입니다": "회사",
        "회사이다": "회사",
        "자산입니다": "자산",
        "자산이다": "자산",
        "정보는 충분하나": "",
        "일부 세부 사항은 생략되었습니다": "",
        "사업보고서에서 제공된 정보에 기반하여 작성되었습니다": "",
    }

    SENTENCE_ENDING_PATTERNS = [
        r"입니다\.?$",
        r"입니다$",
        r"이다\.?$",
        r"한다\.?$",
        r"합니다\.?$",
        r"있습니다\.?$",
        r"있다\.?$",
        r"됩니다\.?$",
        r"된다\.?$",
        r"받는다\.?$",
        r"받습니다\.?$",
        r"수행한다\.?$",
        r"수행합니다\.?$",
        r"제공한다\.?$",
        r"제공합니다\.?$",
        r"생산한다\.?$",
        r"생산합니다\.?$",
        r"운영한다\.?$",
        r"운영합니다\.?$",
    ]

    @classmethod
    def clean_text(cls, text: Any) -> str:
        value = str(text or "").strip()

        for pattern in cls.FORBIDDEN_PATTERNS:
            value = re.sub(pattern, "", value)

        for old, new in cls.PHRASE_REPLACEMENTS.items():
            value = value.replace(old, new)

        for pattern in cls.SENTENCE_ENDING_PATTERNS:
            value = re.sub(pattern, "", value)

        value = re.sub(r"\s+", " ", value)
        value = value.replace(" .", ".")
        value = value.replace(" ,", ",")
        value = value.replace("..", ".")
        value = value.rstrip(".。")
        value = value.strip(" ,")

        return value.strip()

    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []

        for value in values or []:
            item = cls.clean_text(str(value))
            item = item.strip("[]'\" ")
            if item and item not in cleaned:
                cleaned.append(item)

        return cleaned

    @classmethod
    def clean_output(cls, output: StockProfileOutput) -> dict[str, Any]:
        data = output.model_dump()

        text_cols = [
            "business_summary_asof",
            "beginner_description",
            "data_quality_note",
        ]

        list_cols = [
            "main_products_asof",
            "business_segments_asof",
            "risk_keywords_asof",
            "macro_sensitive_factors",
            "news_reaction_tags",
            "asset_personality",
        ]

        for col in text_cols:
            data[col] = cls.clean_text(data.get(col, ""))

        for col in list_cols:
            data[col] = cls.clean_list(data.get(col, []))

        return data


# ============================================================
# 4. Prompt Builder
# ============================================================

class PromptBuilder:
    SYSTEM_PROMPT = """
너는 한국 주식 투자 게임에 들어갈 기업 설명 데이터를 정제한다.

역할:
- DART 사업보고서에서 추출된 raw_facts만 사용한다.
- 설명은 게임 화면의 자산 상세정보에 바로 들어갈 짧은 명사구로 작성한다.
- 투자 추천, 홍보 문구, 과장 표현은 쓰지 않는다.
- business_year는 내부 메타데이터일 뿐이며 문장에 직접 쓰지 않는다.
- raw_facts에 없는 사업, 제품, 사건, 미래 정보를 추가하지 않는다.
- 근거가 부족하면 data_quality_note에만 짧게 남긴다.

문체 규칙:
- 한국어
- 문장형 종결 금지
- 명사구 또는 명사형 표현으로 끝낼 것
- 마침표 사용 금지
- "~하는 기업이다", "~운영한다", "~생산한다", "~제공한다", "~수행한다" 금지
- "다양한", "민감하게 반응", "초보 투자자", "게임 내", "교육용" 같은 AI식 표현 금지
- "2013년 기준", "해당 연도 기준", "당시 기준" 같은 표현 금지
- 가능하면 20~55자 안팎의 압축된 표현 사용

좋은 출력 예시:
- "토목·건축·플랜트 공사와 석유화학 제품 생산 중심의 건설·제조 계열"
- "메모리 반도체, 스마트폰, 디스플레이, 가전 중심의 대형 제조주"
- "전력 판매와 송배전망 운영 중심의 공공 인프라 계열"
- "정유·석유화학 제품과 윤활유 사업 중심의 에너지·화학 계열"
- "은행업과 여신·수신 업무 중심의 금리민감 금융주"
- "건설 경기, 유가, 환율 변화의 영향을 받는 경기민감형 자산"

나쁜 출력 예시:
- "DL은 건설과 제조 분야에서 사업을 운영하는 기업입니다."
- "DL은 다양한 사업을 운영하는 기업이다."
- "2013년 기준으로 건설 부문을 중심으로 성장하고 있습니다."
- "경제 환경 변화에 민감하게 반응할 수 있습니다."
- "초보 투자자용으로 이해하기 쉬운 설명입니다."
""".strip()

    def build_user_prompt(self, item: dict[str, Any]) -> str:
        raw_facts = item.get("raw_facts", {})
        source = item.get("source", {})

        compact_item = {
            "stock_code": item.get("stock_code", ""),
            "corp_code": item.get("corp_code", ""),
            "stock_name": item.get("stock_name", ""),
            "business_year": item.get("business_year", ""),
            "source": source,
            "raw_facts": {
                "company_overview": TextLimiter.limit(raw_facts.get("company_overview", ""), 2200),
                "business_content": TextLimiter.limit(raw_facts.get("business_content", ""), 4500),
                "main_products": TextLimiter.limit(raw_facts.get("main_products", ""), 2200),
                "sales_order": TextLimiter.limit(raw_facts.get("sales_order", ""), 2200),
                "risk": TextLimiter.limit(raw_facts.get("risk", ""), 2200),
                "rnd": TextLimiter.limit(raw_facts.get("rnd", ""), 1200),
            },
        }

        return f"""
아래 JSON은 DART 사업보고서 원문에서 추출한 facts다.
이 facts만 바탕으로 구조화된 기업 설명을 생성해라.

중요:
- business_summary_asof와 beginner_description은 문장이 아니라 명사구로 작성
- 연도 표현 금지
- 마침표 금지
- 회사명 반복 최소화
- 원문 근거가 부족한 경우 단정 금지

입력 JSON:
{json.dumps(compact_item, ensure_ascii=False, indent=2)}
""".strip()


# ============================================================
# 5. OpenAI Generator
# ============================================================

class StockProfileGenerator:
    def __init__(
        self,
        client: OpenAI,
        model: str,
        max_retries: int,
        sleep_seconds: float,
    ):
        self.client = client
        self.model = model
        self.max_retries = max_retries
        self.sleep_seconds = sleep_seconds
        self.prompt_builder = PromptBuilder()

    def generate(self, item: dict[str, Any]) -> StockProfileOutput:
        system_prompt = self.prompt_builder.SYSTEM_PROMPT
        user_prompt = self.prompt_builder.build_user_prompt(item)

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                completion = self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format=StockProfileOutput,
                    temperature=0.1,
                )

                parsed = completion.choices[0].message.parsed

                if parsed is None:
                    raise ValueError("parsed output is None")

                time.sleep(self.sleep_seconds)
                return parsed

            except Exception as e:
                last_error = e
                wait = min(2 ** attempt, 30)
                time.sleep(wait)

        raise RuntimeError(f"LLM 생성 실패: {last_error}") from last_error


# ============================================================
# 6. Pipeline
# ============================================================

class GenerateStockLlmProfilePipeline:
    def __init__(self, config: GenerateConfig):
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        if self.config.overwrite:
            self._delete_existing_outputs()

        self.client = OpenAI()
        self.generator = StockProfileGenerator(
            client=self.client,
            model=config.model,
            max_retries=config.max_retries,
            sleep_seconds=config.sleep_seconds,
        )
        self.appender = JsonlAppender(config.output_jsonl_path)

    def run(self) -> None:
        input_rows = JsonlReader.read(self.config.input_jsonl_path)

        if self.config.limit is not None:
            input_rows = input_rows[: self.config.limit]

        done_keys: set[tuple[str, int]] = set()

        if not self.config.overwrite:
            done_keys = ExistingResultLoader.load_done_keys(self.config.output_jsonl_path)

        failures: list[FailureRow] = []

        total = len(input_rows)
        processed = 0
        skipped = 0

        for item in tqdm(input_rows, total=total):
            stock_code = str(item.get("stock_code", "")).zfill(6)
            corp_code = str(item.get("corp_code", ""))
            stock_name = str(item.get("stock_name", ""))
            business_year = int(item.get("business_year"))
            source = item.get("source", {})
            rcept_no = str(source.get("rcept_no", ""))

            key = (stock_code, business_year)

            if key in done_keys and not self.config.overwrite:
                skipped += 1
                continue

            try:
                parsed = self.generator.generate(item)

                output_row = {
                    "stock_code": stock_code,
                    "corp_code": corp_code,
                    "stock_name": stock_name,
                    "business_year": business_year,
                    "source_name": "DART",
                    "source_report_name": source.get("report_name", ""),
                    "source_rcept_no": source.get("rcept_no", ""),
                    "source_rcept_date": source.get("rcept_date", ""),
                    **OutputCleaner.clean_output(parsed),
                }

                self.appender.append(output_row)
                processed += 1

            except Exception as e:
                failures.append(
                    FailureRow(
                        stock_code=stock_code,
                        corp_code=corp_code,
                        stock_name=stock_name,
                        business_year=str(business_year),
                        rcept_no=rcept_no,
                        stage="llm_generate",
                        message=str(e),
                    )
                )

        self._write_csv_outputs(failures)

        print("\n[완료]")
        print(f"- 입력 rows: {total:,}")
        print(f"- 신규 처리: {processed:,}")
        print(f"- 스킵: {skipped:,}")
        print(f"- 실패: {len(failures):,}")
        print(f"- JSONL: {self.config.output_jsonl_path}")
        print(f"- CSV: {self.config.output_csv_path}")
        print(f"- 실패 로그: {self.config.failure_csv_path}")

    def _delete_existing_outputs(self) -> None:
        for path in [
            self.config.output_jsonl_path,
            self.config.output_csv_path,
            self.config.failure_csv_path,
        ]:
            if path.exists():
                path.unlink()

    def _write_csv_outputs(self, failures: list[FailureRow]) -> None:
        rows: list[dict[str, Any]] = []

        if self.config.output_jsonl_path.exists():
            with self.config.output_jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))

        if rows:
            df = pd.DataFrame(rows)
            df = df.sort_values(["stock_code", "business_year"])
            df.to_csv(self.config.output_csv_path, index=False, encoding="utf-8-sig")

        failure_df = pd.DataFrame([asdict(row) for row in failures])
        failure_df.to_csv(self.config.failure_csv_path, index=False, encoding="utf-8-sig")


# ============================================================
# 7. CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DART raw facts JSONL을 LLM으로 정제해 stock_llm_profile_yearly_clean.csv를 생성합니다."
    )

    parser.add_argument(
        "--input",
        default="data/processed/stock_llm_input_yearly.jsonl",
        help="입력 JSONL 경로. 기본값은 프로젝트 루트 기준",
    )
    parser.add_argument(
        "--output-dir",
        default="data/processed",
        help="출력 폴더. 기본값은 프로젝트 루트 기준",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="OpenAI 모델명. 예: gpt-4o-mini, gpt-4o",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="테스트용 처리 개수. 전체 처리 시 생략",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="요청 간 sleep 초",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="실패 시 재시도 횟수",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="기존 JSONL/CSV 결과를 지우고 다시 처리",
    )

    return parser.parse_args()


def main() -> None:
    load_dotenv(ProjectPaths.ENV_PATH)

    if not os.getenv("OPENAI_API_KEY"):
        raise EnvironmentError(f".env 파일에 OPENAI_API_KEY가 없습니다: {ProjectPaths.ENV_PATH}")

    args = parse_args()

    config = GenerateConfig(
        input_jsonl_path=ProjectPaths.resolve_from_root(args.input),
        output_dir=ProjectPaths.resolve_from_root(args.output_dir),
        model=args.model,
        limit=args.limit,
        sleep_seconds=args.sleep,
        max_retries=args.max_retries,
        overwrite=args.overwrite,
    )

    pipeline = GenerateStockLlmProfilePipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()