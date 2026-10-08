import os, sys, time, httpx
from _target import base
B = base(); P, F = [], []
def ok(name, cond, extra=""):
    (P if cond else F).append(name); print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + str(extra)[:300]) if extra else ""))
def login(u, p): return httpx.post(f"{B}/auth/login", json={"username": u, "password": p}).json().get("token")
def H(t): return {"Authorization": f"Bearer {t}"}
def tr(t, cid, to):
    cur = httpx.get(f"{B}/cases/{cid}", headers=H(t), timeout=60)
    try:
        if cur.status_code == 200 and cur.json().get("status") == to:
            return cur
    except Exception:
        pass
    return httpx.post(f"{B}/cases/{cid}/transition", headers=H(t), json={"to_state": to}, timeout=60)

def J(r):
    try: return r.json()
    except Exception: return {"raw": r.text[:300]}
def step(name, r, want=200):
    j = J(r); ok(name, r.status_code == want, "" if r.status_code == want else f"HTTP {r.status_code} {j}"); return j
def maybe_approve(name, j, checker):
    aid = j.get("approval_id") if isinstance(j, dict) else None
    if not aid: return j
    r = httpx.post(f"{B}/approvals/{aid}/approve", headers=H(checker), json={}, timeout=60)
    res = step(name + " · 서브 승인(auditor2)", r)
    return (res.get("result") or res) if isinstance(res, dict) else res
def listify(j): return j.get("items", []) if isinstance(j, dict) else (j or [])
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
REVIEW_READY = {"sections": {"documents": {"ok": True, "note": ""}, "materials": {"ok": True, "note": ""}, "process": {"ok": True, "note": ""}}, "verdict": "ready", "note": "준비 완료"}
FORMAL = {"process_summary": "원료 입고→혼합→충전→포장", "halal_management": "할랄 감독자 상주·월 1회 내부심사", "non_halal_handling": False, "non_halal_desc": "", "preferred_audit_period": "2026-11", "sales_channels": "국내 온라인·인니 수출", "pledge": True, "signer_name": "홍길동", "signature": PNG}

print("=== 1. 로그인 ===")
at = login("applicant1", "pw")
ct = login("consultant1", "pw")
a1 = login("auditor1", "pw")
a2 = login("auditor2", "pw")
ot = login("operator1", "pw")
ad = login("admin", "admin")
ft = login("fatwa1", "pw")
ok("로그인 7종", all([at, ct, a1, a2, ot, ad, ft]), {"at": bool(at), "ct": bool(ct), "a1": bool(a1), "a2": bool(a2), "ot": bool(ot), "ad": bool(ad), "ft": bool(ft)})

print("=== 2. 기업 신청 ===")
ts = int(time.time())
r = httpx.post(f"{B}/cases", headers=H(at), json={"company_name": f"CycleCo {ts}", "is_msme": False, "sector": "food"}, timeout=60)
j = step("케이스 생성", r)
cid = j.get("case_id") if isinstance(j, dict) else None
ok("case_id 존재", bool(cid), j)
nib = ("9" + str(ts) + "00")[:13]
step("프로필 PATCH", httpx.patch(f"{B}/cases/{cid}/profile", headers=H(at), json={"nib": nib}, timeout=60))
step("제품 등록", httpx.post(f"{B}/cases/{cid}/products", headers=H(at), json={"name": "Sambal Cycle"}, timeout=60))
step("원료 citric acid", httpx.post(f"{B}/cases/{cid}/materials", headers=H(at), json={"name": "citric acid"}, timeout=60))
step("원료 gelatin", httpx.post(f"{B}/cases/{cid}/materials", headers=H(at), json={"name": "gelatin"}, timeout=60))

print("=== 3. 신청 전이 ===")
step("onboarding→application_draft", tr(at, cid, "application_draft"))
step("draft→pre_assessment_ready", tr(at, cid, "ai_pre_assessment_ready"))
step("pre_assessment_ready→running", tr(at, cid, "ai_pre_assessment_running"))
step("running→pathway_determination", tr(at, cid, "pathway_determination"))

