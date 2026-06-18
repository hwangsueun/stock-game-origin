import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import random
import os
import json
import re
from datetime import datetime

# =========================================================
# 설정
# =========================================================
OUTPUT_DIR = "../data/raw"
PAGE_COUNT_CSV = os.path.join(OUTPUT_DIR, "dci_gallery_page_counts.csv")
PAGE_COUNT_JSON = os.path.join(OUTPUT_DIR, "dci_gallery_page_counts.json")
PAGE_COUNT_CHECKPOINT = os.path.join(OUTPUT_DIR, "dci_gallery_page_count_checkpoint.json")

MIN_DELAY = 0.5
MAX_DELAY = 1.2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gall.dcinside.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# =========================================================
# 갤러리 목록
# =========================================================
GALLERY_MAP = {
    "미국주식": {"gall_id": "nyse", "gall_type": "MI"},
    "한국주식": {"gall_id": "krstock", "gall_type": "M"},
    "코스피": {"gall_id": "kospi", "gall_type": "M"},
    "나스닥": {"gall_id": "nasdaqmini", "gall_type": "MI"},
    "해외주식": {"gall_id": "tenbagger", "gall_type": "M"},
    "서학개미": {"gall_id": "globalant", "gall_type": "M"},
    "재테크": {"gall_id": "jaetae", "gall_type": "M"},
    "다우": {"gall_id": "dow100", "gall_type": "M"},
    "차트분석": {"gall_id": "chartanalysis", "gall_type": "M"},
    "해외선물": {"gall_id": "of", "gall_type": "M"},
    "테슬라": {"gall_id": "tesla", "gall_type": "M"},
    "반도체산업": {"gall_id": "tsmcsamsungskhynix", "gall_type": "M"},
    "AI주식": {"gall_id": "aistock", "gall_type": "MI"},
    "S&P 500": {"gall_id": "snp500index", "gall_type": "M"},
    "SCHD": {"gall_id": "schd", "gall_type": "MI"},
    "국내선물옵션ICT": {"gall_id": "ict_kospi", "gall_type": "M"},
    "나스닥 대나무숲": {"gall_id": "nasdaqbehind", "gall_type": "MI"},
    "러시아 주식": {"gall_id": "russiastock", "gall_type": "M"},
    "레버리지 주식": {"gall_id": "leverage3", "gall_type": "M"},
    "마이크로스트래티지": {"gall_id": "mstr", "gall_type": "M"},
    "모의투자": {"gall_id": "motu", "gall_type": "M"},
    "미국 기술주": {"gall_id": "us_tech_stocks", "gall_type": "M"},
    "미국가치투자": {"gall_id": "valueinvestor", "gall_type": "M"},
    "미국주식 갤러리 뒷담": {"gall_id": "mijugal", "gall_type": "MI"},
    "미국주식갤러리 뒷담화": {"gall_id": "usstockus", "gall_type": "MI"},
    "바이오주식": {"gall_id": "crsp", "gall_type": "M"},
    "삼성전자": {"gall_id": "samsungelectron", "gall_type": "M"},
    "실물 자산": {"gall_id": "minigold", "gall_type": "MI"},
    "실전주식투자": {"gall_id": "investment", "gall_type": "MI"},
    "엔비디아": {"gall_id": "geforce", "gall_type": "M"},
    "엔터 주식": {"gall_id": "entertainmentstock", "gall_type": "M"},
    "유사 투자": {"gall_id": "stupid", "gall_type": "M"},
    "인덱스펀드": {"gall_id": "passiveindexfund", "gall_type": "MI"},
    "자산 배분": {"gall_id": "vanguard", "gall_type": "M"},
    "주식과 경제": {"gall_id": "stnec", "gall_type": "M"},
    "증권": {"gall_id": "securities", "gall_type": "M"},
    "지수추종": {"gall_id": "indexetf", "gall_type": "M"},
    "코인 투자": {"gall_id": "coinivest", "gall_type": "M"},
    "투자 노하우": {"gall_id": "start_investing", "gall_type": "M"},
    "페니 주식": {"gall_id": "scamstock123", "gall_type": "M"},
    "해외주식 급등주": {"gall_id": "nasdaq1119", "gall_type": "M"},
    "해외펀드": {"gall_id": "globalfund", "gall_type": "M"},
    "주식사기피해": {"gall_id": "lk6974", "gall_type": "M"},
    "중국 주식": {"gall_id": "chstock", "gall_type": "M"},
    "Neo-KOSPI": {"gall_id": "neo_kospi", "gall_type": "MI"},
    "공모주 투자": {"gall_id": "gonmo", "gall_type": "M"},
    "금융": {"gall_id": "finance", "gall_type": "M"},
    "원금회복": {"gall_id": "kospie", "gall_type": "M"},
    "장기투자": {"gall_id": "buyandhold", "gall_type": "M"},
    "전업투자": {"gall_id": "daytrade", "gall_type": "MI"},
    "주식": {"gall_id": "synthesisstock", "gall_type": "MI"},
    "코스닥": {"gall_id": "kosdaq", "gall_type": "M"},
    "콜풋투표": {"gall_id": "kospii", "gall_type": "MI"},
    "20대 주식": {"gall_id": "20stock", "gall_type": "M"},
    "NYSE(뉴욕증권거래소)": {"gall_id": "japju", "gall_type": "M"},
    "숏포지션": {"gall_id": "shortpositioner", "gall_type": "M"},
    "스테이블코인": {"gall_id": "stablecoin", "gall_type": "M"},
    "옵션미결제": {"gall_id": "gas", "gall_type": "M"},
    "루나코인": {"gall_id": "runacoin", "gall_type": "M"},
    "상상투자": {"gall_id": "imaginationstock", "gall_type": "M"},
    "종합차트": {"gall_id": "allchart", "gall_type": "M"},
    "해외선물 실전투자": {"gall_id": "haesun", "gall_type": "M"},
    "투자일기": {"gall_id": "tradediary", "gall_type": "M"},
    "전문투자자": {"gall_id": "fckthemarket", "gall_type": "MI"},
    "해외투자뉴스소식": {"gall_id": "wsjnews1", "gall_type": "MI"},
    "GRASS 코인": {"gall_id": "grassisfuture", "gall_type": "MI"},
    "벅스코인인범": {"gall_id": "bgscaden", "gall_type": "MI"},
    "차트 패턴": {"gall_id": "chartpattern", "gall_type": "MI"},
    "차트갤러리 대나무숲": {"gall_id": "chartanalysi", "gall_type": "MI"},
    "차트의비밀": {"gall_id": "chartmaster", "gall_type": "MI"},
    "코인빗": {"gall_id": "coinpan", "gall_type": "M"},
    "타이드코인": {"gall_id": "tidecoin", "gall_type": "M"},
    "선물거래": {"gall_id": "futures2023", "gall_type": "M"},
    "실물 금 투자": {"gall_id": "goldmaster", "gall_type": "MI"},
    "해외선물 크루드오일": {"gall_id": "crudeoil", "gall_type": "M"},
    "반도체 후공정": {"gall_id": "semipkg", "gall_type": "M"},
    "배터리산업": {"gall_id": "batteryindustry", "gall_type": "M"},
    "퀀트투자": {"gall_id": "quantinvestment", "gall_type": "MI"},
    "비트코인": {"gall_id": "bitcoins_new1", "gall_type": "G"},
    "주식실패담": {"gall_id": "dragontail", "gall_type": "M"},
    "주식회사 대한민국": {"gall_id": "stockkorea", "gall_type": "M"},
    "채권투자": {"gall_id": "bondinvestment", "gall_type": "M"},
    "반도체": {"gall_id": "semiconductor", "gall_type": "M"},
    "한국증권금융": {"gall_id": "ksfc", "gall_type": "M"},
    "프리세일코인": {"gall_id": "presalecoin", "gall_type": "MI"},
    "국내주식": {"gall_id": "kjusick", "gall_type": "M"},
    "경제이슈": {"gall_id": "issue1", "gall_type": "MI"},
    "가치투자": {"gall_id": "value", "gall_type": "M"},
    "노드페이 코인": {"gall_id": "nodepay777", "gall_type": "M"},
    "벅스코인": {"gall_id": "bgsc", "gall_type": "M"},
    "코인피닛": {"gall_id": "coinfinit", "gall_type": "M"},
    "국내선물옵션": {"gall_id": "koreafutures", "gall_type": "MI"},
    "투자": {"gall_id": "invest", "gall_type": "M"},
    "201505~201701 주식": {"gall_id": "stock_new1", "gall_type": "G"},
    "파이코인": {"gall_id": "picoin", "gall_type": "M"},
    "해외선물옵션": {"gall_id": "futuresoption", "gall_type": "MI"},
    "옵션매수전용": {"gall_id": "options", "gall_type": "MI"},
    "조각 투자": {"gall_id": "estherart", "gall_type": "M"},
    "경제": {"gall_id": "economy", "gall_type": "G"},
    "Lumira 코인": {"gall_id": "lumira", "gall_type": "M"},
    "비트코인 P2P 거래소": {"gall_id": "bitcoin", "gall_type": "M"},
    "정치, 사회 갤러리": {"gall_id": "stock_new2", "gall_type": "G"},  # 추가
    "201305~201505 주식": {"gall_id": "stock_new", "gall_type": "G"},  # 추가
}

