"""S13 검증 — Documents 검수 요약(S13-1) · Fatwa 결정문 생성(S13-2) · Certificate 만료경보(S13-3)."""
import os
import sys
import sqlite3
import httpx
from datetime import date, timedelta

DB_PATH = "glhac.db"
B = os.environ.get("GLHAC_E2E_BASE", "http://127.0.0.1:8800")
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:120]) if x else ""))


def force_status(case_id, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE case_application SET status=? WHERE case_id=?", (status, case_id))
    conn.commit()
    conn.close()


HC = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

# 테스트 케이스 생성
c1 = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "S13 Test Co", "org_id": "org_demo"}).json()
ok("테스트 케이스 생성", "case_id" in c1, c1)
CID = c1.get("case_id", "")

# ── S13-1: Documents 검수 요약 데이터 ─────────────────────────
print("\n=== S13-1 Documents 검수 현황 ===")

# ZIP 없이 개별 문서 업로드 시뮬: 직접 document 레코드 생성 (DB)
import base64, uuid
dummy_pdf = base64.b64encode(b"%PDF-1.4 dummy content for S13").decode()

# 문서 3개 업로드 후 검수 상태 혼재 테스트
docs_before = httpx.get(f"{B}/cases/{CID}/documents", headers=HC).json()
ok("문서 목록 배열", isinstance(docs_before, list), type(docs_before).__name__)

# DB에 직접 문서 레코드 생성 (intake 없이 검수 상태 테스트)
conn = sqlite3.connect(DB_PATH)
doc_ids = []
for doc_type, rev_status in [("halal_cert", "approved"), ("nib", "pending"), ("sjph_doc", "rejected")]:
    did = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO document_asset (document_id, case_id, doc_type, filename, "
        "review_status, confidence) VALUES (?,?,?,?,?,?)",
        (did, CID, doc_type, f"s13_test_{doc_type}.pdf", rev_status, 0.9)
    )
    doc_ids.append(did)
conn.commit()
conn.close()

# 문서 목록 재조회
docs = httpx.get(f"{B}/cases/{CID}/documents", headers=HC).json()
ok("문서 목록 ≥3건", len(docs) >= 3, len(docs))
ok("review_status 필드 존재", all("review_status" in d for d in docs), [d.get("review_status") for d in docs[:3]])

# 검수 상태별 집계 (S13-1 요약 카드 데이터)
appr = [d for d in docs if d.get("review_status") == "approved"]
rej = [d for d in docs if d.get("review_status") == "rejected"]
pend = [d for d in docs if not d.get("review_status") or d.get("review_status") == "pending"]
ok("approved 건수 ≥1", len(appr) >= 1, len(appr))
ok("rejected 건수 ≥1", len(rej) >= 1, len(rej))
ok("pending/null 건수 ≥1", len(pend) >= 1, len(pend))

# PATCH review — 상태 변경 검증 (approved/rejected/rework)
if doc_ids:
    r_appr = httpx.patch(f"{B}/documents/{doc_ids[1]}/review", headers=HC,
                         json={"review_status": "approved"})
    ok("문서 검수 → approved 200", r_appr.status_code == 200, r_appr.status_code)
    if r_appr.status_code == 200:
        ok("review_status=approved 반환", r_appr.json().get("review_status") == "approved",
           r_appr.json().get("review_status"))

    r_rej = httpx.patch(f"{B}/documents/{doc_ids[2]}/review", headers=HC,
                        json={"review_status": "rejected"})
    ok("문서 검수 → rejected 200", r_rej.status_code == 200, r_rej.status_code)

    r_rw = httpx.patch(f"{B}/documents/{doc_ids[0]}/review", headers=HC,
                       json={"review_status": "rework"})
    ok("문서 검수 → rework 200", r_rw.status_code == 200, r_rw.status_code)

# 재분류 검증
if doc_ids:
    r_recl = httpx.patch(f"{B}/documents/{doc_ids[0]}/reclassify", headers=HC,
                         json={"doc_type": "nib"})
    ok("문서 재분류 → 200", r_recl.status_code == 200, r_recl.status_code)

