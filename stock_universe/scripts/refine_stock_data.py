"""
stock_universe 주식 데이터 정제 스크립트
============================================
Google Drive `raw/` 폴더의 FnGuide DataGuide 원본을 ARCHITECTURE.md(IISE-CD-StockGame
레포, 섹션 6~7) DB 스키마에 맞춰 assets / stock_financials / stock_valuation /
stock_price_detail(+공매도) CSV로 변환한다. bond_universe, crypto_universe와 동일한
파이프라인 컨벤션(코드는 GitHub, 데이터는 Drive로 분리 관리)을 따른다.

실행 방법:
  cd stock_universe
  python scripts/refine_stock_data.py
raw xlsx 원본은 Drive `raw/` 폴더에서 받아 `stock_universe/data/raw/`에 넣고 실행한다.
결과물(`stock_universe/data/processed/*.csv`)은 Drive
`캡스톤디자인/Data/processed/stock_universe/`에 업로드한다(코드만 git 추적, `data/`는
.gitignore 대상).

★ 데이터 소스 (2026-07-01 기준, data/raw/ 8개 파일 전부 확보 완료) ★
  - 2013주식최종.xlsx (562KB)  — 시트 '주식 섹터' = 종목명→코드 매핑용(전체 시장 커버,
    STEP1에서만 사용). 이 시트의 업종 컬럼은 2013-12-30 시점 스냅샷이라 그 이후
    상장/분할 종목은 비어 있어 assets의 sector로는 쓰지 않는다(아래 Fin_stock 참고).
  - stock_financial.xlsx (937KB) — 연도별 12개 시트('2012'~'2023'), 분기 재무제표
  - index_total.xlsx (360KB) — 반기별 22개 시트('13-1'~'23-2'), 밸류에이션/재무비율
  - Fin_stock.xlsx (56.6MB) — 5년 단위 9개 시트('80-84'~'20-23') + 'Code' 시트.
    5년 단위 시트: 1980~2023 종가/거래량/유동주식수/시가총액/매출액/외국인·기관·개인
    순매수수량 결합 데이터. stock_price_detail의 주 소스(가장 넓은 커버리지 + 가장
    많은 컬럼). 'Code' 시트: 종목당 1행, 'Current'(최신) 기준 FnGuide Industry Group
    을 담고 있어 유니버스 117개 전원 커버(결측 0) — assets.sector의 유일한 소스.
  - stock_price-volume_npq.xlsx (10.9MB) — 2013~2017 종가/거래량. Fin_stock.xlsx가
    있으면 쓰지 않고, 없을 때만 폴백으로 사용.
  - 2020stock.xlsx (424KB) — 2020년 종가/거래량. 위와 동일하게 폴백 전용.
  - stock-short-selling.xlsx (6.5MB) — 3개 시트(13-17/18-22/23), 차입공매도
    금액/잔고금액/잔고비율. ARCHITECTURE.md DDL에는 없는 컬럼이라 stock_price_detail에
    참고용으로 추가 병합만 한다.
  - tenST.xlsx (4.9MB) — 유니버스 선정 당시 참고용 반기 스냅샷. 사용하지 않는다.

Fin_stock.xlsx / stock_price-volume_npq.xlsx는 Google Drive 커넥터의 10MB 다운로드
제한에 걸려 초기에는 받지 못했다. 사용자가 Drive에서 직접 다운로드해 data/raw/에
넣어준 뒤(56.6MB, 10.9MB 그대로) 최종 실행까지 완료했다.

필요 패키지: pandas, openpyxl (pip install pandas openpyxl)
"""

import io
import re
import sys
import difflib
from pathlib import Path

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ----------------------------------------------------------------------------
# 0. 설정
# 실행 위치: stock_universe/ 에서 `python scripts/refine_stock_data.py`로 실행한다
# (bond_universe, crypto_universe와 동일하게 유니버스 폴더 기준 상대경로).
# ----------------------------------------------------------------------------
RAW_DIR = Path("data/raw")
OUT_DIR = Path("data/processed")
OUT_DIR.mkdir(parents=True, exist_ok=True)

