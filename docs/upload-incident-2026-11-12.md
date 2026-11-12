# 2026-11-12 서류 업로드 사고 수정·로컬 검증

작업 기준: `/Users/dany/glhac-wt-bizparse`, `fix/biz-parse-kr-legacy`, HEAD `25c20b3`.
커밋·배포·운영 DB 변경·실제 알림 발송 없음. 실파일과 측정 DB/로그는 저장소 밖에 두었다.
cli-v2 파이프라인은 Port 1에서 `api.openai.com` DNS 연결 실패로 `REJECT`되었고, 사용자 지침의 실패 시 fallback을 적용했다.

## 배포 전 재검수 수정 2/2 — 추가 4건

기존 미커밋 변경을 보존하고 추가 지적 4건을 수정했다. cli-v2는 이번에도 Port 1의 `api.openai.com` DNS 연결 실패로 `REJECT`되어 사용자 지침의 fallback을 적용했다. 커밋·배포는 하지 않았다.

| 추가 검수 항목 | 반영 및 회귀 재현 |
| --- | --- |
| P1 자동 파싱 중 종결 | `_reprocess_doc`는 OCR·모델을 기다리기 전에 읽기 트랜잭션을 끝내고 추출 결과를 지역 변수에 보관한다. 모든 모델 응답 뒤 `_lock_case`로 최신 행을 잠그고 검증 큐와 같은 `sm.is_terminal` 판정을 거친다. 종결이면 본문·편철·자동입력·감사·알림·검증 큐 저장 전에 중단한다. 실제 파싱 큐의 분류/유형별 추출 응답을 각각 멈추고 HTTP로 `cancelled`, `withdrawn`, `rejected`, `revoked`를 완료한 뒤 재개하여 저장 기록 전체 불변을 검사한다. |
| P1 사라진 자동입력값 | 새 추출 필드와 별도로 해당 문서 `_autofill.changes`의 `applied` 값을 원문 검산한다. 프로필/시설 필드는 원래 역할 키로 검사하고 생성한 시설 ID는 원문 값으로 검산하지 않는다. `Made up` 주소·이전 값 `Previous`·원문 `Address: Seoul`에서 새 `fields={}`를 거쳐 실제 검증 후 회수됨을 확인한다. 새 필드가 `Seoul`로 바뀌는 경우, 수동 확인값·수동 수정값 보존, 재검증 중복 회수 방지도 포함한다. |
| P1 호출 예산 밖 위험 원료 | 공통 파싱 결과 경계에서 공급자/호출 예산과 무관하게 전체 원문을 기존 규칙 스캐너로 검사한다. 발견한 위험 원료를 확인대기와 `material_safety_blockers`로 연결하며 사전 표준명/원문 별칭은 중복시키지 않는다. 앞 `Water`·중간 `Pork`가 있는 약 40만 자 문서에서 OpenAI 추출+검증 8회 모두 `Pork`를 보지 못해도 차단된다. Ollama·AI 비활성 경로, 검증 후 위험 보존, 부정 표현도 검사한다. 정량 결과의 분석항목명은 원료로 오인하지 않되 같은 문서의 별도 원료 구절은 계속 차단한다. 전체 검사 비용을 줄이기 위해 존재하지 않는 별칭에는 경계 정규식을 실행하지 않는다. |
| P2 알림 대상 변경 | 지문에 사유별 업체 조치 여부와 실제 수신 역할 집합·배정 오디터 ID를 포함한다. 동일 문서의 `VERIFY_FAILED`가 원문 불일치에서 확정 유형 불일치(`applicant_action_required=true`)로 바뀌면 업체 알림을 생성한다. 이메일 동의로 인한 대상 변경과 오디터 재배정도 재안내하며 동일 상태 재실행은 억제한다. |

추가 재현 30건은 `tests/test_review_round2.py`(27건), `tests/test_problem_documents_notify.py`(3건)에 고정했다. 기존 동결 경쟁 테스트는 새 쓰기 잠금 내부에서 다른 세션의 동결을 동기 호출하던 위치를 실제 추출 대기 경계로 옮겼으며, 첫 원료 저장 전에 동결되어 기존 범위와 확인대기가 보존된다는 검증은 유지했다. 정상 CoA `Ethanol Result: 0.02%`의 기존 발급 회귀와 별도 원료 구절을 함께 검증했다.

최종 검증은 지정한 `/Users/dany/IdeaProjects/llmdev/glhac-ai-v3/.venv/bin/python -m pytest -q <files> --color=no`로 실행했다. 앞 작업의 70개 파일에 이번 회귀 파일과 원료·최종 규칙 관련 3개 파일을 더한 74개 파일이 대상이다. 사업자 파서는 공개 OCR rate-limit 카운터 격리를 위해 별도 프로세스로 실행했다.

| 최종 실행 | 결과 |
| --- | --- |
| 관련 Python 73개 파일(이전 작업 전체, 파일 DB E1 반복·동시 업로드·XLSX 포함) | **4,532 passed**, 205.70초 |
| 사업자 파서 독립 실행 | **167 passed**, 1.14초 |
| Python 합계 74개 파일 | **4,699 passed**, 실패·skip 없음 |
| `node --test tests/*.cjs tests/test_bulk_review_ui.js` | **162 passed**, 실패·skip 없음 |
| `git diff --check` | 통과 |

