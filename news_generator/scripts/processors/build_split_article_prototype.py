#!/usr/bin/env python3
"""Build a small deterministic prototype of disclosure and later reaction articles."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import pandas as pd


SAMPLE_KEYS = {
    ("032500", "2019-03-07"),  # volume-only earnings
    ("078150", "2022-02-22"),  # 5-day earnings
    ("042700", "2021-03-09"),  # contract
    ("035420", "2017-06-26"),  # investment
    ("036570", "2017-02-06"),  # dividend
    ("068270", "2014-03-11"),  # multi-trigger earnings
    ("094840", "2021-02-04"),  # next-day earnings
    ("047810", "2022-03-28"),  # contract
}

DISPLAY_NAME = {"036570": "엔씨소프트"}
ENTITY_ALIASES = {
    "Forehope Electronic (Ningbo)": "포어호프 일렉트로닉(닝보)",
    "인도네시아 국방부(공군)": "인도네시아 공군",
}
CONTRACT_ITEM_ALIASES = {
    "T-50i 추가 도입 인도네시아 수출": "T-50i 추가 도입 수출 계약",
}


def report_family(report_name: str) -> str:
    if "배당" in report_name:
        return "dividend"
    if "판매" in report_name or "공급계약" in report_name:
        return "contract"
    if "매출액" in report_name or "손익구조" in report_name:
        return "earnings"
    if "시설투자" in report_name or "타법인" in report_name or "유형자산" in report_name:
        return "investment"
    return "other"


def canonical_family(value: str) -> str:
    if value in {"asset_transaction", "equity_investment"}:
        return "investment"
    return value


def fact_value(facts: list[dict], fact_type: str) -> str:
    row = next((f for f in facts if f.get("fact_type") == fact_type), None)
    return str(row.get("text_ko", "")) if row else ""


def issuer_name(text: str) -> str:
    match = re.search(r"공시 당시 회사명은 '(.+?)'이다", text)
    if not match:
        return ""
    return re.sub(r"^(?:㈜|\(주\)|주식회사)\s*", "", match.group(1)).strip()


def amount(text: str) -> str:
    m = re.search(r"(?:은|를|금은|금액은)\s*(약\s*)?([0-9조억만,]+원)", text)
    if not m:
        m = re.search(r"(약\s*)?([0-9조억만,]+원)", text)
    return ("약 " if m and m.group(1) else "") + m.group(2) if m else ""


# 공시 상대방 필드에 이름 대신 들어오는 잡단어(계약변경·비공개 등) — 사명으로 쓰지 않는다.
_JUNK_PARTY = {"해당없음", "해당사항없음", "비공개", "없음", "-", "상세참조",
               "기재생략", "주1)", "주1", "별첨", "첨부참조"}
# 계약 변경·정정 공시에서 상대방 칸에 흘러드는 문구(회사명엔 없는 어휘) → 부분일치 시 폐기.
_JUNK_PARTY_KEYWORDS = ("변경", "정정", "종료", "해지", "취소", "미정")


def _is_junk_party(value: str) -> bool:
    return value in _JUNK_PARTY or any(k in value for k in _JUNK_PARTY_KEYWORDS)


def counterparty(text: str) -> str:
    m = re.search(r"상대방은\s*(.+?)(?:으)?로 공시", text)
    if not m:
        return ""
    raw = m.group(1).strip()
    if _is_junk_party(raw):
        return ""
    party = normalize_entity(raw)
    return "" if _is_junk_party(party) else party


_CORP_SUFFIX_RE = re.compile(
    r"[\s,]*(?:Co\.?|Company|Corp\.?(?:oration)?|Inc\.?|Ltd\.?|Limited|GmbH|LLC|"
    r"Pte\.?(?:\s*Ltd\.?)?|Pty\.?(?:\s*Ltd\.?)?|S\.?A\.?|AG|N\.?V\.?)\.?\s*$",
    flags=re.IGNORECASE,
)


def clean_org_name(value: str) -> str:
    """뉴스 문체용 사명 정리: 괄호 설명·주식회사·영문 법인꼬리표 제거."""
    value = value.strip()
    # 한글명(영문) / 한글명[설명] 형태면 첫 괄호 앞 본명만 사용
    head = re.split(r"[\(\[（]", value, maxsplit=1)[0].strip()
    if head:
        value = head
    # 잔여 괄호·대괄호 제거
    for bracket in "[]()（）":
        value = value.replace(bracket, "")
    # 한국 법인격 제거
    value = re.sub(r"\(주\)|㈜|주식회사", "", value)
    # 콤마로 나열된 복수 법인은 각 세그먼트의 영문 법인 꼬리표(Co., Ltd 등)를 따로 제거
    segments = []
    for part in value.split(","):
        part = part.strip()
        for _ in range(3):
            stripped = _CORP_SUFFIX_RE.sub("", part)
            if stripped == part:
                break
            part = stripped
        part = part.strip()
        if part:
            segments.append(part)
    value = ", ".join(segments)
    return re.sub(r"\s+", " ", value).strip().strip(",").strip()


def normalize_entity(value: str) -> str:
    value = value.strip()
    if value in ENTITY_ALIASES:
        return ENTITY_ALIASES[value]
    return clean_org_name(value)


def target_company(text: str) -> str:
    m = re.search(r"거래 대상 회사는\s*(.+?)(?:으)?로 공시", text)
    if not m:
        return ""
    return clean_org_name(m.group(1))


def contract_item(text: str) -> str:
    match = re.search(r"'(.+?)'으로 공시", text)
    return match.group(1).strip() if match else ""


def correction_reason(text: str) -> str:
    match = re.search(r"정정 사유는 '(.+?)'(?:이?라고|으로|로) 공시", text)
    if not match:
        return ""
    value = match.group(1).strip()
    return value.replace("고객(현지 정부) 사정으로 인한", "현지 정부 사정에 따른")


def contract_name(item: str) -> str:
    if not item:
        return "공급계약"
    if item in CONTRACT_ITEM_ALIASES:
        return CONTRACT_ITEM_ALIASES[item]
    if item.endswith("수주"):
        return item[:-2].rstrip() + " 공급계약"
    if item.endswith("계약"):
        return item
    return item + " 계약"


# 라틴문자로 끝나거나 끝에 라틴이 섞인 한국 종목명의 받침 유무(조사 결정용).
# True=받침 있음(은/을/과), False=받침 없음(는/를/와). 한국어 음독 기준.
_NAME_JONGSEONG = {
    "DL": True, "GS": True, "HMM": True, "S-Oil": True, "한전KPS": True,
    "LS": True, "NHN": True,
    "SK": False, "LG": False, "NAVER": False, "BGF": False, "HLB": False,
    "삼성SDI": False, "DB": False, "OCI": False, "NC": False, "SKC": False,
    "KT": False, "KCC": False, "F&F": False,
}


def has_jongseong(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    if text in _NAME_JONGSEONG:
        return _NAME_JONGSEONG[text]
    ch = text[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - ord("가")) % 28 != 0
    if ch.isdigit():
        return ch in "0136780"  # 영0·일1·삼3·육6·칠7·팔8: 음독 끝소리에 받침
    # 끝이 라틴/기타면 직전 한글 음절의 받침을 따른다(기존 보수적 동작).
    for c in reversed(text):
        if "가" <= c <= "힣":
            return (ord(c) - ord("가")) % 28 != 0
    return False


def particle(text: str, consonant: str, vowel: str) -> str:
    return consonant if has_jongseong(text) else vowel


def event_copy(code: str, fallback_name: str, anchor: str, family: str, report_name: str,
               facts: list[dict]) -> tuple[str, str]:
    name = issuer_name(fact_value(facts, "issuer_name_as_filed")) or DISPLAY_NAME.get(code, fallback_name)
    topic = f"{name}{particle(name, '은', '는')}"
    # '자회사의 주요경영사항' 공시는 실적·계약·투자 주체가 자회사다(공시 주체=지주).
    # 지주 본인의 활동으로 오귀속하지 않도록 '자회사' 프레이밍을 붙인다.
    is_sub = "자회사" in (report_name or "")
    anchor_date = pd.Timestamp(anchor)
    date_label = f"{anchor_date.month}월 {anchor_date.day}일"
    if family == "earnings":
        values = []
        for fact_type, default_label in [("sales", "매출액"), ("operating_profit", "영업이익"),
                                         ("net_income", "당기순이익")]:
            source_text = fact_value(facts, fact_type)
            value = amount(source_text)
            if value:
                label = next((x for x in ["영업손실", "당기순손실", "영업이익", "당기순이익", "매출액"]
                              if x in source_text), default_label)
                values.append(f"{label} {value}")
        period = f"{anchor_date.year - 1}년 " if anchor_date.month <= 4 else ""
        scope = "연결 기준 " if "연결" in fact_value(facts, "statement_scope") else (
            "별도 기준 " if "별도" in fact_value(facts, "statement_scope") else ""
        )
        joined = ", ".join(values)
        recap = ", ".join(values[:2])
        if is_sub:
            return (f"{topic} 자회사의 {period}{scope}{joined} 실적을 {date_label} 공시했다.",
                    f"{topic} 자회사의 {period}{scope}{recap} 실적을 지난 {date_label} 공시했다.")
        return (f"{topic} {period}{scope}{joined}을 기록했다고 {date_label} 공시했다.",
                f"{topic} {period}{scope}{recap}의 실적을 지난 {date_label} 발표했다.")

    if family == "contract":
        value = amount(fact_value(facts, "contract_amount"))
        party = counterparty(fact_value(facts, "counterparty"))
        item = contract_item(fact_value(facts, "contract_item"))
        correction = correction_reason(fact_value(facts, "correction_reason"))
        party_phrase = f"{party}{particle(party, '과', '와')} " if party else ""
        contract_label = contract_name(item)
        actor = "자회사가 " if is_sub else ""
        if correction:
            relationship = f"{party}{particle(party, '과', '와')} 체결한 " if party else "기존 "
            article = f"{topic} {actor}{relationship}{contract_label}의 {correction}을 {date_label} 공시했다."
            clause = f"{topic} {actor}{relationship}{contract_label}의 {correction} 내용을 지난 {date_label} 밝혔다."
        else:
            article = f"{topic} {actor}{party_phrase}{value} 규모 {contract_label}을 체결했다고 {date_label} 공시했다."
            clause = f"{topic} {actor}{party_phrase}{value} 규모 {contract_label}을 체결했다고 지난 {date_label} 밝혔다."
        return article, clause

    if family == "dividend":
        per_share = amount(fact_value(facts, "dividend_per_share"))
        total = amount(fact_value(facts, "dividend_total_amount"))
        if is_sub:
            total_phrase = f" 배당금 총액은 {total}이다." if total else ""
            article = f"{topic} 자회사의 현금배당 결정을 {date_label} 공시했다.{total_phrase}"
            recap_total = f"{total} 규모 " if total else ""
            clause = f"{topic} 자회사의 {recap_total}현금배당 결정을 지난 {date_label} 공시했다."
            return article, clause
        if per_share:
            article = f"{topic} 주당 배당금을 {per_share}으로 {date_label} 결정했다."
            if total:
                article += f" 배당금 총액은 {total}이다."
            clause = f"{topic} 주당 {per_share}의 배당을 지난 {date_label} 결정했다."
        else:
            article = f"{topic} 현금배당을 결정했다고 {date_label} 공시했다. 배당금 총액은 {total}으로 집계됐다."
            clause = f"{topic} {total} 규모의 현금배당을 지난 {date_label} 결정했다."
        return article, clause

    if family == "investment":
        value = amount(fact_value(facts, "investment_amount") or fact_value(facts, "acquisition_amount"))
        target = target_company(fact_value(facts, "target_company"))
        if "타법인" in report_name and target:
            if is_sub:
                article = f"{topic} 자회사의 {target} 지분 {value} 취득 결정을 {date_label} 공시했다."
                clause = f"{topic} 자회사의 {target} 지분 {value} 취득 결정을 지난 {date_label} 공시했다."
            else:
                article = f"{topic} {target} 지분을 {value}에 취득하기로 결정했다고 {date_label} 공시했다."
                clause = f"{topic} {target} 지분을 {value}에 취득하기로 했다고 지난 {date_label} 밝혔다."
        else:
            if is_sub:
                article = f"{topic} 자회사의 {value} 규모 신규 시설투자를 {date_label} 공시했다."
                clause = f"{topic} 자회사의 {value} 규모 신규 시설투자를 지난 {date_label} 공시했다."
            else:
                article = f"{topic} {value} 규모의 신규 시설투자를 결정했다고 {date_label} 공시했다."
                clause = f"{topic} {value} 규모 신규 시설투자를 지난 {date_label} 발표했다."
        return article, clause

    return f"{topic} {date_label} 주요 경영사항을 발표했다.", f"{topic} {date_label} 주요 경영사항을 발표했다."


def direction(value: float) -> str:
    return "올랐다" if value > 0 else "내렸다" if value < 0 else "보합을 기록했다"


def connective_direction(value: float) -> str:
    return "올랐고," if value > 0 else "내렸고," if value < 0 else "보합을 기록했고,"


def index_direction(value: float) -> str:
    return "상승했다" if value > 0 else "하락했다" if value < 0 else "보합을 기록했다"


def reaction_copy(clause: str, price: dict, sector: dict) -> tuple[str, str]:
    reason = price["material_reason"]
    if "ret5d" in reason:
        horizon, publish_date = 5, price["date_5d"]
        stock_ret, sector_ret = float(price["ret_5d"]), float(sector["sector_return_5d"])
        include_sector = sector["index_name"] not in {"제조", "금융", "일반서비스"} and abs(stock_ret - sector_ret) >= 2.0
        verb = connective_direction(stock_ret) if include_sector else direction(stock_ret)
        text = f"{clause} 이후 5거래일간 주가는 {abs(stock_ret):g}% {verb}"
        if include_sector:
            text += f" 같은 기간 {sector['index_name']} 업종지수는 {abs(sector_ret):g}% {index_direction(sector_ret)}"
        return publish_date, text + "."
    if "ret1d" in reason:
        publish_date = price["date_1d"]
        stock_ret, sector_ret = float(price["ret_1d"]), float(sector["sector_return_1d"])
        include_sector = sector["index_name"] not in {"제조", "금융", "일반서비스"} and abs(stock_ret - sector_ret) >= 2.0
        verb = connective_direction(stock_ret) if include_sector else direction(stock_ret)
        text = f"{clause} 다음 거래일 주가는 {abs(stock_ret):g}% {verb}"
        if include_sector:
            text += f" 같은 날 {sector['index_name']} 업종지수는 {abs(sector_ret):g}% {index_direction(sector_ret)}"
        return publish_date, text + "."
    publish_date = price["date_1d"]
    return publish_date, (
        f"{clause} 다음 거래일 거래량은 직전 20거래일 평균의 "
        f"{float(price['vol_mult']):g}배로 집계됐다."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-jsonl", type=Path, required=True)
    ap.add_argument("--price-csv", type=Path, required=True)
    ap.add_argument("--sector-csv", type=Path, required=True)
    ap.add_argument("--dart-detail-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--jsonl-out", type=Path)
    args = ap.parse_args()

    price = {r["custom_id"]: r for r in csv.DictReader(args.price_csv.open(encoding="utf-8-sig"))}
    sector = {r["custom_id"]: r for r in csv.DictReader(args.sector_csv.open(encoding="utf-8-sig"))}
    requests = []
    for line in args.requests_jsonl.read_text(encoding="utf-8").splitlines():
        request = json.loads(line)
        payload = json.loads(request["body"]["messages"][1]["content"])["brief_payload"]
        key = (str(payload["stock_code"]).zfill(6), payload["anchor_date"])
        if key in SAMPLE_KEYS:
            requests.append((request["custom_id"], payload))

    detail = pd.read_csv(args.dart_detail_csv, dtype=str).fillna("")
    lines = ["# 공시 기사 + 후속 반응 기사 프로토타입", ""]
    output_rows = []
    built = 0
    for cid, payload in requests:
        code = str(payload["stock_code"]).zfill(6)
        anchor = payload["anchor_date"]
        family = canonical_family(payload["event_family"])
        day = anchor.replace("-", "")
        rows = detail[(detail["stock_code"] == code) & detail["rcept_no"].str.startswith(day)].copy()
        rows = rows[rows["report_name"].map(report_family).eq(family)]
        if rows.empty:
            continue
        row = rows.iloc[0]
        facts = json.loads(row["facts_json"])
        event_text, clause = event_copy(
            code, payload["stock_name"], anchor, family, row["report_name"], facts
        )
        reaction_date, reaction_text = reaction_copy(clause, price[cid], sector[cid])
        built += 1
        lines += [
            f"## {built}. {DISPLAY_NAME.get(code, payload['stock_name'])} | {family}", "",
            f"- 공시 기사 날짜: `{anchor}`", f"- 공시 기사: {event_text}", "",
            f"- 반응 기사 날짜: `{reaction_date}`", f"- 반응 기사: {reaction_text}", "",
            f"- 근거 공시: `{row['rcept_no']}` {row['report_name']}", "",
        ]
        common = {
            "source_custom_id": cid,
            "stock_code": code,
            "stock_name": DISPLAY_NAME.get(code, payload["stock_name"]),
            "event_family": family,
            "source_rcept_no": row["rcept_no"],
        }
        output_rows.extend([
            {**common, "article_id": f"{cid}__disclosure", "article_type": "disclosure",
             "publish_date": anchor, "news_lines": [event_text]},
            {**common, "article_id": f"{cid}__reaction", "article_type": "market_reaction_followup",
             "publish_date": reaction_date, "news_lines": [reaction_text],
             "material_reason": price[cid]["material_reason"]},
        ])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    jsonl_out = args.jsonl_out or args.out.with_suffix(".jsonl")
    with jsonl_out.open("w", encoding="utf-8") as f:
        for row in output_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[done] article_pairs={built} -> {args.out} | {jsonl_out}")


if __name__ == "__main__":
    main()