F_SECTOR = RAW_DIR / "2013주식최종.xlsx"              # 시트 '주식 섹터'
F_FINANCIAL = RAW_DIR / "stock_financial.xlsx"         # 시트: 연도별(2012~2023)
F_VALUATION = RAW_DIR / "index_total.xlsx"             # 시트: 반기별(13-1~23-2)
F_PRICE_2020 = RAW_DIR / "2020stock.xlsx"              # 단일 시트, 2020년 종가/거래량
F_PRICE_NPQ = RAW_DIR / "stock_price-volume_npq.xlsx"  # 2013~2017 종가/거래량 (10MB 초과, 수동 다운로드 필요)
F_SHORT = RAW_DIR / "stock-short-selling.xlsx"         # 시트: 13-17/18-22/23_short-selling
F_FIN_STOCK = RAW_DIR / "Fin_stock.xlsx"                # 1980~2023 결합 데이터 (56MB, 수동 다운로드 필요)
                                                         # + 'Code' 시트: 최신(Current) 업종(FnGuide Industry Group),
                                                         #   유니버스 117개 전원 커버(결측 0)

# 사용자가 확정한 117개 종목 (게임 유니버스)
SELECTED_NAMES = [
    "SK하이닉스","리노공업","DB하이텍","한미반도체","LG","한국앤컴퍼니","기아",
    "한국타이어앤테크놀로지","롯데정밀화학","고려아연","한솔케미칼","DL","SK케미칼",
    "금호석유화학","OCI홀딩스","에스원","NICE평가정보","현대로템","한화에어로스페이스",
    "키움증권","한국금융지주","미래에셋증권","메리츠금융지주","SK텔레콤","코웨이",
    "F&F홀딩스","F&F","글로벌텍스프리","우리기술투자","삼성카드","삼성생명","KT&G","SK",
    "한전기술","SK디스커버리","GS","한국토지신탁","SK디앤디","강원랜드","골프존",
    "티와이홀딩스","엔씨소프트 (NC)","펄어비스","NAVER","크래프톤","덕산네오룩스",
    "미래컴퍼니","이녹스첨단소재","비덴트","스튜디오드래곤","JYP Ent.","신한지주",
    "카카오뱅크","삼성바이오로직스","셀트리온","SK바이오사이언스","이마트","호텔신라",
    "BGF리테일","한전KPS","한국전력","한국가스공사","현대글로비스","HMM","한진칼","고영",
    "케이엠더블유","솔브레인홀딩스","천보","삼성전기","더블유씨피","HLB","클래시스","씨젠",
    "에스디바이오센서","LG생활건강","케어젠","KCC","삼성물산","효성","현대차","HL홀딩스",
    "롯데케미칼","BGF","한국항공우주","삼성증권","쿠쿠홀딩스","큐캐피탈","S-Oil","카카오",
    "컴투스","서울반도체","아바텍","토비스","톱텍","비아트론","HB테크놀러지","스카이라이프",
    "이노션","제일기획","KT나스미디어","현대백화점","지역난방공사","제주항공","팬오션",
    "대한항공","파트론","슈프리마에이치큐","슈피겐코리아","삼성SDI","아이센스","인바디",
    "디오","뷰웍스","파마리서치","현대퓨처넷","아모레퍼시픽",
]
assert len(SELECTED_NAMES) == 117, f"117개가 아니라 {len(SELECTED_NAMES)}개입니다. 리스트를 다시 확인하세요."

# 이름 표기 차이 수동 보정 (FnGuide 코드명 기준)
# 주의: "엔씨소프트 (NC)"는 override 없이 괄호 제거 fallback만으로 "엔씨소프트"(A036570)에
# 정확히 매칭된다. 과거에 "NC"로 override했더니 fuzzy-match가 무관한 "NPC"(A004250, 화학)에
# 잘못 매칭되는 버그가 있었다 — 실제 FnGuide 코드명은 "NC"가 아니라 "엔씨소프트"였다.
NAME_OVERRIDES = {}