전체 파일 목록은 `/private/tmp/biz-round2-python-files.txt`, 최종 로그는 `/private/tmp/biz-round2-final-python.log`, `/private/tmp/biz-round2-final-biz.log`, `/private/tmp/biz-round2-node.log`다. 테스트 환경은 `GLHAC_NOTIFY_WORKER=0 GLHAC_LLM_PROVIDER=none GLHAC_RATE_LIMIT_PER_MIN=0`이며 AI 활성 회귀는 공급자와 응답만 대역으로 설정했다. 기존 FastAPI/Starlette/SWIG 사용 중단 예정 경고 8건이 있다. 실제 외부 LLM·Rocky8 운영 환경·배포는 이번 검증에 포함하지 않았다. HEAD는 `25c20b3`, 브랜치는 `fix/biz-parse-kr-legacy`로 유지했다.

## 배포 전 재검수 수정 1/2 (U1~U6 + V1~V3)

이 절은 아래 초기 U1~U6 실측 기록 중 업로드 복구·AI 추출·알림 정책보다 우선한다. 이번에도 cli-v2를 먼저 호출했고, Port 1 DNS 연결 실패로 `REJECT`되어 허용된 직접 수정 fallback을 사용했다. 커밋·배포는 하지 않았다.

| 검수 항목 | 수정 및 고정한 재현 |
| --- | --- |
| 1. 확정 무결성 실패 회수 | 원문에 없는 값·번호 역할/형식 오류·확정 유형 불일치는 `fail`. 모델 의견은 `advisory`에 둔다. 메모리 DB의 주소 `Made up`을 실제 `verify_extraction` → `_verify_doc` → 회수 경로로 검사하고, 수동 확인값은 보존한다. `test_doc_intake_integrity.py`는 상세 issue와 `fail`을 확인한다. 소유권 테스트는 검증 함수 mock을 제거하고 원문에 없는 저장값으로 실제 실패를 발생시킨다. |
| 2. 불리언 근거 | 필드별 한국어·영어·인도네시아어 의미 패턴과 부정을 검사한다. `SJPH Manual / Document ID: ABC123`의 무관한 인용문은 채택하지 않으며, 규칙 `False`도 비교한다. 미확인 필드가 `_process_doc`에서 HPAS `ok`로 저장되지 않도록 검사한다. |
| 3. 구조 값 누락 | AI `fields={}` 또는 `Water`만 반환한 전성분표의 `Water/Pork`는 누락 메타데이터와 확인대기에 남는다. 위험 원료 검사 결과는 후속 검증 이후에도 보존한다. 전성분표의 AI 유형 오판도 구조 판정으로 교정하고 review를 남긴다. Ollama의 잘린 본문에는 `_scan_tail`을 다시 실행한다. |
| 4. 업로드 복구 | 클라이언트가 POST별 UUID `upload_id`를 생성한다. 서버는 문서 `fields._upload_id`와 업로더를 저장하며 응답과 본인 문서 목록에 키를 반환한다. 복구는 이 키의 일치만 사용한다. 이름·크기·해시가 같은 오래된 타인 문서로는 복구하지 않는다. 동일 키의 동시 재전송은 한 문서로 처리하고 다른 내용의 재사용은 409로 거부한다. 재분석 후에도 키를 보존한다. |
| 5. 전체 AI 호출 예산 | `GLHAC_DOCUMENT_AI_MAX_CALLS` 기본 8회로 분류·유형별 재추출·후속 검증을 합산한다. 사용량은 `_ai_budget`에 저장하므로 검증 큐 재전송도 잔여 예산만 사용한다. 명시적인 새 재처리는 새 분석 예산을 시작한다. 공개 OCR은 `GLHAC_PUBLIC_AI_MAX_CALLS` 기본 2회이며 문서 예산보다 낮게 제한한다. 초과 시 앞뒤 구간만 읽고 오디터에 ‘부분 분석’을 표시한다. 업체 문제 사유에는 포함하지 않는다. 20MiB TXT의 전체 처리 경로와 공개 HTTP OCR 경로를 검사한다. |
| 6. 업체 알림 | 명시적 `APPLICANT_PROBLEM_CODES`에 `ORIGINAL_NOT_STORED`를 복원한다. 모든 문제 코드의 대상 여부, 확정 유형 불일치의 조건부 `VERIFY_FAILED`, 알 수 없는 코드 제외를 검사한다. 20MiB 초과 압축 원본의 업체 조회와 실제 알림 레코드에 재업로드 안내가 남는다. |
| 7. 역할별 참고 의견 | 공통 재귀 필터를 문서 목록·intake-view·AI 추출 이력·추출/압축/재처리 응답에 적용한다. `/documents`, `/intake-view`, `/ai-extractions`를 실제 역할별 HTTP 호출로 확인한다. 오디터 참고와 부분 분석 안내는 신청자 응답에서 제외하고 원본 DB 메타는 유지한다. |
| 8. 큐 편철 | 확보한 `text_full` 앞 400자를 사용한다. `KEPUTUSAN PENETAPAN TIM MANAJEMEN HALAL`을 311번째 문자부터 배치한 큐 회귀에서 `halal_supervisor` 편철을 확인한다. OCR 재실행은 하지 않는다. |
| 9. 참고서류 유형 강제 | 중소기업확인서·제품설명서·제품 사진은 AI가 사업자등록증/공급사 선언/제품 목록을 반환해도 `other`와 `review`를 유지하고 업무 필드를 제거한다. AI 활성 분류·유형 지정 추출·기존 유형 유지 큐 경로를 모두 검사한다. |

주요 새 재현은 `tests/test_review_revisions.py`, 업로드 응답 유실은 `tests/test_upload_recovery.cjs`에 있다. 기존 테스트의 소유권·수동값 보호·동시성 가드는 유지했다. 일반 서류 업로더 저장 정책에 따라 증빙 메타 테스트는 실제 업로더 일치와 일반 서류의 EXIF/GPS 미수집을 확인한다. 오래된 `test_bulk_review_ui.js`는 공통 번역/실제 화면 하네스와 현재 권한·상태 코드에 맞췄으며, 선택 없음·검토 사유·재전송·취소·폴링 시간 상한 검사를 유지한다.

