"""
디시인사이드 주식 관련 갤러리 수집 스크립트 (연관 갤러리 API 기반)

방법:
1. 시드 갤러리(미국주식, 한국주식 등)에서 시작
2. 연관 갤러리 API(/ajax/gallery_top_ajax/relation)로 following/follower 수집
3. BFS로 연결된 갤러리 전부 탐색
4. 주식/투자 관련인지 키워드로 필터링
5. 결과를 stock_galleries.json으로 저장

[재실행]
- stock_galleries.json 이 이미 있으면 스킵
"""

import requests
import re
import time
import random
import json
import os
from collections import deque

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

RESULT_FILE = "stock_galleries.json"

# ────────────────────────────────────────────────
# 시드 갤러리
# ────────────────────────────────────────────────
SEED_GALLERIES = [
    ("미국주식",   "stockus",          "M"),
    ("한국주식",   "krstock",          "M"),
    ("코스피",     "kospi",            "M"),
    ("나스닥",     "nasdaq",           "M"),
    ("해외주식",   "tenbagger",        "M"),
    ("서학개미",   "globalant",        "M"),
    ("재테크",     "jaetae",           "M"),
    ("다우",       "dow100",           "M"),
    ("차트분석",   "chartanalysis",    "M"),
    ("해외선물",   "of",               "M"),
    ("테슬라",     "tesla",            "M"),
    ("반도체산업", "tsmcsamsungskhynix","M"),
]

# 주식/투자 관련 키워드
STOCK_KEYWORDS = [
    "주식", "증권", "투자", "코스피", "코스닥", "나스닥", "etf", "펀드",
    "반도체", "바이오", "배터리", "2차전지", "조선", "금융", "재테크",
    "코인", "암호화폐", "비트코인", "선물", "옵션", "차트", "주가",
    "배당", "공매도", "상장", "ipo", "리츠", "자산", "경제",
    "삼성전자", "하이닉스", "카카오", "셀트리온", "크래프톤", "현대차",
    "미국주식", "한국주식", "해외주식", "국내주식", "서학개미",
    "테슬라", "애플", "엔비디아", "구글", "아마존",
    "다우", "s&p", "nasdaq", "kospi", "kosdaq",
    "기술주", "성장주", "가치주", "배당주", "소형주",
    "뉴욕증시", "월가", "연준", "금리", "환율",
    "neo-kospi", "schd", "nyse", "숏포지션", "스테이블코인",
    "마이크로스트래티지", "미국기치투자", "미국기술주",
]

# 명백히 무관한 키워드
EXCLUDE_KEYWORDS = [
    "e스포츠", "esports", "게임단", "프로게임", "lck",
    "아이돌", "걸그룹", "보이그룹", "팬카페",
    "야구단", "축구단", "배구단", "농구단",
    "점보스",  # 대한항공 배구단
]


# ────────────────────────────────────────────────
# CSRF 토큰 + gall_no 수집
# ────────────────────────────────────────────────

def get_gallery_meta(session: requests.Session, gall_id: str, gall_type: str = "M") -> tuple[str, str]:
    """
    갤러리 페이지에서 gall_no(내부 번호)와 csrf 토큰 추출.
    반환: (gall_no, csrf_token)
    """
    if gall_type == "M":
        url = f"https://gall.dcinside.com/mgallery/board/lists/?id={gall_id}"
    else:
        url = f"https://gall.dcinside.com/mini/board/lists/?id={gall_id}"

    try:
        resp = session.get(url, headers=HEADERS, timeout=10)
        if len(resp.text) < 3000:
            return "", ""

        # gall_no
        gall_no = re.search(r"open_relation\((\d+)\)", resp.text)
        # csrf
        csrf = session.cookies.get("ci_c", "")
        # gall_type
        g_type = re.search(r"_GALLERY_TYPE_\s*=\s*[\"']([^\"']+)[\"']", resp.text)

        return (
            gall_no.group(1) if gall_no else "",
            csrf,
            g_type.group(1) if g_type else gall_type,
        )
    except Exception:
        return "", "", gall_type


# ────────────────────────────────────────────────
# 연관 갤러리 API 호출
# ────────────────────────────────────────────────

