# GL-HAC AI v3 — 프로덕션 UI/시스템 설계서 / Production Design Spec
### 할랄·샤리아 인증 플랫폼 · Halal·Sharia Certification Platform · KO/EN

| 항목 | 내용 |
|---|---|
| 버전 / Version | **v3.0 (production spec)** |
| 상태 / Status | 설계 확정 — 구현 착수 |
| 작성일 / Date | 2026-07-02 |
| 베이스 / Base | v2(`../glhac-ai`) 정본 · 회의(2026-07-02) 6역할 재구성 |
| 스택 / Stack | Python 3.11 · FastAPI · SQLite(→PostgreSQL 승격) · 로컬 AI(Ollama qwen2.5 · PaddleOCR) |
| 정본 소스 | `app/state_machine.py`(28상태) · `app/main.py`(API) · `app/models.py`(26 엔티티) · `app/static/index.html`(NAV/ROLE_MENUS/ROLE_ACTIONS) |

---

## 1. 개요 / Overview
인도네시아(→글로벌) **할랄·샤리아 인증** 신청·심사·발급 전 과정을 단일 플랫폼에서 처리한다. **자기선언(Self-Declare)·정규(Reguler) 이중경로**, AI 보조(원재료 스크리닝·OCR·경로판정 제안), **역할 기반 접근제어(RBAC)**, **해시체인 감사(무결성)** 를 핵심 축으로 한다.

### 1.1 용어 / Glossary
| 용어 | 설명 |
|---|---|
| SIHALAL / BPJPH | 인니 정부 할랄 인증 시스템/기관. 외부 식별자 검증 대상 |
| SJPH | 할랄제품보증시스템(5요소: 경영책임·재료·제품·공정·모니터링) |
| HPAS | 할랄 제품 보증 평가 |
| LPH | 할랄검사기관(현장 실사 주체) |
| Fatwa | 파트와(위원회 할랄 판정) |
| penyelia_halal | 기업 법정 할랄감독자 |
| pendamping_pph | 자기선언(SEHATI) 검증 동반자 |
| KMA1360 | 자동 할랄 간주 품목(면제 리스트) |

### 1.2 원칙 / Principles
1. **AI는 제안만, 전이는 사람/시스템 액션.** (경로판정·스크리닝은 제안, 확정은 권한자)
2. **게이트 우선:** SIHALAL 미검증 → 제출 차단 · 하람/고위험 의심재료 → 증빙 필수.
3. **모든 전이 = append-only 해시체인 감사**(변조 탐지).
4. **엄격 RBAC:** 프론트 nav 게이팅 + 백엔드 403 이중 강제.
5. **다국어(KO/EN/ID)·다크모드** 전 화면 기본.

---

## 2. 아키텍처 / Architecture
```
[Browser SPA] ──REST──▶ [FastAPI]
  · 역할 nav/action 게이팅       · require_roles 403 강제
  · i18n(KO/EN/ID)·테마         · 상태머신 전이+가드
  · 공통 리스트/폼 컴포넌트       · 해시체인 감사
                                 · 로컬 AI 어댑터(Ollama/PaddleOCR)
                                 · 홍익AI 장기 컨텍스트(CHU-1) 어댑터 ──▶ [CHU-1 :7600 e5·chromadb]
                                 └ DB(SQLite→PostgreSQL): 26 엔티티
```
- **프로덕션 승격 항목:** SQLite→PostgreSQL, JWT(HMAC→비대칭 서명 옵션), 파일 저장소(로컬→객체스토리지), AI 서버 분리, HTTPS/역방향 프록시.
- **장기 컨텍스트 연동:** AI 어시스턴트가 **홍익AI/CHU-1 장기기억**(할랄 규정·파트와 선례·유사 케이스)을 RAG로 회수해 답변 근거 강화. 장애 시 로컬 문맥 폴백. 민감정보 비저장.

---

