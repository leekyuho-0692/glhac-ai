# GL-HAC AI v3 — API 계약서 / API Contract
### 엔드포인트 × 권한 × 화면 · KO/EN

> **소스:** `../glhac-ai/app/main.py`(~80 endpoints) · `ROLE_ACTIONS`(권한) · v3 6역할 재매핑 반영.
> **인증:** `Authorization: Bearer <token>` (JWT HMAC, 프로덕션은 서명검증 승격). 조직 격리: 비admin은 토큰 org로 스코프.
> **권한 표기:** 클(클라)·컨(컨설=영업)·오(오디터)·샤(샤리아)·운(최고운영자)·IT(admin). `any`=로그인 전 역할. **admin(IT) 전부 통과.**
> **에러 규약:** 401(미인증)·403`{code,need,have}`(권한)·404·409(상태전이 위반/가드)·422(검증). 전이 위반 시 `{code, guards:[...]}`.

---

## 0. 공통 / Common
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET | `/health` · `/ai/health` | any | 헬스체크(AI 포함) |
| GET | `/meta/enums` · `/meta/ingredient-suggest` | any | enum·재료 제안 |

## 1. 인증 / Auth  → [auth]
| Method | Path | 권한 | 요청 → 응답 |
|---|---|---|---|
| POST | `/auth/login` | 공개 | `{username,password}` → `{token, user{uid,username,role,org_id}}` |
| POST | `/auth/register` | 공개 | `{username,password,org?}` → `{token,user}` |
| GET | `/auth/me` | 로그인 | → `{uid,username,role,org_id}` |

## 2. 케이스 / Cases  → [caseList][workflow][application]
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET | `/cases` | 로그인(역할 스코프) | 마스터 목록(필터·페이지) |
| POST | `/cases` | 클·컨 | 케이스 생성 |
| GET | `/cases/{id}` | 로그인 | 케이스 상세 |
| PATCH | `/cases/{id}/profile` | 클·컨 | 프로필 저장/임시저장 |
| GET | `/cases/{id}/timeline` | 로그인 | 상태 타임라인(워크스페이스) |
| GET | `/cases/{id}/workflow` · `/readiness` | 로그인 | 워크플로우/준비도 |
| POST | `/cases/{id}/transition` | 권한자(가드) | 상태 전이(가드 검증) |
| GET | `/cases/{id}/audit-verify` | 로그인 | 해시체인 무결성 검증 |
| POST | `/cases/{id}/report` | any | 종합 보고서 |
| GET | `/cases/{id}/export.json` | 로그인 | 케이스 export |

## 3. 제품·원재료 / Products & Materials  → [materials][application]
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET/POST | `/cases/{id}/products` | 조회 로그인 / 생성 클·컨 | 제품 |
| POST | `/cases/{id}/products/{pid}/photo` | 클·컨 | 제품 사진 |
| GET/POST | `/cases/{id}/materials` | 조회 로그인 / 생성 클(+할랄감독) | 원재료 |
| DELETE | `/materials/{mid}` | 클(+할랄감독) | 원재료 삭제 |
| POST | `/cases/{id}/materials/{mid}/evidence` | 클(+할랄감독) | 증빙 첨부 |
| GET | `/cases/{id}/materials/{mid}/evidence` | 로그인 | 증빙 목록 |
| GET | `/cases/{id}/materials/export.xlsx` | 로그인 | Excel |
| POST | `/materials/{mid}/screen` · `/ai/screen-text` | 로그인 | AI 스크리닝 |
| POST | `/cases/{id}/materials/from-label[-b64]` | 컨 | 라벨 OCR 생성 |
| POST | `/cases/{id}/parse-file` · `intake-zip[-stream]` | 클·컨 | 파일 파싱/ZIP |
| POST | `/ai/ocr` · `/ai/label-judgment` | 로그인 | OCR·라벨 판정 |

## 4. SIHALAL·경로·동반자 / Identity·Pathway·Pendamping  → [application]
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| POST | `/cases/{id}/sihalal/identity/link` | 클·컨 | SIHALAL 연동 |
| POST | `/sihalal/identity/{eid}/verify` | 컨 | **동일성 검증(게이트)** |
| POST | `/cases/{id}/pathway/assess` | any | 경로 AI 제안 |
| POST | `/cases/{id}/pathway/confirm` | 컨 | 경로 확정 |
| GET/POST/PATCH | `/orgs/{org}/penyelia[...]` | 클(+할랄감독)·컨 | 할랄감독 등록 |
| POST | `/cases/{id}/pendamping/assign` | 컨 | 동반자 배정 |
| POST | `/cases/{id}/pendamping/verify` | 동반자(클) | 동반자 검증 |