# ----------------------------------------------------------------------------
# 1. FnGuide 시트 공용 리더
# ----------------------------------------------------------------------------
def read_fnguide_sheet(path: Path, sheet_name=0) -> pd.DataFrame:
    """FnGuide DataGuide 내보내기 특유의 상단 메타데이터 행들을 건너뛰고,
    'A열=코드, B열=코드명'인 행을 헤더로 잡아 그 아래 데이터만 DataFrame으로 반환.
    헤더 행 바로 위 행에 지표명이 별도로 있는 경우(예: stock_financial.xlsx)도 병합한다."""
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=str)

    hdr_idx = None
    for i in range(min(30, len(raw))):
        if str(raw.iat[i, 0]).strip() == "코드" and str(raw.iat[i, 1]).strip() == "코드명":
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError(f"{path.name}[{sheet_name}]: '코드/코드명' 헤더 행을 30행 이내에서 찾지 못했습니다.")

    header_row = raw.iloc[hdr_idx].tolist()
    if hdr_idx > 0:
        above_row = raw.iloc[hdr_idx - 1].tolist()
        merged = []
        for h, a in zip(header_row, above_row):
            h = "" if h is None else str(h).strip()
            a = "" if a is None else str(a).strip()
            merged.append(h if h and h != "nan" else a)
        header_row = merged

    data = raw.iloc[hdr_idx + 1:].copy()
    data.columns = [str(c) if c and str(c) != "nan" else f"col_{i}" for i, c in enumerate(header_row)]
    data = data.dropna(subset=["코드"]).reset_index(drop=True)
    return data


def read_all_sheets(path: Path, reader=read_fnguide_sheet) -> dict:
    """워크북의 모든 시트를 {시트명: DataFrame} 으로 반환."""
    xl = pd.ExcelFile(path)
    return {s: reader(path, sheet_name=s) for s in xl.sheet_names}


def _to_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")


# ----------------------------------------------------------------------------
# 2. STEP 1 — 종목명 → 코드 매핑 (2013주식최종.xlsx '주식 섹터' 시트는 전체 시장 커버)
# ----------------------------------------------------------------------------
def build_universe():
    sector_df = read_fnguide_sheet(F_SECTOR, sheet_name="주식 섹터")
    sector_df["_norm"] = sector_df["코드명"].astype(str).str.strip()
    lookup = dict(zip(sector_df["_norm"], sector_df.index))

    rows, unmatched = [], []
    for name in SELECTED_NAMES:
        query = NAME_OVERRIDES.get(name, name).strip()
        idx = lookup.get(query)
        if idx is None:
            stripped = re.sub(r"\s*\(.*?\)", "", query).strip()
            idx = lookup.get(stripped)
        if idx is None:
            candidates = difflib.get_close_matches(query, sector_df["_norm"].tolist(), n=1, cutoff=0.7)
            if candidates:
                idx = lookup[candidates[0]]

        if idx is None:
            unmatched.append(name)
            continue

        r = sector_df.loc[idx]
        rows.append({
            "input_name": name,
            "code": r["코드"],
            "matched_name": r["코드명"],
            "corp_name_kr": r.get("기업한글명", r["코드명"]),
            "fnguide_industry": r.get("FnGuide Industry", ""),
            "fnguide_industry_code": r.get("FnGuide Industry Code", ""),
        })

    universe = pd.DataFrame(rows)
    if unmatched:
        print(f"[경고] 자동 매칭 실패 {len(unmatched)}건 — 2013주식최종.xlsx를 열어 수기 확인 필요:")
        for n in unmatched:
            print(f"   - {n}")
    print(f"[STEP1] 매칭 성공 {len(universe)} / {len(SELECTED_NAMES)}")

    # 2013주식최종.xlsx는 2013-12-30 시점 스냅샷이라 그 이후 상장/분할된 종목(카카오뱅크,
    # 크래프톤, SK바이오사이언스 등)은 업종이 빈다. Fin_stock.xlsx의 'Code' 시트는 유니버스
    # 117개 전원을 'Current'(최신) 기준 FnGuide Industry Group으로 커버하므로(결측 0), 이걸로
    # 업종을 전량 교체해 소스를 하나로 통일하고 분류 레벨도 117개 전부 동일하게 맞춘다.
    fin_stock_sector = _load_fin_stock_sector()
    if fin_stock_sector is not None:
        before_missing = int(universe["fnguide_industry"].isna().sum())
        universe["fnguide_industry"] = universe["code"].map(lambda c: fin_stock_sector.get(c, (None, None))[0])
        universe["fnguide_industry_code"] = universe["code"].map(lambda c: fin_stock_sector.get(c, (None, None))[1])
        after_missing = int(universe["fnguide_industry"].isna().sum())
        print(f"[STEP1] Fin_stock.xlsx 'Code' 시트로 업종 전량 교체 (2013주식최종 결측 {before_missing}건"
              f" -> Fin_stock 적용 후 결측 {after_missing}건)")
    else:
        print(f"[STEP1][경고] {F_FIN_STOCK.name} 없음 — 2013주식최종.xlsx 업종을 그대로 사용"
              f" (결측 {int(universe['fnguide_industry'].isna().sum())}건)")
    return universe


