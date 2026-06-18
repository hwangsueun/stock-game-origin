"""
find_dart_name.py
DART corp_code_cache.xml에서 키워드로 회사명을 검색하는 유틸리티.

사용법:
    python find_dart_name.py KT
    python find_dart_name.py 엔씨
    python find_dart_name.py nc   ← 대소문자 무시

미매칭 회사의 DART 정확한 이름을 찾아 aliases.json에 추가할 때 사용.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CORP_CODE_PATH = Path("corp_code_cache.xml")


def search(keyword: str, max_results: int = 20) -> None:
    if not CORP_CODE_PATH.exists():
        print("corp_code_cache.xml 없음. dart_fetcher.py 먼저 실행하세요.")
        sys.exit(1)

    keyword_lower = keyword.lower()
    root = ET.parse(CORP_CODE_PATH).getroot()

    hits: list[tuple[str, str, str]] = []
    for item in root.iter("list"):
        name = (item.findtext("corp_name") or "").strip()
        if keyword_lower in name.lower():
            hits.append((
                name,
                item.findtext("corp_code", "").strip(),
                item.findtext("stock_code", "").strip(),
            ))

    if not hits:
        print(f"'{keyword}' 와 일치하는 회사가 없습니다.")
        return

    print(f"'{keyword}' 검색 결과 ({len(hits)}건, 상위 {min(len(hits), max_results)}개 표시)\n")
    print(f"{'회사명':<30} {'corp_code':<12} {'stock_code'}")
    print("-" * 58)
    for name, corp_code, stock_code in hits[:max_results]:
        print(f"{name:<30} {corp_code:<12} {stock_code or '-'}")

    if len(hits) > max_results:
        print(f"\n... 외 {len(hits) - max_results}건 더 있음. 키워드를 좁혀 검색하세요.")

    print("\n정확한 이름 확인 후 aliases.json에 추가:")
    print('  { "내가_쓰는_이름": "위에서_찾은_정확한_이름" }')


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python find_dart_name.py <검색어>")
        sys.exit(1)
    search(sys.argv[1])
