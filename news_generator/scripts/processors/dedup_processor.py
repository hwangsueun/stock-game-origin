"""
중복 제거 및 연관성 판정 프로세서
-----------------------------------
수집된 GKG 데이터에서
  1) 중복 기사 제거
  2) 자산 직접/간접/비연관 판정
  3) 최종 필터링 및 정규화
를 수행합니다.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Literal

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import DEDUP_CONFIG, GKG_THEMES, KR_KEYWORD_GROUPS, EN_KEYWORD_GROUPS

logger = logging.getLogger(__name__)

RelevanceLabel = Literal["direct", "indirect", "none"]

# ──────────────────────────────────────────────────────────────
# 직접 연관 키워드 (자산 가격에 즉각 영향)
# ──────────────────────────────────────────────────────────────
DIRECT_KEYWORDS = {
    # 금리·통화정책
    "기준금리", "금리인상", "금리인하", "금리동결",
    "연방기금금리", "기준금리결정", "통화정책",
    "interest rate", "rate hike", "rate cut", "fed funds rate",
    # 환율·외환
    "원달러환율", "환율급등", "환율급락", "달러인덱스",
    "외환위기", "currency crisis", "dollar index",
    # 원자재 가격
    "국제유가", "유가급등", "유가폭락", "WTI", "브렌트유", "OPEC",
    "crude oil", "oil price", "Brent", "WTI price",
    # 물가
    "소비자물가지수", "CPI", "PPI", "물가상승률",
    "inflation rate", "consumer price",
    # 증시 충격
    "코스피급락", "증시폭락", "블랙먼데이", "서킷브레이커",
    "stock market crash", "circuit breaker", "black monday",
    # 거시 지표
    "GDP성장률", "경제성장률", "GDP 쇼크",
    "GDP growth", "recession", "stagflation",
}

# ──────────────────────────────────────────────────────────────
# 간접 연관 키워드 (중·장기 영향)
# ──────────────────────────────────────────────────────────────
INDIRECT_KEYWORDS = {
    # 무역·정책
    "무역전쟁", "관세", "무역분쟁", "경제제재", "수출규제",
    "trade war", "tariff", "sanction", "export control",
    # 지정학
    "지정학리스크", "전쟁", "분쟁", "에너지위기",
    "geopolitical", "war", "conflict", "energy crisis",
    # 기업·산업
    "기업실적", "영업이익", "매출", "구조조정", "감원", "파산",
    "earnings", "revenue", "layoff", "bankruptcy", "restructuring",
    # 공급망
    "공급망", "반도체부족", "물류대란",
    "supply chain", "semiconductor shortage", "logistics",
    # 중앙은행·정책
    "양적완화", "양적긴축", "재정정책", "경기부양",
    "quantitative easing", "QE", "QT", "fiscal stimulus",
    # 금융 일반
    "신용등급", "국채금리", "회사채", "채권",
    "credit rating", "bond yield", "sovereign debt",
}

# ──────────────────────────────────────────────────────────────
# 제외 키워드 (생활·연예·스포츠 등)
# ──────────────────────────────────────────────────────────────
EXCLUDE_KEYWORDS = {
    "연예", "아이돌", "드라마", "영화", "스포츠", "축구", "야구", "농구",
    "결혼", "이혼", "임신", "출산", "사망", "부고", "사고",
    "맛집", "레시피", "여행", "날씨", "생활",
    "celebrity", "entertainment", "movie", "sports", "football",
    "soccer", "baseball", "wedding", "divorce", "accident",
}


# ──────────────────────────────────────────────────────────────
# URL 정규화
# ──────────────────────────────────────────────────────────────

# 쿼리스트링이 기사 ID인 도메인 (제거하면 안 됨)
_QUERY_ID_DOMAINS = (
    "news.kbs.co.kr",    # ?ncd=...
    "imnews.imbc.com",   # ?news_id=...
    "news.sbs.co.kr",    # ?news_id=...
    "www.ytn.co.kr",     # ?s_mcd=...
    "news.jtbc.co.kr",   # ?plink=...
)


def normalize_url(url: str) -> str:
    if not isinstance(url, str):
        return ""
    url = url.lower().strip()
    # 스킴 제거
    url = re.sub(r"^https?://", "", url)
    # www. / m. 제거
    url = re.sub(r"^(?:www|m)\.", "", url)
    # 트레일링 슬래시 제거
    url = url.rstrip("/")
    # 앵커 제거 (항상)
    url = re.sub(r"#.*$", "", url)
    # 쿼리스트링: 기사 ID로 쓰는 도메인은 유지, 나머지는 제거
    if not any(d in url for d in _QUERY_ID_DOMAINS):
        url = re.sub(r"\?.*$", "", url)
    return url


def url_fingerprint(url: str) -> str:
    return hashlib.md5(normalize_url(url).encode()).hexdigest()


# ──────────────────────────────────────────────────────────────
# 연관성 판정
# ──────────────────────────────────────────────────────────────

TRUSTED_DOMAINS = {
    # 방송사
    "news.kbs.co.kr", "news.sbs.co.kr", "imnews.imbc.com", "mnews.jtbc.co.kr",
    "ytn.co.kr", "news.mbn.co.kr", "tvchosun.com",
    # 경제지
    "mk.co.kr", "hankyung.com", "edaily.co.kr", "thebell.co.kr",
    "fn.co.kr", "fnnews.com", "sedaily.com", "businesspost.co.kr",
    # 종합일간지
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "hankookilbo.com",
    # 통신사
    "yna.co.kr",
}

DIRECT_THEMES = {
    "ECON_INTEREST_RATES", "ECON_INFLATION", "ECON_CURRENCY",
    "ECON_STOCKMARKET", "ENV_OIL", "ENV_GAS", "ECON_GLOBALECONOMY",
    "FINANCIAL_CRISIS", "ECON_FISCALDEFICIT", "ECON_DEBT",
    "ECON_FOREIGNTRADE", "ECON_INVESTMENTCAPITALFLOWS",
    "CENTRAL_BANK", "MONETARY_POLICY",
}

INDIRECT_THEME_PREFIXES = ("ECON_", "ENV_", "WB_", "SANCTION", "FISCAL_", "TAX_")


def _parse_themes(themes_raw: str) -> list[str]:
    if not themes_raw or not isinstance(themes_raw, str):  # 이 줄 수정
        return []
    # JSON 배열 시도
    if themes_raw.strip().startswith("["):
        try:
            return json.loads(themes_raw)
        except Exception:
            pass
    # raw 형식: 세미콜론 구분, 각 항목은 "테마,오프셋"
    themes = []
    for item in themes_raw.split(";"):
        item = item.strip()
        if not item:
            continue
        theme = item.split(",")[0].strip()
        if theme:
            themes.append(theme)
    return themes


def score_relevance(title: str, themes_raw: str = "", domain: str = "") -> RelevanceLabel:
    if not isinstance(title, str):
        title = ""
    if not isinstance(themes_raw, str):  # 추가
        themes_raw = ""
    if not isinstance(domain, str):      # 추가
        domain = ""
    text = title.lower()

    # 제외 키워드 (제목 있을 때만)
    if text and any(kw in text for kw in EXCLUDE_KEYWORDS):
        return "none"

    themes = _parse_themes(themes_raw)

    # 직접 연관 테마
    direct_theme_hit = any(t in DIRECT_THEMES for t in themes)
    if direct_theme_hit:
        return "direct"

    # 직접 연관 키워드 (제목 있을 때)
    if text and any(kw.lower() in text for kw in DIRECT_KEYWORDS):
        return "direct"

    # 간접 연관 테마
    indirect_theme_hit = any(
        t.startswith(INDIRECT_THEME_PREFIXES) for t in themes
    )
    if indirect_theme_hit:
        # 신뢰 매체면 바로 통과
        if domain in TRUSTED_DOMAINS or any(d in domain for d in TRUSTED_DOMAINS):
            return "indirect"
        return "indirect"

    # 간접 연관 키워드 (제목 있을 때)
    if text and any(kw.lower() in text for kw in INDIRECT_KEYWORDS):
        return "indirect"

    # 신뢰 매체 + 테마가 하나라도 있으면 간접 연관으로 허용
    if themes and (domain in TRUSTED_DOMAINS or any(d in domain for d in TRUSTED_DOMAINS)):
        return "indirect"

    return "none"


# ──────────────────────────────────────────────────────────────
# 중복 제거
# ──────────────────────────────────────────────────────────────

class ArticleDeduplicator:
    """
    URL 기반 정확 중복 제거 + 제목 유사도 기반 근사 중복 제거.
    """

    def __init__(
        self,
        similarity_threshold: float = DEDUP_CONFIG["title_similarity_threshold"],
        time_window_hours: int = DEDUP_CONFIG["time_window_hours"],
    ):
        self.sim_thresh = similarity_threshold
        self.time_window = pd.Timedelta(hours=time_window_hours)

    def deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        입력 DataFrame에서 중복 제거 후 반환.
        필수 컬럼: url, title, published_at
        """
        if df.empty:
            return df

        n_before = len(df)

        # 1) URL 정규화 후 정확 중복 제거
        df = df.copy()
        df["url_norm"] = df["url"].apply(normalize_url)
        df = df.drop_duplicates(subset="url_norm", keep="first")
        logger.info("URL 중복 제거: %d → %d건", n_before, len(df))

        # 2) 제목 + 시간 기반 근사 중복 제거
        df = self._semantic_dedup(df)
        logger.info("제목 유사도 중복 제거 후: %d건", len(df))

        return df.drop(columns=["url_norm"], errors="ignore")

    def _semantic_dedup(self, df: pd.DataFrame) -> pd.DataFrame:
        """TF-IDF 코사인 유사도로 근사 중복 감지·제거."""
        if "title" not in df.columns or len(df) < 2:
            return df

        df = df.copy()

        # published_at 파싱
        if not pd.api.types.is_datetime64_any_dtype(df["published_at"]):
            df["published_at"] = pd.to_datetime(
                df["published_at"], format="%Y%m%d%H%M%S", errors="coerce"
            )

        df = df.sort_values("published_at").reset_index(drop=True)
        titles = df["title"].fillna("").tolist()

        # TF-IDF
        vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
            max_features=50_000,
            sublinear_tf=True,
        )
        try:
            tfidf_matrix = vectorizer.fit_transform(titles)
        except Exception:
            return df

        keep_mask = [True] * len(df)

        # 슬라이딩 윈도우 내 유사도 검사
        for i in range(len(df)):
            if not keep_mask[i]:
                continue
            t_i = df.iloc[i]["published_at"]
            for j in range(i + 1, len(df)):
                if not keep_mask[j]:
                    continue
                t_j = df.iloc[j]["published_at"]
                if pd.notna(t_i) and pd.notna(t_j):
                    if t_j - t_i > self.time_window:
                        break  # 시간 창 초과 → 이후 검사 불필요

                sim = cosine_similarity(
                    tfidf_matrix[i], tfidf_matrix[j]
                )[0][0]
                if sim >= self.sim_thresh:
                    keep_mask[j] = False  # 후행 기사 제거

        return df[keep_mask].reset_index(drop=True)