def _load_fin_stock_sector() -> dict | None:
    """Fin_stock.xlsx 'Code' 시트에서 {코드: (FnGuide Industry Group, FnGuide Industry Group Code)}
    조회 테이블을 만든다. 이 시트는 일별 가격 시트들과 달리 종목당 한 행이며 'Current' 기준
    업종 분류를 담고 있다."""
    if not F_FIN_STOCK.exists():
        return None
    code_df = read_fnguide_sheet(F_FIN_STOCK, sheet_name="Code")
    return {
        r["코드"]: (r.get("FnGuide Industry Group"), r.get("FnGuide Industry Group Code"))
        for _, r in code_df.iterrows()
    }


# ----------------------------------------------------------------------------
# 3. STEP 2 — assets.csv
# ----------------------------------------------------------------------------
def build_assets(universe: pd.DataFrame):
    assets = pd.DataFrame({
        "asset_id": "STOCK_" + universe["code"].astype(str),
        "code": universe["code"],
        "name": universe["corp_name_kr"],
        "masked_name": None,          # maskingService에서 마지막 단계에 채움
        "asset_type": "stock",
        "sector": universe["fnguide_industry"],
        "sector_code": universe["fnguide_industry_code"],
        "currency": "KRW",
        "is_active": True,
        "is_masked": False,
    })
    print(f"[STEP2] assets 생성 ({len(assets)}행, sector 결측 {assets['sector'].isna().sum()}건 "
          f"— universe 단계에서 이미 보강된 값)")
    assets.to_csv(OUT_DIR / "assets.csv", index=False, encoding="utf-8-sig")
    print(f"[STEP2] assets.csv 저장 ({len(assets)}행)")
    return assets


# ----------------------------------------------------------------------------
# 4. STEP 3 — stock_financials.csv
#    stock_financial.xlsx는 '2012'~'2023' 12개 시트로 나뉘어 있다(연도=시트).
#    각 시트를 모두 읽어 합친 뒤, 코드/회계연도/분기 컬럼 기준으로 반기 집계한다.
# ----------------------------------------------------------------------------
FLOW_ITEMS = ["매출액(천원)", "영업이익(천원)", "당기순이익(천원)"]     # 반기 = 두 분기 합산
BALANCE_ITEMS = ["현금및현금성자산(천원)", "부채총계(천원)", "재고자산(천원)"]  # 반기 = 반기말(2Q/4Q) 시점값

FIELD_MAP = {
    "매출액(천원)": "revenue",
    "영업이익(천원)": "operating_income",
    "당기순이익(천원)": "net_income",
    "현금및현금성자산(천원)": "cash_equivalents",
    "부채총계(천원)": "total_debt",
    "재고자산(천원)": "inventory",
}