# doc-checklist 체크
ck = httpx.get(f"{B}/cases/{CID}/doc-checklist", headers=HC).json()
ok("doc-checklist 응답", "checklist" in ck and "missing" in ck, list(ck.keys()))
ok("checklist 항목 ≥1", len(ck.get("checklist", [])) >= 1, len(ck.get("checklist", [])))

# ── S13-2: Fatwa 결정문 생성 + (다운로드용 응답 필드) ───────────
print("\n=== S13-2 Fatwa 결정문 ===")

# fatwa 결정 저장
r_fatwa = httpx.patch(f"{B}/cases/{CID}/fatwa", headers=HA,
                      json={"decision": "approved",
                            "committee_head": "Ust. Ahmad S13",
                            "committee_secretary": "Ust. Budi S13",
                            "committee_members": ["Ust. C", "Ust. D"],
                            "committee_note": "S13 테스트 결정"})
ok("fatwa 결정 저장 → 200", r_fatwa.status_code == 200, r_fatwa.status_code)

# GET fatwa 상태 확인
fw = httpx.get(f"{B}/cases/{CID}/fatwa", headers=HA).json()
ok("fatwa decision 반환", "decision" in fw, list(fw.keys()))
ok("fatwa decision=approved", fw.get("decision") == "approved", fw.get("decision"))
ok("committee_head 저장됨", fw.get("committee_head") == "Ust. Ahmad S13", fw.get("committee_head"))
ok("committee_members 저장됨", isinstance(fw.get("committee_members"), list), fw.get("committee_members"))

# POST /fatwa/document — 결정문 생성 (fatwa_liaison/operator/admin 전용 — consultant 403)
r_doc403 = httpx.post(f"{B}/cases/{CID}/fatwa/document", headers=HC)
ok("결정문 consultant → 403", r_doc403.status_code == 403, r_doc403.status_code)
r_doc = httpx.post(f"{B}/cases/{CID}/fatwa/document", headers=HA)
ok("결정문 생성 → 200", r_doc.status_code == 200, r_doc.status_code)
if r_doc.status_code == 200:
    doc_body = r_doc.json()
    ok("document 필드 존재", "document" in doc_body, list(doc_body.keys()))
    ok("결정문 내용 있음 (>20자)", len(doc_body.get("document", "")) > 20, len(doc_body.get("document", "")))
    # 다운로드용 텍스트 확인 (S13-2: FE에서 Blob으로 변환)
    doc_text = doc_body.get("document", "")
    ok("결정문에 fatwa 키워드", any(kw in doc_text for kw in ["fatwa", "halal", "Fatwa", "FATWA", "S13", "Ahmad"]),
       doc_text[:80])

# 조건부 결정 → conditional 저장
r_cond = httpx.patch(f"{B}/cases/{CID}/fatwa", headers=HA,
                     json={"decision": "conditional",
                           "committee_note": "조건: 원재료 E471 증빙 제출 후 재심의"})
ok("조건부 결정 저장 → 200", r_cond.status_code == 200, r_cond.status_code)
if r_cond.status_code == 200:
    fw2 = httpx.get(f"{B}/cases/{CID}/fatwa", headers=HA).json()
    ok("conditional 결정 저장됨", fw2.get("decision") == "conditional", fw2.get("decision"))
    ok("committee_note(조건) 저장됨", "E471" in (fw2.get("committee_note") or ""),
       fw2.get("committee_note"))

# ── S13-3: Certificate 만료임박 경보 데이터 ────────────────────
print("\n=== S13-3 Certificate 만료임박 경보 ===")

# 인증서 발급: fatwa approve → scope freeze → issue
# 먼저 fatwa를 approved로 되돌림
httpx.patch(f"{B}/cases/{CID}/fatwa", headers=HA,
            json={"decision": "approved"})

# 제품 추가 (scope freeze 위해)
prod = httpx.post(f"{B}/cases/{CID}/products", headers=HC,
                  json={"name": "S13 Product"}).json()
ok("S13 제품 추가", "product_id" in prod, prod)
PID = prod.get("product_id", "")

# fatwa scope 설정 후 인증서 발급 시도
if PID:
    httpx.patch(f"{B}/cases/{CID}/fatwa", headers=HA,
                json={"decision": "approved", "product_scope": [PID]})

