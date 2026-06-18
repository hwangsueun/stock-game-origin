import os
import re
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict

import pandas as pd
import requests
from dotenv import load_dotenv


@dataclass
class SeriesRule:
    output_col: str
    include_keywords: List[str]
    exclude_keywords: Optional[List[str]] = None


@dataclass
class TableTarget:
    name: str
    query: str
    output_path: str
    raw_output_path: str
    rules: List[SeriesRule]
    preferred_org_id: Optional[str] = None
    preferred_tbl_id: Optional[str] = None


class KosisApiClient:
    SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"
    DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

    def __init__(self, api_key: str, sleep_sec: float = 0.3):
        self.api_key = api_key
        self.sleep_sec = sleep_sec

    @staticmethod
    def _parse_kosis_json_like(text: str):
        """
        KOSIS는 format=json을 줘도 표준 JSON이 아니라
        [{TBL_ID:"...", ORG_ID:"..."}] 같은 JavaScript object literal 형태로 반환하는 경우가 있음.
        이를 표준 JSON으로 변환해서 파싱한다.
        """
        text = text.strip()

        if not text:
            raise RuntimeError("KOSIS 응답이 비어 있습니다.")

        # BOM 제거
        text = text.lstrip("\ufeff")

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # {TBL_ID:"..."} -> {"TBL_ID":"..."}
        fixed = re.sub(
            r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
            r'\1"\2"\3',
            text
        )

        try:
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "KOSIS 응답을 JSON으로 변환하지 못했습니다.\n"
                f"error={e}\n"
                f"original preview:\n{text[:1000]}\n"
                f"fixed preview:\n{fixed[:1000]}"
            )
    @staticmethod
    def _raise_if_kosis_error(data):
        if isinstance(data, dict) and "err" in data:
            raise RuntimeError(f"KOSIS API 오류: {data}")

        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            if "err" in data[0]:
                raise RuntimeError(f"KOSIS API 오류: {data[0]}")

    def search_tables(self, query: str, result_count: int = 30) -> pd.DataFrame:
        params = {
            "method": "getList",
            "apiKey": self.api_key,
            "searchNm": query,
            "sort": "RANK",
            "startCount": "1",
            "resultCount": str(result_count),
            "format": "json",
            "content": "json",
        }

        response = requests.get(self.SEARCH_URL, params=params, timeout=30)
        response.raise_for_status()
        data = self._parse_kosis_json_like(response.text)
        self._raise_if_kosis_error(data)

        df = pd.DataFrame(data)

        if df.empty:
            raise ValueError(f"KOSIS 통합검색 결과가 비어 있습니다. query={query}")

        return df
    

    def fetch_table_all(
        self,
        org_id: str,
        tbl_id: str,
        start_prd: str = "201301",
        end_prd: str = "202312",
        prd_se: str = "M",
    ) -> pd.DataFrame:

        last_error = None

        for obj_depth in range(1, 9):
            params = {
                "method": "getList",
                "apiKey": self.api_key,
                "format": "json",
                "orgId": org_id,
                "tblId": tbl_id,
                "itmId": "ALL",
                "prdSe": prd_se,
                "startPrdDe": start_prd,
                "endPrdDe": end_prd,
            }

            # objL1부터 필요한 깊이까지만 넣음
            for i in range(1, obj_depth + 1):
                params[f"objL{i}"] = "ALL"

            response = requests.get(self.DATA_URL, params=params, timeout=60)
            response.raise_for_status()

            try:
                data = self._parse_kosis_json_like(response.text)
            except Exception as e:
                raise RuntimeError(
                    "KOSIS 데이터 API 응답 파싱 실패.\n"
                    f"status={response.status_code}\n"
                    f"url={response.url}\n"
                    f"error={e}\n"
                    f"preview:\n{response.text[:1000]}"
                )
            

            # KOSIS 에러 응답 처리
            if isinstance(data, dict) and "err" in data:
                err_code = str(data.get("err"))
                err_msg = data.get("errMsg")
                last_error = data

                print(
                    f"[KOSIS 재시도] obj_depth={obj_depth}, "
                    f"err={err_code}, msg={err_msg}"
                )
                print(f"request url: {response.url}")

                # err 20: 필수 분류 누락 가능성 → objL 깊이 증가
                if err_code == "20":
                    continue

                # err 21: 존재하지 않는 objL을 넣었거나, tblId/orgId/prdSe가 틀렸을 가능성
                raise RuntimeError(
                    "KOSIS API 오류 21: 잘못된 요청 변수입니다.\n"
                    "가능 원인:\n"
                    "1. 해당 통계표에 없는 objL2, objL3 등을 넣음\n"
                    "2. tblId 또는 orgId가 실제 API용 ID와 다름\n"
                    "3. prdSe 또는 기간 형식이 통계표 주기와 안 맞음\n"
                    f"last request url:\n{response.url}\n"
                    f"error response:\n{data}"
                )

                # 기타 에러
                raise RuntimeError(f"KOSIS API 오류: {data}")

            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
                if "err" in data[0]:
                    err_code = str(data[0].get("err"))
                    err_msg = data[0].get("errMsg")
                    last_error = data[0]

                    print(
                        f"[KOSIS 재시도] obj_depth={obj_depth}, "
                        f"err={err_code}, msg={err_msg}"
                    )
                    print(f"request url: {response.url}")

                    if err_code == "20":
                        continue

                    if err_code == "21":
                        raise RuntimeError(
                            "KOSIS API 오류 21: 잘못된 요청 변수입니다.\n"
                            f"last request url:\n{response.url}\n"
                            f"error response:\n{data[0]}"
                        )

                    raise RuntimeError(f"KOSIS API 오류: {data[0]}")

            df = pd.DataFrame(data)

            if df.empty:
                last_error = {"err": "EMPTY", "errMsg": "응답은 성공했지만 데이터프레임이 비었습니다."}
                continue

            print(f"[KOSIS 성공] orgId={org_id}, tblId={tbl_id}, obj_depth={obj_depth}")
            print(f"columns: {list(df.columns)}")
            print(f"rows: {len(df)}")

            return df

        raise RuntimeError(
            "KOSIS 데이터 조회 실패. objL1~objL8까지 재시도했지만 성공하지 못했습니다.\n"
            f"last_error={last_error}"
        )