def build_stock_financials(universe: pd.DataFrame):
    sheets = read_all_sheets(F_FINANCIAL)
    print(f"[STEP3] stock_financial.xlsx 시트 {len(sheets)}개(연도별) 병합: {list(sheets.keys())}")
    fin = pd.concat(sheets.values(), ignore_index=True)
    fin = fin[fin["코드"].isin(universe["code"])].copy()

    for col in FLOW_ITEMS + BALANCE_ITEMS:
        if col not in fin.columns:
            print(f"[STEP3][경고] 컬럼 '{col}' 이(가) 없습니다. 실제 헤더: {list(fin.columns)}")

    fin["회계연도"] = _to_num(fin["회계연도"]).astype("Int64")
    fin["half"] = fin["분기"].map({"1Q": 1, "2Q": 1, "3Q": 2, "4Q": 2})
    fin = fin.dropna(subset=["half"])

    records = []
    for (code, year, half), g in fin.groupby(["코드", "회계연도", "half"]):
        g = g.sort_values("분기")
        rec = {"asset_id": "STOCK_" + str(code), "fiscal_year": year, "half": half}
        for col in FLOW_ITEMS:
            rec[FIELD_MAP[col]] = _to_num(g[col]).sum(min_count=1) if col in g else None
        for col in BALANCE_ITEMS:
            rec[FIELD_MAP[col]] = _to_num(g[col]).iloc[-1] if col in g and len(g) else None
        records.append(rec)

    out = pd.DataFrame(records).sort_values(["asset_id", "fiscal_year", "half"])
    out.to_csv(OUT_DIR / "stock_financials.csv", index=False, encoding="utf-8-sig")
    print(f"[STEP3] stock_financials.csv 저장 ({len(out)}행, 반기 집계 완료 — "
          f"{out['fiscal_year'].min()}~{out['fiscal_year'].max()})")
    return out


# ----------------------------------------------------------------------------
# 5. STEP 4 — stock_valuation.csv
#    index_total.xlsx는 '13-1'~'23-2' 22개 시트(반기 스냅샷)로 나뉘어 있다.
#    시트명 자체가 (연도 뒤 2자리)-(반기)이므로 시트명에서 fiscal_year/half를 파싱한다.
# ----------------------------------------------------------------------------
VALUATION_MAP = {
    "매출액증가율(TTM, YoY)(%)": "revenue_growth",
    "영업이익률(TTM)(%)": "op_margin",
    "부채비율(%)": "debt_ratio",
    "순이익률(지배,TTM)(%)": "net_margin",
    "ROE(TTM)(%)": "roe",
    "ROA(%)": "roa",
    "PER(IFRS-연결)": "per",
    "PBR(IFRS-연결)": "pbr",
    "PSR(IFRS-연결)": "psr",
    "EV/EBITDA(TTM)(배)": "ev_ebitda",
    "EPS(지배, TTM)(원)": "eps",
    "BPS(지배)(원)": "bps",
    "SPS(TTM)(원)": "sps",
    "시가총액(백만원)": "market_cap",
}

SHEET_PERIOD_RE = re.compile(r"^(\d{2})-(\d)$")


def build_stock_valuation(universe: pd.DataFrame):
    xl = pd.ExcelFile(F_VALUATION)
    frames = []
    for sheet in xl.sheet_names:
        m = SHEET_PERIOD_RE.match(sheet)
        if not m:
            print(f"[STEP4][경고] 시트명 '{sheet}'에서 fiscal_year/half를 파싱하지 못해 건너뜁니다.")
            continue
        fiscal_year = 2000 + int(m.group(1))
        half = int(m.group(2))

        val = read_fnguide_sheet(F_VALUATION, sheet_name=sheet)
        val = val[val["코드"].isin(universe["code"])].copy()

        out = pd.DataFrame({"asset_id": "STOCK_" + val["코드"].astype(str)})
        out["fiscal_year"] = fiscal_year
        out["half"] = half
        for src, dst in VALUATION_MAP.items():
            out[dst] = _to_num(val[src]) if src in val.columns else None
        frames.append(out)

    result = pd.concat(frames, ignore_index=True).sort_values(["asset_id", "fiscal_year", "half"])
    result.to_csv(OUT_DIR / "stock_valuation.csv", index=False, encoding="utf-8-sig")
    print(f"[STEP4] stock_valuation.csv 저장 ({len(result)}행, {len(xl.sheet_names)}개 반기 스냅샷 병합"
          f" — {result['fiscal_year'].min()}~{result['fiscal_year'].max()})")
    return result