print("=== 4. 경로 판정 ===")
step("pathway assess", httpx.post(f"{B}/cases/{cid}/pathway/assess", headers=H(ad), json={}, timeout=60))
step("pathway confirm", httpx.post(f"{B}/cases/{cid}/pathway/confirm", headers=H(ad), json={"pathway": "reguler"}, timeout=60))
ei = step("sihalal identity link", httpx.post(f"{B}/cases/{cid}/sihalal/identity/link", headers=H(ad), json={"external_email": f"cycle{ts}@x.com"}, timeout=60))
step("sihalal identity verify", httpx.post(f"{B}/sihalal/identity/{ei.get('external_identity_id')}/verify", headers=H(ad), json={"expected_identifier": f"cycle{ts}@x.com"}, timeout=60))
step("→supplementation_submitted", tr(at, cid, "supplementation_submitted"))
step("→consultant_review", tr(ad, cid, "consultant_review"))

print("=== 5. 오디터 배정 ===")
r = httpx.get(f"{B}/admin/users?q=auditor", headers=H(ad), timeout=60)
j = step("admin/users 검색", r)
users = {u.get("username"): u.get("user_id") for u in listify(j)}
main_name = None
for cand in ("auditor1", "auditor2", "auditor3"):
    uid = users.get(cand)
    if not uid: continue
    r = httpx.post(f"{B}/ops/cases/{cid}/assign-auditor", headers=H(ot), json={"auditor_id": uid}, timeout=60)
    if r.status_code == 200:
        main_name = cand; break
    print("  ⏭ assign-auditor", cand, r.status_code, str(J(r))[:120])
ok("assign-auditor(메인)", bool(main_name), main_name)
if main_name: a1 = login(main_name, "pw")
r = httpx.get(f"{B}/cases/{cid}/auditors", headers=H(ot), timeout=60)
j = step("auditors 조회", r)
co = (j.get("co") or []) if isinstance(j, dict) else []
ok("서브 자동 배정", len(co) >= 1, j)
if len(co) == 0:
    step("auditor2 수동 추가", httpx.post(f"{B}/cases/{cid}/auditors", headers=H(ad), json={"username": "auditor2"}, timeout=60))
co_name = co[0]["name"] if co else "auditor2"
a2 = login(co_name, "pw")
ok("서브 승인자", bool(a2), co_name)

print("=== 6. 사전심사 판정(2인 확인) ===")
r = httpx.post(f"{B}/cases/{cid}/preassess/review", headers=H(a1), json=REVIEW_READY, timeout=60)
j = step("preassess review", r)
j = maybe_approve("preassess review", j, a2)
r = httpx.get(f"{B}/cases/{cid}/preassess/review", headers=H(a1), timeout=60)
j = step("preassess 조회", r)
ok("사전심사 ready", "ready" in __import__("json").dumps(j, ensure_ascii=False), j)

print("=== 7. AI 2차 분석 ===")
r = httpx.post(f"{B}/cases/{cid}/ai-second-analysis", headers=H(a1), json={}, timeout=300)
step("ai-second-analysis", r)

print("=== 8. 인증 가능 판정(2인 확인) ===")
r = httpx.post(f"{B}/cases/{cid}/eligibility/verdict", headers=H(a1), json={"verdict": "eligible", "reason": "", "note": ""}, timeout=60)
j = step("eligibility verdict", r)
j = maybe_approve("eligibility verdict", j, a2)
r = httpx.get(f"{B}/cases/{cid}/eligibility", headers=H(a1), timeout=60)
j = step("eligibility 조회", r)
ok("판정 eligible", "eligible" in __import__("json").dumps(j, ensure_ascii=False), j)

print("=== 9. 정식 신청·접수 ===")
step("formal-application", httpx.post(f"{B}/cases/{cid}/formal-application", headers=H(at), json=FORMAL, timeout=60))
r = httpx.get(f"{B}/ops/formal-applications", headers=H(ot), timeout=60)
j = step("접수 대기 목록", r)
ok("접수 대기 목록에 있음", cid in __import__("json").dumps(j, ensure_ascii=False), j)
step("formal-application/accept", httpx.post(f"{B}/cases/{cid}/formal-application/accept", headers=H(ot), json={}, timeout=60))