검증에는 지정한 `/Users/dany/IdeaProjects/llmdev/glhac-ai-v3/.venv/bin/python -m pytest -q <files> --color=no`를 사용했다. 앞 작업에 기재된 63개 파일, 변경/신규 Python 테스트, SJPH 및 입력 검증 3개 모듈을 합친 70개 파일이 대상이다. 사업자 파서는 기존 공개 OCR rate-limit 카운터를 격리하기 위해 별도 프로세스로 실행한다. Node 대상은 `node --test tests/*.cjs tests/test_bulk_review_ui.js`다.

| 최종 실행 | 결과 |
| --- | --- |
| 관련 Python 69개 파일(이전 U1~U6/V1~V3, 새 검수 재현, 실제 DB E1 동시성 포함) | **3,514 passed**, 267.53초 |
| 사업자 파서 독립 실행 | **167 passed**, 1.22초 |
| Python 합계 70개 파일 | **3,681 passed**, 실패·skip 없음 |
| Node 전체 `.cjs` + 일괄 업로드 `.js` | **162 passed**, 실패·skip 없음 |
| `git diff --check` | 통과 |

재현 파일 목록은 `/private/tmp/biz-review-python-files.txt`, 최종 로그는 `/private/tmp/biz-review-final-full.log`, `/private/tmp/biz-review-final-biz.log`, `/private/tmp/biz-review-all-node.log`에 있다. Python의 8개 경고는 기존 FastAPI/Starlette/SWIG 사용 중단 예정 경고다. 테스트 환경은 `GLHAC_NOTIFY_WORKER=0 GLHAC_LLM_PROVIDER=none GLHAC_RATE_LIMIT_PER_MIN=0`이며, AI 활성 회귀는 테스트 안에서 공급자와 모델 응답만 지정한다.

외부 LLM 응답만 오프라인 대역으로 설정했다. 실제 검산·DB 회수·권한·큐·SQLite 동시성 코드는 실행하며 외부 메일은 보내지 않는다. 이번 재검수에서는 실제 외부 LLM, 운영 Rocky8/libarchive의 RAR5 백포트, 운영 배포를 검증하지 않았다.

## 원인과 변경

| 항목 | 확인한 원인 | 변경 |
| --- | --- | --- |
| U1 압축 전체 415 | `_validate_archive_upload()`가 중첩 `.Zip`을 일반 서류로 검사하여 `UNSUPPORTED_TYPE`으로 거절 | ZIP/RAR/7z를 최대 깊이 2까지 처리. 같은 요청의 파일 수·해제 크기·압축비 예산 공유. 메타데이터/미지원 형식/깊이·개수 초과는 `skipped_files`에 경로와 사유 기록 |
| U1 보안 | 기존 RAR/7z는 외부 도구로 전량 디스크 해제 후 읽었음 | libarchive 스트림으로 헤더 크기 확인 후 제한된 읽기. 디스크에 해제하지 않음. 경로 이탈, 링크·특수 파일, 크기·압축비 위반 차단. 허용 서류의 `CONTENT_MISMATCH`는 415 유지. 검사를 끝내기 전 어떤 서류도 저장하지 않음 |
| U1 이름 | ZIP의 UTF-8 플래그가 없는 macOS/Windows 파일명 혼재 | UTF-8 우선, CP949/EUC-KR fallback, NFC 정규화. OOXML은 중첩 압축으로 취급하지 않고 기존 서류 구조 검사 유지 |
| U2 여러 서류 올리기 | 모든 파일을 `/documents`로 보냄 | `.zip/.rar/.7z`를 대소문자 무관 `intake-zip-stream`으로 자동 전환. 권한이 없으면 ZIP 버튼 안내. 건너뜀 목록과 사유를 화면에 표시하고 파일명 HTML escape |
| U3 30초 중단 | `apiFetch`가 경로에 `intake/upload`가 있는지만 검사 | POST/PUT/PATCH의 JSON 본문 `file_b64/zip_b64`로 600초 선택, 호출자 `timeoutMs` 지원. 일반 조회는 30초. 원래 옵션은 변형하지 않고, 스트림 헤더 수신 뒤에도 사용자 취소 신호 유지 |
| U3 서버 성공·화면 실패 | 응답을 잃은 뒤 저장 여부를 확인하지 않음 | `/documents`의 네트워크·타임아웃 및 408/502/503/504 실패 시 목록 재조회. NFC 파일명·원본 바이트 크기 일치, 해시가 있으면 SHA-256까지 일치해야 저장됨으로 복구. 200 헤더 뒤 JSON 본문 유실도 복구. POST를 다시 보내지 않음. 명시적 사용자 취소나 415는 복구 대상 아님 |
| U4 스트림 무응답 | 한 파일 OCR이 오래 걸리면 진행 이벤트도 오래 멈춤 | 10초 간격 heartbeat 이벤트. 파싱 스레드는 DB를 사용하지 않고, 저장은 기존 요청 흐름에서 수행. 연결 종료 시 현재 파일 이후 처리를 중단 |
| U4 요청 지연 | 저장 이후 자동 편철이 본문을 얻으려고 동기 `parse_file()` → PDF 렌더/OCR 실행 | 업로드에서는 파일명/이미 추출된 본문으로만 편철. 저장 후 기존 autoparse 큐에 투입. 큐가 추출한 본문으로 추가 편철하므로 동일 문서 OCR 재실행 제거 |
| U6 중소기업확인서 | `확인서`라는 넓은 이름 규칙으로 공급사 선언서에 잘못 분류 | 중소기업/소상공인 확인서의 구체적 파일명·제목 규칙을 앞에 두어 `other` 참고서류로 보관. 사업자등록·공급사 증빙을 대신 충족하지 않음 |
| U5 업체 잘림 사유 | 내부 `_truncated/_truncations`를 `TRUNCATED` 문제 사유로 만들어 업체에게 분할 업로드 요구 | 문제 사유·업체 알림에서 제거. 검증의 `partial_verification`도 업체 문제 사유에서 제외. 신청자 문서/패키지 조회의 경고 목록에서 제외. 오디터의 상한 메타데이터는 보존하고 ‘자동 분석은 앞부분만 사용’ 정보성 안내 유지 |

