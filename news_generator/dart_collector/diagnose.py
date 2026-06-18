import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class DartCorpDiagnoser:
    def __init__(self, xml_path: str):
        self.xml_path = Path(xml_path)

    def load_root(self):
        if not self.xml_path.exists():
            raise FileNotFoundError(f"XML 파일을 찾을 수 없습니다: {self.xml_path}")

        return ET.parse(self.xml_path).getroot()

    def search(self, keywords):
        root = self.load_root()

        for keyword in keywords:
            print(f"\n=== '{keyword}' 검색 ===")
            found = False

            for item in root.iter("list"):
                name = (item.findtext("corp_name") or "").strip()

                if keyword.lower() in name.lower():
                    found = True
                    print(f"  repr: {repr(name)}")
                    print(f"  corp_code: {item.findtext('corp_code', '').strip()}")
                    print(f"  stock_code: {item.findtext('stock_code', '').strip() or '-'}")

            if not found:
                print("  검색 결과 없음")


def main():
    xml_path = "corp_code_cache.xml"

    if len(sys.argv) >= 2:
        keywords = sys.argv[1:]
    else:
        keywords = ["케이티앤지", "NC", "엔씨"]

    diagnoser = DartCorpDiagnoser(xml_path)
    diagnoser.search(keywords)


if __name__ == "__main__":
    main()
