"""
GDELT 뉴스 수집 설정 파일
대상: 2014~2023년 거시경제·시장 관련 뉴스
"""

from datetime import date

# ──────────────────────────────────────────────
# 수집 기간
# ──────────────────────────────────────────────
DATE_START = date(2014, 1, 1)
DATE_END   = date(2023, 12, 31)

# ──────────────────────────────────────────────
# 언어 우선순위
# ──────────────────────────────────────────────
LANG_PRIMARY   = "Korean"   # kor 한국어 전용

# ──────────────────────────────────────────────
# GDELT GKG 테마 필터
# (V2Themes 컬럼에서 OR 조건으로 매칭)
# ──────────────────────────────────────────────
GKG_THEMES = [
    # 거시경제
    "ECON_GLOBALECONOMY", "ECON_REFORM", "ECON_DEBT", "ECON_FISCALDEFICIT",
    "ECON_INTEREST_RATES", "ECON_INFLATION", "ECON_CURRENCY",
    "ECON_FOREIGNTRADE", "ECON_INVESTMENTCAPITALFLOWS",
    "ECON_UNEMPLOYMENT", "ECON_STOCKMARKET", "ECON_BANKRUPTCY",
    "ECON_AUSTERITY", "ECON_BOYCOTT", "ECON_REMITTANCES",
    # 원자재·에너지
    "ENV_OIL", "ENV_GAS", "ENV_COAL", "ENV_METALS",
    "ENV_NUCLEARPOWER", "ENV_RENEWABLEENERGY",
    "ENV_AGRICULTUREFOOD", "ENV_MINING",
    # 지정학·제재
    "SANCTION", "WB_2661_TRADE_POLICY", "WB_2663_TRADE_FACILITATION",
    "WB_1925_FINANCIAL_MARKETS", "WB_1927_BANKING_SECTOR",
    "WB_696_ECONOMIC_POLICY", "WB_697_ECONOMIC_STATISTICS",
    # 중앙은행·정책
    "CENTRAL_BANK", "MONETARY_POLICY", "FISCAL_POLICY",
    "TAX_REFORM", "INTEREST_RATE", "CURRENCY_DEVALUATION",
    # 기업·증시
    "CORPORATE_EARNINGS", "IPO", "MERGER_ACQUISITION",
    "STOCK_MARKET_CRASH", "FINANCIAL_CRISIS",
]

# ──────────────────────────────────────────────
# GDELT CAMEO 이벤트 코드 (루트 코드)
# 경제·제재·외교·갈등 관련 코드만 선별
# ──────────────────────────────────────────────
CAMEO_ROOT_CODES = [
    "03",  # Express intent to cooperate (경제 협력)
    "04",  # Consult
    "06",  # Provide material cooperation (원조·지원)
    "07",  # Provide economic aid
    "08",  # Yield
    "10",  # Demand
    "11",  # Disapprove
    "12",  # Reject
    "13",  # Threaten (제재 위협)
    "15",  # Exhibit force posture (지정학 리스크)
    "17",  # Coerce / Sanction
    "18",  # Assault (에너지 인프라 공격 등)
]

# ──────────────────────────────────────────────
# 한국어 키워드 (DOC API 검색용)
# 그룹별로 OR 조건, 그룹 간 AND 조건으로 조합 가능
# ──────────────────────────────────────────────
KR_KEYWORD_GROUPS = {
    "거시경제": [
        "GDP", "경제성장률", "경기침체", "경기회복", "기준금리", "금리인상", "금리인하",
        "한국은행", "연방준비제도", "연준", "ECB", "유럽중앙은행",
        "물가상승", "인플레이션", "소비자물가", "생산자물가", "디플레이션",
        "환율", "원달러", "달러강세", "달러약세", "엔화", "위안화",
        "무역수지", "경상수지", "재정적자", "국가부채",
    ],
    "원자재_에너지": [
        "국제유가", "WTI", "브렌트유", "OPEC", "천연가스", "LNG",
        "원자재", "구리", "철광석", "리튬", "니켈", "팔라듐",
        "곡물가격", "밀", "옥수수", "대두", "식량위기",
        "에너지위기", "전력난", "탄소중립", "신재생에너지",
    ],
    "국제정세": [
        "미중무역전쟁", "관세", "무역분쟁", "경제제재", "수출규제",
        "우크라이나", "러시아", "중동분쟁", "이스라엘",
        "공급망", "반도체", "희토류", "지정학",
    ],
    "증시_금융": [
        "코스피", "코스닥", "나스닥", "S&P500", "다우존스",
        "주가폭락", "증시급락", "주식시장", "IPO", "상장",
        "외국인매도", "외국인순매수", "기관투자자",
        "회사채", "국채", "채권금리", "신용스프레드",
    ],
    "정책_규제": [
        "정부정책", "재정정책", "통화정책", "경기부양",
        "금융규제", "핀테크규제", "암호화폐규제", "디지털화폐", "CBDC",
        "세제개편", "법인세", "부동산정책",
    ],
    "기업_실적": [
        "삼성전자", "SK하이닉스", "현대차", "기업실적", "영업이익",
        "매출증가", "매출감소", "구조조정", "감원", "파산",
        "M&A", "인수합병", "지주사", "분사",
    ],
}

