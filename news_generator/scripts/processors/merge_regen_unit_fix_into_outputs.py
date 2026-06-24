#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""단위 버그 정정 재생성분(271건)을 outputs_all.jsonl에 오버레이 병합.

재생성된 custom_id는 새 출력으로 교체하고, 나머지는 기존 출력을 유지한다.
교체 전 백업(outputs_all.before_unit_fix.jsonl)을 남긴다.
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
OUTPUTS = BASE_DIR / "data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.jsonl"
BACKUP = BASE_DIR / "data/interim/pr06a_full_outputs_v4_2_all_stocks/outputs_all.before_unit_fix.jsonl"
REGEN = BASE_DIR / "data/interim/pr06a_regen_v4_2_unit_fix/regen_outputs.jsonl"


def load_by_cid(path: Path) -> dict[str, str]:
    by_cid: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cid = json.loads(line)["custom_id"]
        by_cid[cid] = line
    return by_cid


def main() -> None:
    if not REGEN.exists():
        raise SystemExit(f"재생성 출력 없음: {REGEN}")
    base = load_by_cid(OUTPUTS)
    regen = load_by_cid(REGEN)

    if not BACKUP.exists():
        BACKUP.write_text(OUTPUTS.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[backup] {BACKUP}")

    replaced = 0
    for cid, line in regen.items():
        if cid in base:
            replaced += 1
        base[cid] = line

    # 원래 요청 순서 유지: 요청 파일 순서대로 기록
    req_order = [
        json.loads(l)["custom_id"]
        for l in (BASE_DIR / "data/interim/pr06a_full_requests_v4_2_all_stocks/stock_news_sample_requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if l.strip()
    ]
    with OUTPUTS.open("w", encoding="utf-8") as f:
        written = 0
        for cid in req_order:
            if cid in base:
                f.write(base[cid] + "\n")
                written += 1
    print(f"[merge] regen={len(regen)} replaced={replaced} total_written={written}")
    print(f"  -> {OUTPUTS}")


if __name__ == "__main__":
    main()
