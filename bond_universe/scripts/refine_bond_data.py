"""
bond_universe 채권 데이터 정제 스크립트
============================================
Google Drive `Data/raw/bond_universe/` 원본을 ARCHITECTURE.md(IISE-CD-StockGame 레포,
섹션 6~7) DB 스키마에 맞춰 bond_price_detail / asset_prices CSV로 변환한다.
stock_universe와 동일한 파이프라인 컨벤션(코드는 GitHub, 데이터는 Drive로 분리 관리).

실행 방법:
  cd bond_universe
  python scripts/refine_bond_data.py
raw xlsx 원본은 Drive `Data/raw/bond_universe/`에서 받아 `bond_universe/data/raw/`에 넣는다.
결과물(`bond_universe/data/processed/*.csv`)은 Drive
`캡스톤디자인/Data/processed/bond_universe/`에 업로드한다.

★ 채권 4종 매핑 (ARCHITECTURE.md 시드 기준) ★
  - BOND_CORPAAA ← corporate_bonds.xlsx AAA1/AAA2 시트 (KIS 회사채AAA 지수)
  - BOND_CORPBBB ← corporate_bonds.xlsx BBB1/BBB2 시트 (KIS 회사채BBB 지수)
  - BOND_KTB3Y   ← bond_final.xlsx(원본명 채권최종.xlsx) 채권tot1/tot2의 A114260
                   (KODEX 국고채3년 ETF)
  - BOND_KTB10Y  ← 같은 시트의 A148070 (KIWOOM 국고채10년 ETF)

★ 데이터 소스 ★
  - corporate_bonds.xlsx (281KB, KIS 자산평가 채권지수) — 시트 AAA1/AAA2/BBB1/BBB2
    (등급 × 2기간: 2013-12-30~2018-12-31, 2019-01-02~2023-12-31).
    컬럼: 일자, 총수익지수, 순가격지수, Call가격지수, 평균듀레이션, 평균컨벡서티,
    평균YTM, 종목수, 발행잔액. 섹션 6 "회사채는 총수익지수" 결정에 따라
    price_index = 총수익지수, yield_rate = 평균YTM 을 쓴다.
  - 채권최종.xlsx (3.1MB) → 로컬 저장명 bond_final.xlsx (한글 파일명 인코딩 문제 회피).
    쓰는 시트는 '채권tot1'/'채권tot2'(국고채 ETF 일별 수정시가/거래량/시가총액,
    stock_universe와 동일한 FnGuide wide-matrix 포맷) 뿐이다.
    나머지 시트(키움_통1Y, KODEX_3Y, 키움_국10Y, 회사채* = ETF 구성종목 내역)는
    가격 정제에 불필요해서 쓰지 않는다.

★ 주의사항 (백엔드/DB 공유 필요) ★
  1. assets / bond_info 는 만들지 않는다 — ARCHITECTURE.md 001_init.sql이 채권 4종을
     직접 INSERT 시드하므로 (BOND_KTB3Y '국채 단기' 등), CSV로 중복 생성하면 적재 시
     PK 충돌 위험만 생긴다.
  2. 국고채 가격은 "수익률→가격지수 변환"(섹션 6) 대신 실제 국고채 ETF의 수정시가를
     그대로 쓴다. ETF 가격이 곧 시장이 계산해준 가격지수이며(쿠폰 재투자 반영),
     변환 모델을 자체 구현하는 것보다 정확하다.
  3. 원본 아이템이 수정'시가'(원)라 종가가 아니라 시가 기준이다. 일간 봉에서 채권
     ETF의 시가/종가 차이는 미미해 게임용으로는 무방하나, close_price 컬럼에
     들어가는 값이 실제로는 당일 시가라는 점은 알고 있어야 한다.
  4. 국고채(KTB3Y/KTB10Y)의 yield_rate는 이 원본에 없어 NULL이다. 필요하면
     market_indicator의 국고채 금리(ktb_yield, get_kr_bond_rates.py)와 날짜 조인으로
     보강할 수 있다. 회사채(CORPAAA/CORPBBB)는 평균YTM이 있어 채워진다.
  5. 통안채1년(A122260)과 RISE 중기우량회사채(A136340) 데이터도 원본에 있지만
     아키텍처 채권 4종에 없으므로 쓰지 않는다.
  6. 채권 데이터는 2013-12-30부터 시작한다(주식은 1979년부터). GAME_START_RANGE가
     2013-01-01부터라면 2013년 시작 세션에서 채권 가격이 없는 구간이 생긴다 —
     시작일 선택(turnSelector)에서 2014-01-01 이후로 제한하거나 채권 표시를
     막아야 한다.

필요 패키지: pandas, openpyxl (pip install pandas openpyxl)
"""

import io
import sys
from pathlib import Path

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ----------------------------------------------------------------------------
# 0. 설정 — bond_universe/ 에서 `python scripts/refine_bond_data.py`로 실행
# ----------------------------------------------------------------------------
RAW_DIR = Path("data/raw")
OUT_DIR = Path("data/processed")
OUT_DIR.mkdir(parents=True, exist_ok=True)

F_CORP = RAW_DIR / "corporate_bonds.xlsx"   # KIS 회사채 AAA/BBB 지수
F_KTB = RAW_DIR / "bond_final.xlsx"          # 원본명 채권최종.xlsx — 국고채 ETF 시세

# 회사채: 시트 → asset_id
CORP_SHEET_MAP = {
    "AAA1": "BOND_CORPAAA",
    "AAA2": "BOND_CORPAAA",
    "BBB1": "BOND_CORPBBB",
    "BBB2": "BOND_CORPBBB",
}