# 2단계 파트와: PATCH(approved)=가승인(provisional) → 최종승인(operator/admin) 필요
r_fa = httpx.post(f"{B}/cases/{CID}/fatwa/final-approve", headers=HA)
ok("파트와 최종승인 → 200", r_fa.status_code == 200, r_fa.json() if r_fa.status_code != 200 else 200)

# 발급 가드: rejected/rework 문서가 남아있으면 409 DOCUMENTS_NOT_APPROVED
r_blocked = httpx.post(f"{B}/cases/{CID}/certificate/issue", headers=HA)
ok("미승인 문서 잔존 → 발급 409", r_blocked.status_code == 409 and
   "DOCUMENTS_NOT_APPROVED" in str(r_blocked.json()), r_blocked.status_code)

# 잔여 rework/rejected 문서 승인 처리 후 발급
for did in (doc_ids[0], doc_ids[2]):
    httpx.patch(f"{B}/documents/{did}/review", headers=HC, json={"review_status": "approved"})
r_issue = httpx.post(f"{B}/cases/{CID}/certificate/issue", headers=HA)
ok("인증서 발급 → 200", r_issue.status_code == 200, r_issue.json() if r_issue.status_code != 200 else
   r_issue.json().get("certificate_no"))

# 직접 DB에 cert 레코드 삽입 (발급 가드 우회)
conn2 = sqlite3.connect(DB_PATH)
expiry_near = (date.today() + timedelta(days=45)).isoformat()  # 45일 후 → 경보 대상
expiry_far = (date.today() + timedelta(days=200)).isoformat()  # 200일 후 → 경보 없음
existing_cert = conn2.execute(
    "SELECT id FROM halal_certificate WHERE case_id=?", (CID,)).fetchone()
if existing_cert:
    conn2.execute(
        "UPDATE halal_certificate SET expiry_date=?, issue_date=?, status='active', "
        "certificate_no='S13-CERT-001' WHERE case_id=?",
        (expiry_near, date.today().isoformat(), CID))
else:
    conn2.execute(
        "INSERT INTO halal_certificate (id, case_id, certificate_no, issue_date, expiry_date, status, scope) "
        "VALUES (?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, CID, "S13-CERT-001",
         date.today().isoformat(), expiry_near, "active", "[]"))
conn2.commit()
conn2.close()

# GET /certificate — days_to_expiry 확인
ct = httpx.get(f"{B}/cases/{CID}/certificate", headers=HC).json()
ok("certificate 응답", "issued" in ct, list(ct.keys()))
ok("certificate issued=true", ct.get("issued") == True, ct.get("issued"))
ok("days_to_expiry 필드 존재", "days_to_expiry" in ct, list(ct.keys()))
ok("days_to_expiry=45 (±1)", 44 <= (ct.get("days_to_expiry") or 0) <= 46, ct.get("days_to_expiry"))
ok("days_to_expiry < 90 → 경보 대상", (ct.get("days_to_expiry") or 999) < 90, ct.get("days_to_expiry"))
ok("certificate_no 반환", ct.get("certificate_no") == "S13-CERT-001", ct.get("certificate_no"))
ok("expiry_date 반환", "expiry_date" in ct, ct.get("expiry_date"))

# 만료 이미 지난 경우 (days_to_expiry < 0) 시뮬
conn3 = sqlite3.connect(DB_PATH)
past_expiry = (date.today() - timedelta(days=10)).isoformat()
conn3.execute("UPDATE halal_certificate SET expiry_date=? WHERE case_id=?", (past_expiry, CID))
conn3.commit()
conn3.close()
ct2 = httpx.get(f"{B}/cases/{CID}/certificate", headers=HC).json()
ok("만료 경과 시 days_to_expiry < 0", (ct2.get("days_to_expiry") or 0) < 0, ct2.get("days_to_expiry"))

# 200일 후 만료 → 경보 없음 기준 확인
conn4 = sqlite3.connect(DB_PATH)
conn4.execute("UPDATE halal_certificate SET expiry_date=? WHERE case_id=?", (expiry_far, CID))
conn4.commit()
conn4.close()
ct3 = httpx.get(f"{B}/cases/{CID}/certificate", headers=HC).json()
ok("200일 후 만료 → days_to_expiry ≥90", (ct3.get("days_to_expiry") or 0) >= 90, ct3.get("days_to_expiry"))

# ── 결과 ──────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
