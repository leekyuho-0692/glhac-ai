# FK(외래키) 도입 설계 — R2

작성: 2026-10-19 · 브랜치 `fix/remaining-r2` · 대상: `app/models.py` 전 모델 · 엔진: SQLite(운영) / PostgreSQL(전환 시)

## 1. 배경과 결정

- 모델에 `ForeignKey` 가 하나도 없었다. `app/db.py` 는 연결마다 `PRAGMA foreign_keys=ON` 을 켜지만 **DB 에 FK 정의가 없어
  아무 효과가 없었다.** 그 결과 케이스를 지운 뒤 고아 행이 남았다(과거 운영 8개 테이블 94건, 고아 계약 4건).
- 사용자 결정: **운영 DB 까지 테이블 재생성으로 FK 를 넣는다.** 스키마는 `create_all` + `_migrate()`(누락 컬럼 ALTER) 로 관리되고
  운영에는 `alembic_version` 이 없다. SQLite 는 기존 테이블에 FK 를 ALTER 로 붙일 수 없어 공식 12단계(테이블 재생성) 절차를 쓴다
  (`scripts/fk_rebuild.py`).
- 신규/테스트 DB 는 `create_all` 만으로 FK 가 생긴다. `GLHAC_SQLITE_FK=0` 이면 PRAGMA 를 꺼서 FK 강제만 되돌릴 수 있다(기본 `1`).

## 2. 원칙

1. **감사·법적 기록은 CASCADE 로 지우지 않는다.** 인보이스·결제·환불·인증서·계약·파트와 결정/투표·전자서명·2인 승인 요청은
   `RESTRICT`(부모를 지우려면 사람이 먼저 명시적으로 치워야 한다). `AuditLog`·`WorkflowEvent` 는 아예 FK 대상이 아니다.
2. **순수 종속 행**(제품-원재료 연결, 증빙 링크, 케이스 작업 데이터)은 `CASCADE`.
3. **선택 참조**(문서→원재료/제품, 증빙 문서 참조 등)는 `SET NULL` — 문서는 증거라 남기고 연결만 푼다(기존 `delete_material` 의 MA5 동작과 같다).
4. **FK 가 아닌 것**: 값에 PK 가 아닌 가짜 키가 들어가거나(§4), 폐기해도 남아야 하는 귀속 문자열이거나, 부모 행이 보장되지 않는 컬럼.
5. 기존 케이스 purge(`POST /admin/cases/purge`)가 **감사로그를 의도적으로 보존**하는 것과 맞춘다 — `audit_log.case_id` 는 사라진 케이스를
   가리키는 것이 정상이므로 FK 로 묶지 않는다. purge 는 케이스 범위 모델을 FK 의존 순서(자식 먼저)로 지운다(`_fk_delete_order`).

## 3. 적용하는 FK 관계 (73개: CASCADE 44 · RESTRICT 13 · SET NULL 16)

표는 모델 메타데이터에서 생성했다(`scripts/fk_orphans.py` 도 같은 목록을 읽는다). 마지막 열은 운영 스냅샷
`glhac_v3_snapshot_202610091643.db`(2026-10-09) **복사본**에서 실측한 위반 행 수다.

