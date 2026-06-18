# pr06a v3.3 Bulk Generation Gate

이 문서는 LLM 사용량을 아끼기 위해 pr06a v3.3 결과를 전체 기간 본생성으로 확대하기 전에 반드시 통과해야 하는 기준을 정리한다.

## 원칙

- 모델의 `style_self_check`는 참고하지 않고 외부 audit 결과를 기준으로 판단한다.
- `detail_news`는 `detail_source_facts_ko`만 사용해야 한다.
- `raw_write_safe_facts_ko_for_audit_only`는 추적용이며 본문 생성 재료로 쓰면 안 된다.
- `topic_hints_ko`는 제목 보조용이며 본문 확장에 쓰면 안 된다.
- `claim_level == no_market_claim`이면 주가, 거래량, 투자심리, 시장 반응 표현을 금지한다.
- 5개 샘플, 19개 전체 샘플, 전체 기간 본생성은 각각 별도 게이트로 통과시킨다.

## Gate 1. 코드와 요청 파일 확인

```bash
cd "/Users/hgs/Desktop/IISE CD/news_generator"

python -m py_compile scripts/processors/pr06a_build_stock_news_sample_requests.py
python -m py_compile scripts/processors/audit_pr06a_v3_3_generated_stock_news.py

grep -n "DetailFactBuilder\|detail_source_facts_ko\|raw_write_safe_facts_ko_for_audit_only\|max-detail-facts" \
  scripts/processors/pr06a_build_stock_news_sample_requests.py
```

통과 기준:

- `DetailFactBuilder`가 존재한다.
- `detail_source_facts_ko`가 request payload에 포함된다.
- `raw_write_safe_facts_ko_for_audit_only`가 request payload에 포함된다.
- `--max-detail-facts` 옵션이 존재한다.
- `python -m py_compile`이 실패하지 않는다.

## Gate 2. 5개 샘플 audit

```bash
cd "/Users/hgs/Desktop/IISE CD"

python news_generator/scripts/processors/audit_pr06a_v3_3_generated_stock_news.py \
  --requests-jsonl "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests_from_briefs_v3_3/stock_news_sample_requests_5.jsonl" \
  --outputs-jsonl "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/stock_news_sample_outputs.jsonl" \
  --output-dir "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/audit_v3_3"
```

통과 기준:

- `pass == total`
- `json_parse_failed` 없음
- `status_not_accepted` 없음
- `sentence_count_should_be_1` 없음
- `bad_terms` 없음
- `market_terms_under_no_market_claim` 없음
- `source_label_or_prompt_artifact` 없음
- `used_facts_not_subset_of_detail_source_facts` 없음
- `numeric_detail_not_in_detail_source_facts` 없음

## Gate 3. 19개 전체 샘플 batch

5개 샘플이 Gate 2를 통과한 뒤에만 19개 전체 샘플을 실행한다.

```bash
cd "/Users/hgs/Desktop/IISE CD/news_generator"

python scripts/processors/run_pr06a_stock_news_sample_batch.py \
  --input-jsonl "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests_from_briefs_v3_3/stock_news_sample_requests.jsonl" \
  --output-jsonl "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/stock_news_sample_outputs_19.jsonl" \
  --batch-id-file "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/batch_id_19.txt" \
  --poll
```

이후 audit:

```bash
cd "/Users/hgs/Desktop/IISE CD"

python news_generator/scripts/processors/audit_pr06a_v3_3_generated_stock_news.py \
  --requests-jsonl "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_requests_from_briefs_v3_3/stock_news_sample_requests.jsonl" \
  --outputs-jsonl "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/stock_news_sample_outputs_19.jsonl" \
  --output-dir "/Users/hgs/Desktop/IISE CD/data/interim/pr06a_stock_news_sample_outputs/audit_v3_3_19"
```

통과 기준:

- 19개 모두 PASS
- 실패가 1개라도 있으면 전체 기간 본생성 금지
- 실패 유형이 입력 fact 문제인지, prompt 문제인지, audit 과잉 차단인지 분리한다.

## Gate 4. 전체 기간 본생성 전 입력 audit

전체 기간용 request를 만든 뒤, LLM 호출 전에 요청 파일만 검사한다.

필수 확인:

- `detail_source_facts_ko`가 비어 있는 요청 없음
- `detail_source_facts_ko` 안에 금지어 없음
- `detail_sentence_rule == exactly_one_sentence`인 요청의 `detail_source_facts_ko`는 1개
- `claim_level == no_market_claim`인 요청에 시장 반응 표현을 요구하는 payload 없음
- selected row 수와 예상 LLM 비용을 먼저 산출
- batch는 chunk 단위로 나누고, 첫 chunk audit 통과 후 다음 chunk로 진행

## 최종 확대 기준

전체 기간 본생성은 아래 조건이 모두 맞을 때만 진행한다.

- 5개 샘플: 5/5 PASS
- 19개 샘플: 19/19 PASS
- 전체 기간 request 입력 audit PASS
- 첫 본생성 chunk audit 100% PASS

이 기준을 만족하지 못하면 prompt를 다시 강화하기보다 먼저 `detail_source_facts_ko` 생성 규칙과 입력 filtering을 수정한다.