## 5. 문서·SJPH / Documents & SJPH  → [documents][sjphIntegration]
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET | `/cases/{id}/documents` | 로그인 | 문서 목록 |
| GET | `/documents/{id}/file` | 로그인 | 문서 파일 |
| PATCH | `/documents/{id}/review` | 컨 | 문서 검수 |
| GET | `/cases/{id}/doc-checklist` | any | 필수서류 체크리스트 |
| GET/PATCH | `/cases/{id}/sjph` | 조회 / 편집 클·컨·할랄감독 | SJPH 5요소 |
| POST | `/cases/{id}/sjph/manual` | any | Manual SJPH 생성 |
| GET/POST | `/cases/{id}/sjph-evidence` | 클·컨·할랄감독 | SJPH 증빙 |
| GET | `/cases/{id}/hpas-auto` | 로그인 | HPAS 자동 평가 |

## 6. 심사 / Audit  → [audit][mockAudit]  ⚠ v3 권한 이관
| Method | Path | 권한(v3) | 설명 |
|---|---|---|---|
| GET/POST | `/cases/{id}/findings` | 조회 로그인 / 생성 **오·컨** | 지적 |
| PATCH/DELETE | `/findings/{id}` | **오·컨** | 지적 수정/삭제 |
| GET/POST/DELETE | `/cases/{id}/auditor-pool[...]` | 오·**운** | 심사팀 |
| GET/POST | `/cases/{id}/onsite-checklist` | 오·샤·운 | 현장/모의 체크리스트 |
| GET/POST | `/admin/lph-references` | IT | LPH 레퍼런스 |
| GET | `/cases/{id}/lph-assignment` | 로그인 | LPH 배정 조회 |
| POST | `/cases/{id}/lph-assignment` | **샤·운** ⚠(v2 컨→이관) | LPH 배정 |

## 7. 파트와·인증서 / Fatwa & Certificate  → [fatwa][certificate]  ⚠ v3 권한 이관
| Method | Path | 권한(v3) | 설명 |
|---|---|---|---|
| GET | `/cases/{id}/fatwa` | 로그인 | 파트와 조회 |
| PATCH | `/cases/{id}/fatwa` | **샤·운** ⚠(v2 컨→이관) | 파트와 결정 |
| POST | `/cases/{id}/fatwa/document` | 샤·운 | 결정문 생성 |
| POST | `/cases/{id}/certificate/issue` | **샤·운** ⚠(v2 컨→이관) | 인증서 발급 |
| GET | `/cases/{id}/certificate` | 로그인 | 인증서 조회 |
| POST | `/cases/{id}/certificate/change-impact` | any | 변경영향 |
| POST | `/cases/{id}/renew` · `/certificate/unlock` | 운·IT | 갱신·잠금해제 |

## 8. 청구·논의 / Invoice & Discussion  → [invoice]
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| GET/POST | `/cases/{id}/invoices` | 조회 로그인 / 생성 **컨** | 청구서 |
| PATCH | `/invoices/{id}/pay` | 컨·클 | 결제 표시 |
| GET/POST | `/cases/{id}/discussions` | 로그인 | 논의/코멘트 |

## 9. AI 어시스턴트 / Assistant  → [assistant]
| Method | Path | 권한 | 설명 |
|---|---|---|---|
| POST | `/cases/{id}/ask` | any | 케이스 Q&A |
| POST | `/ai/explain` | any | AI 설명 |

## 10. 관리 / Admin  → [system]  · IT 전용
| Method | Path | 설명 |
|---|---|---|
| GET/POST/PATCH/DELETE | `/admin/users[...]` | 사용자 관리 |
| GET/POST | `/admin/orgs` | 조직 관리 |
| GET | `/admin/cases` | 전 케이스 |
| GET/POST | `/admin/ontology/stats`·`/reseed` | 온톨로지 |
| GET | `/admin/kma1360-exempt` · `/admin/audit` | 면제·전역 감사 |
| POST | `/admin/seed-reset` | seed 초기화(위험) |

---

## 11. v2 → v3 권한 이관 요약 / Permission migration (백엔드 `require_roles` 수정 대상)
| 액션 | v2 | **v3** |
|---|---|---|
| `lph_assign` (`POST lph-assignment`) | consultant | **샤리아·최고운영자** |
| `fatwa_decide` (`PATCH /fatwa`) | consultant·fatwa_liaison | **샤리아·최고운영자** |
| `cert_issue` (`POST certificate/issue`) | consultant | **샤리아·최고운영자** |
| `finding_edit` | consultant·auditor | **오디터·컨설(유지)** |
| (신규 역할) | — | **`operator`(최고운영자)** DEFAULT_USERS·게이팅 추가 |

> 프론트 `ROLE_ACTIONS`/`ROLE_MENUS`(index.html) + 백엔드 `require_roles`(main.py) + `DEFAULT_USERS`(auth.py) 동시 반영. 회귀: `tests/e2e_rbac.py`·`fe_rbac.py`.