print("=== 10. 견적·계약 ===")
step("quote", httpx.post(f"{B}/cases/{cid}/quote", headers=H(ot), json={}, timeout=60))
step("contract/request", httpx.post(f"{B}/cases/{cid}/contract/request", headers=H(at), json={}, timeout=60))
r = httpx.post(f"{B}/cases/{cid}/contract/approve", headers=H(ot), json={}, timeout=60)
j = step("contract/approve", r)
contract_id = j.get("contract_id") if isinstance(j, dict) else None
ok("contract_id", bool(contract_id), j)
step("contract/receive", httpx.post(f"{B}/cases/{cid}/contract/receive", headers=H(at), json={}, timeout=60))
step("contract/request-signature", httpx.post(f"{B}/cases/{cid}/contract/request-signature", headers=H(a1), json={}, timeout=60))
step("contract sign A", httpx.post(f"{B}/contracts/{contract_id}/sign", headers=H(at), params={"party": "A", "name": "홍길동"}, timeout=60))
step("contract sign B", httpx.post(f"{B}/contracts/{contract_id}/sign", headers=H(ot), params={"party": "B", "name": "GL HAC"}, timeout=60))
r = httpx.post(f"{B}/cases/{cid}/contract/confirm", headers=H(ot), json={}, timeout=60)
j = step("contract/confirm", r)
ok("계약 confirmed", (j.get("status") if isinstance(j, dict) else None) == "confirmed", j)

print("=== 11. 회차 입금 ===")
r = httpx.get(f"{B}/cases/{cid}/invoices", headers=H(ot), timeout=60)
j = step("invoices 조회", r)
invs = listify(j)
ok("회차 청구서 생성됨", len(invs) >= 1, len(invs))
for inv in invs:
    iid = inv.get("invoice_id")
    step(f"invoice paid {iid}", httpx.patch(f"{B}/invoices/{iid}/status", headers=H(ot), json={"status": "paid", "reason": "입금 확인"}, timeout=60))

print("=== 12. 서류 사전심사 전이 ===")
step("→document_pre_audit_requested", tr(ad, cid, "document_pre_audit_requested"))
step("→document_pre_audit_in_review", tr(ad, cid, "document_pre_audit_in_review"))
step("→document_pre_audit_approved", tr(ad, cid, "document_pre_audit_approved"))

print("=== 13. 준비 서류 ===")
r = httpx.get(f"{B}/cases/{cid}/prep", headers=H(ad), timeout=60)
j = step("prep 조회", r)
keys = [i["item_key"] for i in listify(j)]
ok("준비 서류 목록", len(keys) >= 1, len(keys))
fails = 0
for k in keys:
    r = httpx.patch(f"{B}/cases/{cid}/prep/{k}", headers=H(a1), json={"status": "confirmed"}, timeout=60)
    if r.status_code != 200: fails += 1
ok("준비 서류 확인 전부 200", fails == 0, fails)
step("교육 서류 배포(운영)", httpx.post(f"{B}/cases/{cid}/education-docs/issue", headers=H(ot), json={}, timeout=60))
step("교육 서류 동의(기업)", httpx.post(f"{B}/cases/{cid}/education-docs/consent", headers=H(at), json={"agree": True, "name": "홍길동", "image": PNG}, timeout=60))
r = httpx.post(f"{B}/cases/{cid}/prep/complete", headers=H(a1), json={}, timeout=60)
j = step("prep/complete", r)
j = maybe_approve("prep/complete", j, a2)

print("=== 14. 모의심사 ===")
r = httpx.post(f"{B}/cases/{cid}/mock-audit/decision", headers=H(a1), json={"result": "pass"}, timeout=60)
j = step("mock-audit/decision", r)
j = maybe_approve("mock-audit/decision", j, a2)

print("=== 15. LPH·현장 전이 ===")
step("lph-assignment", httpx.post(f"{B}/cases/{cid}/lph-assignment", headers=H(ad), json={"lph_name": "LPH Sucofindo"}, timeout=60))
step("→lph_assignment", tr(ot, cid, "lph_assignment"))
step("→onsite_audit_scheduled", tr(a1, cid, "onsite_audit_scheduled"))
step("→onsite_audit_in_progress", tr(a1, cid, "onsite_audit_in_progress"))
step("→audit_closed", tr(a1, cid, "audit_closed"))
step("→hpas_evaluation_ready", tr(a1, cid, "hpas_evaluation_ready"))
step("→final_package_preparation", tr(ot, cid, "final_package_preparation"))
step("→fatwa_review", tr(ot, cid, "fatwa_review"))