def get_related_via_api(session: requests.Session, gall_no: str, gall_type: str, referer_id: str) -> list[dict]:
    """
    /ajax/gallery_top_ajax/relation API로 연관 갤러리 수집.
    following(이 갤러리가 추가한) + follower(타 갤러리가 추가한) 모두 반환.
    반환: [{"name": gall_id, "ko_name": 갤러리명, "gall_type": ...}, ...]
    """
    csrf = session.cookies.get("ci_c", "")
    headers = {
        **HEADERS,
        "Referer": f"https://gall.dcinside.com/mgallery/board/lists/?id={referer_id}",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    data = {
        "ci_t":     csrf,
        "gall_no":  gall_no,
        "gall_type": gall_type,
    }
    try:
        resp = session.post(
            "https://gall.dcinside.com/ajax/gallery_top_ajax/relation",
            data=data,
            headers=headers,
            timeout=10,
        )
        result = resp.json()
        following = result.get("following", [])
        follower  = result.get("follower", [])
        return following + follower
    except Exception as e:
        return []


# ────────────────────────────────────────────────
# 주식 관련 여부 판단
# ────────────────────────────────────────────────

def is_stock_related(name: str) -> bool:
    name_lower = name.lower().replace(" ", "")
    if any(kw.replace(" ", "") in name_lower for kw in EXCLUDE_KEYWORDS):
        return False
    if any(kw.replace(" ", "") in name_lower for kw in STOCK_KEYWORDS):
        return True
    return False


# ────────────────────────────────────────────────
# BFS 탐색
# ────────────────────────────────────────────────

def bfs_crawl() -> dict:
    if os.path.exists(RESULT_FILE):
        print(f"  → {RESULT_FILE} 이미 존재, 로드합니다.")
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    session   = requests.Session()
    visited   = set()
    confirmed = {}
    queue     = deque()

    # 시드 갤러리 초기화
    for ko_name, gall_id, gall_type in SEED_GALLERIES:
        confirmed[ko_name] = {"gall_id": gall_id, "gall_type": gall_type}
        visited.add(gall_id)
        queue.append((ko_name, gall_id, gall_type))

    print(f"  시드 {len(SEED_GALLERIES)}개로 BFS 시작\n")

    step = 0
    while queue:
        ko_name, gall_id, gall_type = queue.popleft()
        step += 1
        print(f"  [{step:>3}] {ko_name} ({gall_id}) 메타 수집...", end=" ", flush=True)

        # gall_no, csrf 수집
        gall_no, csrf, actual_type = get_gallery_meta(session, gall_id, gall_type)
        if not gall_no:
            print("gall_no 없음, 스킵")
            time.sleep(random.uniform(0.5, 1.0))
            continue

        print(f"gall_no={gall_no} → 연관갤 API 호출...", end=" ", flush=True)
        time.sleep(random.uniform(0.5, 1.0))

        # 연관 갤러리 수집
        related = get_related_via_api(session, gall_no, actual_type, gall_id)
        print(f"{len(related)}개 발견", end="")

        new_count = 0
        for item in related:
            rel_id   = item.get("name", "")
            rel_name = item.get("ko_name", rel_id)
            rel_type = item.get("gall_type", "M")

            if not rel_id or rel_id in visited:
                continue
            visited.add(rel_id)

            if is_stock_related(rel_name) or is_stock_related(rel_id):
                confirmed[rel_name] = {"gall_id": rel_id, "gall_type": rel_type}
                queue.append((rel_name, rel_id, rel_type))
                new_count += 1
                print(f"\n    ✓ 추가: {rel_name} ({rel_id})", end="")

        print(f" → 신규 {new_count}개")
        _save(confirmed)
        time.sleep(random.uniform(1.0, 2.0))

    return _save(confirmed)


def _save(confirmed: dict) -> dict:
    output = {"count": len(confirmed), "relevant": confirmed}
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return output


# ────────────────────────────────────────────────
# 메인
# ────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("디시인사이드 주식 갤러리 BFS 탐색 (연관갤 API)")
    print("=" * 55)

    if os.path.exists(RESULT_FILE):
        import sys
        ans = input(f"\n{RESULT_FILE} 이 이미 있습니다. 다시 탐색할까요? (y/N): ")
        if ans.lower() != "y":
            print("종료합니다.")
            sys.exit(0)
        os.remove(RESULT_FILE)

    result    = bfs_crawl()
    confirmed = result.get("relevant", {})

    print(f"\n{'=' * 55}")
    print(f"탐색 완료: 총 {len(confirmed)}개 주식 관련 갤러리")
    print(f"결과: {RESULT_FILE}\n")
    print("[수집된 갤러리 목록]")
    for name, info in sorted(confirmed.items()):
        print(f"  {name}: {info['gall_id']} ({info['gall_type']})")


if __name__ == "__main__":
    main()