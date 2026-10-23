# 보증 조건 연계 중고거래

선택한 보증 상품의 사실 정보와 문서 근거를 확인하고, 물건값 D와 보증 프리미엄 W를 나누어 정산하는 로컬 시연 서비스입니다. Streamlit과 HTML 화면은 같은 Python 서비스와 SQLite 원장을 사용합니다.

Python 3.10 이상에서 실행합니다. 기본 주소는 `127.0.0.1`이며 외부 모델 호출과 실제 파일 업로드는 기본 비활성입니다. 기본 모델과 임베딩은 명시적인 `MOCK` 어댑터입니다. 로컬 Chroma와 LangGraph의 `retrieve`·`generate` 두 노드는 실제 실행되지만, 토큰 해시 임베딩과 결정적 모의 응답으로 실제 의미 검색·LLM 품질을 검증할 수는 없습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m warranty streamlit
```

HTML 화면은 다음 명령으로 실행하고 `http://127.0.0.1:8000`에서 엽니다.

```powershell
.\.venv\Scripts\python.exe -m warranty api
```

`PRIVATE_DATA_DIR`에는 DB·벡터·문서·평가 결과를, `PRIVATE_CONFIG_DIR`에는 실행 설정과 세 종류의 제품 프롬프트를 둡니다. 두 디렉터리는 저장소 밖에 있어야 합니다. 기본값은 사용자 홈 아래 `.warranty/data`와 `.warranty/config`입니다. `.env.example`은 변수 이름 안내이며 앱이 `.env`를 자동으로 읽지는 않습니다. 실행할 셸 환경에 필요한 값을 주입합니다. 아래의 꺾쇠 괄호 값은 실제 비공개 값으로 바꿔야 하는 자리 표시자입니다.

```powershell
$env:PRIVATE_CONFIG_DIR = '<저장소 밖 설정 폴더>'
$env:PRIVATE_DATA_DIR = '<저장소 밖 데이터 폴더>'
.\.venv\Scripts\python.exe -m warranty status
```

메타데이터 입력은 `METADATA_PATH` 또는 `runtime.json`의 `metadata_path`로 지정합니다. `METADATA_EXPECTED_HASH` 또는 `metadata_expected_hash`를 지정하면 SHA-256을 검증합니다. 입력을 지정하지 않으면 합성 항목으로 시작하며, 잘못된 입력을 합성 자료로 바꿔 숨기지 않습니다. 미확인 가격·이전 가능 여부는 미확인으로 보존합니다.

```powershell
.\.venv\Scripts\python.exe -m warranty validate-input --metadata '<비공개 메타데이터.json>' --sha256 '<SHA-256>'
```

`PRIVATE_CONFIG_DIR/runtime.json`의 최소 실행 구조는 다음과 같습니다. 이 예시는 외부 처리를 끈 합성 실행 설정입니다. `models`의 모델 ID·버전·임베딩 차원은 승인된 값을 정한 뒤 설정하거나 대응 환경변수로 주입합니다.

```json
{
  "schema_version": 1,
  "models": {
    "chat_provider": "mock",
    "embedding_provider": "mock",
    "public_protocol_path": null,
    "external_processing_approved": false,
    "allow_question_processing": false,
    "usage_approval_id": null,
    "usage_limits": {
      "chat_requests": 0,
      "embedding_requests": 0,
      "input_bytes": 0
    }
  },
  "collection": {
    "enabled": false,
    "network_enabled": false,
    "manager_actor": null,
    "worker_enabled": true
  },
  "settlement": {
    "max_attempts": 3
  },
  "retrieval": {
    "chunk_size": 1000,
    "chunk_overlap": 200,
    "top_k": 6,
    "strategy": "similarity",
    "fetch_k": 30,
    "lambda_mult": 0.5
  }
}
```

모델은 `models.chat_provider`와 `models.embedding_provider`에서 `mock`, `openai`, `gms`로 각각 선택합니다. `chat_model`, `chat_model_version`, `embedding_model`, `embedding_model_version`, `embedding_dimension`을 JSON에 지정할 수 있으며, JSON의 값이 대응 환경변수보다 우선합니다. 모델 버전은 운영자가 확인해 지정한 값이며, 제공자가 응답에서 보고한 모델 식별자와 구분합니다.