# ──────────────────────────────────────────────
# 영어 키워드 (글로벌 이벤트 보완)
# ──────────────────────────────────────────────
EN_KEYWORD_GROUPS = {
    "macro": [
        "interest rate hike", "interest rate cut", "Federal Reserve", "Fed rate",
        "inflation", "CPI", "PPI", "GDP growth", "recession", "stagflation",
        "dollar index", "currency crisis", "exchange rate",
        "ECB", "Bank of Japan", "Bank of Korea",
    ],
    "commodities": [
        "crude oil", "WTI", "Brent", "OPEC production cut",
        "natural gas", "LNG price", "copper price", "lithium",
        "grain price", "wheat", "food inflation",
    ],
    "geopolitics": [
        "US China trade war", "tariff", "economic sanctions", "export controls",
        "Ukraine war commodity", "Middle East conflict oil",
        "supply chain disruption", "semiconductor shortage",
    ],
    "markets": [
        "stock market crash", "market selloff", "S&P 500", "KOSPI",
        "corporate earnings", "earnings beat", "earnings miss",
        "bond yield", "credit spread", "emerging market",
    ],
}

# ──────────────────────────────────────────────
# 제외 도메인 (광고·스팸·저품질)
# ──────────────────────────────────────────────
EXCLUDE_DOMAINS = [
    "blogspot.com", "wordpress.com", "tistory.com",  # 개인 블로그 (저품질)
    "naver.me", "me2.do",  # 단축 URL
    "ad.co.kr", "ad.kr",   # 광고
    # 영어 매체 (GDELT 언어 분류 오류 케이스)
    "koreatimes.com", "koreaherald.com", "arirang.com",
    # 뉴스 어그리게이터 (원본 중복 위험)
    "news.nate.com", "news.naver.com", "news.daum.net",
    "v.daum.net", "n.news.naver.com",
]

# ──────────────────────────────────────────────
# 우선 포함 도메인 (신뢰 매체)
# ──────────────────────────────────────────────
PREFERRED_DOMAINS_KR = [
    # 경제 전문
    "mk.co.kr", "hankyung.com", "edaily.co.kr",
    "thebell.co.kr", "fn.co.kr", "fnnews.com",
    "businesspost.co.kr", "sedaily.com",
    # 종합 일간지
    "chosun.com", "joongang.co.kr", "donga.com",
    "hani.co.kr", "kyunghyang.com",
    # 방송
    "yonhapnews.co.kr", "yna.co.kr",
    "kbs.co.kr", "mbc.co.kr", "jtbc.co.kr",
]

PREFERRED_DOMAINS_EN = [
    "reuters.com", "bloomberg.com", "ft.com",
    "wsj.com", "nytimes.com", "economist.com",
    "cnbc.com", "marketwatch.com", "apnews.com",
]

# ──────────────────────────────────────────────
# 중복 제거 설정
# ──────────────────────────────────────────────
DEDUP_CONFIG = {
    "title_similarity_threshold": 0.85,   # 제목 유사도 임계값 (cosine)
    "time_window_hours": 48,              # 동일 이벤트 간주 시간 창
    "url_normalize": True,               # URL 정규화 후 비교
}

# ──────────────────────────────────────────────
# 출력 설정
# ──────────────────────────────────────────────
OUTPUT_CONFIG = {
    "format": "parquet",          # parquet | csv | jsonl
    "partition_by": "year_month", # 파티션 기준
    "output_dir": "./output",
    "log_dir": "./logs",
    "chunk_size": 100_000,        # 파일당 최대 레코드 수
}

# ──────────────────────────────────────────────
# BigQuery 설정
# ──────────────────────────────────────────────
BQ_CONFIG = {
    "project_id": "YOUR_GCP_PROJECT_ID",    # ← 변경 필요
    "dataset": "gdelt_collected",
    "gkg_table": "gdelt-bq.gdeltv2.gkg",
    "events_table": "gdelt-bq.gdeltv2.events",
    "mentions_table": "gdelt-bq.gdeltv2.eventmentions",
    "location": "US",
}