class KosisMacroCollector:
    LABEL_COL_PATTERNS = [
        "ITM_NM",
        "C1_NM",
        "C2_NM",
        "C3_NM",
        "C4_NM",
        "C5_NM",
        "C6_NM",
        "C7_NM",
        "C8_NM",
        "UNIT_NM",
    ]

    BAD_RATE_KEYWORDS = [
        "증감률",
        "증감",
        "전년",
        "전월",
        "전기",
        "기여도",
        "%",
        "비율",
    ]

    GOOD_INDEX_KEYWORDS = [
        "원지수",
        "지수",
        "금액",
        "무역수지",
    ]

    def __init__(
        self,
        client: KosisApiClient,
        output_dir: str = "data/raw",
    ):
        self.client = client
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _norm(text) -> str:
        if pd.isna(text):
            return ""
        text = str(text)
        text = re.sub(r"\s+", "", text)
        return text.upper()

    def _get_label_cols(self, df: pd.DataFrame) -> List[str]:
        return [col for col in self.LABEL_COL_PATTERNS if col in df.columns]

    def _make_label(self, df: pd.DataFrame) -> pd.Series:
        label_cols = self._get_label_cols(df)

        if not label_cols:
            raise ValueError(f"라벨 컬럼을 찾지 못했습니다. columns={list(df.columns)}")

        return (
            df[label_cols]
            .fillna("")
            .astype(str)
            .agg(" | ".join, axis=1)
            .str.replace(r"\s+\|\s+\|", " | ", regex=True)
            .str.strip(" |")
        )

    def _choose_table_from_search(
        self,
        search_df: pd.DataFrame,
        target: TableTarget,
    ) -> Dict[str, str]:
        if target.preferred_org_id and target.preferred_tbl_id:
            return {
                "ORG_ID": target.preferred_org_id,
                "TBL_ID": target.preferred_tbl_id,
                "TBL_NM": target.name,
            }

        df = search_df.copy()

        for col in ["ORG_ID", "TBL_ID", "TBL_NM", "ORG_NM", "STRT_PRD_DE", "END_PRD_DE"]:
            if col not in df.columns:
                df[col] = ""

        # 국가데이터처/통계청 계열 우선
        df["score"] = 0

        df.loc[df["ORG_ID"].astype(str).eq("101"), "score"] += 30
        df.loc[df["TBL_NM"].astype(str).str.contains(target.query, na=False), "score"] += 50

        query_parts = re.split(r"\s+", target.query)
        for part in query_parts:
            if part:
                df.loc[df["TBL_NM"].astype(str).str.contains(part, na=False), "score"] += 10
                df.loc[df.get("CONTENTS", "").astype(str).str.contains(part, na=False), "score"] += 5

        df = df.sort_values("score", ascending=False).reset_index(drop=True)

        if df.empty or df.loc[0, "score"] <= 0:
            raise ValueError(f"적절한 통계표 후보를 고르지 못했습니다. query={target.query}")

        selected = df.iloc[0]

        return {
            "ORG_ID": str(selected["ORG_ID"]),
            "TBL_ID": str(selected["TBL_ID"]),
            "TBL_NM": str(selected["TBL_NM"]),
        }

    def _score_label(self, label: str, rule: SeriesRule) -> int:
        label_norm = self._norm(label)

        score = 0

        for keyword in rule.include_keywords:
            if self._norm(keyword) in label_norm:
                score += 100

        for keyword in rule.exclude_keywords or []:
            if self._norm(keyword) in label_norm:
                score -= 200

        for keyword in self.BAD_RATE_KEYWORDS:
            if self._norm(keyword) in label_norm:
                score -= 80

        for keyword in self.GOOD_INDEX_KEYWORDS:
            if self._norm(keyword) in label_norm:
                score += 10

        return score

    def _extract_one_series(
        self,
        raw_df: pd.DataFrame,
        rule: SeriesRule,
    ) -> pd.DataFrame:
        df = raw_df.copy()

        if "PRD_DE" not in df.columns:
            raise ValueError(f"PRD_DE 컬럼이 없습니다. columns={list(df.columns)}")

        if "DT" not in df.columns:
            raise ValueError(f"DT 컬럼이 없습니다. columns={list(df.columns)}")

        df["label"] = self._make_label(df)
        df["label_score"] = df["label"].apply(lambda x: self._score_label(x, rule))

        candidates = df[df["label_score"] > 0].copy()

        if candidates.empty:
            label_sample = (
                df[["label"]]
                .drop_duplicates()
                .head(80)
                .to_string(index=False)
            )
            raise ValueError(
                f"{rule.output_col} 매칭 실패.\n"
                f"include={rule.include_keywords}, exclude={rule.exclude_keywords}\n"
                f"라벨 샘플:\n{label_sample}"
            )

        label_scores = (
            candidates.groupby("label")["label_score"]
            .max()
            .reset_index()
            .sort_values("label_score", ascending=False)
            .reset_index(drop=True)
        )

        chosen_label = label_scores.loc[0, "label"]

        print(f"[선택 라벨] {rule.output_col}: {chosen_label}")

        selected = candidates[candidates["label"] == chosen_label].copy()

        selected["date"] = pd.to_datetime(
            selected["PRD_DE"].astype(str) + "01",
            format="%Y%m%d",
            errors="coerce",
        )
        selected[rule.output_col] = pd.to_numeric(selected["DT"], errors="coerce")

        selected = selected.dropna(subset=["date"]).copy()
        selected = selected[["date", rule.output_col]].copy()
        selected = selected.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        return selected

    def _extract_rules_to_wide(
        self,
        raw_df: pd.DataFrame,
        rules: List[SeriesRule],
    ) -> pd.DataFrame:
        series_dfs = []

        for rule in rules:
            one = self._extract_one_series(raw_df, rule)
            series_dfs.append(one)

        result = series_dfs[0]

        for df in series_dfs[1:]:
            result = result.merge(df, on="date", how="outer")

        result = result.sort_values("date").reset_index(drop=True)

        return result
    

    def collect_target(self, target: TableTarget) -> pd.DataFrame:
        print("\n" + "=" * 100)
        print(f"[대상] {target.name}")

        candidate_tables = []

        # preferred_org_id / preferred_tbl_id가 있으면 우선 사용
        if target.preferred_org_id and target.preferred_tbl_id:
            candidate_tables.append({
                "ORG_ID": target.preferred_org_id,
                "TBL_ID": target.preferred_tbl_id,
                "TBL_NM": target.name,
            })

            print(
                f"[검색 생략] preferred table 사용: "
                f"orgId={target.preferred_org_id}, "
                f"tblId={target.preferred_tbl_id}, "
                f"name={target.name}"
            )

        else:
            search_df = self.client.search_tables(target.query, result_count=50)

            search_path = self.output_dir / f"kosis_search_{target.name}.csv"
            search_df.to_csv(search_path, index=False, encoding="utf-8-sig")
            print(f"[검색 후보 저장] {search_path}")

            for col in ["ORG_ID", "TBL_ID", "TBL_NM", "ORG_NM"]:
                if col not in search_df.columns:
                    search_df[col] = ""

            # 검색 후보를 그대로 순회하되, 통계청 orgId=101을 우선
            search_df["candidate_score"] = 0
            search_df.loc[search_df["ORG_ID"].astype(str).eq("101"), "candidate_score"] += 50
            search_df.loc[
                search_df["TBL_NM"].astype(str).str.contains(target.query, na=False),
                "candidate_score"
            ] += 100

            # 월별/지수/산업활동 관련 이름 우대
            for kw in ["월", "지수", "산업활동", "생산", "소매판매", "설비투자"]:
                search_df.loc[
                    search_df["TBL_NM"].astype(str).str.contains(kw, na=False),
                    "candidate_score"
                ] += 10

            search_df = search_df.sort_values(
                "candidate_score",
                ascending=False
            ).reset_index(drop=True)

            for _, row in search_df.iterrows():
                candidate_tables.append({
                    "ORG_ID": str(row["ORG_ID"]),
                    "TBL_ID": str(row["TBL_ID"]),
                    "TBL_NM": str(row["TBL_NM"]),
                })

        last_error = None

        for i, cand in enumerate(candidate_tables, start=1):
            org_id = cand["ORG_ID"]
            tbl_id = cand["TBL_ID"]
            tbl_nm = cand["TBL_NM"]

            print("\n" + "-" * 80)
            print(f"[후보 테스트 {i}] orgId={org_id}, tblId={tbl_id}, tblNm={tbl_nm}")

            try:
                raw_df = self.client.fetch_table_all(
                    org_id=org_id,
                    tbl_id=tbl_id,
                    start_prd="201301",
                    end_prd="202312",
                    prd_se="M",
                )

                if "PRD_DE" not in raw_df.columns:
                    raise ValueError(f"PRD_DE 없음. columns={list(raw_df.columns)}")

                # 월별 자료인지 검사
                prd_values = raw_df["PRD_DE"].astype(str).dropna().unique()
                monthly_like = [
                    x for x in prd_values
                    if len(x) == 6 and x.isdigit()
                ]

                print(f"[후보 검사] unique PRD_DE={len(prd_values)}, monthly_like={len(monthly_like)}")
                print(f"[PRD_DE 샘플] {sorted(list(prd_values))[:10]}")

                if len(monthly_like) < 100:
                    raise ValueError(
                        f"월별 시계열로 보기 어려움. monthly_like={len(monthly_like)}"
                    )

                raw_df.insert(0, "selected_org_id", org_id)
                raw_df.insert(1, "selected_tbl_id", tbl_id)
                raw_df.insert(2, "selected_tbl_nm", tbl_nm)

                raw_path = self.output_dir / target.raw_output_path
                raw_df.to_csv(raw_path, index=False, encoding="utf-8-sig")
                print(f"[RAW 저장] {raw_path}, rows={len(raw_df)}")

                wide_df = self._extract_rules_to_wide(raw_df, target.rules)

                if wide_df.empty or len(wide_df) < 100:
                    raise ValueError(
                        f"정리본 행 수 부족: rows={len(wide_df)}"
                    )

                out_path = self.output_dir / target.output_path
                wide_df.to_csv(out_path, index=False, encoding="utf-8-sig")

                print(f"[정리본 저장] {out_path}")
                print(wide_df.head())
                print(wide_df.tail())
                print(f"rows: {len(wide_df)}")

                time.sleep(self.client.sleep_sec)

                return wide_df

            except Exception as e:
                last_error = e
                print(f"[후보 실패] orgId={org_id}, tblId={tbl_id}, error={e}")
                continue

        raise RuntimeError(
            f"{target.name}에 적합한 월별 통계표를 찾지 못했습니다.\n"
            f"last_error={last_error}"
        )

    def run(self):
        targets = [
            TableTarget(
                name="korea_trade",
                query="수출입총괄",
                preferred_org_id="134",
                preferred_tbl_id="DT_134001_001",
                raw_output_path="kosis_trade_raw_201301_202312.csv",
                output_path="korea_trade_201301_202312.csv",
                rules=[
                    SeriesRule(
                        output_col="export_amount_usd_thousand",
                        include_keywords=["수출", "금액"],
                        exclude_keywords=["수입", "건수", "중량", "증감"],
                    ),
                    SeriesRule(
                        output_col="import_amount_usd_thousand",
                        include_keywords=["수입", "금액"],
                        exclude_keywords=["수출", "건수", "중량", "증감"],
                    ),
                    SeriesRule(
                        output_col="trade_balance_usd_thousand",
                        include_keywords=["무역수지"],
                        exclude_keywords=["증감", "비율"],
                    ),
                ],
            ),
            TableTarget(
                name="industrial_production",
                query="전산업생산지수",
                raw_output_path="kosis_industrial_production_raw_201301_202312.csv",
                output_path="korea_industrial_production_201301_202312.csv",
                rules=[
                    SeriesRule(
                        output_col="industrial_production_index",
                        include_keywords=["전산업"],
                        exclude_keywords=["증감", "전년", "전월", "기여도", "%"],
                    ),
                ],
            ),
            TableTarget(
                name="mining_manufacturing_production",
                query="광공업생산지수",
                raw_output_path="kosis_mining_manufacturing_production_raw_201301_202312.csv",
                output_path="korea_mining_manufacturing_production_201301_202312.csv",
                rules=[
                    SeriesRule(
                        output_col="mining_manufacturing_production_index",
                        include_keywords=["광공업"],
                        exclude_keywords=["증감", "전년", "전월", "기여도", "%"],
                    ),
                ],
            ),
            TableTarget(
                name="retail_sales",
                query="소매판매액지수",
                raw_output_path="kosis_retail_sales_raw_201301_202312.csv",
                output_path="korea_retail_sales_201301_202312.csv",
                rules=[
                    SeriesRule(
                        output_col="retail_sales_index",
                        include_keywords=["소매판매"],
                        exclude_keywords=["증감", "전년", "전월", "기여도", "%"],
                    ),
                ],
            ),
            TableTarget(
                name="facility_investment",
                query="설비투자지수",
                raw_output_path="kosis_facility_investment_raw_201301_202312.csv",
                output_path="korea_facility_investment_201301_202312.csv",
                rules=[
                    SeriesRule(
                        output_col="facility_investment_index",
                        include_keywords=["설비투자"],
                        exclude_keywords=["증감", "전년", "전월", "기여도", "%"],
                    ),
                ],
            ),
        ]

        outputs = {}

        for target in targets:
            outputs[target.name] = self.collect_target(target)

        real_activity = outputs["industrial_production"]

        for key in [
            "mining_manufacturing_production",
            "retail_sales",
            "facility_investment",
        ]:
            real_activity = real_activity.merge(outputs[key], on="date", how="outer")

        real_activity = real_activity.sort_values("date").reset_index(drop=True)

        real_path = self.output_dir / "korea_real_activity_201301_202312.csv"
        real_activity.to_csv(real_path, index=False, encoding="utf-8-sig")

        print("\n" + "=" * 100)
        print(f"[실물활동 병합본 저장] {real_path}")
        print(real_activity.head())
        print(real_activity.tail())
        print(f"rows: {len(real_activity)}")

        return outputs


def main():
    load_dotenv()

    api_key = os.getenv("KOSIS_API_KEY")

    if not api_key:
        raise RuntimeError("KOSIS_API_KEY가 .env에 없습니다.")

    client = KosisApiClient(
        api_key=api_key,
        sleep_sec=0.3,
    )

    collector = KosisMacroCollector(
        client=client,
        output_dir="data/raw",
    )

    collector.run()


if __name__ == "__main__":
    main()