#!/usr/bin/env python3
"""국내 정책·법·정치 공식 발표 레이어(거시뉴스 보강용).

거시뉴스 공식 발표 레이어(build_official_macro_releases.py)가 미국 Fed/BEA 중심이라
국내 정책·법·정치 이벤트가 비어 있다. 이 빌더는 같은 규약(OfficialRelease 스키마)을
따르되 국내 소스를 모은다:

  - 국회   → 의안정보 API(열린국회정보 open.assembly.go.kr, serviceKey 필요)  : 본회의 의결
  - 헌재   → 결정문(ccourt.go.kr)                                              : 주요 결정
  - 공정위 → 의결·보도자료(ftc.go.kr)                                          : 과징금·기업결합
  - 금감원 → 보도자료·제재(fss.or.kr)                                          : 회계감리·제재
  - 선거   → NEC(nec.go.kr) + Wikidata(CC0)                                     : 전국단위 선거

원칙(기존 레이어와 동일):
  - 소스 URL + 추출된 사실치가 있는 레코드만 기록(무조작).
  - 가용일은 보수적으로(발표 다음 영업일 = next_weekday). pr04가 다시 실제 거래일로 정렬.
  - 라이선스를 명시(license). 사실/수치 자체는 비저작물.

pr04 합류:
  - 출력 CSV는 _load_official_release_calendar가 요구하는 컬럼을 모두 포함한다.
  - 단 pr04의 approved_domains 화이트리스트에 신규 도메인(assembly.go.kr, ccourt.go.kr,
    ftc.go.kr, fss.or.kr, nec.go.kr, wikidata.org)을 추가해야 합류 가능(통합 단계에서 처리).
  - 개별 종목 단위 규제 조치(공정위/금감원)는 affected_stock_codes에 종목코드를 채워
    종목 뉴스 파이프라인에서도 재사용할 수 있게 한다.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_CSV = BASE_DIR / "data/raw/official_policy_legal_releases_2013_2023.csv"

WD_SPARQL = "https://query.wikidata.org/sparql"
NEC_HOME = "https://www.nec.go.kr"
NEC_STATS = "http://info.nec.go.kr"
ASSEMBLY_BILL_API = "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn"

USER_AGENT = "IISE-CD policy-legal release collector/1.0 (research pipeline)"


@dataclass(frozen=True)
class PolicyLegalRelease:
    event_date: str            # 가용일(발표 다음 영업일); pr04가 실제 거래일로 재정렬
    source_release_date: str   # 실제 발표/의결/선거일
    event_type: str            # official_release
    release_category: str      # election | legislation | court_ruling | regulatory_action
    region: str                # kr
    institution: str
    title: str
    description: str
    reference_period: str
    affected_markets: str
    direction: str             # positive | negative | neutral | mixed
    severity: str              # high | moderate | low
    source_url: str
    key_figures_json: str
    license: str
    source_layer: str          # 어느 collector가 만들었는지(provenance)
    affected_stock_codes: str = ""  # 개별 종목 라우팅 훅(공정위/금감원). "005930;000660" 형식
    # 전체는 원장으로 적재하되 거시 캘린더에는 시장영향 큰 건만 승격(GDELT 원장→승격 패턴).
    macro_eligible: str = "true"
    verification_status: str = "official_source_verified"


# --------------------------------------------------------------------------- #
# 공용 유틸 (기존 build_official_macro_releases.py 규약 재사용)
# --------------------------------------------------------------------------- #
def http_get(url: str, params: dict[str, str] | None = None, timeout: int = 60,
             retries: int = 4) -> str:
    if params:
        url = f"{url}?{urlencode(params)}"
    error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except Exception as exc:  # noqa: BLE001 - 단순 재시도
            error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"request failed after {retries} attempts: {url} :: {error}")


def strip_tags(html_text: str) -> str:
    clean = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
    clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    # 일부 게시판 제목은 이중 인코딩(&amp;#37;)이라 두 번 디코딩해야 엔티티가 풀린다.
    clean = html.unescape(html.unescape(clean))
    return re.sub(r"\s+", " ", clean).strip()


def next_weekday(raw_date: str) -> str:
    """발표일 다음 영업일(주말 건너뜀). 가용일을 보수적으로 잡는다."""
    result = date.fromisoformat(raw_date) + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result.isoformat()


# 조사 일치: 앞 단어 끝소리 받침 여부로 조사 선택(영문·숫자는 한국어 발음 기준).
_JOSA_LATIN_BATCHIM = set("FLMNRZ")   # 에프·엘·엠·엔·알·제트
_JOSA_DIGIT_BATCHIM = set("1368")     # 일·삼·육·팔


def has_batchim(word: str) -> bool:
    w = re.sub(r"(?:\([^)]*\)|㈜)\s*$", "", word.strip())  # 끝의 (주)/(NEC)/㈜ 제거
    w = w.strip().rstrip("']\"”’」』.")
    if not w:
        return False
    ch = w[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    if ch.isascii() and ch.isalpha():
        return ch.upper() in _JOSA_LATIN_BATCHIM
    if ch.isdigit():
        return ch in _JOSA_DIGIT_BATCHIM
    return False


def josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """word의 끝소리 받침 여부로 붙일 조사(만)를 반환. 'word'가 따옴표로 감싸여도 word만 넘기면 됨."""
    return with_batchim if has_batchim(word) else without_batchim


def strip_corp_marker(text: str) -> str:
    """회사명에 형식상 붙는 법인격 표기 제거(시장 통용명 사용). 예: (주)카카오→카카오."""
    text = re.sub(r"\(주\)|\(유\)|\(재\)|\(사\)|㈜|주식회사|유한회사|유한책임회사", "", text)
    text = re.sub(r"\b(?:Co\.,?\s*Ltd\.?|Ltd\.?|Inc\.?|Corp\.?)\b", "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()


# --------------------------------------------------------------------------- #
# 선거: NEC + Wikidata(CC0)  — 키 불필요, 즉시 동작
# --------------------------------------------------------------------------- #
# Wikidata가 날짜·QID(provenance)를 공급하고, 한국어 정식 명칭은 NEC 기준의 공개 사실.
# 전국단위 선거만 남기고 재보궐·당대표·서울시장 보선은 typeLabel/label로 배제.
_ELECTION_KEEP_TYPES = {
    "South Korean presidential election",
    "South Korean legislative elections",
    "municipal election",
}
_ELECTION_DROP_TOKENS = ("by-election", "mayoral", "seoul", "records", "leadership", "primary")

# 전국단위 선거 한국어 정식 명칭 + 중요도(NEC 공개 사실). 날짜는 Wikidata로 교차검증.
_KR_ELECTION_NAMES: dict[str, tuple[str, str, str]] = {
    # 선거일: (한국어 명칭, 카테고리 설명, severity)
    "2014-06-04": ("제6회 전국동시지방선거", "local", "moderate"),
    "2016-04-13": ("제20대 국회의원선거", "legislative", "high"),
    "2017-05-09": ("제19대 대통령선거", "presidential", "high"),
    "2018-06-13": ("제7회 전국동시지방선거", "local", "moderate"),
    "2020-04-15": ("제21대 국회의원선거", "legislative", "high"),
    "2022-03-09": ("제20대 대통령선거", "presidential", "high"),
    "2022-06-01": ("제8회 전국동시지방선거", "local", "moderate"),
}


def collect_elections(start_year: int, end_year: int) -> list[PolicyLegalRelease]:
    query = f"""