| # | 자식.컬럼 | → 부모.PK | NULL | ondelete | 스냅샷 고아 |
|---|---|---|---|---|---|
| 1 | `ai_extraction.case_id` | `case_application.case_id` | 허용 | CASCADE | 0 |
| 2 | `approval_request.case_id` | `case_application.case_id` | 허용 | RESTRICT | 0 |
| 3 | `audit_finding.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 4 | `audit_plan.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 5 | `auditor_pool.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 6 | `board_post.consultant_id` | `app_user.user_id` | 허용 | SET NULL | 0 |
| 7 | `board_reply.parent_reply_id` | `board_reply.reply_id` | 허용 | CASCADE | 0 |
| 8 | `board_reply.post_id` | `board_post.post_id` | 불가 | CASCADE | 0 |
| 9 | `case_auditors.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 10 | `case_auditors.user_id` | `app_user.user_id` | 불가 | CASCADE | 0 |
| 11 | `certificate_scope_item.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 12 | `certificate_scope_item.document_id` | `document_asset.document_id` | 허용 | SET NULL | 0 |
| 13 | `change_impact.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 14 | `consultant_invite.consultant_id` | `app_user.user_id` | 불가 | CASCADE | 0 |
| 15 | `consultant_payout.consultant_id` | `app_user.user_id` | 불가 | RESTRICT | 0 |
| 16 | `consultant_profile.consultant_id` | `app_user.user_id` | 불가 | CASCADE | 0 |
| 17 | `consultation.case_id` | `case_application.case_id` | 허용 | SET NULL | 0 |
| 18 | `contract.case_id` | `case_application.case_id` | 불가 | RESTRICT | 0 |
| 19 | `corrective_action.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 20 | `deposit.matched_invoice_id` | `invoice.invoice_id` | 허용 | SET NULL | 0 |
| 21 | `discussion.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 22 | `document_asset.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 23 | `document_asset.material_id` | `material.material_id` | 허용 | SET NULL | 0 |
| 24 | `document_asset.product_id` | `product.product_id` | 허용 | SET NULL | 0 |
| 25 | `document_request.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 26 | `document_request.notification_id` | `notification.notification_id` | 허용 | SET NULL | 0 |
| 27 | `external_identity.case_id` | `case_application.case_id` | 허용 | CASCADE | 0 |
| 28 | `fatwa_decision.case_id` | `case_application.case_id` | 불가 | RESTRICT | 0 |
| 29 | `fatwa_vote.case_id` | `case_application.case_id` | 불가 | RESTRICT | 0 |
| 30 | `feedback_comment.feedback_id` | `feedback.feedback_id` | 불가 | CASCADE | 0 |
| 31 | `feedback_image.feedback_id` | `feedback.feedback_id` | 불가 | CASCADE | 0 |
| 32 | `halal_certificate.case_id` | `case_application.case_id` | 불가 | RESTRICT | 0 |
| 33 | `hpas_evaluation.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 34 | `integration_event.case_id` | `case_application.case_id` | 허용 | SET NULL | 0 |
| 35 | `invoice.case_id` | `case_application.case_id` | 불가 | RESTRICT | 0 |
| 36 | `lph_assignment.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 37 | `material.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 38 | `material_evidence_links.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 39 | `material_evidence_links.document_id` | `document_asset.document_id` | 불가 | CASCADE | 0 |
| 40 | `material_evidence_links.material_id` | `material.material_id` | 불가 | CASCADE | 0 |
| 41 | `material_measurement.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 42 | `material_measurement.document_id` | `document_asset.document_id` | 허용 | SET NULL | 0 |
| 43 | `material_measurement.material_id` | `material.material_id` | 허용 | CASCADE | 0 |
| 44 | `notification.case_id` | `case_application.case_id` | 허용 | CASCADE | 0 |
| 45 | `onsite_checklist.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 46 | `org.consultant_id` | `app_user.user_id` | 허용 | SET NULL | 0 |
| 47 | `payment.case_id` | `case_application.case_id` | 허용 | RESTRICT | 0 |
| 48 | `payment.invoice_id` | `invoice.invoice_id` | 허용 | RESTRICT | 0 |
| 49 | `payment_match_candidate.case_id` | `case_application.case_id` | 허용 | SET NULL | 0 |
| 50 | `payment_match_candidate.deposit_id` | `deposit.id` | 허용 | CASCADE | 0 |
| 51 | `payment_match_candidate.invoice_id` | `invoice.invoice_id` | 허용 | SET NULL | 0 |
| 52 | `pendamping_assignment.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 53 | `penyelia_halal.user_id` | `app_user.user_id` | 허용 | SET NULL | 0 |
| 54 | `product.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 55 | `product_material.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 56 | `product_material.material_id` | `material.material_id` | 불가 | CASCADE | 0 |
| 57 | `product_material.product_id` | `product.product_id` | 불가 | CASCADE | 0 |
| 58 | `refund.case_id` | `case_application.case_id` | 허용 | RESTRICT | 0 |
| 59 | `refund.invoice_id` | `invoice.invoice_id` | 허용 | RESTRICT | 0 |
| 60 | `signature.case_id` | `case_application.case_id` | 허용 | RESTRICT | 0 |
| 61 | `sjph_evidence.case_id` | `case_application.case_id` | 불가 | CASCADE | 0 |
| 62 | `sjph_evidence.document_id` | `document_asset.document_id` | 허용 | SET NULL | 0 |
| 63 | `sys_menu.parent_menu_id` | `sys_menu.menu_id` | 허용 | CASCADE | 0 |
| 64 | `sys_menu_i18n.menu_id` | `sys_menu.menu_id` | 불가 | CASCADE | 0 |
| 65 | `sys_menu_permission.menu_id` | `sys_menu.menu_id` | 불가 | CASCADE | 0 |
| 66 | `sys_org_menu.menu_id` | `sys_menu.menu_id` | 불가 | CASCADE | 0 |
| 67 | `sys_role_menu.menu_id` | `sys_menu.menu_id` | 불가 | CASCADE | 0 |
| 68 | `sys_user_menu.menu_id` | `sys_menu.menu_id` | 불가 | CASCADE | 0 |
| 69 | `sys_user_menu.user_id` | `app_user.user_id` | 불가 | CASCADE | 0 |
| 70 | `vehicle.case_id` | `case_application.case_id` | 허용 | SET NULL | 0 |
| 71 | `vehicle_cleaning.penyelia_id` | `penyelia_halal.penyelia_id` | 허용 | RESTRICT | 0 |
| 72 | `vehicle_cleaning.photo_doc_id` | `document_asset.document_id` | 허용 | SET NULL | 0 |
| 73 | `vehicle_cleaning.vehicle_id` | `vehicle.vehicle_id` | 불가 | CASCADE | 0 |

### 정책 근거 요약

| 정책 | 대상 | 이유 |
|---|---|---|
| RESTRICT (13) | `halal_certificate`·`contract`·`invoice`·`payment`·`refund`·`fatwa_decision`·`fatwa_vote`·`signature`·`approval_request` 의 `case_id`, `payment.invoice_id`, `refund.invoice_id`, `vehicle_cleaning.penyelia_id`, `consultant_payout.consultant_id` | 법적·재무 기록. 부모를 지울 때 함께 사라지면 안 된다. 서명한 페냘리아 행도 서명 근거라 보존 |
| CASCADE (44) | 케이스 작업 데이터(제품·원재료·문서·체크리스트·평가·현장심사 등), 연결 테이블, 메뉴 설정, 게시판 답글, 피드백 첨부 | 부모 없이는 의미가 없는 종속 행 |
| SET NULL (16) | 문서→원재료/제품, 증빙문서 참조, 외부연동 이벤트·상담·차량·매칭후보의 `case_id`, 입금→인보이스 매칭 | 선택 참조. 로그/증거 행은 남기고 연결만 푼다 |

## 4. FK 로 만들지 않은 컬럼 (전수 조사 결과)

`app/models.py` 의 `*_id`·참조성 컬럼을 모두 훑어, 위 73개 외에 FK 가 아닌 것과 그 이유를 적는다.
"스냅샷 고아"는 같은 복사본에서 `NOT IN (부모 PK)` 로 센 값이다(NULL 제외).

### 4.1 가짜(의사) 키가 실제로 들어가는 컬럼 — 코드에서 확인

| 컬럼 | 실제로 들어가는 값 | 스냅샷 고아 | 비고 |
|---|---|---|---|
| `workflow_event.case_id` | `invite:<CODE>`, `users:<UID>`, `v4nav:…`, `reports:<org>`, 조직 id(청구 설정), 규정 id, **오디터 프로필 이벤트는 user_id**, 케이스 id | 4 (오디터 프로필 3 + 삭제된 케이스 1) | 법적 해시 체인. FK 금지 |
| `audit_log.case_id` | 케이스 id(삭제 후에도 남는다) · 의사 키 | 3,736 (case) / 73 (actor_id) / 39 (org_id) | purge 가 의도적으로 보존. FK 금지 |
| `generated_document.case_id` | 케이스 id **또는 `reports:<org_id>`**(기간 보고서 `period_report`, `POST /admin/reports`) | 0 (스냅샷엔 보고서 없음) | FK 를 걸어 보니 `test_period_report` 가 깨졌다 → **FK 제외**. 후속 과제: 보고서는 `case_id=NULL`+`org_id` 로 바꾸고 FK 를 건다 |
| `corrective_action.finding_id` | `audit_finding.finding_id` **또는 `eval:<요소>`**(평가 판정이 자동 생성한 CAR, `evaluation_verdict`) | 0 | FK 를 걸어 보니 `test_p2_batch_b` 가 깨졌다 → **FK 제외**(`case_id` 는 FK 유지) |
| `signature.subject_id` | `subject_type` 에 따라 인증서 id 또는 문서 id (다형) | 0 | 다형 참조. `signature.case_id` 만 FK |
| `sys_menu_permission.target_id` | `assignment_type` 에 따라 역할 코드/조직 id/사용자 id (다형) | 0 | 다형. `menu_id` 만 FK |
| `sys_role_menu.role_id` | `ROLE_AUDITOR` 등 역할 코드 | – | 코드값. 테이블 없음 |

### 4.2 `org_id` 계열 — org 행이 보장되지 않아 제외

`org` 는 "org_id 문자열 정본을 가진 경량 레지스트리"로, **행이 지연 생성**된다. 시드가 만드는 사용자(`admin` 의 `org_id='*'`,
데모 계정의 `org_demo`)는 `org` 행 없이 생기고, `Org(...)` 는 가입·케이스 생성·조정 등 여러 경로에서 필요할 때 만든다.
`app_user.org_id`/`case_application.org_id` 에 FK 를 걸어 보니 신규 DB 부트스트랩(`seed_users`)부터 실패했다.
운영 스냅샷은 위반 0 이지만 **신규 DB 를 부트스트랩할 수 없으므로 제외**했다.
대상: `case_application`·`app_user`·`facility`·`vehicle`·`vehicle_cleaning`·`penyelia_halal`·`external_identity`·`generated_document`·
`notification`·`product`·`contract`·`consultation`·`feedback`·`approval_request`·`staff_signup_request`·`sys_org_menu` 의 `org_id`,
`audit_log.org_id`. 후속 과제: org 행 생성 보장(시드·가입 경로) 후 `app_user.org_id`·`case_application.org_id` 를 RESTRICT 로 도입.
(스냅샷 14개 org 는 모든 참조를 충족한다.)

### 4.3 사용자 귀속 문자열 — 사용자를 지워도 "누가 했는가"는 남아야 함

`document_asset.uploaded_by`, `ai_extraction.reviewer_id`, `payment_match_candidate.reviewer_id`, `pendamping_assignment.pendamping_id`,
`audit_log.actor_id`(스냅샷 73건은 이미 삭제된 사용자), `*.created_by`/`recorded_by`/`submitted_by`/`decided_by`/`requested_by`/`reviewer`/
`verified_by`, `feedback.author`(username), `fatwa_vote.member`(이름). FK + SET NULL 로 만들면 귀속이 지워지고, RESTRICT 면 사용자 삭제가 막힌다 → 제외.

### 4.4 JSON 배열·외부 식별자·코드

- JSON 배열: `case_application.facility_ids`/`product_ids`, `halal_certificate.frozen_*_ids`, `contract.product_ids`,
  `fatwa_decision.product_scope`, `consultant_payout.invoice_ids` — FK 불가.
- 외부/표시 문자열: `integration_event.external_id`, `invoice.payment_ref`, `consultant_invite.code`, `board_post.ref_code`,
  `external_identity.external_*`.
- 순환 회피: `org.consultant_id → app_user` 는 FK(SET NULL) 로 넣었고(위 표), `app_user.org_id → org` 를 제외했으므로 org↔user 순환은 없다.

## 5. 삭제 경로 영향 (FK ON 에서 찾아 고친 곳)

| 경로 | 문제 | 조치 |
|---|---|---|
| `POST /admin/cases/purge`, `POST /admin/orgs/purge-orphans` | 이름순으로 지워 `invoice` 가 `payment` 보다 먼저 지워지면 RESTRICT 위반(조직 purge 는 `penyelia_halal` 이 `vehicle_cleaning` 보다 먼저) | 삭제 순서를 `metadata.sorted_tables` 역순(자식 먼저)으로(`_fk_delete_order`). 감사로그 보존 규약은 그대로 |
| `DELETE /admin/users/{id}` | 지급 기록이 있는 컨설턴트는 RESTRICT 로 500 | 409 `USER_HAS_RECORDS` |
| `POST /cases/{id}/matrix/link` | 없는 제품/원재료 id 를 조용히 연결(`IntegrityError` 를 중복으로 오인) | 이 케이스의 실제 제품·원재료인지 확인 → 404 |
| `POST /orgs/{id}/vehicles`, `POST /consultations` | 없는 `case_id` 를 그대로 저장 | 404 `CASE_NOT_FOUND` |
| 그 밖의 FK 위반 전반 | 500 | 전역 핸들러: 409 `FK_CONSTRAINT` (서버 오류가 아니라 요청이 데이터와 맞지 않음) |
| `DELETE /materials/{id}`, 제품 삭제, 원재료 중복정리 등 | 이미 자식 정리 후 삭제 | 변경 없음. 중복정리가 지운 원재료의 제품 연결은 이제 DB 가 CASCADE 로 치운다(전엔 고아로 남았다) |

## 6. 운영 적용 절차 (요약)

1. `scripts/fk_orphans.py` — 위반 보고(읽기 전용). 감사·법적 기록(RESTRICT) 고아가 있으면 **중단하고 사람이 판단**.
2. `scripts/fk_orphans.py --apply` — CASCADE 고아 삭제 / SET NULL 고아 NULL화(단일 트랜잭션).
3. `scripts/fk_rebuild.py` — dry-run 후 `--apply`. 사전 점검(고아 0·컬럼 대응·NOT NULL·트리거/뷰)이 통과해야만 진행,
   테이블별 행 수 전·후 대조·`foreign_key_check` 0건·`integrity_check ok` 가 아니면 ROLLBACK.
4. 런북: `scripts/fk_migrate_on.sh` (로컬) → `scripts/fk_migrate_remote.sh` (서버). `docs/운영_배포_가이드.md` §6-1 참조.

## 7. 열린 쟁점 (사람이 판단)

- 스냅샷에는 RESTRICT 고아가 없지만, **운영 현재 DB** 에는 있을 수 있다(과거 계약 4건 등). 런북은 발견 시 중단하고 목록만 낸다.
  처리 방법은 케이스별로 (a) 부모 케이스 복원 (b) 보존용 보관소로 이관 후 삭제 (c) 법무 승인 후 삭제 중에서 정한다.
- 운영 DB 에 모델에 없는 컬럼(`material.derivation_detail`, `material.process_info` — 스냅샷에서 확인, 전부 NULL)이 있다.
  재생성은 이를 버린다 → `--allow-drop-empty-columns` 로 명시 허용해야 하고, **값이 있으면 어떤 옵션으로도 진행하지 않는다.**
- NOT NULL 인데 NULL 인 옛 행(`_migrate` 가 컬럼을 NULL 허용으로 추가함)은 모델의 스칼라 기본값으로 채워 복사한다(앱이 이미 그렇게 읽는 값).
  기본값이 없으면 중단.
- `org` FK(§4.2), 기간 보고서·평가 CAR 의 의사 키(§4.1)는 후속 과제.
