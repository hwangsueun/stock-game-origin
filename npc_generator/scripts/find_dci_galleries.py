"""
디시인사이드 종목별 갤러리 ID 탐색 스크립트
- 대상 종목 전체에 대해 마이너갤/일반갤 존재 여부 확인
- 결과를 gallery_map.json으로 저장
- 애매한 경우(이름 불일치)는 review 목록으로 분리하여 수동 확인 유도
"""

import requests
import re
import time
import random
import json
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ────────────────────────────────────────────────
# 탐색 대상 종목 + 후보 갤러리 ID 목록
# ────────────────────────────────────────────────
STOCK_CANDIDATES = {
    "SK하이닉스":        ["skhynix", "hynix", "sk_hynix"],
    "LG":                ["lg", "lgcorp", "lggroup"],
    "KCC":               ["kcc", "kccglass"],
    "삼성물산":          ["samsungcnt", "samsungmulsan"],
    "효성":              ["hyosung", "hyosungcorp"],
    "현대차":            ["hyundai", "hyundaimotor", "hyundaicar", "hmc"],
    "HL홀딩스":          ["hlholdings", "hl"],
    "한국앤컴퍼니":      ["hankookco", "hankook"],
    "한국타이어앤테크놀로지": ["hankooktire", "hktire"],
    "고려아연":          ["koreazinc", "goryeazinc", "kz"],
    "롯데케미칼":        ["lottechem", "lottekemical"],
    "BGF":               ["bgf", "bgfretail"],
    "롯데정밀화학":      ["lottefinechem", "lottefine"],
    "에스원":            ["sone", "s1corp"],
    "한국항공우주":      ["kai", "kaicorp", "korea_aerospace"],
    "삼성증권":          ["samsungsec", "samsungsecurities"],
    "키움증권":          ["kiwoom", "kiwoomsec"],
    "한국금융지주":      ["koreainvestment", "kifin"],
    "SK텔레콤":          ["skt", "sktt", "sktelekom"],
    "코웨이":            ["coway", "cowaylife"],
    "쿠쿠홀딩스":        ["cuckoo", "cuckooholdings"],
    "삼성카드":          ["samsungcard"],
    "삼성생명":          ["samsunglife", "samsunglifeins"],
    "KT&G":              ["ktng", "kt_g"],
    "SK":                ["skcorp", "skgroup"],
    "S-Oil":             ["soil", "s_oil"],
    "강원랜드":          ["gangwonland", "kwland"],
    "NAVER":             ["naver", "naverstock"],
    "엔씨소프트":        ["ncsoft", "nc_soft"],
    "카카오":            ["kakao", "kakaocorp"],
    "컴투스":            ["com2us", "comtus"],
    "펄어비스":          ["pearlabyss", "bdo", "pearlabyss_stock"],
    "서울반도체":        ["seoulsemicon", "seoulled"],
    "아바텍":            ["avatec"],
    "토비스":            ["tobis"],
    "톱텍":              ["toptec"],
    "비아트론":          ["viatron"],
    "HB테크놀러지":      ["hbtech", "hb_tech"],
    "미래컴퍼니":        ["miraeco", "miraecorp"],
    "덕산네오룩스":      ["duksan", "duksanneo"],
    "스카이라이프":      ["skylife", "kbskylife"],
    "이노션":            ["innocean"],
    "제일기획":          ["cheil", "cheilworldwide"],
    "스튜디오드래곤":    ["studiodragon", "dragon"],
    "신한지주":          ["shinhan", "shinhanjiju"],
    "셀트리온":          ["celltrion", "celtrion"],
    "삼성바이오로직스":  ["samsungbio", "sbio", "sbl"],
    "현대백화점":        ["hyundaidep", "hdept"],
    "이마트":            ["emart", "e_mart"],
    "한국전력":          ["kepco", "koreanelec"],
    "한전KPS":           ["kepco_kps", "kps"],
    "현대글로비스":      ["glovis", "hyundaiglovis"],
    "한진칼":            ["hanjinkal", "hjkal"],
    "제주항공":          ["jejuair", "jeju_air"],
    "팬오션":            ["panocean", "pan_ocean"],
    "대한항공":          ["koreanair", "kal", "korean_air"],
    "파트론":            ["partron"],
    "고영":              ["koh_young", "kohyoung"],
    "삼성SDI":           ["samsungsdi", "sec_sdi", "sdi"],
    "씨젠":              ["seegene"],
    "아이센스":          ["isens", "i_sens"],
    "HLB":               ["hlb", "hlblife", "hlbstock"],
    "인바디":            ["inbody"],
    "디오":              ["dio", "dioimplant"],
    "뷰웍스":            ["vieworks"],
    "파마리서치":        ["pharmaresearch", "pharma"],
    "아모레퍼시픽":      ["amorepacific", "amore"],
    "케어젠":            ["caregen"],
    "LG생활건강":        ["lghnh", "lg_hnh"],
    "리노공업":          ["rinoindustrial", "rino"],
    "한미반도체":        ["hanmisemi", "hanmi_semi"],
    "한솔케미칼":        ["hansol", "hansolchem"],
    "DL":                ["dl", "dlcorp"],
    "SK케미칼":          ["skchemical", "sk_chem"],
    "금호석유화학":      ["kumho", "kumhochem"],
    "OCI홀딩스":         ["oci", "ociholdings"],
    "NICE평가정보":      ["nice", "nicecredit"],
    "현대로템":          ["rotem", "hyundairotem"],
    "한화에어로스페이스": ["hanwhaaero", "hanwha_aero", "hanwhaaerospace"],
    "미래에셋증권":      ["miraeasset", "miraestock"],
    "메리츠금융지주":    ["meritz", "meritzfin"],
    "F&F홀딩스":         ["fnf", "fnfholdings"],
    "F&F":               ["fnf_brand", "ff"],
    "우리기술투자":      ["wooritech", "wootech"],
    "한전기술":          ["kepcotech", "kepco_tech"],
    "SK디스커버리":      ["skdiscovery", "sk_disc"],
    "골프존":            ["golfzon"],
    "크래프톤":          ["krafton", "pubg"],
    "이녹스첨단소재":    ["ienox", "inoxam"],
    "JYP Ent.":          ["jyp", "jypent", "jypentertainment"],
    "카카오뱅크":        ["kakaobank", "kakao_bank"],
    "SK바이오사이언스":  ["skbio", "skbioscience"],
    "호텔신라":          ["hotelshilla", "shilla"],
    "BGF리테일":         ["bgfretail", "bgf_retail"],
    "한국가스공사":      ["kogas", "korea_gas"],
    "HMM":               ["hmm_shipping", "hmm2", "hmm_stock"],
    "케이엠더블유":      ["kmw"],
    "솔브레인홀딩스":    ["soulbrain", "solbrain"],
    "천보":              ["chunbo", "cheonbo"],
    "삼성전기":          ["samsemco", "samsungelmo"],
    "더블유씨피":        ["wcp", "wcpcorp"],
    "클래시스":          ["classys"],
    "에스디바이오센서":  ["sdbiosensor", "sd_bio"],
    "삼성전자":          ["samsungelec", "sec", "samsung_elec"],
}