# 국고채 ETF: FnGuide 코드 → asset_id (통안채1년 A122260은 유니버스 외라 제외)
KTB_CODE_MAP = {
    "A114260": "BOND_KTB3Y",    # KODEX 국고채3년
    "A148070": "BOND_KTB10Y",   # KIWOOM 국고채10년
}
KTB_PRICE_ITEM = "수정시가(원)"
KTB_SHEETS = ["채권tot1", "채권tot2"]


def _to_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")


# ----------------------------------------------------------------------------
# 1. 회사채 — KIS 지수 시트 (헤더 '일자' 행 탐지 후 그 아래가 데이터)
# ----------------------------------------------------------------------------
def read_corp_sheet(sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(F_CORP, sheet_name=sheet_name, header=None, dtype=str)
    hdr_idx = None
    for i in range(min(15, len(raw))):
        if str(raw.iat[i, 0]).strip() == "일자":
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError(f"{F_CORP.name}[{sheet_name}]: '일자' 헤더 행을 찾지 못했습니다.")

    header = [str(c).strip() for c in raw.iloc[hdr_idx].tolist()]
    data = raw.iloc[hdr_idx + 1:].copy()
    data.columns = header
    # 헤더 바로 아래 단위 행(원/%/건/천원)이 있으면 날짜 파싱 실패로 자연 제거된다
    data["trade_date"] = pd.to_datetime(data["일자"], errors="coerce").dt.date
    data = data.dropna(subset=["trade_date"])
    return pd.DataFrame({
        "trade_date": data["trade_date"],
        "price_index": _to_num(data["총수익지수"]),
        "yield_rate": _to_num(data["평균YTM"]),
    })


def build_corp_bonds() -> pd.DataFrame:
    frames = []
    for sheet, asset_id in CORP_SHEET_MAP.items():
        df = read_corp_sheet(sheet)
        df["asset_id"] = asset_id
        frames.append(df)
        print(f"[STEP1] {F_CORP.name}[{sheet}] -> {asset_id} ({len(df)}행)")
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["asset_id", "trade_date"]).drop_duplicates(["asset_id", "trade_date"])


# ----------------------------------------------------------------------------
# 2. 국고채 — 채권tot 시트 (stock_universe와 동일한 wide-matrix melt)
# ----------------------------------------------------------------------------
def _find_label_row(raw: pd.DataFrame, label: str, search_rows=30) -> int:
    for i in range(min(search_rows, len(raw))):
        if str(raw.iat[i, 0]).strip() == label:
            return i
    raise ValueError(f"'{label}' 행을 상단 {search_rows}행 이내에서 찾지 못했습니다.")


def melt_ktb_sheet(sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(F_KTB, sheet_name=sheet_name, header=None, dtype=str)
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
        code, item = codes[col_pos], items[col_pos]
        if code not in KTB_CODE_MAP or str(item).strip() != KTB_PRICE_ITEM:
            continue
        for date_val, v in zip(dates, values.iloc[:, col_pos]):
            if pd.isna(v) or pd.isna(date_val):
                continue
            records.append((KTB_CODE_MAP[code], date_val, v))

    out = pd.DataFrame(records, columns=["asset_id", "trade_date", "price_index"])
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out["price_index"] = _to_num(out["price_index"])
    return out


def build_ktb_bonds() -> pd.DataFrame:
    frames = []
    for sheet in KTB_SHEETS:
        df = melt_ktb_sheet(sheet)
        frames.append(df)
        print(f"[STEP2] {F_KTB.name}[{sheet}] 처리 ({len(df)}행)")
    out = pd.concat(frames, ignore_index=True)
    out["yield_rate"] = float("nan")  # 원본에 국고채 YTM 없음 — 주의사항 4 참고
    return out.sort_values(["asset_id", "trade_date"]).drop_duplicates(["asset_id", "trade_date"])


# ----------------------------------------------------------------------------
# 3. 산출물 — bond_price_detail.csv + asset_prices.csv
# ----------------------------------------------------------------------------
def main():
    corp = build_corp_bonds()
    ktb = build_ktb_bonds()
    bonds = pd.concat([corp, ktb], ignore_index=True)
    bonds = bonds.sort_values(["asset_id", "trade_date"])

    detail = bonds[["asset_id", "trade_date", "yield_rate", "price_index"]]
    detail.to_csv(OUT_DIR / "bond_price_detail.csv", index=False, encoding="utf-8-sig")
    print(f"[STEP3] bond_price_detail.csv 저장 ({len(detail)}행, "
          f"{detail['trade_date'].min()}~{detail['trade_date'].max()} — DDL과 1:1 일치)")

    ap = bonds[["asset_id", "trade_date", "price_index"]].rename(columns={"price_index": "close_price"})
    ap = ap.dropna(subset=["close_price"])
    ap["change_rate"] = ap.groupby("asset_id")["close_price"].pct_change().round(4)
    ap["currency"] = "KRW"
    ap.to_csv(OUT_DIR / "asset_prices.csv", index=False, encoding="utf-8-sig")
    first_day_nulls = int(ap["change_rate"].isna().sum())
    print(f"[STEP4] asset_prices.csv 저장 ({len(ap)}행, change_rate 결측 {first_day_nulls}건 — "
          f"채권 4종 최초 거래일 각 1건이면 정상)")

    for aid, g in bonds.groupby("asset_id"):
        print(f"  - {aid}: {len(g)}행, {g['trade_date'].min()}~{g['trade_date'].max()}, "
              f"yield_rate 결측 {g['yield_rate'].isna().sum()}건")

    print("\n완료. data/processed/ 폴더에서 bond_price_detail.csv, asset_prices.csv 를 확인하세요.")


if __name__ == "__main__":
    main()
