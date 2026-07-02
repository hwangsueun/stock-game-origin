# bond_universe

채권 4종(국고채 3년/10년, 회사채 AAA/BBB) 데이터 정제 파이프라인. `stock_universe`와
동일한 컨벤션을 따른다 — 코드는 이 레포(GitHub)에서 추적하고, 원본/가공 데이터는
Google Drive에서 관리한다(`data/`는 `.gitignore` 대상).

## 실행 방법

```bash
cd bond_universe
mkdir -p data/raw
# Drive `Data/raw/bond_universe/`에서 아래 2개 xlsx를 data/raw/ 에 다운로드
#   corporate_bonds.xlsx  → 그대로 저장
#   채권최종.xlsx          → bond_final.xlsx 로 저장 (한글 파일명 인코딩 문제 회피)
python scripts/refine_bond_data.py
# 결과: data/processed/{bond_price_detail,asset_prices}.csv
```

## 채권 4종 매핑 (ARCHITECTURE.md 시드 기준)

| asset_id | 소스 | price_index | yield_rate |
|---|---|---|---|
| `BOND_CORPAAA` | corporate_bonds.xlsx AAA1/AAA2 (KIS 회사채AAA 지수) | 총수익지수 | 평균YTM |
| `BOND_CORPBBB` | corporate_bonds.xlsx BBB1/BBB2 (KIS 회사채BBB 지수) | 총수익지수 | 평균YTM |
| `BOND_KTB3Y` | 채권최종.xlsx 채권tot1/tot2의 A114260 (KODEX 국고채3년 ETF) | 수정시가(원) | NULL |
| `BOND_KTB10Y` | 같은 시트의 A148070 (KIWOOM 국고채10년 ETF) | 수정시가(원) | NULL |

## 산출물 → Drive 업로드 위치

`data/processed/*.csv`는 `캡스톤디자인/Data/processed/bond_universe/`에 올린다.

## ARCHITECTURE.md 대비 확인 결과 (2026-07-02)

- `bond_price_detail.csv`(9,862행, 2013-12-30~2023-12-29): `asset_id, trade_date,
  yield_rate, price_index` — DDL과 1:1 일치, PK 중복 0.
- `asset_prices.csv`(9,862행): `asset_id, trade_date, close_price, change_rate,
  currency` — close_price 결측 0, change_rate 결측 4건(채권 4종 최초 거래일 각 1건, 정상).
- **assets / bond_info CSV는 일부러 만들지 않았다** — `001_init.sql`이 채권 4종을 직접
  INSERT 시드하므로 CSV로 중복 생성하면 적재 시 PK 충돌 위험만 생긴다.
- 국고채는 "수익률→가격지수 변환"(섹션 6) 대신 실제 국고채 ETF 수정시가를 그대로
  쓴다. ETF 가격이 곧 시장이 계산한 가격지수(쿠폰 재투자 반영)라 자체 변환 모델보다
  정확하다. 단, 원본 아이템이 수정'시가'라 close_price에 들어가는 값이 실제로는
  당일 시가다(일간 채권 ETF에서 시가/종가 차이는 미미).
- 국고채 `yield_rate`는 원본에 없어 NULL — 필요 시 market_indicator의 국고채
  금리(`ktb_yield`)와 날짜 조인으로 보강 가능.
- **채권 데이터는 2013-12-30 시작**(주식은 1979년부터). `GAME_START_RANGE`가
  2013-01-01부터면 2013년 시작 세션에 채권 가격 없는 구간이 생긴다 — `turnSelector`
  시작일 하한을 2014-01-01로 두거나 해당 구간 채권 거래를 막아야 한다.
- 통안채1년(A122260), RISE 중기우량회사채(A136340) 데이터도 원본에 있으나 채권
  4종 유니버스에 없어 쓰지 않는다.
