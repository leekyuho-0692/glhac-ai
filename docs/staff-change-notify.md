# 직원의 업체 데이터 변경 통보

직원이 업체 데이터를 수정하거나 삭제하면 같은 트랜잭션에 `change_record`를 저장한다.
업체 본인(`applicant`이며 대상과 같은 `org_id`), 요청 컨텍스트 없는 작업,
명시적으로 분리한 자동처리는 기록하지 않는다. 신규 행 추가도 통보 대상이 아니다.

## 포함 모델

| 모델 | 테이블 | 범위 |
| --- | --- | --- |
| CaseApplication | case_application | 신청 건 항목·상태·확장 정보 |
| DocumentAsset | document_asset | 서류 이름·유형·검토 상태·공개 fields 키·연결 |
| Material | material | 원재료 입력 정보 |
| Product | product | 제품 입력 정보 |
| ProductMaterial | product_material | 제품과 원재료의 연결 |
| MaterialEvidenceLink | material_evidence_links | 원재료와 증빙의 연결 |
| MaterialMeasurement | material_measurement | 측정 입력값·시험기관·근거 |
| CertificateScopeItem | certificate_scope_item | 증빙 서류의 인증 범위 |
| SjphEvidence | sjph_evidence | SJPH 제출 증빙 |
| Facility | facility | 업체 시설 정보 |
| Vehicle | vehicle | 업체 운송 차량 정보 |
| VehicleCleaning | vehicle_cleaning | 업체 차량의 세척 기록 |
| PenyeliaHalal | penyelia_halal | 업체 할랄 감독자 정보 |
| Org | org | name, address, profile_ext의 공개 키만 |
| User | app_user | 변경 전 역할이 applicant인 업체 계정만 |

별도의 `Applicant` 모델/테이블은 없다. `User.role == 'applicant'`가 업체 계정이다.
시설·차량·감독자·증빙 연결도 업체가 소유한 신청 자료이므로 포함했다.
케이스가 없는 조직 자원은 업체 단위로 묶는다.

## 제외 모델 전체 목록

- 심사·배정·업무 운영: ChangeImpact(`change_impact`), PendampingAssignment(`pendamping_assignment`),
  AuditFinding(`audit_finding`), LphAssignment(`lph_assignment`), CaseAuditor(`case_auditors`),
  HpasEvaluation(`hpas_evaluation`), OnsiteChecklist(`onsite_checklist`), AuditorPool(`auditor_pool`),
  AuditPlan(`audit_plan`), CorrectiveAction(`corrective_action`), ApprovalRequest(`approval_request`),
  Discussion(`discussion`), Consultation(`consultation`), DocumentRequest(`document_request`).
- 발급·의결·계약·서명·생성 산출물: HalalCertificate(`halal_certificate`), FatwaDecision(`fatwa_decision`),
  FatwaVote(`fatwa_vote`), Contract(`contract`), Signature(`signature`), GeneratedDocument(`generated_document`).
  생성 문서는 내부 심사 보고서·기간 보고서와 같은 테이블을 공유하므로 대상에서 제외했다.
- 결제·정산: Invoice(`invoice`), Payment(`payment`), Deposit(`deposit`),
  PaymentMatchCandidate(`payment_match_candidate`), Refund(`refund`), PaymentOperation(`payment_operation`),
  ConsultantPayout(`consultant_payout`).
- 감사·알림·연동·AI: WorkflowEvent(`workflow_event`), AuditLog(`audit_log`), ChainAnchor(`chain_anchor`),
  Notification(`notification`), MailLog(`mail_log`), ChangeRecord(`change_record`),
  ExternalIdentity(`external_identity`), IntegrationEvent(`integration_event`), AiExtraction(`ai_extraction`).
- 기준·메뉴: IngredientOntology(`ingredient_ontology`), RuleVersion(`rule_version`),
  LphReference(`lph_reference`), SysMenu(`sys_menu`), SysMenuI18n(`sys_menu_i18n`),
  SysRoleMenu(`sys_role_menu`), SysUserMenu(`sys_user_menu`), SysOrgMenu(`sys_org_menu`),
  SysMenuPermission(`sys_menu_permission`).
- 직원·초대·게시판: ConsultantInvite(`consultant_invite`), StaffSignupRequest(`staff_signup_request`),
  ConsultantProfile(`consultant_profile`), BoardPost(`board_post`), BoardReply(`board_reply`),
  Feedback(`feedback`), FeedbackImage(`feedback_image`), FeedbackComment(`feedback_comment`).

