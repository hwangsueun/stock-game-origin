import os
import requests

# .env 파일에서 환경변수 로드
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8", errors="ignore") as _f:
        for _line in _f:
            _line = _line.strip()
            if "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "iisecd-dc-candidate-cache"
headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

JOB_IDS = [2117, 2142]

for job_id in JOB_IDS:
    for part in range(30):
        path = f"candidates_{job_id}_part{part}.json"
        resp = requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 404:
            print(f"[{job_id}] part{part}부터 없음")
            break
        print(f"삭제: {path} → {resp.status_code}")

print("완료")