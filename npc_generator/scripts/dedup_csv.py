"""
중복 게시글/댓글 제거 스크립트
- post_no 기준으로 중복된 행을 제거하고 첫 번째 행만 유지합니다.
- 원본은 .bak으로 백업한 후 덮어씁니다.
"""

import os
import shutil
import pandas as pd

# ── 경로 설정 ────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
BASE_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
RUN_NAME = "large_top10_2013_2023"

POSTS_CSV    = os.path.join(BASE_OUTPUT_DIR, f"dci_posts_{RUN_NAME}.csv")
COMMENTS_CSV = os.path.join(BASE_OUTPUT_DIR, f"dci_comments_{RUN_NAME}.csv")


def dedup_posts(filepath: str):
    print(f"\n[게시글] {filepath}")
    if not os.path.exists(filepath):
        print("  파일 없음, 스킵")
        return

    df = pd.read_csv(filepath, dtype=str)
    before = len(df)
    print(f"  원본 행 수: {before:,}")

    # gall_id + post_no 조합으로 중복 제거, 마지막 등장 행 유지
    # (재시작 시 같은 post_no가 다시 수집되므로 최신 데이터가 아래쪽에 있음)
    df_dedup = df.drop_duplicates(subset=["gall_id", "post_no"], keep="last").reset_index(drop=True)
    after = len(df_dedup)
    removed = before - after

    print(f"  중복 제거 후 행 수: {after:,}  (제거된 행: {removed:,})")

    if removed == 0:
        print("  중복 없음, 파일 유지")
        return

    bak = filepath + ".bak"
    shutil.copy2(filepath, bak)
    print(f"  원본 백업: {bak}")

    df_dedup.to_csv(filepath, index=False, encoding="utf-8-sig")
    print(f"  저장 완료: {filepath}")


def dedup_comments(filepath: str):
    print(f"\n[댓글] {filepath}")
    if not os.path.exists(filepath):
        print("  파일 없음, 스킵")
        return

    df = pd.read_csv(filepath, dtype=str)
    before = len(df)
    print(f"  원본 행 수: {before:,}")

    # gall_id + post_no + cmt_no 조합으로 중복 제거
    df_dedup = df.drop_duplicates(subset=["gall_id", "post_no", "cmt_no"], keep="last").reset_index(drop=True)
    after = len(df_dedup)
    removed = before - after

    print(f"  중복 제거 후 행 수: {after:,}  (제거된 행: {removed:,})")

    if removed == 0:
        print("  중복 없음, 파일 유지")
        return

    bak = filepath + ".bak"
    shutil.copy2(filepath, bak)
    print(f"  원본 백업: {bak}")

    df_dedup.to_csv(filepath, index=False, encoding="utf-8-sig")
    print(f"  저장 완료: {filepath}")


if __name__ == "__main__":
    dedup_posts(POSTS_CSV)
    dedup_comments(COMMENTS_CSV)
    print("\n완료")