# ----------------------------------------------------------------------------
# 6. STEP 5 — 가격/거래량 (+ 공매도) — wide-matrix 시트 melt
#    2020stock.xlsx / stock_price-volume_npq.xlsx / stock-short-selling.xlsx는
#    모두 같은 레이아웃이다:
#      행: Refresh, 달력기준, 코드 포트폴리오, 아이템 포트폴리오, 출력주기,
#          비영업일, 주말포함, 기간, 코드, 코드명, 유형, 아이템코드, 아이템명,
#          집계주기, (날짜별 데이터...)
#      열: A열=행 라벨, B열부터 종목별 아이템이 반복(종가/거래량 등 n개 컬럼씩)
#    '코드'/'아이템명' 행과 '집계주기' 다음 행부터의 날짜 인덱스를 이용해
#    long-format으로 melt한 뒤 종목×날짜×아이템으로 pivot한다.
# ----------------------------------------------------------------------------
def _find_label_row(raw: pd.DataFrame, label: str, search_rows=30) -> int:
    for i in range(min(search_rows, len(raw))):
        if str(raw.iat[i, 0]).strip() == label:
            return i
    raise ValueError(f"'{label}' 행을 상단 {search_rows}행 이내에서 찾지 못했습니다.")


def melt_fnguide_wide_sheet(path: Path, sheet_name, universe_codes: set) -> pd.DataFrame:
    """코드/아이템명이 열 방향으로 반복되는 FnGuide 일별 wide 시트를
    (asset_id, trade_date, item, value) long-format으로 변환."""
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=str)

    code_row = _find_label_row(raw, "코드")
    item_row = _find_label_row(raw, "아이템명")
    freq_row = _find_label_row(raw, "집계주기")
    data_start = freq_row + 1

    codes = raw.iloc[code_row, 1:].tolist()
    items = raw.iloc[item_row, 1:].tolist()
    dates = raw.iloc[data_start:, 0]
    values = raw.iloc[data_start:, 1:]

    records = []
    for col_pos in range(values.shape[1]):
        code = codes[col_pos]
        item = items[col_pos]
        if pd.isna(code) or pd.isna(item) or code not in universe_codes:
            continue
        col_vals = values.iloc[:, col_pos]
        for date_val, v in zip(dates, col_vals):
            if pd.isna(v) or pd.isna(date_val):
                continue
            records.append((code, date_val, item, v))

    if not records:
        return pd.DataFrame(columns=["asset_id", "trade_date", "item", "value"])

    long_df = pd.DataFrame(records, columns=["code", "trade_date", "item", "value"])
    long_df["asset_id"] = "STOCK_" + long_df["code"].astype(str)
    long_df["trade_date"] = pd.to_datetime(long_df["trade_date"]).dt.date
    long_df["value"] = _to_num(long_df["value"])
    return long_df[["asset_id", "trade_date", "item", "value"]]


PRICE_ITEM_MAP = {
    "종가(원)": "close_price",
    "거래량(주)": "volume",
}

SHORT_ITEM_MAP = {
    "차입공매도금액(원)": "short_sale_amount",
    "차입공매도잔고금액(백만원)": "short_balance_amount",
    "차입공매도잔고비율(%)": "short_balance_ratio",
}

# Fin_stock.xlsx (1980~2023, 시트 9개: 80-84~20-23 + 'Code')는 위 price/short 파일과
# 동일한 wide-matrix 레이아웃이며 종목당 8개 아이템이 반복된다:
#   거래량(주), 유동주식수(주), 시가총액(백만원), 매출액(천원, 결산주기),
#   종가(원), 외국인총합계 순매수수량(일간)(주), 기관 순매수수량(일간)(주), 개인 순매수수량(일간)(주)
# 매출액은 결산(반기) 주기라 stock_financials.csv(반기 재무제표)와 중복되므로 여기서는 제외한다.
# '순매수수량'은 매수-매도 순증감이며, ARCHITECTURE.md DDL의 foreign_qty/inst_qty/indiv_qty에
# 대응시키되 실제 의미는 "총매수수량"이 아니라 "순매수수량"임을 유의해야 한다.
FIN_STOCK_ITEM_MAP = {
    "종가(원)": "close_price",
    "거래량(주)": "volume",
    "유동주식수(주)": "shares_outstanding",
    "시가총액(백만원)": "market_cap",
    "외국인총합계 순매수수량(일간)(주)": "foreign_qty",
    "기관 순매수수량(일간)(주)": "inst_qty",
    "개인 순매수수량(일간)(주)": "indiv_qty",
}