포함 모델은 15개, 제외 모델은 새 기록 모델 자체를 포함해 53개다.
`app_user` 중 직원 계정은 제외한다. 직원 계정 삭제로 업체 알림을 만들지 않는다.

## 기록에서 제외하는 값

비밀번호·토큰·시크릿·자격증명·API/private key, 파일 본문/base64·OCR 전문/좌표·번역 캐시,
행 버전·생성/수정/로그인 시각·업로더/생성자/검증자 메타데이터,
정규화 키·AI 신뢰도/스크리닝/계산 판정값은 저장하지 않는다.
JSON의 `_` 시작 키와 민감 키는 중첩 객체·배열에서도 제거한다.
내부 tenant 설정과 추출 처리 목록(`pending_handled`)도 제외한다.

JSON 객체는 공개 하위 키별 diff로 저장한다. 긴 값은 200자로 자른다.
절단된 값이 같아도 실제 값이 달랐던 경우를 구분하도록 `value_hashes`에 비교용 해시를 남긴다.
삭제에는 이름·파일명·신청 번호 등 대상 식별 문구를 보존한다.
행위자 이름은 `display_name`을 우선하고, 이메일·전화번호 형태는 사용하지 않는다.

## 요청 컨텍스트와 자동처리

`app.change_context.ChangeActorMiddleware`가 요청마다 독립된 `RequestActor`를 `ContextVar`에 넣는다.
`auth.get_current_user()`가 토큰/계정 상태 검증을 마친 후 DB의 사용자 ID·역할·소속·표시 이름을 저장한다.
`require_roles`, `rbac.require_action`, `optional_user`도 같은 인증 함수를 통과한다.
동기 의존성과 동기 핸들러가 서로 다른 스레드풀 호출로 실행되어도 요청 객체를 공유한다.
마지막 응답 본문을 보낼 때 비활성화해 Starlette background task로 행위자가 전파되지 않게 하고,
예외를 포함한 요청 종료의 `finally`에서 ContextVar를 복원한다.

자동처리 경계는 `automatic`/`automatic_changes`로 표시했다. 그 전에 남은 수동 변경은 먼저 flush하고,
자동 변경은 캡처를 끈 상태에서 flush한다. 적용 경로:

- `_reprocess_doc`, `_apply_intake_autofill`, `parse_file_ep`, `autofill_profile`
- `_apply_owned_autofill`, `_revert_failed_autofill`, 문서 기반 `_apply_agg_to_case`
- `_apply_document_measurements`, `_register_from_judgment`, `_screen_reviewed_material`
- `_auto_advance`

백그라운드·주기 작업에는 기본적으로 요청 컨텍스트가 없다.
사람의 확인대기 선택·수정, 문서 검토와 직접 프로필 저장은 수집 대상이다.

## 묶음 발송과 수신자

기존 `_notify_worker_loop`에서 `queue_problem_document_notifications` 다음에
`queue_staff_change_notifications`를 호출한다. `GLHAC_NOTIFY_WORKER=1`일 때 기존 30초 주기를 사용한다.
두 관리자 drain API에서도 변경 통보 큐를 확인한다.

`GLHAC_CHANGE_NOTIFY_DELAY_S` 기본값은 300초다. 케이스별 또는 업체별 마지막 변경 이후의 조용한 시간을 센다.
대상·항목별 최초 before와 최종 after를 합치고 원복은 생략한다. 삭제는 그 대상의 앞선 수정을 대체한다.
내용이 없어도 기록의 `notified_at`을 갱신한다.
SQLite의 writer 잠금을 조회 전에 확보하고, Notification 생성과 `notified_at` 갱신을 함께 커밋한다.

업체의 applicant 계정 각각에 화면 알림과 이메일을 만든다. 주소는 `contact_email`, 이메일 형태의 `username` 순이다.
applicant가 없으면 `Org.consultant_id`의 계정으로 보내며 컨설턴트 프로필 이메일도 지원한다.
수신자가 전혀 없으면 warning만 남기고 기록을 완료 처리한다.
업무 데이터 변경 통보에는 업체의 일반 채널 수신동의 게이트를 적용하지 않는다.