# ──────────────────────────────────────────────────────────────
# 통합 프로세서
# ──────────────────────────────────────────────────────────────

class ArticleProcessor:
    """수집 데이터 → 최종 정제 데이터 파이프라인."""

    def __init__(self):
        self.deduplicator = ArticleDeduplicator()

    def process(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        전체 처리 파이프라인:
          1. 연관성 판정 → none 제거
          2. 중복 제거
          3. 컬럼 정리 및 타입 변환
        """
        if df.empty:
            return df

        logger.info("처리 시작: %d건", len(df))

        # 1) 연관성 판정
        df = df.copy()
        title_col  = "title"       if "title"       in df.columns else None
        themes_col = "themes_json" if "themes_json" in df.columns else \
                     "themes_raw"  if "themes_raw"  in df.columns else None
        domain_col = "domain"      if "domain"      in df.columns else None

        # 도메인 컬럼 없으면 URL에서 추출
        if not domain_col and "url" in df.columns:
            df["domain"] = df["url"].str.extract(r'https?://(?:(?:www|m)\.)?([^/:]+)')
            domain_col = "domain"

        df["relevance"] = df.apply(
            lambda r: score_relevance(
                r.get(title_col, "") if title_col else "",
                r.get(themes_col, "") if themes_col else "",
                r.get(domain_col, "") if domain_col else "",
            ),
            axis=1,
        )
        df = df[df["relevance"] != "none"].reset_index(drop=True)
        logger.info("연관성 필터 후: %d건 (direct/indirect만)", len(df))

        # 2) 중복 제거
        df = self.deduplicator.deduplicate(df)

        # 3) 최종 컬럼 정리
        df = self._finalize(df)
        logger.info("처리 완료: %d건", len(df))
        return df

    @staticmethod
    def _finalize(df: pd.DataFrame) -> pd.DataFrame:
        """타입 정규화 및 불필요 컬럼 제거."""
        # published_at → datetime
        if "published_at" in df.columns:
            df["published_at"] = pd.to_datetime(
                df["published_at"].astype(str),
                format="%Y%m%d%H%M%S",
                errors="coerce",
            )

        # ref_date → date 문자열 정리
        if "ref_date" in df.columns:
            df["ref_date"] = df["ref_date"].astype(str).str[:8]

        # 도메인 추출 (없으면)
        if "domain" not in df.columns and "url" in df.columns:
            df["domain"] = df["url"].str.extract(r"https?://(?:www\.)?([^/]+)")

        # 불필요 컬럼 제거
        drop_cols = [c for c in ["url_norm", "Extras"] if c in df.columns]
        df = df.drop(columns=drop_cols)

        return df.reset_index(drop=True)


# ──────────────────────────────────────────────────────────────
# 배치 처리 유틸리티
# ──────────────────────────────────────────────────────────────

def process_parquet_files(
    input_dir: str | Path,
    output_dir: str | Path,
    pattern: str = "gkg_*.parquet",
) -> None:
    """
    input_dir의 parquet 파일들을 일괄 처리하여 output_dir에 저장.
    """
    input_dir  = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    processor = ArticleProcessor()
    files = sorted(input_dir.glob(pattern))
    logger.info("처리 대상: %d개 파일", len(files))

    for fpath in files:
        out_path = output_dir / fpath.name
        if out_path.exists():
            logger.debug("스킵 (기존): %s", fpath.name)
            continue
        try:
            df = pd.read_parquet(fpath)
            processed = processor.process(df)
            processed.to_parquet(out_path, index=False, engine="pyarrow")
            logger.info("%s: %d → %d건", fpath.name, len(df), len(processed))
        except Exception as e:
            logger.error("%s 처리 실패: %s", fpath.name, e)