# 주식 관련 주요 갤러리 (고정)
BASE_GALLERIES = {
    "미국주식갤":  "stockus",
    "한국주식갤":  "krstock",
    "코스피갤":    "kospi",
    "나스닥갤":    "nasdaq",
    "해외주식갤":  "tenbagger",
    "반도체산업갤": "tsmcsamsungskhynix",
}


def check_gallery(gall_id: str, gall_type: str = "mgallery") -> tuple[bool, str]:
    """
    갤러리 존재 여부 + 실제 갤러리명 반환
    반환: (존재여부, 갤러리명)
    """
    if gall_type == "mgallery":
        url = f"https://gall.dcinside.com/mgallery/board/lists?id={gall_id}&page=1"
    else:
        url = f"https://gall.dcinside.com/board/lists/?id={gall_id}&page=1"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        text = resp.text

        # 존재하지 않거나 폐쇄된 경우
        if len(text) < 3000:
            return False, ""
        if "alert" in text[:300] and ("존재하지 않" in text or "폐쇄" in text):
            return False, ""

        soup = BeautifulSoup(text, "html.parser")

        # 갤러리 이름 추출
        title_tag = (
            soup.select_one("h2.block_megall_tit")
            or soup.select_one("h4.block_megall_tit")
            or soup.select_one(".gall_tit")
            or soup.select_one("title")
        )
        gall_name = title_tag.get_text(strip=True) if title_tag else ""

        # 게시글이 실제로 있는지 확인
        has_posts = len(soup.select("tr.ub-content")) > 0

        return has_posts, gall_name

    except Exception:
        return False, ""


