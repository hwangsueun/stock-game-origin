# stock_universe

주식 117개 종목 데이터 정제 파이프라인. `bond_universe`, `crypto_universe`와 동일한 컨벤션을
따른다 — 코드는 이 레포(GitHub)에서 추적하고, 원본/가공 데이터는 Google Drive에서 관리한다
(`data/`는 `.gitignore` 대상).

## 실행 방법

```bash
cd stock_universe
mkdir -p data/raw
# Drive `raw/` 폴더에서 아래 6개 xlsx를 data/raw/ 에 다운로드
python scripts/refine_stock_data.py
# 결과: data/processed/{assets,stock_financials,stock_valuation,stock_price_detail,stock_short_selling}.csv
```

필요한 raw 파일은 6개다: `Fin_stock.xlsx`, `stock_financial.xlsx`, `index_total.xlsx`,
`stock_price-volume_npq.xlsx`, `2020stock.xlsx`, `stock-short-selling.xlsx`.
`2013주식최종.xlsx`, `tenST.xlsx`는 더 이상 필요 없다 — `Fin_stock.xlsx`의 'Code' 시트
하나로 종목명→코드 매칭과 업종(FnGuide Industry Group)을 모두 해결한다(자세한 이유는
스크립트 상단 docstring 참고).

`Fin_stock.xlsx`(56.6MB), `stock_price-volume_npq.xlsx`(10.9MB)는 Drive 커넥터의 10MB
다운로드 제한 때문에 브라우저에서 직접 받아야 한다.

## 산출물 → Drive 업로드 위치

`data/processed/*.csv`는 `캡스톤디자인/Data/processed/stock_universe/`에 올린다.

## ARCHITECTURE.md(IISE-CD-StockGame) 대비 확인 결과

DB 담당자가 migration/seed를 작성할 때 참고할 사항 (2026-07-02 기준, `stock_price_detail`
DDL이 OHLC 대신 `close_price`를 쓰도록 수정된 이후):

- `assets`, `stock_financials`, `stock_valuation`, `stock_price_detail`은 DDL 컬럼과
  1:1로 맞는다 (`stock_price_detail`: `asset_id, trade_date, close_price, volume,
  foreign_qty, inst_qty, indiv_qty, shares_outstanding, market_cap`, 결측 0).
- `stock_price_detail.close_price`는 공통 `asset_prices.close_price`와 같은 값이
  들어가는 **의도된 중복**이다. 거래 체결/총자산 평가는 항상 `asset_prices`를 쓰고,
  `stock_price_detail.close_price`는 종목 상세화면에서 조인 한 번 덜 하려는 편의용
  사본이다. **`asset_prices.csv`는 아직 이 스크립트에서 만들지 않았다** — 별도 작업 필요.
- `foreign_qty`/`inst_qty`/`indiv_qty`는 원본 컬럼명이 "순매수수량"이다. 매수-매도
  순증감(음수 가능)이며 "총매수수량"이 아니다. DDL 컬럼명은 그대로 써도 되지만 의미를
  혼동하지 않도록 서버 쪽에 공유가 필요하다.
- 차입공매도(`short_sale_amount`/`short_balance_amount`/`short_balance_ratio`)는
  ARCHITECTURE.md DDL에 없는 데이터라 `stock_price_detail.csv`에 안 섞고
  `stock_short_selling.csv`로 완전히 분리했다.
- `masked_name`/`is_masked`는 비워뒀다(섹션 6 마스킹은 최종 적재 단계에서 `maskingService`가
  채우는 것이 맞다).