# =========================================================
# URL 생성
# =========================================================
def make_list_url(gall_id: str, gall_type: str, page: int = 1) -> str:
    if gall_type == "MI":
        return f"https://gall.dcinside.com/mini/board/lists/?id={gall_id}&page={page}"
    elif gall_type == "G":
        return f"https://gall.dcinside.com/board/lists/?id={gall_id}&page={page}"
    else:
        return f"https://gall.dcinside.com/mgallery/board/lists/?id={gall_id}&page={page}"

# =========================================================
# 체크포인트
# =========================================================
def load_checkpoint():
    if os.path.exists(PAGE_COUNT_CHECKPOINT):
        with open(PAGE_COUNT_CHECKPOINT, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "started_at": str(datetime.now()),
        "done": {},
        "last_gallery": None,
        "last_updated": None,
    }

def save_checkpoint(cp):
    cp["last_updated"] = str(datetime.now())
    with open(PAGE_COUNT_CHECKPOINT, "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)

# =========================================================
# 페이지 수 파싱
# =========================================================
def fetch_list_html(gall_id: str, gall_type: str, page: int = 1) -> str:
    url = make_list_url(gall_id, gall_type, page)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text

def parse_total_pages_from_html(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    # 1) "페이지 17 이동" 같은 문구 우선
    matches = re.findall(r"페이지\s*(\d+)\s*이동", text)
    if matches:
        return max(map(int, matches))

    # 2) 페이지네이션 링크 숫자들 추출
    candidates = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            candidates.add(int(m.group(1)))

        label = a.get_text(" ", strip=True)
        if label.isdigit():
            candidates.add(int(label))

    if candidates:
        return max(candidates)

    # 3) 현재 페이지 span/strong류에서 숫자 추출
    for tag in soup.select("span.on, em.on, strong.on, a.on"):
        label = tag.get_text(" ", strip=True)
        if label.isdigit():
            candidates.add(int(label))

    if candidates:
        return max(candidates)

    return 1

def get_total_pages(gall_id: str, gall_type: str) -> int:
    html = fetch_list_html(gall_id, gall_type, page=1)
    total_pages = parse_total_pages_from_html(html)
    return total_pages

# =========================================================
# 저장
# =========================================================
def save_results(done_map: dict):
    rows = []
    for gall_name, item in done_map.items():
        rows.append({
            "gallery_name": gall_name,
            "gall_id": item["gall_id"],
            "gall_type": item["gall_type"],
            "total_pages": item["total_pages"],
            "checked_at": item["checked_at"],
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["total_pages", "gallery_name"], ascending=[False, True])
        df.to_csv(PAGE_COUNT_CSV, index=False, encoding="utf-8-sig")

    with open(PAGE_COUNT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

# =========================================================
# 메인
# =========================================================
def collect_gallery_page_counts():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    cp = load_checkpoint()
    done_map = cp.get("done", {})

    galleries = list(GALLERY_MAP.items())
    print(f"\n총 {len(galleries)}개 갤러리")
    print(f"이미 페이지 수 확인 완료: {len(done_map)}개\n")

    for idx, (gall_name, info) in enumerate(galleries, 1):
        gall_id = info["gall_id"]
        gall_type = info["gall_type"]

        if gall_name in done_map:
            print(f"[{idx:>3}/{len(galleries)}] {gall_name} ({gall_id}) → 이미 완료, 스킵")
            continue

        print(f"\n[{idx:>3}/{len(galleries)}] {gall_name} ({gall_id} / {gall_type})")
        cp["last_gallery"] = gall_name
        save_checkpoint(cp)

        try:
            total_pages = get_total_pages(gall_id, gall_type)
        except Exception as e:
            print(f"  [실패] {gall_name} → {e}")
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            continue

        done_map[gall_name] = {
            "gall_id": gall_id,
            "gall_type": gall_type,
            "total_pages": total_pages,
            "checked_at": str(datetime.now()),
        }

        cp["done"] = done_map
        save_checkpoint(cp)
        save_results(done_map)

        print(f"  → total_pages = {total_pages}")
        print(f"  → 저장 완료")

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print("\n===== 갤러리별 페이지 수 수집 완료 =====")
    print(f"CSV : {PAGE_COUNT_CSV}")
    print(f"JSON: {PAGE_COUNT_JSON}")
    print(f"체크포인트: {PAGE_COUNT_CHECKPOINT}")

if __name__ == "__main__":
    collect_gallery_page_counts()