def find_gallery_id(stock_name: str, candidates: list[str]) -> dict:
    """
    종목명에 대한 갤러리 ID 탐색
    반환: {"stock": ..., "gall_id": ..., "gall_type": ..., "gall_name": ..., "status": confirmed/review/not_found}
    """
    for gall_type in ["mgallery", "board"]:
        for gid in candidates:
            exists, gall_name = check_gallery(gid, gall_type)
            if exists:
                # 갤러리명에 종목명 키워드가 포함되는지 확인
                stock_keyword = stock_name.replace("홀딩스","").replace("그룹","").replace(" ","").lower()
                gall_name_clean = gall_name.replace(" ","").lower()

                status = "confirmed" if stock_keyword[:2] in gall_name_clean else "review"

                return {
                    "stock":     stock_name,
                    "gall_id":   gid,
                    "gall_type": gall_type,
                    "gall_name": gall_name,
                    "status":    status,
                }
            time.sleep(random.uniform(0.3, 0.7))

    return {
        "stock":     stock_name,
        "gall_id":   "",
        "gall_type": "",
        "gall_name": "",
        "status":    "not_found",
    }


def main():
    results = []
    confirmed = []
    review = []
    not_found = []

    total = len(STOCK_CANDIDATES)
    print(f"총 {total}개 종목 갤러리 탐색 시작\n")

    for idx, (stock_name, candidates) in enumerate(STOCK_CANDIDATES.items(), 1):
        print(f"[{idx:>3}/{total}] {stock_name} 탐색 중...", end=" ", flush=True)

        result = find_gallery_id(stock_name, candidates)
        results.append(result)

        if result["status"] == "confirmed":
            confirmed.append(result)
            print(f"✓ 확인됨: {result['gall_type']}/{result['gall_id']} ({result['gall_name'][:20]})")
        elif result["status"] == "review":
            review.append(result)
            print(f"? 검토필요: {result['gall_type']}/{result['gall_id']} ({result['gall_name'][:20]})")
        else:
            not_found.append(result)
            print(f"✗ 없음")

        time.sleep(random.uniform(0.5, 1.0))

    # 결과 저장
    output = {
        "base_galleries": BASE_GALLERIES,
        "confirmed":  {r["stock"]: {"gall_id": r["gall_id"], "gall_type": r["gall_type"], "gall_name": r["gall_name"]} for r in confirmed},
        "review":     {r["stock"]: {"gall_id": r["gall_id"], "gall_type": r["gall_type"], "gall_name": r["gall_name"]} for r in review},
        "not_found":  [r["stock"] for r in not_found],
    }

    with open("gallery_map.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n\n===== 탐색 완료 =====")
    print(f"확인됨:    {len(confirmed)}개")
    print(f"검토필요:  {len(review)}개  ← gallery_map.json에서 수동 확인 필요")
    print(f"없음:      {len(not_found)}개")
    print(f"\n결과 저장: gallery_map.json")

    if review:
        print(f"\n[검토 필요 목록]")
        for r in review:
            print(f"  {r['stock']}: {r['gall_type']}/{r['gall_id']} → '{r['gall_name'][:30]}'")


if __name__ == "__main__":
    main()