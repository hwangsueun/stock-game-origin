"""
migrate_checkpoint.py
─────────────────────
로컬 checkpoint JSON + gallery_policy_decision.csv 를 읽어
Supabase crawl_jobs 테이블에 전체 job 을 등록하고,
이미 완료된 갤러리는 status='done' 으로 표시하는 1회성 마이그레이션 스크립트.

실행 방법:
    python migrate_checkpoint.py \
        --checkpoint data/raw/policy_collection_checkpoint.json \
        --policy     data/gall_division/gallery_policy_decision.csv \
        --direction  forward        # 이 체크포인트가 forward 수집분이면

    # reverse 체크포인트가 따로 있으면 한 번 더 실행:
    python migrate_checkpoint.py \
        --checkpoint data/raw/policy_collection_checkpoint_reverse.json \
        --policy     data/gall_division/gallery_policy_decision.csv \
        --direction  reverse
"""

import argparse
import json
import os
import sys

import pandas as pd

# shared_checkpoint.py 가 같은 디렉터리에 있어야 함
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared_checkpoint import SharedCheckpointClient

# python-dotenv 있으면 자동 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ─────────────────────────────────────────────
# 갤러리 정책 테이블 로드
# ─────────────────────────────────────────────
def load_policy_table(policy_csv: str) -> list[dict]:
    df = pd.read_csv(policy_csv)
    required = {"gallery_name", "gall_id", "gall_type", "total_pages", "collection_policy"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"누락 컬럼: {sorted(missing)}")

    rows = []
    for r in df.to_dict("records"):
        policy = str(r["collection_policy"]).strip()
        if policy == "gap_unassigned":
            continue
        rows.append({
            "gallery_name":      str(r["gallery_name"]).strip(),
            "gall_id":           str(r["gall_id"]).strip(),
            "gall_type":         str(r["gall_type"]).strip(),
            "total_pages":       int(r["total_pages"]),
            "collection_policy": policy,
        })
    return rows


# ─────────────────────────────────────────────
# 로컬 체크포인트 로드
# ─────────────────────────────────────────────
def load_local_checkpoint(checkpoint_path: str) -> set[str]:
    """
    done_galleries 키에서 완료된 done_key 집합을 반환.
    done_key 형식: "gall_id::collection_policy"
    """
    if not os.path.exists(checkpoint_path):
        print(f"[경고] 체크포인트 파일 없음: {checkpoint_path} → 완료 항목 0건으로 진행")
        return set()

    with open(checkpoint_path, "r", encoding="utf-8") as f:
        cp = json.load(f)

    done = cp.get("done_galleries", {})
    completed_keys = {k for k, v in done.items() if v == "done"}
    print(f"[로컬 체크포인트] 완료 갤러리 수: {len(completed_keys)}")
    return completed_keys


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="로컬 체크포인트 → Supabase 마이그레이션")
    parser.add_argument("--checkpoint", required=True, help="로컬 checkpoint JSON 경로")
    parser.add_argument("--policy",     required=True, help="gallery_policy_decision.csv 경로")
    parser.add_argument(
        "--direction", required=True,
        choices=["forward", "reverse", "policy_forward", "policy_reverse"],
        help="이 체크포인트의 수집 방향 (policy 수집이면 policy_forward / policy_reverse)"
    )
    parser.add_argument(
        "--worker",  default="migrator",
        help="Supabase에 등록할 worker_name (기본: migrator)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=200,
        help="Supabase bulk insert 배치 크기 (기본: 200)"
    )
    args = parser.parse_args()

    # ── 데이터 로드 ──────────────────────────────────────────────────────
    policies       = load_policy_table(args.policy)
    completed_keys = load_local_checkpoint(args.checkpoint)
    direction      = args.direction
    client         = SharedCheckpointClient(claimed_by=args.worker)

    print(f"\n[정책 갤러리 수] {len(policies)}")
    print(f"[방향]           {direction}")
    print(f"[배치 크기]      {args.batch_size}")
    print()

    # ── job 목록 구성 ────────────────────────────────────────────────────
    # 이 프로젝트는 갤러리 1개 = shard 1개 (shard_no=0, 전체 페이지)
    jobs_to_insert: list[dict] = []

    for pol in policies:
        done_key = f"{pol['gall_id']}::{pol['collection_policy']}"
        is_done  = done_key in completed_keys

        # DB에 page_start <= page_end 제약이 있으므로 항상 1 ~ total_pages 로 저장
        # 방향은 direction 컬럼으로만 구분
        page_start = 1
        page_end   = pol["total_pages"]
        is_forward_dir = direction in ("forward", "policy_forward")

        # collection_policy: policy 계열 direction에서만 기록
        col_policy = pol["collection_policy"] if direction.startswith("policy") else None

        # next_page: forward계열은 1부터, reverse계열은 total_pages부터 시작
        # done이면 완료 표시 (범위 밖 값으로 설정)
        if is_done:
            next_page = page_end + 1 if is_forward_dir else page_start - 1
        else:
            next_page = page_start if is_forward_dir else page_end

        jobs_to_insert.append({
            "gall_id":           pol["gall_id"],
            "gall_type":         pol["gall_type"],
            "gall_name":         pol["gallery_name"],
            "shard_no":          1,
            "total_pages":       pol["total_pages"],
            "page_start":        page_start,
            "page_end":          page_end,
            "collection_policy": col_policy,
            "direction":         direction,
            "status":            "done" if is_done else "todo",
            "next_page":         next_page,
        })

    done_count    = sum(1 for j in jobs_to_insert if j["status"] == "done")
    pending_count = sum(1 for j in jobs_to_insert if j["status"] == "todo")
    print(f"[등록 예정] total={len(jobs_to_insert)} | done={done_count} | pending={pending_count}")

    # ── Supabase 일괄 등록 ───────────────────────────────────────────────
    inserted_total = 0
    skipped_total  = 0
    batch_size     = args.batch_size

    for i in range(0, len(jobs_to_insert), batch_size):
        batch = jobs_to_insert[i: i + batch_size]
        inserted = client.register_jobs_bulk(batch)
        skipped  = len(batch) - inserted
        inserted_total += inserted
        skipped_total  += skipped
        print(
            f"  배치 {i // batch_size + 1}: "
            f"삽입={inserted} / 중복 스킵={skipped} "
            f"(누계 삽입={inserted_total})"
        )

    print()
    print("=" * 60)
    print("[마이그레이션 완료]")
    print(f"  전체 갤러리:     {len(jobs_to_insert)}")
    print(f"  Supabase 신규 삽입: {inserted_total}")
    print(f"  중복으로 스킵:   {skipped_total}")
    print(f"    ├ done 으로 등록:    {done_count}")
    print(f"    └ pending 으로 등록: {pending_count}")
    print("=" * 60)
    print()
    print("[다음 단계]")
    print("  forward/reverse 수집:")
    print("    python collect_forward.py   # WORKER_NAME=hgs_forward_01")
    print("    python collect_reverse.py  # WORKER_NAME=kte_reverse_01")
    print("  policy forward/reverse 수집:")
    print("    python collect_policy_posts.py   # DIRECTION=policy_forward")
    print("    python collect_policy_posts_reverse.py  # DIRECTION=policy_reverse")
    print("  (모든 워커가 동일한 Supabase crawl_jobs 테이블을 공유합니다)")


if __name__ == "__main__":
    main()