## 3. 데이터 모델 / Data Model (26 엔티티)
| 도메인 | 엔티티 |
|---|---|
| 케이스 | `CaseApplication`(pathway·state·risk_category·org) · `WorkflowEvent`(감사, 해시체인) · `Discussion` |
| 제품·재료 | `Product` · `ProductMaterial` · `Material`(screen_status: halal/mushbooh/haram·severity·evidence) · `IngredientOntology` |
| 자기선언 | `PenyeliaHalal` · `PendampingAssignment` · `ExternalIdentity`(SIHALAL·identifier_match) |
| 심사 | `AuditFinding` · `LphAssignment` · `AuditorPool` · `LphReference` · `OnsiteChecklist` · `HpasEvaluation` |
| SJPH·문서 | `SjphEvidence` · `DocumentAsset` |
| 판정·발급 | `FatwaDecision` · `HalalCertificate` · `RuleVersion` |
| 청구·조직·사용자 | `Invoice` · `Org` · `User`(role·org_id) |

**핵심 필드(발췌):** 기업 프로필(`businessName·nib·responsiblePerson·halalSupervisor·factoryRegNo·category·submissionType`), 재료(`type·source·cert·risk·supportInfo{msds·coa·productionProcessFlow·animalPorkFreeFacility}`).

---

## 4. 상태 머신 / State Machine (28상태, 이중경로)
```
onboarding → application_draft → ai_pre_assessment_ready → ai_pre_assessment_running → pathway_determination
 ├ self_declare_eligible → sjph_lite_prepared → pendamping_verification → self_declaration_submitted → committee_verification → certificate_issued
 ├ supplementation_required → supplementation_submitted → consultant_review → …
 └ consultant_review → document_pre_audit_requested → …_in_review → …_approved → lph_assignment
      → onsite_audit_scheduled → onsite_audit_in_progress ⇄ corrective_action_required/submitted → audit_closed
      → hpas_evaluation_ready → final_package_preparation → fatwa_review → (fatwa_approved | supplementation_required) → certificate_issued
certificate_issued → post_certification_monitoring → (renewal_preparation | change_impact) → renewal_preparation
```
- **가드(예):** `guard_pathway_selfdeclare`(risk=low·MSME·임계재료無·증빙완비) · `guard_selfdeclare_submit`(pendamping=verified) · SIHALAL verified.
- **감사:** 전 전이 `WorkflowEvent` append-only + 해시체인. `GET /cases/{id}/audit-verify` 무결성 검증.

---

## 5. 역할 · RBAC / Roles & Access (엄격 이중 강제)
### 5.1 6역할 (회의 확정) ↔ v2 코드역할
| 역할 | 성격 | v2 code |
|---|---|---|
| 클라이언트 / Client | 외부 고객(신청 기업) | `applicant`(+`penyelia_halal`·`pendamping_pph` 흡수) |
| 컨설턴트 / Consultant | **영업**(유치·신청지원·청구) | `consultant`(축소) |
| 오디터 / Auditor | **현장실사 현업**(서류 조회, 신청 제외) | `auditor` |
| 샤리아 / Sharia | **중간관리자**(심의·LPH배정·파트와상정·발급) | `fatwa_liaison`(확장) |
| 최고 업무운영자(최종승인자) | 총괄+**최종 승인** | (신설) |
| ITO 관리자 / ITO Admin | 시스템 admin | `admin` |

### 5.2 화면 × 역할 (nav 게이팅)
| 화면 | 클라 | 컨설 | 오디터 | 샤리아 | 최고운영 | ITO |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| dashboard | ● | ● | ● | ● | ● | ● |
| caseList | ● | ● | · | ●RO | ● | ● |
| workflow(워크스페이스) | ● | ● | · | · | ● | ● |
| application | ● | ●지원 | ·✗ | · | · | ● |
| materials | ● | · | ●RO | · | · | ● |
| sjphIntegration | ● | · | ●RO | · | · | ● |
| documents | ● | ● | ● | ● | ● | ● |
| audit | · | · | ● | ●LPH배정 | ● | ● |
| mockAudit | · | · | ● | ● | ● | ● |
| sjphEvaluation | ● | · | ●RO | ●상정 | ● | ● |
| invoice | ●RO | ● | · | · | ●RO | ● |
| fatwa | · | · | · | ● | ● | ● |
| certificate | · | · | · | ●발급 | ●승인 | ● |
| assistant | ● | ● | ● | ● | ● | ● |
| system | · | · | · | · | · | ● |
| **계** | 10 | 7 | 8 | 9 | 11 | 15 |

