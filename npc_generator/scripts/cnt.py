import pandas as pd
import os

raw = "/Users/hgs/Desktop/IISE CD/npc_generator/data/raw/"

files = [
    "dci_comments.csv",
    "dci_comments_hgs_forward_01.csv",
    "dci_comments_large_rank11_20_2013_2023.csv",
    "dci_comments_large_top10_2013_2023.csv",
    "dci_comments_policy_merged.csv",
]

dfs = []
for f in files:
    path = raw + f
    if not os.path.exists(path):
        print(f"{f}: 없음 스킵")
        continue
    df = pd.read_csv(path, low_memory=False, dtype={"gall_id": str, "post_no": str, "cmt_no": str})
    print(f"{f}: {len(df)}행")
    dfs.append(df)

merged = pd.concat(dfs).drop_duplicates(subset=["gall_id", "post_no", "cmt_no"])
merged.to_csv(raw + "dci_comments_final.csv", index=False, encoding="utf-8-sig")
print(f"\n최종: {len(merged)}행")