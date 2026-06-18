import osp
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("COINGECKO_API_KEY")
BASE_URL = "https://pro-api.coingecko.com/api/v3"

headers = {
    "x-cg-pro-api-key": API_KEY
}

url = f"{BASE_URL}/coins/list"
params = {
    "include_platform": "false"
}

resp = requests.get(url, headers=headers, params=params, timeout=30)
resp.raise_for_status()

coins = resp.json()
df = pd.DataFrame(coins)

os.makedirs("data/raw", exist_ok=True)
df.to_csv("data/raw/coins_list.csv", index=False, encoding="utf-8-sig")

print(f"saved: {len(df)} coins")