print("=== 16. 심사보고서·정본 ===")
r = httpx.post(f"{B}/cases/{cid}/audit-report", headers=H(a1), json={}, timeout=60)
j = step("audit-report", r)
gid = j.get("gen_doc_id") if isinstance(j, dict) else None
ok("gen_doc_id", bool(gid), j)
step("gen-docs approve", httpx.post(f"{B}/gen-docs/{gid}/approve", headers=H(ad), json={}, timeout=60))
for el in ("commitment", "materials", "process", "product", "monitoring"):
    step(f"sjph {el}", httpx.patch(f"{B}/cases/{cid}/sjph", headers=H(ad), json={"element": el, "status": "ok"}, timeout=60))
step("penyelia", httpx.post(f"{B}/orgs/org_demo/penyelia", headers=H(ad), json={"name": "Pen Halal Cycle"}, timeout=60))
step("audit-report/sign", httpx.post(f"{B}/cases/{cid}/audit-report/sign", headers=H(a1), json={"name": "Auditor Kim"}, timeout=60))
step("audit-report/client-sign", httpx.post(f"{B}/cases/{cid}/audit-report/client-sign", headers=H(at), json={"name": "홍길동", "image": PNG, "signature": PNG}, timeout=60))
r = httpx.post(f"{B}/cases/{cid}/audit-report/send-fatwa", headers=H(a1), json={}, timeout=60)
j = step("send-fatwa", r)
j = maybe_approve("send-fatwa", j, a2)
ok("D-17 정본 발급", ((j.get("canonical") or {}).get("doc") if isinstance(j, dict) else None) == "D-17", j)
ok("D-18 취합본", ((j.get("final_package") or {}).get("doc") if isinstance(j, dict) else None) == "D-18", j)

print("=== 17. 샤리아 대리(오디터) ===")
step("fatwa/vote", httpx.post(f"{B}/cases/{cid}/fatwa/vote", headers=H(a1), json={"member": "auditor", "vote": "approve", "note": "ok"}, timeout=60))
step("fatwa/sign", httpx.post(f"{B}/cases/{cid}/fatwa/sign", headers=H(a1), json={"member": "auditor", "image": PNG}, timeout=60))
step("fatwa/confirm", httpx.post(f"{B}/cases/{cid}/fatwa/confirm", headers=H(a1), json={}, timeout=60))
step("운영자 최종승인(final-approve)", httpx.post(f"{B}/cases/{cid}/fatwa/final-approve", headers=H(ot), json={}, timeout=60))

print("=== 18. 인증서 ===")
r = httpx.post(f"{B}/cases/{cid}/certificate/issue", headers=H(ot), json={}, timeout=60)
j = step("certificate/issue", r)
if not (isinstance(j, dict) and j.get("certificate_no")) and isinstance(j, dict) and j.get("approval_id"):
    aid = j.get("approval_id")
    r = httpx.post(f"{B}/approvals/{aid}/approve", headers=H(ft), json={}, timeout=60)
    if r.status_code != 200:
        r = httpx.post(f"{B}/approvals/{aid}/approve", headers=H(a2), json={}, timeout=60)
    jj = step("certificate approve", r)
    j = (jj.get("result") if isinstance(jj, dict) else None) or jj
ok("인증서 번호", bool(isinstance(j, dict) and j.get("certificate_no")), j)
r = httpx.get(f"{B}/cases/{cid}", headers=H(ad), timeout=60)
j = step("case 조회", r)
ok("상태 certificate_issued", (j.get("status") if isinstance(j, dict) else None) == "certificate_issued", j.get("status") if isinstance(j, dict) else j)

print("=== 19. 산출물 ===")
step("canonical", httpx.get(f"{B}/cases/{cid}/canonical", headers=H(ad), timeout=60))
r = httpx.get(f"{B}/cases/{cid}/gen-docs", headers=H(ad), timeout=60)
j = step("gen-docs", r)
docs = listify(j)
print("  생성 문서:", sorted({d.get("doc_type") or d.get("doc") or d.get("kind") or "?" for d in docs if isinstance(d, dict)}))

print("=== 20. 요약 ===")
print(f"PASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패 목록:")
    for x in F: print("  - " + x)
out = os.environ.get("GLHAC_CYCLE_OUT")
if out:
    open(out, "w").write(cid or "")
sys.exit(1 if F else 0)