기본 가드: 파일 항목 200개, 전체 해제 크기 200,000,000바이트, 단일 항목 크기 기존 환경 설정, 압축비 100. 내부 압축 컨테이너 자체와 건너뛴 항목의 선언 크기도 전체 크기에 포함한다. 항목 수는 내부 압축/메타데이터를 포함하여 합산하고 초과분을 건너뜀 목록으로 반환한다. ZIP/RAR/7z에 같은 정책을 적용한다. `DEPTH_LIMIT`은 세 번째 압축을 더 풀지 않았다는 뜻이다.

RAR/7z에는 libarchive가 필요하다. macOS 로컬에서 실제 RAR5/7z 합성 파일을 해제했고, Dockerfile에는 `libarchive-tools` 설치를 추가했다. 이 환경에서 Docker 이미지를 빌드하지는 않았다. 합성 RAR fixture는 [RAR 공식 형식 설명](https://www.rarlab.com/technote.htm)을 기준으로 생성한다.

## U4: 요청이 오래 걸린 구체적 경로

`POST /cases/{id}/documents` → `_validate_upload` → 문서 추가/flush → `_audit` → `_autofile_evidence` → `_evidence_key_for_doc` → `parse_file` → PDF 렌더 → `_ocr_bytes` → `ai_local.ocr_image` → `_worker_call`.

새 문서는 유형 기본값이 `other`, 본문이 비어 있으므로 자동 편철의 본문 fallback에 들어간다. COA/MSDS/사진 PDF도 base64 3,000,000자 미만이면 이 경로를 탄다. `_evidence_key_for_doc`는 결과의 앞 400자만 쓰지만, **400자로 자르기 전에 PDF 전체 파싱/OCR을 실행**했다. 종료 후에야 `autoparse_enqueue`에 넣기 때문에, 응답의 `autoparse_queued`가 요청 내부의 무거운 작업 부재를 뜻하지 않았다.

`_audit()`의 기본 `commit=True`로 원본은 이미 저장된 상태에서 OCR을 기다렸다. 따라서 화면이 30초에 포기해도 서버 원본은 남는다. 별도 재현에서 편철 진입 시 SQLite 쓰기 트랜잭션은 없었고, 가짜 OCR 2초 동안 다른 빠른 업로드는 0.003초에 완료됐다. **이 경로의 직접 원인은 DB 쓰기 락이 아니다.**

요청 안의 OCR과 autoparse의 OCR은 같은 `ai_local._wlock()`을 공유한다. 이 락은 워커 응답까지 유지되고, `OCR_TIMEOUT`은 락을 얻은 뒤 적용된다. 앞선 백그라운드 OCR 대기 + 문서의 여러 페이지 + 한국어/대체 언어 OCR이 요청 시간에 더해진다. 운영의 50.9·196.8·255.3초는 사용자 제공 로그이고, 로컬에서 해당 운영 워커 스케줄까지 동일하게 재현한 값은 아니다.

수정 전에는 `GLHAC_AUTOPARSE=0`으로 백그라운드 분석을 꺼도 실파일 업로드가 아래처럼 지연됐다. 요청 내부 `parse_file` 시간을 별도 계측했으며 거의 모든 시간이 일치한다.

| 실파일 | 크기(바이트) | 페이지 | 수정 전 응답(초) | 그중 동기 파싱(초) | 수정 후 응답(초), 큐 끔 | 수정 후 응답(초), 실제 큐 켬 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 제품 사진 PDF | 676,103 | 6 | 31.895 | 31.833 | 0.095 | 0.060 |
| 제품 COA PDF | 202,034 | 1 | 20.019 | 20.008 | 0.016 | 0.089 |
| 제품 MSDS PDF | 542,310 | 2 | 52.377 | 52.361 | 0.012 | 0.011 |

수정 후 같은 조건(`GLHAC_AUTOPARSE=0`)에서는 요청 내부 `parse_file` 호출이 0회였다. 최종 코드의 별도 새 DB에서 실제 큐를 켠 재실행도 위와 같이 빠르게 반환했고 모두 `autoparse_queued=true`였다. 시간은 단일 로컬 측정값이며 운영 성능 보장값은 아니다.

새 회귀 테스트는 가짜 OCR을 이벤트로 멈춰 둔 동안 첫 업로드와 다음 업로드가 각각 1.5초 미만에 반환됨을 검증한다. 원본 크기 조회, 큐에서 문서당 한 번만 파싱, 파싱 후 본문 기반 증빙 편철도 확인한다.

## U5: 한 페이지가 2,500자를 넘는 이유

실 사업자등록증명(영문)은 1페이지에 한글·영문 라벨, 안내문, 서식 문구를 함께 담고 있다. native 텍스트는 2,596자이며 파서가 덧붙인 줄바꿈을 포함하면 2,597자다. 703개 텍스트 span의 내용과 좌표를 대조했을 때 같은 내용·같은 위치의 중복은 0개다. PDF를 PNG로 렌더링하여 한 페이지의 정상 서식임도 확인했다.

제공된 제품 MSDS는 실제 2페이지, 사진 PDF는 6페이지다. 수정 전 실 OCR 추출은 각각 3,672자와 3,621자였다. `parse_file`은 native 텍스트가 충분한 페이지에는 OCR을 더하지 않으며, OCR 대체 언어 결과도 이어 붙이지 않고 더 좋은 한 결과를 선택한다. 확인된 파일에서 텍스트를 임의로 지울 중복 추출 근거는 없으므로 추출 본문은 변경하지 않았다.

## 실파일 전체 흐름과 검증 결과

서버 포트 바인딩은 `PermissionError: [Errno 1] Operation not permitted`여서 FastAPI TestClient를 사용했다. `GLHAC_DEV=1`, 새 SQLite DB, 실제 PaddleOCR(기본 worker·대체 언어 설정)을 사용했다. 외부 알림 워커와 문서 검증 큐는 껐다. 로컬 LLM 연결은 사용할 수 없어 공급자 판정은 `none`이다. 따라서 표의 분류·필드는 실제 코드의 규칙/구조 파서 결과이며 LLM을 성공한 것처럼 대체한 결과가 아니다.

실 ZIP에는 지원 서류 50건(최상위 7건 + 중첩 압축 43건)이 있다. 합계 189페이지/이미지 중 55페이지/이미지는 OCR 대상이다. 원본 그대로 순차 처리하므로 전체 분석은 개별 업로드 저장 응답보다 훨씬 오래 걸린다. 메타데이터 11건과 `.crdownload` 1건은 건너뛴다.

중소기업확인서는 별도 새 DB에서 최종 코드로 업로드하여 HTTP 200, 저장 응답 0.066초, 큐 처리까지 0.094초를 확인했다. 185,745바이트, 1페이지, 추출 텍스트 522자, 분류 `other`, 구조화 필드 `{}`이다. 원문은 보관하되 기존 필수 서류를 대신 충족하지 않는다.

## 실제 ZIP 결과

| 항목 | 측정 결과 |
| --- | --- |
| 원본 ZIP 크기 | 16,377,342바이트 |
| 중첩 검사·해제 사전 검증 | 0.165초 |
| 실제 OCR 포함 전체 스트림 요청 | HTTP 200, 2,213.292초(36분 53초) |
| 저장 | 서류 50건, 원본·전체 텍스트 보관 |
| 지원 서류 합계 크기 | 18,750,575바이트(가드 예산은 내부 압축·메타데이터도 추가 합산) |
| 실제 텍스트 파싱 합계 | 2,193.835초 |
| 분류·필드 추출 합계 | 18.773초 |
| OCR | 한국어 55회, 대체 언어 47회, 합계 2,188.378초 |
| 오류/중복 | OCR 오류 0회, 동일 텍스트·좌표 중복 0개 |
| 건너뜀 | 12건: 메타데이터 11건, 미지원 확장자 1건 |
| 유형 집계 | COA/MSDS 45건, 사업자등록 2건, 공급사 선언·설명서 1건, 기타 참고 2건 |
| 내부 상한 정보 | 25건. 업체의 문제 서류 사유로 사용하지 않음 |

전체 시간 대부분은 OCR이다. 특히 `19. Cosroma BKC-100T_MSDS.pdf`의 9페이지 OCR에 812.978초가 걸렸다. 이 실측을 근거로 한 파일 처리 중에도 10초 간격 heartbeat를 보내도록 보완했다. 실제 36분 측정은 heartbeat 보완 전 시작한 프로세스에서 끝냈다. 최종 코드의 스트림 경로는 위 실측에서 얻은 원본 텍스트/OCR 좌표를 재사용해 별도 새 DB에 다시 저장하는 방식으로 확인했다.

최종 코드 재생은 18.771초, HTTP 200, 저장 50건·건너뜀 12건·동일한 유형 집계였으며 신청자 경고 목록은 0건이었다. 이 값은 OCR을 다시 실행하지 않은 재생 시간이다. heartbeat는 느린 가짜 파서와 연결 종료 회귀 테스트에서 따로 확인했다. 이후 파서 재사용을 해제하고 실제 파일 4건(사진·COA·MSDS·중소기업확인서)을 새 케이스에 개별 업로드했다. 모두 즉시 HTTP 200으로 저장·큐 투입되고, 실제 OCR을 포함한 네 건의 큐 처리는 합계 107.450초 후 완료됐다. 최종 문서 목록의 `autoparse_queued`는 모두 false, 유형은 각각 `other/coa_msds/coa_msds/other`였다.

LLM 없이 회사명·주소·제품/원재료 집계를 추출하지 못했으므로 해당 자동채움 결과는 비어 있다. 규칙 기반 수치 추출은 12개 문서에 측정 항목 또는 수치 미확정 후보를 기록했다. 아래 수치는 검사 적합성 판단이 아니라 추출 결과이며, 원문의 부등호·단위·판독 근거는 `measurements_meta`에 유지된다. `—`는 텍스트는 추출했지만 별도 구조화 필드가 없음을 뜻한다.

파일명은 원본의 한글, 공백, `™`/`®`, `.Zip`을 유지했다. `4. ISO22716 인증서.pdf`는 기존 분류 정책에 따라 `other`이며 할랄 인증서로 간주하지 않는다.

### 최상위 `Drjcos/` 7건

| 파일명 | 분류 | 쪽/이미지 | 추출 문자 | 파싱(초) | 구조화 추출 |
| --- | --- | ---: | ---: | ---: | --- |
| 4. ISO22716 인증서.pdf | 기타 참고 | 1 | 1,339 | 23.862 | — |
| 6. 닥토필 프로페셔널 포 페이스 사진.pdf | 기타 참고 | 6 | 3,621 | 30.999 | — |
| 5. DactorPeel Professional For Face_COA.pdf | COA/MSDS | 1 | 785 | 20.172 | — |
| 1. 닥터제이코스_사업자등록증명(영문).pdf | 사업자등록 | 1 | 2,597 | 0.006 | — |
| 5. DactorPeel Professional For Face_MSDS.pdf | COA/MSDS | 2 | 3,672 | 52.063 | — |
| 2. 제품설명서_닥토필 프로페셔널 포 페이스.pdf | 공급사 선언·설명서 | 2 | 2,603 | 0.011 | — |
| 닥터제이코스_사업자등록증명_2020.pdf | 사업자등록 | 2 | 762 | 19.417 | — |

### 중첩 `Drjcos/5. 원료 COA, MSDS.Zip/` 43건

| 파일명 | 분류 | 쪽/이미지 | 추출 문자 | 파싱(초) | 구조화 추출 |
| --- | --- | ---: | ---: | ---: | --- |
| 1. DactorTec™ KISsoo(W)_COA.pdf | COA/MSDS | 1 | 731 | 0.003 | — |
| 1. DactorTec™ KISsoo(W)_MSDS.pdf | COA/MSDS | 6 | 11,558 | 0.013 | — |
| 10. LEMON OIL_COA.pdf | COA/MSDS | 1 | 439 | 16.192 | — |
| 10. LEMON OIL_MSDS.pdf | COA/MSDS | 5 | 12,010 | 0.012 | — |
| 11. Leafresh(NB)_COA.pdf | COA/MSDS | 1 | 985 | 11.552 | As(ppm)=2 |
| 11. Leafresh(NB)_MSDS.pdf | COA/MSDS | 6 | 11,902 | 0.010 | — |
| 12. Clewat N2_COA.jpg | COA/MSDS | 1 | 452 | 88.470 | — |
| 12. Clewat N2_MSDS.pdf | COA/MSDS | 6 | 13,490 | 0.035 | — |
| 13. Phyocell Goji _COA.pdf | COA/MSDS | 1 | 1,379 | 0.006 | — |
| 13. Phyocell Goji _MSDS.pdf | COA/MSDS | 6 | 12,293 | 0.011 | — |
| 14. InnoSolve ® EHG_COA.pdf | COA/MSDS | 1 | 970 | 0.003 | — |
| 14. InnoSolve ® EHG_MSDS.pdf | COA/MSDS | 5 | 9,074 | 0.012 | — |
| 15. DactorTec™ KiS-EXO (Lactobacillus Extracellular Vesicles)_COA.pdf | COA/MSDS | 1 | 622 | 11.138 | Pb(ppm)=10, As(ppm)=2 |
| 15. DactorTec™ KiS-EXO (Lactobacillus Extracellular Vesicles)_MSDS.pdf | COA/MSDS | 5 | 9,343 | 142.863 | — |
| 16. XILOGEL_COA.jpg | COA/MSDS | 1 | 992 | 35.990 | As(ppm)=0.5, Pb(ppm)=0.3 |
| 16. XILOGEL_MSDS.pdf | COA/MSDS | 13 | 22,750 | 0.018 | — |
| 17. Cosroma® GSH_COA.pdf | COA/MSDS | 1 | 1,035 | 13.123 | Cd(ppm)=5, Hg(ppm)=1, As(ppm)=2, Pb(ppm)=5 |
| 17. Cosroma® GSH_MSDS.pdf | COA/MSDS | 9 | 25,860 | 316.278 | As(ppm)=미확정 |
| 18. Sodium Hyaluronate_COA.pdf | COA/MSDS | 1 | 1,015 | 24.306 | — |
| 18. Sodium Hyaluronate_MSDS.pdf | COA/MSDS | 5 | 6,526 | 0.012 | — |
| 19. Cosroma BKC-100T_COA.pdf | COA/MSDS | 1 | 953 | 85.525 | Cd(ppm)=5, Hg(ppm)=2, Pb(ppm)=5 |
| 19. Cosroma BKC-100T_MSDS.pdf | COA/MSDS | 9 | 26,265 | 812.978 | As(ppm)=미확정 |
| 2. POTASSIUM COCOATE SOAP_COA.jpg | COA/MSDS | 1 | 1,179 | 103.454 | — |
| 2. POTASSIUM COCOATE SOAP_MSDS.pdf | COA/MSDS | 9 | 15,894 | 0.057 | — |
| 20. Salmon Milt (DNA-Na)_COA.pdf | COA/MSDS | 1 | 886 | 0.004 | Pb(ppm)=20, As(ppm)=5 |
| 20. Salmon Milt (DNA-Na)_MSDS.pdf | COA/MSDS | 4 | 6,835 | 0.011 | — |
| 21. Cosroma AAR-3_COA.pdf | COA/MSDS | 1 | 1,297 | 0.027 | Cd(ppm)=5, Hg(ppm)=1, As(ppm)=2, Pb(ppm)=5 |
| 21. Cosroma AAR-3_MSDS.pdf | COA/MSDS | 9 | 27,671 | 0.049 | — |
| 22. DactorTec™ KISsom_COA.pdf | COA/MSDS | 1 | 877 | 0.005 | As(ppm)=2 |
| 22. DactorTec™ KISsom_MSDS.pdf | COA/MSDS | 8 | 11,660 | 0.026 | — |
| 23. Sodium Azulene Sulfonate_COA.pdf | COA/MSDS | 1 | 1,663 | 0.003 | — |
| 23. Sodium Azulene Sulfonate_MSDS.pdf | COA/MSDS | 7 | 8,802 | 0.017 | — |
| 4. Ethanol K95_COA.pdf | COA/MSDS | 1 | 946 | 25.063 | ethanol(%)=미확정 |
| 4. Ethanol K95_MSDS.pdf | COA/MSDS | 8 | 15,596 | 0.018 | — |
| 5. RESPLANTA AQUA OLIVE_COA.pdf | COA/MSDS | 1 | 1,023 | 23.697 | — |
| 5. RESPLANTA AQUA OLIVE_MSDS.pdf | COA/MSDS | 9 | 18,283 | 257.271 | — |
| 6. PALMERA G995U_COA.pdf | COA/MSDS | 1 | 1,894 | 33.755 | Pb(ppm)=미확정 |
| 6. PALMERA G995U_MSDS.pdf | COA/MSDS | 6 | 12,699 | 0.011 | — |
| 7. M-HYDRO EG_MSDS.pdf | COA/MSDS | 6 | 10,111 | 0.014 | — |
| 8. InnoDiol 6_COA.pdf | COA/MSDS | 1 | 717 | 20.013 | — |
| 8. InnoDiol 6_MSDS.pdf | COA/MSDS | 5 | 8,108 | 0.009 | — |
| 9.  Potassium Hydroxide_COA.pdf | COA/MSDS | 1 | 1,048 | 25.236 | — |
| 9.  Potassium Hydroxide_MSDS.pdf | COA/MSDS | 7 | 12,894 | 0.015 | — |

### 건너뜀 12건

| 파일 경로 | 사유 |
| --- | --- |
| __MACOSX/._Drjcos | 시스템 메타데이터 |
| __MACOSX/Drjcos/._4. ISO22716 인증서.pdf | 시스템 메타데이터 |
| __MACOSX/Drjcos/._6. 닥토필 프로페셔널 포 페이스 사진.pdf | 시스템 메타데이터 |
| Drjcos/.DS_Store | 시스템 메타데이터 |
| __MACOSX/Drjcos/._.DS_Store | 시스템 메타데이터 |
| __MACOSX/Drjcos/._5. DactorPeel Professional For Face_COA.pdf | 시스템 메타데이터 |
| __MACOSX/Drjcos/._1. 닥터제이코스_사업자등록증명(영문).pdf | 시스템 메타데이터 |
| __MACOSX/Drjcos/._5. DactorPeel Professional For Face_MSDS.pdf | 시스템 메타데이터 |
| __MACOSX/Drjcos/._2. 제품설명서_닥토필 프로페셔널 포 페이스.pdf | 시스템 메타데이터 |
| __MACOSX/Drjcos/._닥터제이코스_사업자등록증명_2020.pdf | 시스템 메타데이터 |
| Drjcos/5. 원료 COA, MSDS.Zip/7. M-HYDRO EG_COA.crdownload | 미지원 형식(.crdownload) |
| __MACOSX/Drjcos/._5. 원료 COA, MSDS.Zip | 시스템 메타데이터 |

ZIP 결과의 필수 서류 미충족 목록은 공장등록증, 제품 목록, 공정 흐름도, 원재료 목록, 공급사 할랄 인증서, SJPH 매뉴얼이다. 50건 저장 성공과 인증 패키지 완비 여부는 별개다.

MuPDF가 `23. Sodium Azulene Sulfonate_MSDS.pdf` 처리 중 누락된 xref 객체 경고를 출력했지만, 해당 문서는 8,802자를 추출하고 정상 저장했다. OCR/문서 파싱 실패로 기록된 항목은 없었다.

실측 후 같은 프로세스의 추가 개별 업로드는 36분 경과로 테스트 로그인 토큰이 만료되어 401이 반환됐다. 이 값은 성공 응답 시간에서 제외했다. 최종 코드 재확인은 새 로그인·새 DB에서 수행했다. 브라우저의 기존 `apiFetch`는 401 시 refresh 후 재호출한다.

### 중소기업확인서 개별 업로드

| 파일 | HTTP | 크기 | 쪽 | 추출 문자 | 최종 분류 | 필드 | 단독 저장 응답 | 큐 완료까지 |
| --- | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: |
| 닥터제이코스_중소기업확인서.pdf | 200 | 185,745바이트 | 1 | 522 | other(기업 규모 참고서류) | `{}` | 0.066초 | 0.094초 |

## 테스트

관련 Python 63개 파일의 최종 수집 대상은 **3,382개**, 전체 Node 대상은 **148개**다. 아래처럼 넓은 회귀 실행과 수정된 범위의 재실행으로 전부 통과했다. 테스트 fixture에는 합성 PDF/ZIP/RAR/7z만 사용했으며 고객 원본을 추가하지 않았다.

| 실행 | 결과 |
| --- | --- |
| 관련 Python 기본 실행(사업자 파서 별도) | 3,159 passed / 257.91초 |
| 사업자 파서 독립 실행 | 169 passed / 1.47초 |
| RAR 포함 중첩 압축 + 문서 요청 API/발송 | 35 passed |
| 제품 보고서·UI 접근 관련 추가 | 34 passed / 6.53초 |
| 중소기업 분류 보완 후 분류·intake 등 | 478 passed / 14.72초 |
| 최종 heartbeat·중첩 압축·intake 변경 후 | 427 passed / 14.97초 |
| 전체 Node (`node --test tests/*.cjs`) | 148 passed |
| 최종 Python 대상 수집 | 3,382 tests / 63 files |

위 행에는 재실행이 겹치므로 행별 건수를 합산하지 않는다. 최초 넓은 실행의 실패는 제거한 TRUNCATED 계약을 기대하던 4건과, 누적된 공개 OCR rate-limit 카운터에 걸린 사업자 테스트 5건이었다. 전자는 새 계약으로 수정했고, 후자는 해당 모듈의 독립 실행으로 확인했다. 운영 rate limit이나 사업자 파서 동작을 이 문제 때문에 변경하지 않았다.

새 Python 테스트는 깊이 2·`.Zip`·전체 크기/개수 합산·메타데이터·미지원 파일·위조 415·저장 전 사전 검증·경로 이탈·링크·CP949/NFD UTF-8·실제 합성 RAR5/7z·느린 OCR 중 즉시 응답·파싱 한 번·본문 기반 큐 편철·중소기업 분류·heartbeat/연결 종료를 검증한다. 알림 테스트는 잘림만 있으면 업체 통지가 발생하지 않고 오디터 정보가 남는지 확인한다.

새 Node 테스트는 모든 `file_b64/zip_b64` 업로드 POST의 긴 타임아웃, 응답 본문 유실까지 포함한 목록 재조회, 파일명/크기/해시 구분, 415/사용자 취소 제외, 압축 자동 전환/권한 안내, 건너뜀 표시의 HTML escape, 복구된 분석 상태, 스트림 헤더 뒤 취소 신호를 검증한다.

재현 명령(사업자 모듈은 공개 OCR rate-limit 카운터 격리를 위해 별도 프로세스에서 실행):

```bash
PY=/Users/dany/IdeaProjects/llmdev/glhac-ai-v3/.venv/bin/python
GLHAC_NOTIFY_WORKER=0 GLHAC_LLM_PROVIDER=none GLHAC_RATE_LIMIT_PER_MIN=0 "$PY" -m pytest -q \
  tests/test_ai_r2_apply.py \
  tests/test_ai_r2_intake.py \
  tests/test_ai_r3_intake.py \
  tests/test_ai_r3_materials.py \
  tests/test_ai_r4.py \
  tests/test_aud_intake_view.py \
  tests/test_audit_high_b_2026_10.py \
  tests/test_audit_mid_a_2026_10.py \
  tests/test_audit_mid_b_2026_10.py \
  tests/test_audit_mid_c_2026_10.py \
  tests/test_autofill_ownership.py \
  tests/test_autoparse.py \
  tests/test_axis_d1_freeze_issue.py \
  tests/test_axis_d3_payment_ops.py \
  tests/test_axis_s1_input_rules.py \
  tests/test_canonical_docs.py \
  tests/test_cases_purge.py \
  tests/test_coa_measurements.py \
  tests/test_dictionary_single_source.py \
  tests/test_doc_checklist_pathway.py \
  tests/test_doc_intake_integrity.py \
  tests/test_doc_request_api.py \
  tests/test_doc_request_send.py \
  tests/test_docgen.py \
  tests/test_domain_dict.py \
  tests/test_evidence_autofile.py \
  tests/test_evidence_metadata.py \
  tests/test_final_e1_concurrency.py \
  tests/test_final_e3_ai.py \
  tests/test_form_tables_and_llm_failure.py \
  tests/test_forms_settings.py \
  tests/test_intake_cost.py \
  tests/test_llm_provider.py \
  tests/test_logistics_scheme.py \
  tests/test_material_detail.py \
  tests/test_no_ai_mode.py \
  tests/test_ops_dashboard.py \
  tests/test_org_halal_model_l3.py \
  tests/test_org_ocr_multilang_l5.py \
  tests/test_org_policy_a2.py \
  tests/test_org_reconcile_l2.py \
  tests/test_p2_batch_a.py \
  tests/test_p2_batch_b.py \
  tests/test_p2_final.py \
  tests/test_parse_failures_screening.py \
  tests/test_phase3_evidence.py \
  tests/test_prep_flow.py \
  tests/test_problem_documents_notify.py \
  tests/test_product_report.py \
  tests/test_profile_preview.py \
  tests/test_protected_terms.py \
  tests/test_provider_parity.py \
  tests/test_r7_f2.py \
  tests/test_r7_f3.py \
  tests/test_r7_f4.py \
  tests/test_remaining_r1.py \
  tests/test_sector_flow.py \
  tests/test_smoke.py \
  tests/test_uileg_access.py \
  tests/test_uiv4_regressions.py \
  tests/test_upload_nested_archive.py \
  tests/test_v3_smoke.py \
  --color=no
GLHAC_NOTIFY_WORKER=0 GLHAC_LLM_PROVIDER=none GLHAC_RATE_LIMIT_PER_MIN=0 "$PY" -m pytest -q tests/test_biz_doc_parse.py --color=no
node --test tests/*.cjs
```

## 측정 자료와 검증 한계

실측 원본 결과: `/private/tmp/glhac-upload-review/real-flow.json`, `manifest.json`, `baseline.json`, `after.json`, `lock-baseline.json`, `native-inspection.json`, `sme-final.json`, `final-replay.json`. 실행 로그도 같은 임시 디렉터리에 있다. 저장소에는 본 보고서와 합성 회귀 테스트만 추가했다.

외부 LLM 추출·LLM 검증, 실제 알림 발송, 운영 nginx 경유 브라우저 연결, Docker 이미지 빌드는 이 로컬 환경에서 검증하지 않았다. 코드·TestClient·Node 테스트와 실제 OCR/규칙 기반 결과를 검증했다. 커밋이나 배포는 수행하지 않았다.
