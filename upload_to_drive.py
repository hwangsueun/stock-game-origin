"""
IISE CD 데이터 → Google Drive 업로드 스크립트
----------------------------------------------
실행 전 준비:
  pip install google-auth google-auth-oauthlib google-api-python-client

실행:
  python upload_to_drive.py

처음 실행 시 브라우저에서 Google 계정 인증 창이 열립니다.
인증 후 token.json이 생성되며, 이후엔 자동 로그인됩니다.

Google Cloud Console에서 OAuth2 credentials.json 발급 필요:
  https://console.cloud.google.com/apis/credentials
  → Create Credentials → OAuth client ID → Desktop App
  → credentials.json 다운로드 후 이 파일과 같은 폴더에 저장
"""

import os
import sys
import json
import time
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Drive 폴더 ID (이미 생성된 폴더) ─────────────────────────────────────
FOLDER_IDS = {
    "raw":                      "1LWVhU1EmPQlXWJczNC38LvUl4O_bhKE2",
    "raw/market_indicator":     "19MO59UUKULHBvz_A8FSphQXlQarq1kaE",
    "raw/bond_universe":        "1UgLHLRwbNFbsEX3sCz-zerP0y4iLp98H",
    "raw/market_event":         "1gTQeKWziLtFGvzmpyF1xO_6lo7pXaoVR",
    "raw/crypto_universe":      "14wH8fH7Y8uZ8EpOjw_pAH0XY99FCvEr8",
    "processed":                "1XQgdAwy4iTG3YW9H6i_ue0WiS4qSTcKQ",
    "processed/crypto_universe":"1PsHXdq8XyjQkWumfjlnP1TxVLTq7hyqI",
    "processed/market_indicator":"16tPG2h8pyOw9BG1DN3yKQ__woK1vek70",
    "interim":                  "13v9eem9qnthvbFLK7xa3C0dZ-DMgmArs",
    "interim/news_generator":   "1TlNvqm7Z6PK61jWvhRzKgZFEDD0TC3HT",
    "interim/npc_generator":    "1lQbBNmt5V_xZp9Cz7D0_0Sn5oPRMUv3X",
    "interim/news_pipeline":    "1ck39OVPz-UtaA55okmse20zaEw48Zt01",
    "done":                     "1SA9vXTXG6WgSRPCAemgYWMHKq8TJyr-G",
}

# ── 로컬 경로 → Drive 폴더 매핑 ──────────────────────────────────────────
BASE_DIR = Path(__file__).parent

UPLOAD_MAP = [
    # (로컬 폴더 경로, Drive 폴더 키, 재귀 여부)
    ("bond_universe/data",                      "raw/bond_universe",         True),
    ("data/raw/market_event",                   "raw/market_event",          False),
    ("data/raw/coin_history_pre2014",           "raw/crypto_universe",       False),
    ("market_indicator/data/raw",               "raw/market_indicator",      False),

    ("crypto_universe/data/processed",          "processed/crypto_universe", False),
    ("data/processed",                          "processed/crypto_universe", False),
    ("market_indicator/data/processed",         "processed/market_indicator",False),

    ("data/interim",                            "interim/news_pipeline",     True),
    ("news_generator/data/interim",             "interim/news_generator",    True),
    ("news_generator/data/processed",           "interim/news_generator",    True),
    ("npc_generator/data",                      "interim/npc_generator",     True),

    ("demo/llm_generated_news_2018.csv",        "done",                      False),

    # 코인 원본 (1268개 - 시간 걸림)
    ("crypto_universe/data/raw/coin_history",   "raw/crypto_universe",       True),
]

SKIP_PATTERNS = {".DS_Store", "__pycache__", ".env", "venv", ".venv", "node_modules"}
UPLOAD_EXTS   = {".csv", ".json", ".xlsx", ".parquet", ".txt", ".md"}

SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_service():
    creds = None
    token_path = Path("token.json")
    creds_path = Path("credentials.json")

    if not creds_path.exists():
        print("❌ credentials.json 없음.")
        print("   https://console.cloud.google.com/apis/credentials 에서")
        print("   OAuth2 Desktop App 자격증명을 만들고 credentials.json 저장 후 재실행.")
        sys.exit(1)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def should_skip(path: Path) -> bool:
    return any(p in SKIP_PATTERNS for p in path.parts)


def get_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".csv":  "text/csv",
        ".json": "application/json",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".txt":  "text/plain",
        ".md":   "text/markdown",
    }.get(ext, "application/octet-stream")


def upload_file(service, local_path: Path, parent_id: str, uploaded: set):
    key = str(local_path)
    if key in uploaded:
        return
    if should_skip(local_path) or local_path.suffix.lower() not in UPLOAD_EXTS:
        return

    print(f"  ↑ {local_path.name}", end=" ", flush=True)
    try:
        media = MediaFileUpload(str(local_path), mimetype=get_mime(local_path), resumable=True)
        meta  = {"name": local_path.name, "parents": [parent_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
        uploaded.add(key)
        print("✓")
    except Exception as e:
        print(f"✗ ({e})")
    time.sleep(0.1)  # rate limit


def upload_folder(service, local_dir: Path, parent_id: str, recursive: bool, uploaded: set):
    if not local_dir.exists():
        print(f"  [skip] {local_dir} 없음")
        return

    # Drive에 서브폴더 생성 캐시
    subfolder_cache = {}

    def _get_or_create_subfolder(name, pid):
        cache_key = (name, pid)
        if cache_key in subfolder_cache:
            return subfolder_cache[cache_key]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [pid]}
        r = service.files().create(body=meta, fields="id").execute()
        subfolder_cache[cache_key] = r["id"]
        return r["id"]

    def _walk(cur_dir: Path, cur_parent: str):
        for item in sorted(cur_dir.iterdir()):
            if should_skip(item):
                continue
            if item.is_file():
                upload_file(service, item, cur_parent, uploaded)
            elif item.is_dir() and recursive:
                sub_id = _get_or_create_subfolder(item.name, cur_parent)
                _walk(item, sub_id)

    print(f"\n📂 {local_dir} → Drive/{parent_id}")
    _walk(local_dir, parent_id)


def main():
    print("🔐 Google 인증 중...")
    service = get_service()
    print("✅ 인증 완료\n")

    uploaded = set()

    for local_rel, drive_key, recursive in UPLOAD_MAP:
        local_path = BASE_DIR / local_rel
        parent_id  = FOLDER_IDS[drive_key]

        if local_path.is_file():
            print(f"\n📄 {local_rel}")
            upload_file(service, local_path, parent_id, uploaded)
        else:
            upload_folder(service, local_path, parent_id, recursive, uploaded)

    print(f"\n✅ 완료! 총 {len(uploaded)}개 파일 업로드됨")


if __name__ == "__main__":
    main()