SELECT ?e ?eLabel ?d ?typeLabel WHERE {{
  ?e wdt:P31 ?type. ?type wdt:P279* wd:Q40231.
  ?e wdt:P17 wd:Q884; wdt:P585 ?d.
  FILTER(?d >= "{start_year}-01-01T00:00:00Z"^^xsd:dateTime &&
         ?d <  "{end_year + 1}-01-01T00:00:00Z"^^xsd:dateTime)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} ORDER BY ?d
"""
    payload = json.loads(http_get(WD_SPARQL, {"query": query, "format": "json"}))
    rows = payload["results"]["bindings"]

    releases: dict[str, PolicyLegalRelease] = {}
    for binding in rows:
        type_label = binding.get("typeLabel", {}).get("value", "")
        en_label = binding.get("eLabel", {}).get("value", "")
        if type_label not in _ELECTION_KEEP_TYPES:
            continue
        if any(token in en_label.lower() for token in _ELECTION_DROP_TOKENS):
            continue
        election_date = binding["d"]["value"][:10]
        names = _KR_ELECTION_NAMES.get(election_date)
        if not names:  # 큐레이션된 전국단위 선거만(예상 밖 날짜는 보수적으로 제외)
            continue
        ko_name, office, severity = names
        qid = binding["e"]["value"].rsplit("/", 1)[-1]
        if election_date in releases:  # 같은 날 중복 엔티티(예: 총선 records) 1건만
            continue
        releases[election_date] = PolicyLegalRelease(
            event_date=next_weekday(election_date),
            source_release_date=election_date,
            event_type="official_release",
            release_category="election",
            region="kr",
            institution="중앙선거관리위원회(NEC)",
            title=f"{ko_name} 실시",
            description=f"{election_date} {ko_name}가 실시됐다.",
            reference_period=election_date,
            affected_markets="kospi/kosdaq/usdkrw/kr_rates",
            direction="neutral",
            severity=severity,
            source_url=NEC_HOME,  # pr04는 https:// 요구. 구체 출처는 key_figures에 보존.
            key_figures_json=json.dumps({
                "election_name": ko_name,
                "office": office,
                "election_date": election_date,
                "wikidata_qid": qid,
                "nec_stats_url": NEC_STATS,
                "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
            }, ensure_ascii=False),
            license="CC0-1.0 (Wikidata) / NEC 공개통계",
            source_layer="nec_wikidata",
        )
    return sorted(releases.values(), key=lambda r: r.source_release_date)


# --------------------------------------------------------------------------- #
# 국회: 의안정보 API(열린국회정보)  — 실제 API 검증 완료
# --------------------------------------------------------------------------- #
# 엔드포인트 nzmimeepazxkubdpn(의안정보). 필수인자 AGE(대수). 표준응답
#   { "<svc>": [ {head:[{list_total_count}]}, {row:[...]} ] }.
# 실제 필드(검증): BILL_NAME, PROC_DT(본회의 의결일), PROC_RESULT(원안가결/수정가결/…),
#   COMMITTEE, PROPOSER, DETAIL_LINK(http→https 변환). 서버측 PROC_RESULT 필터 지원.
# 본회의 가결만 채택: 원안가결+수정가결(대안반영폐기는 위원장 대안으로 별도 가결되므로 제외=중복방지).
# 2013~2023 = 19·20·21대(PROC_DT 연도로 최종 필터).
_ASSEMBLY_AGES = (19, 20, 21)
_ASSEMBLY_PASS_RESULTS = ("원안가결", "수정가결")

# 시장영향 큰 주요 입법만 거시 캘린더에 승격(macro_eligible). 매칭 키워드는 key_figures에 보존.
_BILL_MACRO_KEYWORDS = (
    "예산", "추가경정", "추경", "세법", "조세", "소득세", "법인세", "부가가치세",
    "종합부동산세", "상속", "증여", "관세", "금융", "자본시장", "은행", "보험", "증권",
    "부동산", "주택", "주거", "임대차", "재건축", "재개발", "노동", "최저임금", "근로기준",
    "국민연금", "연금", "공정거래", "하도급", "대규모유통", "가맹", "중소기업", "벤처",
    "상법", "한국은행", "전기사업", "원자력", "에너지", "탄소", "반도체", "소재부품장비",
    "데이터", "개인정보", "통신", "방송", "자동차", "항공", "조선", "철강", "건설산업",
)


def _bill_macro_keyword(bill_name: str) -> str | None:
    for kw in _BILL_MACRO_KEYWORDS:
        if kw in bill_name:
            return kw
    return None


def _assembly_call(api_key: str, age: int, result: str, page: int, per_page: int) -> tuple[int, list[dict]]:
    payload = json.loads(http_get(ASSEMBLY_BILL_API, {
        "KEY": api_key, "Type": "json", "pIndex": str(page), "pSize": str(per_page),
        "AGE": str(age), "PROC_RESULT": result,
    }))
    if "RESULT" in payload and len(payload) == 1:  # ERROR-300 등
        return 0, []
    total, rows = 0, []
    for value in payload.values():
        if not isinstance(value, list):
            continue
        for block in value:
            if isinstance(block, dict) and "head" in block:
                total = int(block["head"][0].get("list_total_count", 0))
            if isinstance(block, dict) and "row" in block:
                rows = block["row"]
    return total, rows


def collect_national_assembly(start_year: int, end_year: int,
                              per_page: int = 300) -> list[PolicyLegalRelease]:
    api_key = os.environ.get("ASSEMBLY_API_KEY") or os.environ.get("DATA_GO_KR_KEY")
    if not api_key:
        print("[national_assembly] ASSEMBLY_API_KEY 없음 → 건너뜀. "
              "open.assembly.go.kr 또는 data.go.kr에서 발급 후 .env에 ASSEMBLY_API_KEY=... 추가.")
        return []

    releases: list[PolicyLegalRelease] = []
    for age in _ASSEMBLY_AGES:
        for result_filter in _ASSEMBLY_PASS_RESULTS:
            page = 1
            while True:
                total, rows = _assembly_call(api_key, age, result_filter, page, per_page)
                if not rows:
                    break
                for row in rows:
                    proc_date = str(row.get("PROC_DT") or "").strip()[:10].replace("/", "-")
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", proc_date):
                        continue
                    if not start_year <= int(proc_date[:4]) <= end_year:
                        continue
                    result = str(row.get("PROC_RESULT") or "").strip()
                    bill_name = str(row.get("BILL_NAME") or "").strip()
                    committee = str(row.get("COMMITTEE") or "").strip()
                    proposer = str(row.get("PROPOSER") or "").strip()
                    bill_no = str(row.get("BILL_NO") or "").strip()
                    link = str(row.get("DETAIL_LINK") or "").strip()
                    if link.startswith("http://"):
                        link = "https://" + link[len("http://"):]
                    if not link.startswith("https://"):
                        link = "https://likms.assembly.go.kr/bill/main.do"
                    keyword = _bill_macro_keyword(bill_name)
                    eligible = keyword is not None
                    releases.append(PolicyLegalRelease(
                        event_date=next_weekday(proc_date),
                        source_release_date=proc_date,
                        event_type="official_release",
                        release_category="legislation",
                        region="kr",
                        institution="국회",
                        title=f"{bill_name} {result}",
                        description=(
                            f"{proc_date} 국회 본회의에서 '{bill_name}'{josa(bill_name, '이', '가')} {result}됐다."
                            + (f" 소관 위원회는 {committee}이다." if committee else "")
                        ),
                        reference_period=proc_date,
                        affected_markets="kospi/kosdaq",
                        direction="neutral",
                        severity="high" if eligible else "low",
                        source_url=link,
                        key_figures_json=json.dumps({
                            "bill_no": bill_no, "bill_name": bill_name, "proc_result": result,
                            "committee": committee, "proposer": proposer,
                            "age": age, "macro_keyword": keyword,
                        }, ensure_ascii=False),
                        license="공공누리 제1유형(열린국회정보)",
                        source_layer="assembly_openapi",
                        macro_eligible="true" if eligible else "false",
                    ))
                if page * per_page >= total:
                    break
                page += 1
                time.sleep(0.2)
    return releases


# --------------------------------------------------------------------------- #
# 헌재 / 공정위 / 금감원  — 다음 단계(스텁). 키 불필요(스크레이프/큐레이션).
# --------------------------------------------------------------------------- #
# 헌재 시장·정치 영향 주요 결정(검증된 사건번호·선고일). 무조작 원칙상 확실한 건만.
# 전수 확장: 국가법령정보 공동활용 API(law.go.kr DRF, target=detc 헌재결정례)는 OC 등록
# (www.law.go.kr 회원가입→OPEN API 신청, IP/도메인 등록) 필요. OC 확보 시 위헌·헌법불합치·
# 탄핵·정당해산 종국결과로 필터해 자동 확장 가능(현재는 큐레이션 시드).
CCOURT_BOARD = "https://www.ccourt.go.kr/site/kor/ex/bbs/List.do?cbIdx=1129"
_CONSTITUTIONAL_COURT_DECISIONS = (
    {
        "decision_date": "2014-12-19", "case_no": "2013헌다1",
        "title": "통합진보당 해산 결정", "result": "인용(정당해산)",
        "description": "헌법재판소가 2013헌다1 정당해산심판에서 통합진보당의 해산을 결정했다.",
        "severity": "high", "gdelt_gkg_overlap": False,
    },
    {
        "decision_date": "2017-03-10", "case_no": "2016헌나1",
        "title": "대통령(박근혜) 탄핵심판 인용", "result": "인용(파면)",
        "description": "헌법재판소가 2016헌나1 탄핵심판에서 대통령 박근혜의 파면을 결정했다.",
        "severity": "high", "gdelt_gkg_overlap": True,  # GDELT GKG IMPEACHMENT와 중복
    },
)


def collect_constitutional_court(start_year: int, end_year: int) -> list[PolicyLegalRelease]:
    releases: list[PolicyLegalRelease] = []
    for d in _CONSTITUTIONAL_COURT_DECISIONS:
        if not start_year <= int(d["decision_date"][:4]) <= end_year:
            continue
        releases.append(PolicyLegalRelease(
            event_date=next_weekday(d["decision_date"]),
            source_release_date=d["decision_date"],
            event_type="official_release",
            release_category="court_ruling",
            region="kr",
            institution="헌법재판소",
            title=d["title"],
            description=d["description"],
            reference_period=d["decision_date"],
            affected_markets="kospi/kosdaq/usdkrw",
            direction="neutral",
            severity=d["severity"],
            source_url=CCOURT_BOARD,
            key_figures_json=json.dumps({
                "case_no": d["case_no"], "result": d["result"],
                "gdelt_gkg_overlap": d["gdelt_gkg_overlap"],
            }, ensure_ascii=False),
            license="공공누리(헌법재판소)",
            source_layer="ccourt_curated",
            macro_eligible="true",  # 모두 고신호
        ))
    return releases


# 공정위/금감원 보도자료 게시판. 페이징·행구조 실검증 완료.
# pageUnit 실검증: FTC 최대 50, FSS 최대 100(행/페이지) → 전기간 크롤 요청수 대폭 감소.
FTC_LIST = "https://www.ftc.go.kr/www/selectBbsNttList.do?key=12&bordCd=3&pageUnit=50&pageIndex={page}"
FTC_VIEW = "https://www.ftc.go.kr/www/selectBbsNttView.do?key=12&bordCd=3&nttSn={sn}"
FSS_LIST = "https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218&pageUnit=100&pageIndex={page}"
FSS_VIEW = "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId={sn}&menuNo=200218"

# 시장 관련(개별종목·거시) 신호 키워드 → macro_eligible/severity high. 없으면 routine(eligible=false).
_REG_MACRO_KEYWORDS = (
    "과징금", "기업결합", "담합", "부당공동행위", "시정명령", "고발", "제재", "동의의결",
    "불공정거래", "분식회계", "회계감리", "영업정지", "인가취소", "과태료", "부당지원",
    "일감몰아주기", "하도급", "기술유용", "리콜", "표시광고", "경영유의", "기관경고",
)
# 게시판 노이즈(개정안/예고/통계/안내 등) 제외용
_REG_DROP_KEYWORDS = ("개정안", "행정예고", "입법예고", "설명회", "공청회", "채용", "비교정보")
# 제재성 신호(대상 기업에 부정적) → direction=negative
_REG_NEGATIVE_SIGNALS = ("제재", "과징금", "담합", "고발", "위반", "남용", "불공정", "부당",
                         "불허", "유용", "과태료", "분식", "제동")

# 종목 마스터: 뉴스 파이프라인 유니버스(stock_code, 회사명). 전체 유니버스 마스터가 별도로
# 갖춰지면 --stock-master-csv로 교체 가능(현재는 파이프라인 프로파일에서 구성).
_STOCK_MASTER_CANDIDATES = (
    "data/processed/stock_llm_profile_yearly_raw.csv",   # input_name/dart_name
    "data/processed/stock_llm_profile_yearly_clean.csv", # stock_name
)
_STOCK_NAME_DENYLIST = {"동부", "동양", "대한", "한국", "우리", "신한", "하나", "삼성", "현대",
                        "LG", "SK", "GS", "CJ", "DL", "효성", "한화", "롯데", "포스코"}
BOARD_MAX_PAGES = 30          # 안전 상한(검증용 기본). 전기간 백필 시 --board-max-pages로 상향.
BOARD_DELAY_SEC = 0.3
ENRICH_DETAIL = False         # --enrich-detail: 공정위/금감원 상세페이지 조치 추출(크롤 2배).


def _load_stock_master(master_csv: Path | None = None) -> list[tuple[str, str]]:
    """(회사명, 종목코드) 목록. 최장명 우선(부분매칭 오류 감소)."""
    paths = [master_csv] if master_csv else [BASE_DIR / p for p in _STOCK_MASTER_CANDIDATES]
    name_cols = ("input_name", "dart_name", "stock_name", "company_name")
    code_cols = ("stock_code", "ticker", "code")
    name_to_code: dict[str, str] = {}
    for path in paths:
        if not path or not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = next((re.search(r"\d{6}", str(row.get(c) or "")) for c in code_cols
                            if re.search(r"\d{6}", str(row.get(c) or ""))), None)
                name = next((str(row.get(c)).strip() for c in name_cols
                            if (row.get(c) or "").strip()), "")
                if name and len(name) >= 2 and code and name not in _STOCK_NAME_DENYLIST:
                    name_to_code.setdefault(name, code.group(0))
        if name_to_code:
            break  # 첫 번째로 데이터가 있는 마스터 사용
    return sorted(name_to_code.items(), key=lambda kv: -len(kv[0]))


_CORP_MARKER = r"\(주\)|㈜|주식회사|\(유\)|\(재\)|\(사\)"
# 이름 뒤에 붙는 한국어 조사(이게 붙어도 단어끝으로 인정). 긴 것 우선.
_JOSA = ("으로서", "으로", "에서", "에게", "이라", "라는", "의", "은", "는", "이", "가", "을",
         "를", "와", "과", "도", "에", "로", "및", "측", "사", "들", "만", "과의", "와의")


def _right_boundary(after_text: str) -> bool:
    """이름 오른쪽이 단어끝인가: 비단어문자이거나, 닫힌 조사집합 뒤가 단어끝이면 True."""
    if not after_text:
        return True
    if not re.match(r"[가-힣A-Za-z0-9]", after_text[0]):
        return True
    for josa in sorted(_JOSA, key=len, reverse=True):
        if after_text.startswith(josa):
            rest = after_text[len(josa):]
            if not rest or not re.match(r"[가-힣A-Za-z0-9]", rest[0]):
                return True
    return False


def _name_in_title(name: str, title: str) -> bool:
    """오탐 억제: 긴 이름은 단어경계(조사 허용), 짧은 이름(≤3)은 법인마커 인접일 때만 인정."""
    for m in re.finditer(re.escape(name), title):
        i, j = m.start(), m.end()
        before = title[i - 1] if i > 0 else " "
        left_ok = not re.match(r"[가-힣A-Za-z0-9]", before)
        if not left_ok:
            continue  # 더 긴 단어 안에 박힌 경우(NCC 속 NC, 오디오북 속 디오) 제외
        right_ok = _right_boundary(title[j:])
        if len(name) >= 4 and right_ok:
            return True
        near = title[max(0, i - 6): j + 6]
        if right_ok and re.search(_CORP_MARKER, near):  # 짧은 이름은 ㈜/(주) 인접 요구
            return True
    return False


def _match_stock_codes(title: str, master: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    codes, names = [], []
    for name, code in master:
        if code in codes:
            continue
        if _name_in_title(name, title):
            codes.append(code)
            names.append(name)
    return codes, names


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _parse_ftc_rows(raw: str) -> list[dict[str, str]]:
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", raw, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 5:
            continue
        date = _strip(tds[4])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        mt = re.search(r'p-table__text">(.*?)</span>', tds[2], re.S)
        sn = re.search(r"nttSn=(\d+)", tds[2])
        rows.append({
            "date": date, "gubun": _strip(tds[1]), "dept": _strip(tds[3]),
            "title": _strip(mt.group(1)) if mt else "", "sn": sn.group(1) if sn else "",
        })
    return rows


def _parse_fss_rows(raw: str) -> list[dict[str, str]]:
    rows = []
    m = re.search(r"<tbody.*?</tbody>", raw, re.S)
    body = m.group(0) if m else raw
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 4:
            continue
        date = next((_strip(t) for t in tds if re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", _strip(t))), "")
        if not date:
            continue
        mt = re.search(r'<a[^>]*href="[^"]*nttId=(\d+)[^"]*"[^>]*>(.*?)</a>', tds[1], re.S)
        rows.append({
            "date": date, "gubun": "보도", "dept": _strip(tds[2]) if len(tds) > 2 else "",
            "title": _strip(mt.group(2)) if mt else _strip(tds[1]),
            "sn": mt.group(1) if mt else "",
        })
    return rows


# 상세페이지 조치 추출(과징금/시정명령 등). 유형은 본문 substring으로 고신뢰, 금액은 best-effort.
_DETAIL_ACTIONS = ("과징금", "시정명령", "검찰 고발", "고발", "과태료", "동의의결",
                   "영업정지", "기관경고", "경고", "조건부 승인", "승인", "불허", "시정조치")
# 기사에 쓰는 명료 조치(경고/승인 등 모호어는 description엔 제외, key_figures엔 보존)
_DETAIL_ACTIONS_HEADLINE = ("과징금", "시정명령", "고발", "과태료", "동의의결", "영업정지", "불허")


def _extract_detail_facts(body: str) -> dict:
    # 조치 유형(과징금/시정명령 등)은 본문 substring으로 고신뢰. 금액은 인접 오추출(예: 과징금
    # 기준액)이 잦아 무조작 원칙상 추출하지 않는다(기사엔 조치 유형만 반영).
    actions = [a for a in _DETAIL_ACTIONS if a in body]
    parties = re.search(r"(\d+)\s*개\s*사업자", body)
    facts: dict = {"actions": actions}
    if parties:
        facts["num_parties"] = int(parties.group(1))
    return facts


def _enrich_regulatory_release(rel: PolicyLegalRelease, view_url: str) -> PolicyLegalRelease:
    """상세페이지에서 조치 유형을 추출해 description·key_figures 보강(무조작: 본문 추출분만)."""
    from dataclasses import replace
    try:
        body = strip_tags(http_get(view_url, timeout=30))
    except Exception:
        return rel
    facts = _extract_detail_facts(body)
    actions = [a for a in facts.get("actions", []) if a in _DETAIL_ACTIONS_HEADLINE]
    if not actions:
        return rel
    # 중복 의미 정리(시정조치⊂시정명령 등 단순화)
    seen: list[str] = []
    for a in actions:
        if a not in seen:
            seen.append(a)
    action_str = "·".join(seen[:3])
    parties = facts.get("num_parties")
    subject = f"{parties}개 사업자에 " if parties else ""
    title_clean = strip_corp_marker(rel.title)
    new_desc = (f"{rel.source_release_date} {rel.institution}{josa(rel.institution, '이', '가')} "
                f"'{title_clean}'에서 {subject}{action_str}{josa(action_str, '을', '를')} 부과·결정했다.")
    kf = json.loads(rel.key_figures_json)
    # 핵심 조치만 저장(경고·승인 등 부차 표기는 기사 노이즈라 제외 — description과 동일 집합).
    kf["detail_actions"] = seen
    if parties:
        kf["num_parties"] = parties
    return replace(rel, description=new_desc, key_figures_json=json.dumps(kf, ensure_ascii=False))


def _collect_board(start_year: int, end_year: int, *, list_tpl: str, view_tpl: str,
                   parse_fn, institution: str, category: str, source_layer: str,
                   license_str: str, only_gubun_bodo: bool,
                   enrich_detail: bool = False) -> list[PolicyLegalRelease]:
    master = _load_stock_master()
    releases: list[PolicyLegalRelease] = []
    page = 1
    while page <= BOARD_MAX_PAGES:
        try:
            rows = parse_fn(http_get(list_tpl.format(page=page)))
        except Exception as exc:  # noqa: BLE001
            print(f"[{source_layer}] page {page} 실패: {exc}")
            break
        if not rows:
            break
        page_years = [int(r["date"][:4]) for r in rows if r["date"]]
        for r in rows:
            year = int(r["date"][:4])
            if not start_year <= year <= end_year:
                continue
            title = r["title"]
            if only_gubun_bodo and "보도" not in r["gubun"]:
                continue
            if any(k in title for k in _REG_DROP_KEYWORDS):
                continue
            codes, names = _match_stock_codes(title, master)
            keyword = next((k for k in _REG_MACRO_KEYWORDS if k in title), None)
            eligible = keyword is not None
            if not eligible and not codes:
                continue  # 시장신호 없고 종목매칭도 없으면 제외(routine)
            view = view_tpl.format(sn=r["sn"]) if r["sn"] else institution
            rel = PolicyLegalRelease(
                event_date=next_weekday(r["date"]),
                source_release_date=r["date"],
                event_type="official_release",
                release_category=category,
                region="kr",
                institution=institution,
                title=title,
                description=f"{r['date']} {institution}{josa(institution, '이', '가')} "
                            f"'{strip_corp_marker(title)}'{josa(strip_corp_marker(title), '을', '를')} 발표했다."
                            + (f" 담당부서는 {r['dept']}이다." if r["dept"] else ""),
                reference_period=r["date"],
                affected_markets="kospi/kosdaq" if not codes else "kr_single_stock",
                # 제재성 조치(과징금·시정명령·고발·담합·위반)는 대상에 부정적. 승인/인가는 중립.
                direction="negative" if any(k in title for k in _REG_NEGATIVE_SIGNALS) else "neutral",
                severity="high" if (eligible and (codes or any(
                    k in title for k in ("과징금", "담합", "기업결합", "제재", "분식")))) else "moderate",
                source_url=view,
                key_figures_json=json.dumps({
                    "macro_keyword": keyword, "matched_stocks": names, "dept": r["dept"],
                }, ensure_ascii=False),
                license=license_str,
                source_layer=source_layer,
                affected_stock_codes=";".join(codes),
                macro_eligible="true" if eligible else "false",
            )
            if enrich_detail and r["sn"]:
                rel = _enrich_regulatory_release(rel, view)
                time.sleep(BOARD_DELAY_SEC)
            releases.append(rel)
        # 날짜 내림차순 → 페이지 전체가 start_year 이전이면 종료
        if page_years and max(page_years) < start_year:
            break
        page += 1
        time.sleep(BOARD_DELAY_SEC)
    return releases


def collect_ftc(start_year: int, end_year: int) -> list[PolicyLegalRelease]:
    """공정거래위원회 보도자료(ftc.go.kr). 과징금·기업결합·담합 제재 등.

    개별기업 사건은 affected_stock_codes에 종목코드 매핑(거시+종목 양쪽 라우팅).
    """
    return _collect_board(
        start_year, end_year, list_tpl=FTC_LIST, view_tpl=FTC_VIEW,
        parse_fn=_parse_ftc_rows, institution="공정거래위원회",
        category="regulatory_action", source_layer="ftc_board",
        license_str="공공누리 제1유형(공정거래위원회)", only_gubun_bodo=True,
        enrich_detail=ENRICH_DETAIL,
    )


def collect_fss(start_year: int, end_year: int) -> list[PolicyLegalRelease]:
    """금융감독원 보도자료(fss.or.kr). 회계감리·제재·불공정거래·인허가 등."""
    return _collect_board(
        start_year, end_year, list_tpl=FSS_LIST, view_tpl=FSS_VIEW,
        parse_fn=_parse_fss_rows, institution="금융감독원",
        category="regulatory_action", source_layer="fss_board",
        license_str="공공누리 제1유형(금융감독원)", only_gubun_bodo=False,
        enrich_detail=ENRICH_DETAIL,
    )


COLLECTORS = {
    "elections": collect_elections,
    "national_assembly": collect_national_assembly,
    "constitutional_court": collect_constitutional_court,
    "ftc": collect_ftc,
    "fss": collect_fss,
}


def write_csv(path: Path, records: Iterable[PolicyLegalRelease]) -> None:
    rows = [asdict(record) for record in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PolicyLegalRelease.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(rows)


# 일별 캡 우선순위용: 시장영향 최상위 입법/규제 키워드.
_TOP_BILL_KEYWORDS = ("예산", "추가경정", "추경", "세법", "조세", "소득세", "법인세",
                      "부가가치세", "종합부동산세", "금융", "자본시장", "부동산", "주택",
                      "한국은행", "최저임금", "국민연금", "공정거래", "상법")
_TOP_REG_KEYWORDS = ("과징금", "기업결합", "담합", "부당공동행위", "고발", "분식회계", "영업정지")


def _policy_priority(row: dict[str, str]) -> int:
    """일별 캡 시 남길 우선순위 점수(높을수록 우선)."""
    cat = row.get("release_category", "")
    title = row.get("title", "")
    if cat == "court_ruling":
        return 100
    if cat == "election":
        return 95
    if cat == "legislation":
        score = 60
        if any(k in title for k in _TOP_BILL_KEYWORDS):
            score += 25
        if "원안가결" in title:
            score += 3  # 원안가결>수정가결 동률 시 우선
        return score
    if cat == "regulatory_action":
        score = 50
        if row.get("affected_stock_codes"):
            score += 25  # 게임 종목에 걸리는 제재는 개별종목 가치도 있어 우선
        if any(k in title for k in _TOP_REG_KEYWORDS):
            score += 10
        return score
    return 40


def merge_into_calendar(policy_csv: Path, official_macro_csv: Path, combined_out: Path,
                        max_policy_per_day: int = 2) -> int:
    """정책·법 레이어 CSV와 기존 official 거시발표 CSV를 한 합본 캘린더로 머지.

    pr04는 official_release_calendar를 단일 경로로 읽으므로, 두 레이어를 컬럼 합집합으로
    합쳐 combined_out에 쓴다. 정책·법 레이어 전용 컬럼(license/source_layer/
    affected_stock_codes)은 official 행에서 빈 값으로 채워진다.

    일별 캡: 거시는 하루 5칸이고 official release는 must-cover라, 국회 본회의 일괄처리일처럼
    정책·법이 몰리면 시장기사를 잠식하고 must-cover 초과로 생성이 실패한다. event_date별로
    우선순위(헌재/선거>주요입법>종목매칭·주요규제) 상위 max_policy_per_day건만 승격하고
    나머지는 원장(policy_csv)에만 남긴다.
    pr04 --official-release-calendar <combined_out> 로 넘기면 거시뉴스에 합류.
    """
    def read_rows(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    # 정책·법 레이어는 전체 원장. 거시 캘린더에는 macro_eligible 행만 승격(국회 routine 입법 제외).
    eligible = [r for r in read_rows(policy_csv) if r.get("macro_eligible", "true") != "false"]
    # event_date별 우선순위 캡
    by_day: dict[str, list[dict[str, str]]] = {}
    for r in eligible:
        by_day.setdefault(r.get("event_date", ""), []).append(r)
    policy_rows: list[dict[str, str]] = []
    for day, rows_day in by_day.items():
        rows_day.sort(key=lambda r: (-_policy_priority(r), r.get("title", "")))
        policy_rows.extend(rows_day[:max_policy_per_day])
    macro_rows = read_rows(official_macro_csv)
    fieldnames: list[str] = []
    for row in (*macro_rows, *policy_rows):
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    combined = sorted(
        (*macro_rows, *policy_rows),
        key=lambda r: (r.get("event_date", ""), r.get("institution", ""), r.get("title", "")),
    )
    combined_out.parent.mkdir(parents=True, exist_ok=True)
    with combined_out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in combined:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return len(combined)


def main() -> None:
    global BOARD_MAX_PAGES, ENRICH_DETAIL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2013)
    parser.add_argument("--end-year", type=int, default=2023)
    parser.add_argument("--sources", nargs="+", default=["elections"],
                        choices=[*COLLECTORS, "all"],
                        help="수집할 소스(기본 elections). all = 전체.")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--official-macro-csv", type=Path, default=None,
                        help="기존 official 거시발표 CSV. --combined-out과 함께 주면 합본 캘린더 생성.")
    parser.add_argument("--combined-out", type=Path, default=None,
                        help="합본 캘린더 출력 경로(pr04 --official-release-calendar에 넘길 파일).")
    parser.add_argument("--board-max-pages", type=int, default=BOARD_MAX_PAGES,
                        help="공정위/금감원 게시판 페이지 상한(전기간 백필 시 상향).")
    parser.add_argument("--max-policy-per-day", type=int, default=2,
                        help="합본 캘린더 일별 정책·법 이벤트 상한(시장기사 잠식·must-cover 초과 방지).")
    parser.add_argument("--enrich-detail", action="store_true",
                        help="공정위/금감원 상세페이지에서 조치 유형(과징금/시정명령 등) 추출(크롤 2배).")
    args = parser.parse_args()
    BOARD_MAX_PAGES = args.board_max_pages
    ENRICH_DETAIL = args.enrich_detail

    selected = list(COLLECTORS) if "all" in args.sources else args.sources
    records: list[PolicyLegalRelease] = []
    per_source: dict[str, int] = {}
    for name in selected:
        rows = COLLECTORS[name](args.start_year, args.end_year)
        per_source[name] = len(rows)
        records.extend(rows)

    # (event_date, institution, title) 기준 dedup 후 정렬
    unique = {(r.source_release_date, r.institution, r.title): r for r in records}
    ordered = sorted(unique.values(), key=lambda r: (r.source_release_date, r.institution, r.title))
    write_csv(args.output_csv, ordered)

    by_category: dict[str, int] = {}
    for record in ordered:
        by_category[record.release_category] = by_category.get(record.release_category, 0) + 1
    summary = {
        "rows": len(ordered),
        "per_source": per_source,
        "by_category": by_category,
        "output": str(args.output_csv),
    }
    if args.official_macro_csv and args.combined_out:
        merged = merge_into_calendar(args.output_csv, args.official_macro_csv, args.combined_out,
                                     max_policy_per_day=args.max_policy_per_day)
        summary["combined_rows"] = merged
        summary["combined_out"] = str(args.combined_out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