### 5.3 액션 × 역할 (백엔드 `require_roles` 403 강제)
| 액션 | 엔드포인트 | 허용 역할(회의 반영) |
|---|---|---|
| case_create·profile·product | `POST /cases`·`PATCH profile`·products | 클라이언트·컨설턴트 |
| material CRUD·matrix_link | `POST/DELETE materials`·`/matrix/link` | 클라이언트(+할랄감독) |
| label_ocr·zip_intake·parse | `materials/from-label*`·`intake-zip`·`parse-file` | 클라이언트·컨설턴트 |
| doc_review | `PATCH documents/{id}/review` | 컨설턴트 |
| sihalal_link / verify | `sihalal/identity/link`·`/verify` | 링크: 클라·컨설 / 검증: 컨설 |
| pathway_confirm | `pathway/confirm` | 컨설턴트 |
| pendamping_assign / verify | `pendamping/assign`·`/verify` | 배정: 컨설 / 검증: 동반자(클라) |
| finding_edit | `findings` CRUD | **오디터**·컨설 |
| **lph_assign** | `lph-assignment` | **샤리아·최고운영자** |
| **fatwa_decide** | `PATCH /fatwa` | **샤리아**(+최고운영자) |
| **cert_issue** | `certificate/issue` | **샤리아·최고운영자** |
| invoice_create / pay | `invoices`·`/pay` | 생성: 컨설 / 결제: 컨설·클라 |
> ⚠️ v2 대비 이관: `lph_assign·cert_issue·pathway→fatwa 상정`을 **컨설턴트에서 샤리아·최고운영자로** 이동. 백엔드 `ROLE_ACTIONS`/`require_roles` 재매핑 필수.

---

## 6. 정보구조 · 내비게이션 / IA & Navigation
- **공통(인증 전):** 로그인·가입(`/auth`) · 온보딩(state `onboarding`).
- **레이아웃:** 좌 사이드바(역할 필터 nav) + 상단바(제목·[code]·상태 뱃지 + 🌐다국어 + 🌙/☀️테마) + 메인.
- **라우팅:** `#/{screen}` 또는 `#/case/{id}/{section}`(워크스페이스 섹션). nav는 `ROLE_MENUS[role]`로 필터.

---

## 7. 화면별 상세 명세 / Screen Specs
> 표준 항목: **목적 · 레이아웃 · 컴포넌트 · 상태(empty/loading/error) · 액션·권한 · API · 리스트요건**. 대표 4화면 심화, 나머지 §부록 표.

### 7.1 application 신청 (클라이언트·컨설턴트[지원])
- **목적:** 케이스 프로필·제품·SIHALAL 검증·경로판정·pendamping까지 신청 준비.
- **레이아웃:** 5블록 — ①프로필/제품 ②SIHALAL 동일성 ③경로판정(AI 제안→확정) ④pendamping ⑤진행 타임라인.
- **컴포넌트:** 폼(자동파싱 채움) · 파일 드래그앤드랍 · 검증 상태칩 · 경로 배지(SD/REG) · 스테퍼.
- **상태:** empty(신규 케이스 안내) · loading(파싱 중 스켈레톤) · error(검증 실패/필수 누락 인라인).
- **액션·권한:** SIHALAL link(클라·컨설)·verify(컨설) · pathway_confirm(컨설) · pendamping_assign(컨설)/verify(동반자).
- **API:** `POST /cases`·`PATCH /cases/{id}/profile`·`POST sihalal/identity/link|verify`·`POST pathway/confirm`·`POST pendamping/assign|verify`·`GET timeline`.
- **게이트:** SIHALAL 미검증 시 제출 버튼 비활성.

