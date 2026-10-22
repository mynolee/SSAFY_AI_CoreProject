# 보증 조건 연계 중고거래

선택한 보증 상품의 사실 정보와 문서 근거를 확인하고, 물건값 D와 보증 프리미엄 W를 나누어 정산하는 로컬 시연 서비스입니다. Streamlit과 HTML 화면은 같은 Python 서비스와 SQLite 원장을 사용합니다.

Python 3.10 이상에서 실행합니다. 기본 주소는 `127.0.0.1`이며 외부 모델 호출과 실제 파일 업로드는 기본 비활성입니다. 시연 지갑과 증빙은 실제 자금이나 제조사의 이전 확인을 의미하지 않습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m warranty streamlit
```

HTML 화면은 다음 명령으로 실행하고 `http://127.0.0.1:8000`에서 엽니다.

```powershell
.\.venv\Scripts\python.exe -m warranty api
```

입력 파일은 환경변수 `METADATA_PATH` 또는 `PRIVATE_CONFIG_DIR/runtime.json`의 `metadata_path`로 지정합니다. 선택적으로 `METADATA_EXPECTED_HASH` 또는 `metadata_expected_hash`를 지정해 SHA-256을 검증할 수 있습니다. 입력을 지정하지 않으면 합성 항목으로 시작합니다. 잘못된 입력을 합성 자료로 대체해 숨기지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m warranty validate-input --metadata <input.json> --sha256 <sha256>
.\.venv\Scripts\python.exe -m warranty status
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`PRIVATE_DATA_DIR`에는 DB·벡터·문서를, `PRIVATE_CONFIG_DIR`에는 실행 설정과 `prompts/query.json`, `prompts/selection.json`, `prompts/answer.json`을 둡니다. 두 디렉터리는 저장소 밖에 있어야 합니다. 기본값은 사용자 홈 아래 `.warranty/data`와 `.warranty/config`입니다. 프롬프트 파일의 필수 변수와 출력 계약은 `warranty.prompts`에 정의되어 있습니다. 누락된 설정은 안전한 오류 코드로 안내합니다.

확인되지 않은 가격·이전 가능 여부는 미확인으로 남깁니다. 링크 목록은 문서 본문으로 계산하지 않습니다. 공식 문서의 본문·이용 검토·계약 적용성이 준비되지 않으면 `DOC_BLOCKED`와 `NEEDS_INFORMATION`을 반환하며 W 지급을 허용하지 않습니다.

기본 모델과 임베딩은 명시적인 `MOCK` 어댑터입니다. 로컬 Chroma 저장·검색과 LangGraph 실행을 검증하는 용도이며 실제 의미 검색·LLM 품질 검증을 대신하지 않습니다. 외부 모델의 키와 이름을 설정하는 것만으로 수집·전송 권한이 부여되지 않습니다.

D 지급에는 구매자 수령 확인과 양측의 동일 정산안 승인이 필요합니다. W 지급에는 적용성이 확인된 검토와 양측의 동일 최신 증빙 확인도 필요합니다. 조건부 검토의 조건은 증빙 묶음에서 각각 명시적으로 확인하며, 모델의 조건 목록을 충족 확인으로 사용하지 않습니다. 환불은 계약의 반환 조건과 양측 승인에 따라 별도로 처리합니다. 원장은 복식 분개를 기록하고 결과가 불명확한 작업의 예약을 유지합니다.

`warranty.evaluation.ComparisonRunner`는 같은 질문에 대한 두 원응답과 실제 처리 시간, 유사도/MMR 검색 결과를 비공개 경로에 기록합니다. `MOCK` 수집 결과와 `LIVE`를 구분하며, 본문·외부 처리 승인·모델 정보가 없는 LIVE 비교는 실행을 차단합니다. 수집만으로 정확성 점수나 합격 판정을 만들지 않습니다.

공식 조건 확인을 돕는 정보이며 제공기관의 확인이나 금융·법률 전문가의 판단을 대신하지 않습니다. 원본 자료·개인 증빙·키·실행 DB·프롬프트는 소스 저장소에 포함하지 않습니다.