| 선택 | 실행 설정 |
| --- | --- |
| `mock` | 결정적 합성 응답과 로컬 해시 임베딩. 외부 요청 없음 |
| `openai` | 현재 앱 팩터리에서는 공개 전송 계약이 없는 원격 처리를 차단함 |
| `gms` | 검증된 `public_protocol_path`와 공개 전송 어댑터, 명시적 LIVE 실행·처리 범위·모델·사용량·크레딧 예산이 모두 필요 |

GMS 연결 정보는 [공개 빠른 시작 가이드](https://gms.ssafy.io/web/public/guide/quickstart)에서 확인할 수 있습니다. 이 앱의 게이트웨이는 `https://gms.ssafy.io/gmsapi/api.openai.com/v1`로 고정됩니다. 하위 게이트웨이의 허용 목록은 채팅의 `chat/completions` 경로에 `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o-mini`, 임베딩의 `embeddings` 경로에 `text-embedding-3-small`, `text-embedding-3-large`를 포함합니다. `responses`의 `gpt-4.1`은 요청 형식·파서 구성만 제공하며 현재 LIVE는 비활성입니다. 하위 어댑터의 허용 목록과 앱의 공개 경로 지원 범위, 실제 호출 성공·계정별 사용 가능 여부는 구분합니다.

위 목록은 하위 게이트웨이의 모델 허용 범위입니다. 현재 앱 팩터리의 공개 채팅 경로는 `gpt-4.1-mini`와 `chat/completions`로 제한하며, 공개 범위 파일의 정확한 질문·출처·출력 한도와 일치해야 합니다. 최대 요청 크기는 16,384 UTF-8 바이트, 출력 상한은 512토큰 이내입니다. 허용 목록에 있는 모델도 실제 실행 승인 없이 호출하지 않습니다.

GMS 채팅 설정의 `gms_chat_route`와 `gms_max_output_tokens`는 각각 `GMS_CHAT_ROUTE`, `GMS_MAX_OUTPUT_TOKENS`에 대응합니다. 출력 한도는 확인한 정수 1~4096을 지정합니다. `chat/completions` 요청에는 `model`, `messages`, `max_tokens`, `stream=false`, `store=false`를 전송합니다. `store=false`는 공급자 응답 저장을 끄는 요청이며 GMS의 로그 보관 정책을 보장하지 않습니다. `responses`의 구성 형식은 `model`과 문자열 `input`이며, 출력 비용 상한이 확정되지 않아 현재 프로필 유무와 관계없이 LIVE 준비 상태가 되지 않습니다. 임베딩은 문자열 하나씩 요청하며 `dimensions` 필드를 전송하지 않습니다. `GMS_EMBEDDING_DIMENSION`은 확인한 실제 응답 차원 1~8192를 검증하는 설정입니다. 미설정 차원은 준비되지 않은 상태로 남습니다. `GMS_CHAT_MODEL_VERSION`과 `GMS_EMBEDDING_MODEL_VERSION`도 확인한 경우에 지정합니다.

원격 처리에는 `ENABLE_REMOTE_MODELS=true`, 자격 정보, `external_processing_approved`, 질문 처리 승인과 출처별 행위 검토가 필요합니다. GMS에는 유효한 `usage_approval_id`, 요청·바이트 한도, 확정된 크레딧 프로필과 명시적 LIVE 실행 범위가 추가로 필요합니다. 키를 설정해도 요청이나 크레딧 사용을 자동으로 시작하지 않습니다. 일반 `status`, API·Streamlit 요청, 작업 처리기와 테스트는 GMS LIVE 범위를 부여하지 않습니다. 상태 조회는 모델 설정·LIVE 준비 상태·색인 건수를 구분하며, 시작 시 원격 임베딩을 실행하지 않습니다.

비공개 설정 파일은 아래 역할을 나누어 사용합니다. 실제 파일 내용과 권리 검토 기록을 저장소에 넣지 않습니다.

| 파일 | 내용 |
| --- | --- |
| `runtime.json` | 메타데이터 위치, 모델 선택, 수집·작업 처리기, 검색 설정 |
| `live_budget.json` | GMS 모델별 확인된 가격·입출력 상한, 목적별 크레딧 배정과 시연 잔여 예산 |
| `source_policy.json` | 스키마 1의 `registry`, `reviews`, `capabilities` |
| `document_plan.json` | 스키마 1의 `documents`와 항목별 `targets` |
| `prompts/query.json` | 검색어 생성 프롬프트 |
| `prompts/selection.json` | 등록 후보 선별 프롬프트 |
| `prompts/answer.json` | 근거 기반 구조화 응답 프롬프트 |

출처 정책의 비어 있는 구조 예시는 `{"schema_version":1,"registry":[],"reviews":[],"capabilities":{}}`입니다. 이 상태에서는 문서 처리 권한이 없습니다. `registry`는 출처 ID와 승인된 정확한 HTTPS URL·호스트를, `capabilities`는 서버 실행 주체의 `DATA_MANAGER` 권한을 지정합니다. 거래 관리자는 이 권한을 자동으로 받지 않습니다.

`reviews`에는 행위별 상태, 검토 근거와 URL, 검토일, 적용 범위·조건, 사용자 범위·처리 환경, `version_scope`, `allowed_actors`를 명시합니다. `VERSION` 범위에는 해당 `version_id`가 필요하며, 버전별 검토는 출처 전체 `SOURCE` 검토보다 우선합니다. 행위는 `view_link`, `fetch`, `store_body`, `extract`, `local_embed`, `external_process`, `display_excerpt`, `public_redistribute`로 구분합니다. 모델이나 문서 내용으로 권한을 추가하지 않습니다. 검토되지 않은 이용 범위는 보류합니다.

문서 계획의 빈 구조는 `{"schema_version":1,"documents":[]}`입니다. 각 문서는 `source_id`, `title`, `canonical_url`, `document_type`과 `targets`를 갖습니다. 각 대상에는 `item_id`, 문서 `role`, `applicability`, `applicability_basis`를 기록합니다. 역할은 `PRIMARY_DESCRIPTION`, `SERVICE_TERMS`, `INSURANCE_TERMS`, `TRANSFER_GUIDE`로 구분합니다. 적용성에는 국가·가입 기간·상품 세대·결제 방식·가입 채널·계약 문서 종류의 확인된 범위를 기록합니다. 합성 문서는 `is_synthetic=true`로 명시합니다. 로컬 본문을 가져올 때의 `body_path`는 비공개 데이터 디렉터리 안의 HTML/PDF여야 합니다.

문서 계획은 앱 시작 시 형식을 검증합니다. 실제 등록과 본문 가져오기는 다음 명시적 명령으로 수행합니다. 제품 프롬프트의 필수 필드·입력 변수·출력 스키마는 `warranty.prompts`에 정의되어 있으며, 누락된 프롬프트는 안전한 설정 오류로 안내합니다.

```powershell
.\.venv\Scripts\python.exe -m warranty documents-sync --actor '<DATA_MANAGER 실행 주체>'
.\.venv\Scripts\python.exe -m warranty documents-import --actor '<DATA_MANAGER 실행 주체>'
.\.venv\Scripts\python.exe -m warranty index-metadata --actor '<DATA_MANAGER 실행 주체>'
.\.venv\Scripts\python.exe -m warranty prepare-documents --actor 'buyer' --item 'SYNTHETIC-001' --question '보증 이전 조건을 확인해 주세요' --applicability (Join-Path $env:PRIVATE_CONFIG_DIR 'applicability.json')
.\.venv\Scripts\python.exe -m warranty document-worker --limit 1
.\.venv\Scripts\python.exe -m warranty job --actor 'buyer' --job-id '<반환된 작업 ID>'
```

`documents-sync`는 승인된 계획의 문서와 미수집 항목 대상을 등록하고, `documents-import`는 계획에 지정한 로컬 본문을 가져옵니다. `prepare-documents`는 즉시 작업 ID를 반환합니다. CLI에서는 `document-worker`가 대기 작업을 처리합니다. UI에서는 `collection.worker_enabled=true`일 때 로컬 처리기가 실행되며, 요청자는 자신의 작업을 조회합니다. `DATA_MANAGER`는 관리 권한으로 조회할 수 있습니다. 상태는 `QUEUED`, `RUNNING`, `COMPLETED`, `BLOCKED`, `FAILED`이며 실제 단계와 본문·청크 건수를 표시합니다. `worker` 명령은 별도의 모의 정산 처리기입니다.

이 일반 준비·작업 명령은 GMS LIVE 범위를 열지 않습니다. GMS 색인·문서 준비에는 아래의 `live-index-metadata`, `live-prepare-documents` 전용 명령을 사용합니다.

수집을 사용하려면 `collection.enabled=true`와 검토된 `manager_actor`를 지정합니다. HTTP 본문 요청에는 별도로 `network_enabled=true`와 출처별 허용이 필요합니다. 요청자와 수집 관리자 권한은 구분됩니다. 기본 검색기는 승인된 등록 대상 목록에서 생성 검색어로 후보를 찾는 로컬 검색기이며, 웹 검색 API 연결은 없습니다. 후보 선별은 본문 수집 전에 실행되고, 선택 항목의 자기 문서는 관련성 선별에서 탈락해도 필수 대상으로 유지합니다. 링크는 본문으로 계산하지 않습니다.

이미 수집한 본문은 동일 조건의 요청과 색인 재시도에 재사용합니다. 새 revision은 검증 후 활성화하며, 실패한 작업이 이전 정상 revision을 바꾸지 않습니다. 중단된 작업의 비공개 staging은 공개하지 않고 재시도에 재사용할 수 있습니다. 공식 본문·이용 검토·계약 적용성이 준비되지 않으면 `DOC_BLOCKED`와 `NEEDS_INFORMATION`으로 안내하며 W 지급을 허용하지 않습니다.

GMS의 동일 텍스트·게이트웨이·모델·버전·차원 임베딩은 비공개 영속 캐시를 재사용합니다. 항목이나 청크 revision이 달라도 텍스트가 같으면 재사용하며, 모델·차원·텍스트가 바뀌면 새 결과가 필요합니다. 캐시 적중 전에도 현재 출처 권한과 LIVE 범위를 검사합니다. 적중 시 새 GMS 요청·크레딧 예약은 0이며, 동시 미스에는 처리권을 적용합니다. 손상된 캐시는 안전한 오류로 중단합니다. 캐시는 원문 텍스트·API 키를 저장하지 않으며, 본문 수집 캐시와 별도로 동작합니다.

D 지급에는 구매자 수령 확인과 양측의 동일 정산안 승인이 필요합니다. W 지급에는 적용성이 확인된 검토와 양측의 동일 최신 증빙 확인도 필요합니다. 조건부 검토의 조건은 증빙 묶음에서 각각 명시적으로 확인하며, 모델의 조건 목록을 충족 확인으로 사용하지 않습니다. 환불은 계약의 반환 조건과 양측 승인에 따라 별도로 처리합니다. 원장은 복식 분개를 기록하고 결과가 불명확한 작업의 예약을 유지합니다.

두 화면에서 기존 계약의 반환 조건·증빙 기준·확인 기한·분쟁 규칙을 수정할 수 있습니다. 새 계약 버전에는 양측의 새 동의가 필요하며, 예치 후에는 금액을 바꿀 수 없습니다. 정산 재시도 상한은 `MAX_SETTLEMENT_ATTEMPTS` 환경변수 또는 `runtime.json`의 `settlement.max_attempts`로 설정합니다. 허용 범위는 정수 1~10이고 기본값은 3이며, 실행 설정 파일이 환경변수보다 우선합니다. 결과가 불명확한 작업은 재실행 대신 대사로 확인합니다.

GMS의 프로젝트 전체 크레딧 상한은 임베딩·재시도·동시 호출을 포함해 70,000입니다. 이 프로젝트의 예산 정책은 비시연 누적 35,000 이하와 시연용 35,000 보존입니다. 실제 LIVE 전 승인된 예산 프로필의 목적별 배정과 `demo_reserve_credits`가 이 정책을 지키는지 확인해야 합니다. 검증·평가·최종 확인·시연·임베딩 준비에 필요한 양만 사용합니다. 개발 중에는 합성 자료·모의 전송으로 검증합니다. 요청 횟수·입력 바이트 한도와 크레딧 상한은 함께 적용됩니다. 기본 요청·바이트 한도는 모두 0이고, 확정된 가격·입출력 상한·목적별 배정이 없으면 LIVE는 차단됩니다.

`PRIVATE_CONFIG_DIR/live_budget.json`의 비활성 구조 예시는 다음과 같습니다. 실행에 사용할 가격이나 승인 범위를 포함하지 않습니다.

```json
{
  "profile_id": "pending",
  "pricing_confirmed": false,
  "pricing_version": "unconfirmed",
  "allocations": {
    "evaluation": 0,
    "verification": 0,
    "final_verification": 0,
    "demo": 0,
    "embedding_preparation": 0
  },
  "demo_reserve_credits": 0,
  "models": {}
}
```

확정 프로필의 `models`는 모델 ID를 키로 하며, 모델별 필드는 다음과 같습니다. 실제 가격·허용 응답 모델·상한을 확인한 뒤 비공개 프로필에 기입합니다.

| 필드 | 확인할 값 |
| --- | --- |
| `kind`, `route` | `chat`과 `chat.completions`/`responses`, 또는 `embedding`과 `embeddings` |
| `input_microcredits_per_token`, `output_microcredits_per_token` | 확인한 토큰당 가격을 정수 마이크로크레딧으로 표현. 1크레딧은 1,000,000마이크로크레딧 |
| `input_bound`, `input_bound_confirmed` | 전송 UTF-8 바이트 기반 상한 `wire_utf8_bytes`와 검토 확인 |
| `input_token_overhead`, `max_input_tokens` | 확인한 추가 입력 상한과 모델 입력 한도 |
| `output_bound`, `output_bound_confirmed`, `max_output_tokens` | `chat.completions`는 `max_tokens`. 스키마의 `responses`·`model_maximum` 조합은 현재 어댑터의 LIVE 실행을 활성화하지 않음. 임베딩은 `none`, 출력 토큰·출력 가격 0 |
| `allowed_reported_models` | 선택 사항. 확인한 제공자 응답 모델 식별자의 허용 목록 |

임베딩의 이 프로필 형식은 출력 과금이 없다는 확인을 전제로 합니다. 과금 방식이 확인되기 전에는 `pricing_confirmed=false`와 LIVE 차단을 유지합니다.

목적별 배정 합계는 전체 상한 이하여야 하며 `demo_reserve_credits`는 `demo` 배정 안에 포함됩니다. 승인 ID는 실행 설정과 두 예산 원장에 같은 범위로 연결됩니다. 모델·가격·승인 범위가 달라지면 기존 승인을 재사용하지 않습니다. 새 승인 ID도 프로젝트 전체 누적 사용량을 초기화하지 않습니다.

매 요청 전에 비용 상한을 원자적으로 예약합니다. 성공·실패·시간초과·결과 불명과 실비용 감소에도 예약한 상한을 자동 환불하지 않습니다. 재시도도 전체 한도 안에서 집계하며, 동일 예약의 재생으로 HTTP를 다시 실행하지 않습니다. 제공자 실크레딧, 제공자 토큰 사용량, 토큰 기반 추정과 예약 상한은 따로 기록합니다. 실크레딧을 받지 못한 값은 미확인으로 남깁니다. 미허용 응답 모델이나 예약 상한을 넘는 사용량은 프로젝트를 동결해 추가 LIVE 실행을 막습니다.

비교 평가 입력과 결과도 저장소 밖에 둡니다. `evaluate`는 무RAG/유RAG 원응답과 검색 비교를 기록합니다. 성능 측정은 준비된 상태에서 20회 이상, 대표 답변 비교는 동일 질문 5개 이상을 사용합니다. 평가 설정의 `measure_cold_prepare`를 켜면 최초 수집·색인 구간을 따로 측정하며, 측정하지 않은 구간은 미측정으로 남깁니다. similarity/MMR은 같은 후보·필터·revision에서 비교합니다. 정확성·근거·모름 인정 평가는 실제 응답과 기준 자료를 검토해 작성합니다.

```powershell
.\.venv\Scripts\python.exe -m warranty evaluate --cases (Join-Path $env:PRIVATE_CONFIG_DIR 'evaluation-cases.json') --evaluation-config (Join-Path $env:PRIVATE_CONFIG_DIR 'evaluation-config.json')
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

실패한 문서 준비 작업은 `retry-job --actor '<요청 주체>' --job-id '<작업 ID>'`로 다시 대기시킬 수 있습니다. 명시적 재시도는 실패·무결과 캐시의 대기시간을 우회하며, 이미 수집한 정상 본문은 재사용합니다. GMS LIVE 문서 준비는 전용 명령을 사용합니다.

수집한 답변 비교 결과의 검토 양식은 아래 명령으로 만듭니다. 양식의 원응답 식별자와 확인한 기준 자료를 보존해 검토를 작성한 뒤 보고서를 생성합니다. 검토자 유형은 사람과 에이전트 보조를 구분하며, 검토 보고서가 전체 요구사항의 합격을 자동 판정하지 않습니다. 이 두 명령은 저장된 비공개 파일만 읽고 모델을 호출하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m warranty evaluate-review-template --artifact '<답변 비교 결과.json>' --output-dir '<비공개 검토 폴더>'
.\.venv\Scripts\python.exe -m warranty evaluate-review --artifact '<답변 비교 결과.json>' --review '<작성한 검토.json>' --output-dir '<비공개 검토 폴더>'
```

승인된 GMS 설정·가격 프로필·자료 처리 범위가 준비된 후에는 아래 전용 명령으로 LIVE를 실행합니다. 평가 설정 파일의 `mode`도 `LIVE`여야 합니다. LIVE 평가는 채팅·임베딩 모델 버전을 모두 명시하고, 같은 임베딩 모델·버전·차원으로 준비된 실제 공식 문서 색인이 필요합니다. 항목은 처리할 ID를 명시하며, 적용 조건 파일에는 확인한 범위만 기록합니다. `live-index-metadata`와 `live-prepare-documents`의 목적은 `embedding_preparation`이고, `live-query`의 목적은 `verification`, `final_verification`, `demo` 중에서 지정합니다. `live-prepare-documents`는 해당 명령 안에서 준비를 실행합니다. 일반 문서 작업 큐에 LIVE 권한을 저장하지 않습니다.

`live-query`는 준비된 색인에 질의하고 답변 캐시를 사용하지 않아 매번 새 응답을 생성합니다. 문서 수집·색인은 `live-prepare-documents`로 별도 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m warranty evaluate --live --approval-id '<승인 ID>' --cases (Join-Path $env:PRIVATE_CONFIG_DIR 'evaluation-cases.json') --evaluation-config (Join-Path $env:PRIVATE_CONFIG_DIR 'evaluation-config.json')
.\.venv\Scripts\python.exe -m warranty live-index-metadata --live --approval-id '<승인 ID>' --actor '<자료 관리자>' --items '<선택 항목 ID>'
.\.venv\Scripts\python.exe -m warranty live-prepare-documents --live --approval-id '<승인 ID>' --actor '<실행 주체>' --item '<선택 항목 ID>' --question '<확인할 질문>' --applicability (Join-Path $env:PRIVATE_CONFIG_DIR 'applicability.json')
.\.venv\Scripts\python.exe -m warranty live-query --live --approval-id '<승인 ID>' --purpose verification --actor '<실행 주체>' --item '<선택 항목 ID>' --question '<확인할 질문>' --applicability (Join-Path $env:PRIVATE_CONFIG_DIR 'applicability.json')
```

합성 본문·모의 전송 테스트와 실제 제공자·공식 본문의 LIVE 실행 결과는 따로 확인합니다. 최종 통합 테스트·LIVE 검증 결과는 실제 실행한 소스와 결과에 맞춰 기록하며, 이 실행 안내는 성공 실적을 뜻하지 않습니다. 실제 금융 처리나 제공기관의 보증 이전 API 연계는 제공하지 않습니다. 실제 업로드 검사 경로도 준비되지 않아 업로드는 계속 비활성입니다.

## 공개 RAG 처리 범위

`models.public_protocol_path`는 `PRIVATE_CONFIG_DIR` 안의 공개 처리 범위 JSON을 가리킵니다. 항목 ID, 합성 질문의 SHA-256, 공개 문서의 제목·HTTPS URL·발췌문과 그 SHA-256, 문서 임베딩 대상 발췌문의 SHA-256을 지정합니다. 파일은 전송할 수 있는 내용의 범위만 정의하며 출처 이용 권한이나 LIVE 실행 승인을 부여하지 않습니다. 설정이 없거나 범위가 맞지 않으면 기존 비공개 입력 형식으로 원격 전송하지 않고 차단합니다.

```powershell
.\.venv\Scripts\python.exe -m warranty public-protocol
```

이 명령은 공개 범위의 설정 여부·항목 수·질문 수·문서 수를 조회합니다. 원문이나 키를 출력하지 않으며 LIVE를 시작하지 않습니다. 기본 `mock` 설정의 문서 준비·검색·질의는 위의 로컬 명령과 화면에서 수행합니다.

공개 채팅 요청은 공개 지침과 정확히 지정한 합성 질문, `S1` 같은 임시 인용 표식·제목·URL·발췌문으로 구성합니다. 항목 메타데이터·계약 적용성·비공개 제품 프롬프트·내부 ID는 로컬에 남습니다. 응답의 표식은 서버에서 로컬 근거와 화면 인용 번호로 복원하며, 알려지지 않은 표식이나 잘못된 응답 형식은 자동 수정 호출 없이 거절합니다. 응답 분류가 없으면 `NEEDS_INFORMATION`으로 처리하고, 다른 분류에는 인용을 요구합니다. 모델의 분류는 후보이며 문서 부족·적용성 미확인·상충을 확인하는 서버 검증과 W 지급 조건을 대신하지 않습니다.

답변의 인용 표기가 전부 빠졌고 응답의 인용 목록은 모두 유효한 경우에는 서버가 그 목록의 번호를 한 번 붙인 뒤 같은 검증을 다시 수행합니다. 일부 표기만 있거나 잘못된 표식·인용 목록이면 거절하며, 추가 모델 호출은 하지 않습니다. 이 로컬 표기 복원 여부는 비공개 평가 기록에 별도로 남깁니다.

공개 응답의 선택적 조건 목록도 조건 문장과 인용 표식만 받습니다. 서버가 이를 로컬 근거와 연결하며, 조건 ID·당사자의 충족 확인·승인·지급 여부는 모델 응답으로 정하지 않습니다. 모델의 조건 후보는 기존 증빙 체크리스트에서 별도로 확인합니다.

공개 범위를 사용하면 메타데이터 색인은 로컬 MOCK 임베딩으로 분리합니다. 원격 문서·질문 임베딩은 정확히 지정한 공개 발췌문·합성 질문으로 제한하며, 같은 공개 범위를 사용하는 MOCK 문서 준비에도 지정 발췌문 해시를 적용합니다. 문서 상태의 `document_scope`는 지정 발췌 범위와 등록 본문 전체 색인의 차이를 표시합니다. 일부 발췌 준비를 전체 문서·전체 메타데이터의 의미 검색 검증으로 해석하지 않습니다. 근거 화면에는 유형·제목·원문 위치·문서 버전·출처 링크를 표시하고, 합성 시연 문서는 그 유형을 명시합니다.

GMS는 이 프로젝트의 승인된 채팅 LLM과 RAG 임베딩에만 사용합니다. 키는 승인된 프로젝트 실행기에만 연결하며 에이전트 자체 추론·다른 프로젝트·앱·전역 환경 등록에 사용하지 않습니다. 키 등록이나 공개 범위 파일 생성은 호출 승인이 아닙니다. 실제 전송할 자료·질문·모델·바이트·호출 한도·예상 비용을 확인하고 해당 단계의 승인을 받은 뒤에만 LIVE를 실행합니다. 기존 연결 확인 승인은 공식 문서 평가나 추가 호출의 승인을 대신하지 않습니다. MOCK 통과는 실제 GMS 응답 품질·20회 성능·최종 시연·사람 상호 점검의 완료를 뜻하지 않습니다.

공식 조건 확인을 돕는 정보이며 제공기관의 확인이나 금융·법률 전문가의 판단을 대신하지 않습니다. 시연 지갑·금액·증빙은 실제 자금이나 제조사의 이전 확인을 의미하지 않습니다. 원본 자료·실제 데이터·권리 검토·개인 증빙·키·프롬프트·실행 설정·DB·벡터·로그·평가 산출물·개인 검증 도구는 소스 저장소에 포함하지 않습니다.
