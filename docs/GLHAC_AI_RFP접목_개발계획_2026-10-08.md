# GL-HAC AI — 발주문서(RFP/SRS 2026-10-06) 현행 소스 접목 개발계획

작성 2026-10-08 · 기준 소스 `glhac-ai` cdc87ad(=운영 glhac.co.kr 10/04 배포본) · 브랜치 `feat/rfp-2026-10`
근거: `~/Downloads/GLHAC_AI할랄인증_01~04` + 프로토타입 `(1).html`, 갭표 `gap_A.md`(3.1~3.6)·`gap_B.md`(3.7~3.16)

## 0. 원칙
- **새로 짓지 않는다.** RFP 는 "프로토타입 참고·신규 설계"를 권하지만 현행 v3(FastAPI+SQLite, 라우트 441·모델 60·v4 모듈 43)가 운영 중이므로 SRS 규칙을 기존 모델·상태머신·v4 모듈에 붙인다.
- 흐름도(04)가 우선하는 규칙 5개 — 2인 확인 · 관리자 접수 · 계약 당사자 · 정족수 3인 · 영문 정본 — 는 서버에서 강제한다(화면 숨김 금지).
- 각 묶음은 **플래그/데이터 조건으로 점진 적용**(기존 테스트 804 유지), 새 테스트 추가, 코더≠검수자.
- 시연 기능(ADM-06)·현지 결제·ERP·네이티브 앱은 범위 밖.

## 1. 갭 통계 (SRS 117항목 + 문서 21종)
| 영역 | 있음 | 부분 | 없음 |
|---|---|---|---|
| 3.1~3.6 (COM·MEM·PRE·AUD·FRM·CTR) 57 | 10 | 38 | 9 |
| 3.7~3.16 (PRP·ONS·SHA·CRT·DOC·NTF·RPT·ADM·AI·I18N) 60 | 11 | 37 | 11 (+제외 1) |
| 문서 D-01~D-20·D-02R 21 | 7 | 9 | 5 (D-05·10·11·14·18) |

## 2. 묶음과 순서 (1차 MVP 우선)
| # | 묶음 | SRS | 접목 지점(현행) | 난이도 |
|---|---|---|---|---|
| ① | **메인·서브 2인 확인** | AUD-03/04/05/06, MEM-06, COM-06 | `MAKER_CHECKER`+`ApprovalRequest`+`approvals.js` 확장, `CaseAuditor(role=co)`, 판정 3라우트(`preassess/review`·`mock-audit/decision`·`audit-report/send-fatwa`) 본체 분리 | 상 |
| ② | **인증 가능 판정 → 정식 신청 → 관리자 접수** | FRM-01~07, PRE-08/09/10, COM-05 | AI 2차 분석(D-04)=`assess_pathway`+증빙율, 인증가능 판정(오디터·2인)+D-05, 정식 신청서 D-06(서약·대표 서명)·`eligible/formalWait` 상태, 접수=operator `contract/approve` + 반려 | 상 |
| ③ | **견적·계약·회차 입금·고지** | CTR-01~11, NTF-02/03, ADM-05 | `Invoice` 회차·지급조건, 견적 발행 주체 이관(consultant→operator), `sign_contract` 역할 가드, 고지/재고지/입금확인 알림(관리자 결정형 발송 창), 기한 설정 | 상 |
| ④ | 분야 5종·필수 서류·가입 | PRE-02/03, MEM-01/02/04/05, COM-08 | `scheme` 2종→`sector` 5종(`intake.doc_requirements` 분기 확장), 초대 링크 분야 프리필, 컨설턴트 변경 요청(ApprovalRequest) | 중 |
| ⑤ | 샤리아 정족수 3인·위원장 확정·D-19 | SHA-01~06 | `_fatwa_quorum_ok`/`_fatwa_tally`/`fatwa/committee`/`PATCH fatwa` 4곳 단일화, 조건부 투표값, 위원장 확정 | 중 |
| ⑥ | D-17·D-18 영문 정본·언어 탭·취합본 | ONS-05/06/07, I18N-03/04/05, DOC-01 | `factory-audit.docx?lang=` 3본 생성·저장, `GeneratedDocument` lang/번호/해시, 기업 확인 서명, D-18 취합 | 중 |
| ⑦ | 준비 서류 13종 상태·교육 서류 일괄 동의 | PRP-01~05 | `SJPH_EVIDENCE_ITEMS`10+`REQUIRED_DOCS`7 → 13종 카탈로그 + `SjphEvidence` 상태 4단, D-10~14 일괄 서명 | 중 |
| ⑧ | 기간별 인증현황 보고서 | RPT-01~07 | `analytics_summary` 확장(기간·전기 비교), 보고서 번호·수치 스냅샷 모델 | 중 |
| ⑨ | 양식 관리·열람 권한·알림 설정 | DOC-04/05/06, NTF-03, ADM-03/04, I18N-02 | `app/assets/*.docx` → 업로드 서식 버전, 문서별 역할 열람표, 역할 기본 언어 | 하~중 |
| ⑩ | 소소한 보정(하) | COM-02/03/07, PRE-06/07, AUD-02/07, CTR-03/04/10, CRT-04 … | 필드·가드·프런트 | 하 |