### 7.2 materials 원재료 AI (클라이언트·오디터[RO])
- **목적:** 원재료 등록·AI 임계 스크리닝(할랄/의심/하람)·온톨로지 매칭·증빙.
- **컴포넌트:** **원재료 목록(리스트 컴포넌트: 상태·심각도·증빙유무 필터)** · 스크리닝 결과 패널(사유·근거) · 라벨 OCR·ZIP 인테이크 업로더 · 증빙 첨부.
- **규칙:** `risk∈{high,medium}` → supportInfo(msds/coa/공정도/무동물·돈지방시설) 필수(§`supportRequired`).
- **액션·권한:** material CRUD(클라+할랄감독) · label_ocr(컨설) · matrix_link(클라).
- **API:** `POST/DELETE materials`·`materials/from-label`·`intake-zip`·`/matrix/link`·`material/{id}/evidence`·`materials/export.xlsx`.
- **오디터:** 읽기전용(성분 대조).

### 7.3 audit 심사 (오디터·샤리아[LPH배정]·최고운영)
- **목적:** LPH 배정·현장 실사·지적(Findings)·시정조치(CAR).
- **컴포넌트:** **심사 큐(배정 케이스 목록)** · **지적 목록**(일자·영역·심각도·시정·기한) · AI 지적 우선순위 · 종결 증빙.
- **액션·권한:** finding CRUD(**오디터**·컨설) · lph_assign(**샤리아·최고운영자**) · 심사 종결.
- **API:** `POST/PATCH/DELETE findings`·`POST lph-assignment`·`onsite-checklist`.

### 7.4 fatwa 파트와 (샤리아·최고운영)
- **목적:** 최종 패키지 심의·위원회 결정·결정문.
- **컴포넌트:** **심의 대기 큐** · 최종 패키지 뷰 · 위원 구성(위원장/간사/위원) · 결정(승인/조건부/반려)·결정문.
- **액션·권한:** fatwa_decide(**샤리아**·최고운영자) · 파트와 상정(샤리아·최고운영).
- **API:** `PATCH /fatwa`·`fatwa/document`.

### 7.5 부록 — 화면별 요약표
| 화면 | 핵심 컴포넌트 | 리스트요건 | 주요 API |
|---|---|---|---|
| dashboard | 인니/세계 지도·단계집계·감사피드·공지 | – | `admin/cases`·`audit`·집계 |
| caseList | **마스터 목록**(상태·경로·담당·기한 필터·검색·페이지) | ★본체 | `GET /cases` |
| workflow | 28상태 스테퍼·섹션 링크·다음액션 | 케이스별 | `GET timeline`·전이 API |
| sjphIntegration | SJPH 5요소·HPAS 매핑·제품 동기화 | 요소/제품 목록 | `PATCH /sjph`·`sjph/manual` |
| documents | 필수서류 체크리스트·뷰어·검수 | ★문서 목록 | `documents`·`doc-checklist` |
| mockAudit | 사진·자료 체크→통과/거부 | ★대상 큐 | (내부 심사) |
| sjphEvaluation | HPAS 평가·최종패키지·상정 | ★평가 대상 | `hpas`·평가 API |
| invoice | 청구 생성·결제 | ★청구서 목록 | `invoices`·`/pay` |
| certificate | 발급(자동일자)·사후관리·변경영향 | ★인증서 목록 | `certificate/issue`·`change-impact` |
| assistant | 케이스 Q&A·스크리닝·체크리스트 | – | `ask`·`ai/screen-text` |
| system | 사용자·조직·온톨로지·감사 | ★3종 목록 | `admin/*` |

---

## 8. 공통 컴포넌트 · 디자인 시스템 / Design System
### 8.1 토큰 / Tokens
- **색:** ink `#111827` · sub `#374151` · line `#CBD5E1` · bg `#F8FAFC` · primary `#2563EB` · success `#10B981` · warn `#F59E0B` · danger `#EF4444` · accent `#8B5CF6`. **다크:** bg `#0F172A` · surface `#1E293B` · text `#E5E7EB`.
- **타이포:** Pretendard(KO) / system(EN). scale 11–32.
- **스페이싱:** 4px 그리드. radius 8/12/16.
- **테마 토큰은 CSS 변수**로 → 다크/화이트 토글이 루트 클래스 스위치.