화면 알림은 payload의 `recipient_user_id`로 대상 계정을 제한한다.
`notify_text()`가 KO/EN/ID 문구 사전을 사용하며, 메일 언어는 케이스/업체 프로필의 lang/language/locale을 따른다.
메일 제목의 N은 병합 후 변경 항목 수와 삭제 건수의 합이다.
지연 알림 발송은 기존 drain의 채널별 재시도와 `mail_log`를 그대로 사용한다.

## 즉시 통보 경로

- `POST /admin/cases/purge`: 케이스와 포함된 하위 데이터 삭제 전 명시적 캡처.
- `DELETE /admin/users/{user_id}`: applicant 계정 삭제 전 이메일 확보.
- `POST /admin/orgs/purge-orphans`: 업체·조직 자원 삭제 전 컨설턴트 수신자 확보.
- 같은 모델의 `Session.delete()`도 `before_flush`에서 캡처한다. 현재 별도의 일반 업체 삭제 API는 없다.

원재료 삭제의 bulk 연결 정리와 제품 삭제의 ProductMaterial bulk 삭제도 명시적 헬퍼로 기록한다.
ORM에 보이지 않는 FK CASCADE 삭제와 SET NULL 변경은 부모가 사라지기 전에 대상 모델만 수집한다.

수신자와 업체명을 삭제 전에 확보하고, 루트 트랜잭션의 `before_commit`에서 최종 기록을 묶는다.
`after_commit`에서 `mail_delivery.send`를 호출하고 성공/실패를 별도 세션의 `mail_log`에 기록한다.
rollback/savepoint rollback은 즉시 통보를 취소하며, 메일 실패는 이미 완료된 삭제를 실패시키지 않는다.

## 설계 조정과 기존 DB

`before_flush` 외에 `do_orm_execute`에서 성공한 SQL UPDATE도 비교한다.
기존 `write_case`/`write_document_fields`의 compare-and-swap와 상태 UPDATE가 ORM flush를 우회하기 때문이다.
SQL UPDATE는 실행 전에 SQLite writer 잠금을 확보해 같은 트랜잭션의 전후 값을 비교한다.

`ChangeRecord`에는 삭제 대상과의 FK를 두지 않고 purge 목록에서 제외했다.
기존 `mail_log`도 케이스 purge에서 보존해 과거 발송 이력이 사라지지 않게 했다.
새 컬럼 `actor_name`, `value_hashes`는 각각 삭제 후 행위자 표시와 절단 값의 정확한 원복 비교를 위한 최소 스냅샷이다.

앱 시작의 `Base.metadata.create_all(bind=engine)`이 기존 SQLite DB에도 새 테이블과 인덱스를 만든다.
기존 테이블에 새 컬럼은 추가하지 않아 별도 ALTER가 필요 없다. 운영 DB에는 직접 접근하거나 DDL을 실행하지 않았다.

## 검증

`tests/test_staff_change_notify.py`는 실제 SQLite FK·트랜잭션·HTTP 인증 경로를 사용한다.
메일만 monkeypatch하여 삭제 후 발송 순서와 실패 처리를 확인한다.
실행 Python은 `/Users/dany/glhac-wt-residual/.venv/bin/python`, PYTHONPATH는 이 워크트리다.

최종 결과: **187개 파일 / 11,511개 테스트 통과 / 실패 0 / 건너뜀 0**.
새 기능 파일은 **38개 테스트 통과**. 전체 실행 시간은 1,122.06초다.
`tests/run_isolated.sh`와 같은 파일별 독립 pytest 프로세스, `GLHAC_RATE_LIMIT_PER_MIN=0`으로
모든 `tests/test_*.py`를 실행하고 파일별 결과와 테스트 수를 합산했다.
외부 서버 E2E 스크립트는 `GLHAC_E2E_BASE` 미설정으로 실행하지 않았다.

초기 단일 프로세스 실행에서는 앞선 OCR 테스트의 임시 Paddle 모델 캐시가 남아
`test_ocr_worker.py`의 3건이 실패했다. 원인 확인을 위해 중단 후 해당 파일을 단독 실행해
7건 통과를 확인했고, 최종 전체 격리 실행에서도 7건 모두 통과했다. OCR 코드는 수정하지 않았다.

코드 생성용 cli-v2 호출은 Port 1의 OpenAI API 연결 실패로 REJECT되었다.
사용자가 제공한 AGENTS.md의 실패 시 fallback 규칙에 따라 직접 구현했다.
커밋은 생성하지 않았고 HEAD는 `ef42077`이다.