2차 범위(⑤~⑧ 일부)·3차(AI 고도화·SiHalal·세금계산서·자동 번역)는 1차 완료 후.

## 3. 묶음 ① 설계 — 2인 확인
**규칙(흐름도 16p)**: 메인의 처리 4종(사전심사 판정·인증가능 판정·모의심사 완료·샤리아 상정)은 실행되지 않고 "서브 확인 대기"로 저장 → 서브가 [점검 확인·승인]하면 실행, [의견 달아 되돌리기]면 메인에게 의견 전달 → 모두 업무 기록. 메인은 요청 취소 가능. 서브는 열람만. 메인 배정 시 서브 없으면 담당 건수 적은 오디터 자동 배정, 서브 최소 1명.

**접목**
- `TWO_PERSON_ACTIONS = {"preassess.review", "mock_audit.decision", "audit_report.send_fatwa"}` (+ ② 에서 `eligibility.verdict`). `MAKER_CHECKER` 에 같은 키로 `{"maker":{"auditor"}, "checker":"co_auditor", "label":…}` 등록.
- 적용 조건: **케이스에 서브(`CaseAuditor.role=co`)가 배정돼 있으면 필수**, 없으면 종전대로 즉시 실행(기존 테스트·운영 데이터 보호). `GLHAC_TWO_PERSON=strict` 면 서브 없을 때 409 `CO_AUDITOR_REQUIRED`.
- 메인 판별: `_case_main_auditor_uid(db, c)` = `_ops_latest_assignment` 의 auditor_id. 서브 판별: `CaseAuditor(role=co)`. **서브가 판정 라우트를 부르면 403 `NOT_MAIN_AUDITOR`**(AUD-06). operator/admin 은 종전 권한 유지.
- 3개 라우트의 본체를 `_do_preassess_review / _do_mock_audit_decide / _do_send_fatwa(db, c, actor, body:dict)` 로 분리하고, 라우트는 `_two_person_gate(...)` 를 먼저 탄다: 조건 충족 시 `ApprovalRequest(action_type, payload=body)` 생성 후 `202 {"pending":true, approval_id, message}` 반환.
- `_check_checker`: two-person 액션은 역할이 아니라 **그 케이스의 co 오디터**(또는 admin)만, self 금지. `_exec_approved`: payload 로 `_do_*` 호출(actor = 요청자 uid·role 보존) + 이벤트 `two_person.confirmed {main, sub, approval_id}`.
- 되돌림: `reject` → 이벤트 `<action>.returned` + 메인에게 `Notification`(의견 포함). 취소: `POST /approvals/{id}/cancel`(요청자 본인, pending 만).
- 자동 서브 배정: `ops_assign_auditor` 에서 메인 확정 후 co 없으면 담당 건수 최소 오디터(메인 제외) 자동 추가, 이벤트 `auditor.co_auto_assigned`. 마지막 co 삭제는 pending 요청 있으면 409.
- v4: `approvals.js` 를 aud 역할에서도 보이게(서브 홈 배지 `NAVC['aud-home']` + 승인함), 승인/되돌리기(의견 필수)/취소 버튼, 판정 화면 3곳은 202 응답이면 "서브 오디터 확인 대기" 토스트.
- 테스트 `tests/test_two_person.py`: 서브 있음→pending / 서브 승인→실행·이벤트 / 되돌림→알림 / 서브 판정 호출 403 / self 403 / 서브 없음→즉시 / 취소 / 자동 배정 / strict 409.

## 4. 진행 기록
- 2026-10-08 갭표 완료, 묶음 ① 착수.
