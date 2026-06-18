"""
shared_checkpoint.py  —  Supabase 기반 분산 체크포인트
────────────────────────────────────────────────────────
서로 다른 컴퓨터에서 forward / reverse 워커가 동시에 돌 때
Supabase PostgreSQL을 중앙 저장소로 사용한다.

기존 코드와 동일한 인터페이스를 유지하므로 import 줄만 바꾸면 된다:
    from shared_checkpoint import (
        SharedCheckpointClient, CrawlJob,
        get_page_iter_range, get_next_page_after, is_job_finished,
    )

──────────────────────────────────────────────────────────
Supabase 테이블 DDL  (Supabase SQL Editor에서 한 번만 실행)
──────────────────────────────────────────────────────────
CREATE TABLE crawl_jobs (
    id                BIGSERIAL PRIMARY KEY,
    gall_id           TEXT        NOT NULL,
    gall_type         TEXT        NOT NULL,
    gall_name         TEXT        NOT NULL,
    shard_no          INT         NOT NULL DEFAULT 0,
    page_start        INT         NOT NULL,
    page_end          INT         NOT NULL,
    next_page         INT         NOT NULL,
    direction         TEXT        NOT NULL CHECK (direction IN ('forward','reverse','policy_forward','policy_reverse')),
    collection_policy TEXT,                          -- policy 수집 전용 (general_full 등). forward/reverse는 NULL
    status            TEXT        NOT NULL DEFAULT 'todo'
                      CHECK (status IN ('todo','claimed','done','failed')),
    claimed_by        TEXT,
    claimed_at        TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (gall_id, shard_no, direction)
);

CREATE INDEX ON crawl_jobs (status, direction, heartbeat_at);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$;

CREATE TRIGGER trg_crawl_jobs_updated_at
BEFORE UPDATE ON crawl_jobs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

──────────────────────────────────────────────────────────
환경변수  (.env 파일 또는 OS 환경변수)
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
──────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import time
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# python-dotenv 있으면 자동 로드, 없으면 OS 환경변수 사용
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL: str = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY: str = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

_TABLE = "crawl_jobs"
_API   = f"{SUPABASE_URL}/rest/v1/{_TABLE}"
_BASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

_CLAIM_MAX_RETRY  = 5
_CLAIM_RETRY_WAIT = (1.0, 3.0)


# ─────────────────────────────────────────────
# 데이터 클래스  (기존 인터페이스와 동일)
# ─────────────────────────────────────────────
@dataclass
class CrawlJob:
    id:         int
    gall_id:    str
    gall_type:  str
    gall_name:  str
    shard_no:   int
    page_start: int
    page_end:   int
    next_page:  int
    direction:         str        # "forward" | "reverse" | "policy_forward" | "policy_reverse"
    collection_policy: str | None = None  # policy 수집 전용. forward/reverse는 None
    candidate_cache:   dict | None = None  # 후보 빌드 중간저장


# ─────────────────────────────────────────────
# 헬퍼 함수  (기존 인터페이스와 동일)
# ─────────────────────────────────────────────
def _is_forward(direction: str) -> bool:
    return direction in ("forward", "policy_forward")


def get_page_iter_range(job: CrawlJob) -> list[int]:
    """
    job의 direction에 따라 next_page 부터 순회할 페이지 목록 반환.
    DB에는 항상 page_start <= page_end 로 저장되므로
    reverse/policy_reverse는 next_page 가 page_end → page_start 방향으로 감소.
    """
    if _is_forward(job.direction):
        return list(range(job.next_page, job.page_end + 1))
    else:
        return list(range(job.next_page, job.page_start - 1, -1))


def get_next_page_after(direction: str, current_page: int) -> int:
    return current_page + 1 if _is_forward(direction) else current_page - 1


def is_job_finished(direction: str, page_start: int, page_end: int, next_page: int) -> bool:
    if _is_forward(direction):
        return next_page > page_end
    else:
        return next_page < page_start


# ─────────────────────────────────────────────
# 내부 HTTP 유틸
# ─────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(params: dict) -> list[dict]:
    resp = requests.get(
        _API,
        headers=_BASE_HEADERS,
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _patch(params: dict, payload: dict) -> list[dict]:
    resp = requests.patch(
        _API,
        headers={**_BASE_HEADERS, "Prefer": "return=representation"},
        params=params,
        json=payload,
        timeout=15,
    )
    if not resp.ok:
        print(f"[Supabase PATCH 에러] {resp.status_code}: {resp.text[:400]}")
    resp.raise_for_status()
    return resp.json()


def _post(payload: dict | list, prefer: str = "return=representation") -> list[dict]:
    resp = requests.post(
        _API,
        headers={**_BASE_HEADERS, "Prefer": prefer},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _row_to_job(row: dict) -> CrawlJob:
    return CrawlJob(
        id=row["id"],
        gall_id=row["gall_id"],
        gall_type=row["gall_type"],
        gall_name=row["gall_name"],
        shard_no=row["shard_no"],
        page_start=row["page_start"],
        page_end=row["page_end"],
        next_page=row["next_page"],
        direction=row["direction"],
        collection_policy=row.get("collection_policy"),
        candidate_cache=row.get("candidate_cache"),
    )


# ─────────────────────────────────────────────
# SharedCheckpointClient  (기존 인터페이스와 동일)
# ─────────────────────────────────────────────
class SharedCheckpointClient:
    """
    Supabase를 백엔드로 사용하는 분산 체크포인트 클라이언트.
    낙관적 락(optimistic lock)으로 중복 claim을 방지한다.
    """

    def __init__(self, claimed_by: str):
        self.claimed_by = claimed_by

    # ── Job 등록 ─────────────────────────────────────────────────────────

    def register_job(
        self,
        gall_id:           str,
        gall_type:         str,
        gall_name:         str,
        shard_no:          int,
        page_start:        int,
        page_end:          int,
        direction:         str,
        collection_policy: str | None = None,
        status:            str = "todo",
    ) -> dict:
        """
        단일 shard job 등록.
        UNIQUE(gall_id, shard_no, direction) 충돌 시 기존 행을 유지(무시).
        collection_policy: policy 수집 전용 (general_full 등). forward/reverse는 None.
        """
        next_page = page_start if _is_forward(direction) else page_end
        row = {
            "gall_id":           gall_id,
            "gall_type":         gall_type,
            "gall_name":         gall_name,
            "shard_no":          shard_no,
            "page_start":        page_start,
            "page_end":          page_end,
            "next_page":         next_page,
            "direction":         direction,
            "collection_policy": collection_policy,
            "status":            status,
        }
        resp = requests.post(
            _API,
            headers={**_BASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "gall_name,direction,shard_no"},
            json=row,
            timeout=15,
        )
        if not resp.ok:
            print(f"[Supabase 에러] {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else {}

    def register_jobs_bulk(self, jobs: list[dict]) -> int:
        """
        여러 job 일괄 등록. 중복은 무시. 실제 삽입 건수 반환.

        jobs 각 원소 형식:
        {
            "gall_id", "gall_type", "gall_name",
            "shard_no", "page_start", "page_end",
            "direction",
            "status"  (optional, default "todo")
        }
        """
        if not jobs:
            return 0

        # next_page 자동 계산
        normalized = []
        for j in jobs:
            direction = j["direction"]
            row = {**j}
            if "next_page" not in row:
                row["next_page"] = j["page_start"] if _is_forward(direction) else j["page_end"]
            if "status" not in row:
                row["status"] = "todo"
            normalized.append(row)

        resp = requests.post(
            _API,
            headers={**_BASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "gall_name,direction,shard_no"},
            json=[r for r in normalized if r.get("status") != "done"],
            timeout=30,
        )
        if not resp.ok:
            print(f"[Supabase 에러] {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return len(resp.json())

    # ── Job claim ────────────────────────────────────────────────────────

    def claim_next_job(
        self, direction: str, stale_minutes: int = 20
    ) -> Optional[CrawlJob]:
        """
        todo 이거나 stale 상태인 claimed job 을 선점.
        낙관적 락으로 경합 시 자동 재시도.
        더 이상 없으면 None 반환.
        """
        for _ in range(_CLAIM_MAX_RETRY):
            candidate = self._find_claimable(direction, stale_minutes)
            if candidate is None:
                return None

            job_id     = candidate["id"]
            old_status = candidate["status"]
            old_hb     = candidate.get("heartbeat_at")

            # 낙관적 락 조건: id + status 로만 조건을 걸고
            # heartbeat_at 은 NULL 필터 호환성 문제로 제외
            # (id가 PK이므로 status 조건만으로도 충분히 경합 방지)
            lock_params: dict = {
                "id":     f"eq.{job_id}",
                "status": f"eq.{old_status}",
            }

            updated = _patch(lock_params, {
                "status":       "claimed",
                "claimed_by":  self.claimed_by,
                "claimed_at":   _now_iso(),
                "heartbeat_at": _now_iso(),
            })

            if updated:
                return _row_to_job(updated[0])

            # 다른 워커가 먼저 선점 → 짧게 대기 후 재시도
            time.sleep(random.uniform(*_CLAIM_RETRY_WAIT))

        return None

    def _get_busy_gall_ids(self, direction: str) -> set[str]:
        """
        반대 direction에서 현재 claimed 중인 gall_id 목록 반환.
        policy_forward ↔ policy_reverse 간 중복 스캔 방지용.
        """
        opposite = (
            "policy_reverse" if direction == "policy_forward"
            else "policy_forward" if direction == "policy_reverse"
            else None
        )
        if not opposite:
            return set()
        try:
            rows = _get({
                "direction": f"eq.{opposite}",
                "status":    "eq.claimed",
                "select":    "gall_id",
            })
            return {r["gall_id"] for r in rows}
        except Exception:
            return set()

    def _find_claimable(self, direction: str, stale_minutes: int) -> Optional[dict]:
        """
        todo 우선, 없으면 stale claimed 반환.
        policy 계열은 반대 direction이 같은 갤러리를 스캔 중이면 건너뜀
        (빌드 완료 후 캐시 공유 대기).
        """
        # 반대 direction에서 스캔 중인 갤러리 목록
        busy_gall_ids = self._get_busy_gall_ids(direction)

        # 1) todo — busy 갤러리 제외하고 순서대로 탐색
        rows = _get({
            "direction": f"eq.{direction}",
            "status":    "eq.todo",
            "order":     "id.asc",
            "limit":     "20",  # 여러 개 가져와서 busy 제외 후 선택
        })
        for row in rows:
            if row["gall_id"] not in busy_gall_ids:
                return row
        # busy 갤러리만 남아있으면 일단 첫 번째 반환 (무한 대기 방지)
        if rows:
            return rows[0]

        # 2) stale claimed
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        ).isoformat()
        rows = _get({
            "direction":    f"eq.{direction}",
            "status":       "eq.claimed",
            "heartbeat_at": f"lt.{stale_cutoff}",
            "order":        "heartbeat_at.asc",
            "limit":        "1",
        })
        return rows[0] if rows else None

    # ── Heartbeat ────────────────────────────────────────────────────────

    def heartbeat_job(self, job_id: int, last_page: int = 0, next_page: int = 0) -> None:
        """
        진행 중 주기적으로 호출해 stale로 분류되지 않도록 한다.
        next_page > 0 인 경우에만 next_page도 함께 갱신한다.
        """
        payload: dict = {"heartbeat_at": _now_iso()}
        if next_page > 0:
            payload["next_page"] = next_page
        if last_page > 0:
            payload["last_page"] = last_page
        _patch({"id": f"eq.{job_id}"}, payload)

    # ── 완료 / 실패 ──────────────────────────────────────────────────────

    def complete_job(self, job_id: int) -> None:
        _patch(
            {"id": f"eq.{job_id}"},
            {
                "status":       "done",
                "finished_at": _now_iso(),
                "heartbeat_at": _now_iso(),
            },
        )

    def fail_job(self, job_id: int, error_message: str) -> None:
        _patch(
            {"id": f"eq.{job_id}"},
            {
                "status":        "failed",
                "last_error": str(error_message)[:2000],
                "heartbeat_at":  _now_iso(),
            },
        )

    # ── Stale reset ──────────────────────────────────────────────────────

    def reset_stale_jobs(self, direction: str, stale_minutes: int = 20) -> int:
        """
        heartbeat 가 stale_minutes 이상 갱신되지 않은 claimed job 을 todo 로 되돌린다.
        반환값: 되돌린 job 수.
        """
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        ).isoformat()
        rows = _patch(
            {
                "direction":    f"eq.{direction}",
                "status":       "eq.claimed",
                "heartbeat_at": f"lt.{stale_cutoff}",
            },
            {
                "status":      "todo",
                "claimed_by": None,
                "claimed_at":  None,
            },
        )
        return len(rows)

    # ── 모니터링 ─────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """direction/status 별 job 수 반환 (모니터링용)."""
        rows = _get({"select": "status,direction", "limit": "10000"})
        summary: dict = {}
        for r in rows:
            key = f"{r['direction']}/{r['status']}"
            summary[key] = summary.get(key, 0) + 1
        return summary