### 8.2 공통 컴포넌트 / Components
| 컴포넌트 | 요건 |
|---|---|
| **DataList(리스트)** | 정렬·필터·검색·페이지네이션·행 액션·CSV·**역할별 컬럼 게이팅**·empty/loading/error. P0. |
| Form | 자동채움·인라인 검증·필수 표시·저장/임시저장 |
| Uploader | 드래그앤드랍·다중·타입/용량 검증·진행률 |
| StatusBadge | 상태별 색(검토중/통과/거부/승인/발급…) |
| Stepper | 28상태 매핑·현재/완료/차단 표시 |
| RoleSidebar | `ROLE_MENUS` 필터·활성·메뉴 수 |
| Chrome | 🌐 언어 셀렉트(KO/EN/ID) + 🌙/☀️ 테마 토글(로컬 저장) |
| Toast/Notify | 성공/오류·운영자 메시지·공지 |

---

## 9. 워크플로우 UX / Flow UX
- **경로판정:** AI 제안 카드(근거) → 권한자(컨설) 확정. 자기선언 부적격 사유 명시.
- **모의심사(mockAudit):** 내부 심사(클라이언트 미노출). 통과/거부(사유 필수).
- **시정조치(CAR):** 지적→시정요청→제출→재심 루프. 이력 누적.
- **반려/재신청 이력:** 숨김/펼침 토글로 과거 버전 열람.

---

## 10. 비기능 요구 / NFR
| 항목 | 기준 |
|---|---|
| i18n | **KO/EN/ID** 전 문자열 리소스화, 셀렉트박스 즉시 전환, 로케일 저장 |
| 테마 | 다크/화이트, 시스템 연동, 로컬 저장, 대비 WCAG AA |
| 접근성 | 키보드 내비·포커스·aria·명도 대비 |
| 보안 | RBAC 이중 강제·JWT(서명 검증)·CSRF·SIHALAL 게이트·PII 최소·전송 암호화 |
| 감사 | 해시체인 무결성·전 전이 기록·감사 검증 API |
| 성능 | 목록 페이지네이션·서버 필터·대용량 첨부 스트리밍 |
| 신뢰성 | AI 장애 시 수동 폴백(제안 없이 진행 가능) |
| 배포 | 복제 배포(인니 전국/글로벌)·환경 분리 |

---

## 11. 검증 · 수용기준 / Verification
- 백엔드 403 매트릭스(액션×역할) 재작성 후 **회귀 통과**(v2 `tests/e2e_rbac.py`·`fe_rbac.py` 확장).
- 상태머신 전이 e2e(`e2e_s1..s12`)를 6역할 기준으로 재매핑.
- 각 화면: empty/loading/error 스냅샷 + 권한별 nav/액션 노출 검증.

## 12. v2 → v3 마이그레이션 / Migration
1. RBAC 재매핑(`ROLE_MENUS`·`ROLE_ACTIONS`·`require_roles`) — 6역할 + 최고운영자 신설 + 권한 이관(lph/cert/fatwa).
2. 신규 화면: **mockAudit** · 리스트 컴포넌트(C0) 및 화면별 목록.
3. 공통 크롬(다국어·다크모드) 도입.
4. DB 승격(PostgreSQL)·저장소 분리.
5. 데이터: `data/`는 v2 심볼릭 링크(학습/온톨로지 공유).

## 13. 리스크 · 오픈 / Risks & Open
- 최고 업무운영자 권한 경계(승인 vs 운영) 세부.
- penyelia/pendamping 흡수에 따른 자기선언 UX 단순화 vs 정확성.
- 리스트 컴포넌트(C0) 지연 시 다수 화면 병목 → **최우선**.

---
*정본 참조: `../glhac-ai`(v2 소스) · `~/Downloads/GLHAC_AI_v2_*`(설계서·작업내역서·와이어프레임) · 본 문서(v3 프로덕션 스펙).*