def _pivot_items(long_df: pd.DataFrame, item_map: dict) -> pd.DataFrame:
    long_df = long_df[long_df["item"].isin(item_map)].copy()
    long_df["field"] = long_df["item"].map(item_map)
    wide = long_df.pivot_table(
        index=["asset_id", "trade_date"], columns="field", values="value", aggfunc="first"
    ).reset_index()
    wide.columns.name = None
    return wide


def _melt_all_sheets(path: Path, codes: set, item_map: dict, exclude_sheets=()) -> pd.DataFrame:
    xl = pd.ExcelFile(path)
    frames = []
    for sheet in xl.sheet_names:
        if sheet in exclude_sheets:
            continue
        long_df = melt_fnguide_wide_sheet(path, sheet, codes)
        frames.append(_pivot_items(long_df, item_map))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["asset_id", "trade_date"]).drop_duplicates(["asset_id", "trade_date"])


def build_stock_price_detail(universe: pd.DataFrame):
    codes = set(universe["code"])

    if F_FIN_STOCK.exists():
        # Fin_stock.xlsx가 1980~2023 전체를 커버하는 가장 완전한 소스이므로 이를 기준으로 삼는다.
        price = _melt_all_sheets(F_FIN_STOCK, codes, FIN_STOCK_ITEM_MAP, exclude_sheets={"Code"})
        print(f"[STEP5] {F_FIN_STOCK.name} 처리 완료 (종가/거래량/유동주식/시가총액/수급 포함, {len(price)}행)")
    else:
        # Fin_stock이 없으면 종가/거래량만이라도 2020stock.xlsx + stock_price-volume_npq.xlsx로 채운다.
        price_frames = []
        for f in (F_PRICE_2020, F_PRICE_NPQ):
            if f.exists():
                price_frames.append(_melt_all_sheets(f, codes, PRICE_ITEM_MAP))
                print(f"[STEP5] {f.name} 처리 완료 (종가/거래량만)")
            else:
                print(f"[STEP5][안내] {f.name} 없음 — 건너뜁니다.")
        if not price_frames:
            print("[STEP5] 가격 데이터 없음 — stock_price_detail.csv 생성 생략")
            return None
        price = pd.concat(price_frames, ignore_index=True)
        price = price.sort_values(["asset_id", "trade_date"]).drop_duplicates(["asset_id", "trade_date"])
        print(f"[STEP5][안내] {F_FIN_STOCK.name} 없음(56.6MB, 10MB 제한으로 자동 다운로드 불가) — "
              f"open/high/low, foreign_qty/inst_qty/indiv_qty, shares_outstanding, market_cap 컬럼이 빠졌습니다.")

    if F_SHORT.exists():
        short = _melt_all_sheets(F_SHORT, codes, SHORT_ITEM_MAP)
        price = price.merge(short, on=["asset_id", "trade_date"], how="left")
        print(f"[STEP5] {F_SHORT.name} 공매도 데이터 병합 완료 ({len(short)}행)")
    else:
        print(f"[STEP5][경고] {F_SHORT.name} 없음 — 공매도 컬럼 없이 저장합니다.")

    price.to_csv(OUT_DIR / "stock_price_detail.csv", index=False, encoding="utf-8-sig")
    print(f"[STEP5] stock_price_detail.csv 저장 ({len(price)}행, "
          f"{price['trade_date'].min()}~{price['trade_date'].max()})")
    print("[STEP5][참고] open_price/high_price/low_price는 원본 raw 데이터 어디에도 없어 이번에도 "
          "비어 있다(FnGuide DataGuide 원천 자체가 종가만 제공). 필요하면 별도 시세 소스를 추가해야 한다.")
    return price


# ----------------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    universe = build_universe()
    universe.to_csv(OUT_DIR / "_name_code_mapping.csv", index=False, encoding="utf-8-sig")

    assets = build_assets(universe)
    financials = build_stock_financials(universe)
    valuation = build_stock_valuation(universe)
    price_detail = build_stock_price_detail(universe)

    print("\n완료. data/processed/ 폴더에서 assets.csv, stock_financials.csv, "
          "stock_valuation.csv, stock_price_detail.csv, _name_code_mapping.csv 를 확인하세요.")
