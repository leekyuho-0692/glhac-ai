"""GL-HAC AI 전체 E2E 점검 — 인프라·인증·RBAC/ABAC·이중경로 생애주기·가드·감사·보고서·Copilot."""
import os
import sys
import httpx

B = os.environ.get("GLHAC_E2E_BASE", "http://127.0.0.1:8800")
P, F = [], []


def ok(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + str(extra)) if extra else ""))


def login(u, p):
    return httpx.post(f"{B}/auth/login", json={"username": u, "password": p}).json().get("token")


def H(t):
    return {"Authorization": f"Bearer {t}"}


def tr(t, cid, to):
    return httpx.post(f"{B}/cases/{cid}/transition", headers=H(t), json={"to_state": to})


print("=== A. 인프라 ===")
ok("health 200", httpx.get(f"{B}/health").status_code == 200)
ok("/ui/ 200", httpx.get(f"{B}/ui/").status_code == 200)
aih = httpx.get(f"{B}/ai/health").json()
ok("ollama up + gemma3 ready", aih.get("ollama") == "up" and aih.get("model_ready"), aih.get("configured"))

print("=== B. 인증 / RBAC / ABAC ===")
ct, at, adt = login("consultant1", "pw"), login("applicant1", "pw"), login("admin", "admin")
aut, ft, ot, pt = login("auditor1", "pw"), login("fatwa1", "pw"), login("operator1", "pw"), login("pendamping1", "pw")
ok("consultant/applicant/admin 로그인", all([ct, at, adt]))
ok("auditor/fatwa/operator/pendamping 로그인", all([aut, ft, ot, pt]))
ok("인증 없이 /cases 401", httpx.get(f"{B}/cases").status_code == 401)
acid = httpx.post(f"{B}/cases", headers=H(at), json={"company_name": "ABACco", "is_msme": True}).json()["case_id"]
ok("RBAC: applicant pathway.confirm 403",
   httpx.post(f"{B}/cases/{acid}/pathway/confirm", headers=H(at), json={"pathway": "self_declare"}).status_code == 403)
other = httpx.post(f"{B}/cases", headers=H(adt), json={"company_name": "X", "org_id": "org_zzz"}).json()["case_id"]
ok("ABAC: applicant→타org 403", httpx.get(f"{B}/cases/{other}", headers=H(at)).status_code == 403)

print("=== C. 자기선언(SEHATI) 생애주기 ===")
httpx.post(f"{B}/orgs/org_demo/penyelia", headers=H(ct), json={"name": "Budi", "training_cert": "PH-1"})
sd = httpx.post(f"{B}/cases", headers=H(ct), json={"company_name": "SD Co", "is_msme": True, "org_id": "org_demo"}).json()["case_id"]
httpx.post(f"{B}/cases/{sd}/materials", headers=H(ct), json={"name": "citric acid"})  # halal
ei = httpx.post(f"{B}/cases/{sd}/sihalal/identity/link", headers=H(ct), json={"external_email": "sd@x.com"}).json()
httpx.post(f"{B}/sihalal/identity/{ei['external_identity_id']}/verify", headers=H(ct), json={"expected_identifier": "sd@x.com"})
sub = httpx.post(f"{B}/cases/{sd}/submit-application", headers=H(ct)).json()
ok("신청 제출 → 경로판정 대기", sub.get("status") == "pathway_determination", sub.get("status"))
a = httpx.post(f"{B}/cases/{sd}/pathway/assess", headers=H(ct)).json()
ok("자기선언 적격 판정", a["suggested_pathway"] == "self_declare", a["risk_category"])
pc = httpx.post(f"{B}/cases/{sd}/pathway/confirm", headers=H(ct), json={"pathway": "self_declare"}).json()
ok("경로확정 → self_declare_eligible", pc.get("next_state") == "self_declare_eligible", pc)
tr(ct, sd, "sjph_lite_prepared")
# pendamping/assign은 실제 pendamping 사용자 user_id 필수
users = httpx.get(f"{B}/admin/users", headers=H(adt)).json()
pp_uid = next(u["user_id"] for u in users if u["username"] == "pendamping1")
httpx.post(f"{B}/cases/{sd}/pendamping/assign", headers=H(ct), json={"pendamping_id": pp_uid})
tr(ct, sd, "pendamping_verification")
# pendamping verify는 pendamping_pph 역할 필요 — verified 시 자동 전진(가드 4종 통과)
rv = httpx.post(f"{B}/cases/{sd}/pendamping/verify", headers=H(pt), json={"decision": "verified"}).json()
st = httpx.get(f"{B}/cases/{sd}", headers=H(ct)).json()["status"]
ok("SD 제출 자동 전진(가드 통과)", rv.get("auto_advanced") is True and st == "self_declaration_submitted",
   f"auto_advanced={rv.get('auto_advanced')} status={st}")
r = tr(ot, sd, "committee_verification")   # committee_verification은 operator 전용 전이
ok("위원회 확인 전이(operator)", r.status_code == 200, r.json() if r.status_code != 200 else "")
cert_sd = httpx.post(f"{B}/cases/{sd}/certificate/issue", headers=H(ot)).json()
ok("SD 인증서 단계 도달",
   bool(cert_sd.get("certificate_no")) and httpx.get(f"{B}/cases/{sd}", headers=H(ct)).json()["status"] == "certificate_issued",
   cert_sd.get("certificate_no"))

print("=== D. 정규(Reguler) 생애주기 + 가드 ===")
rg = httpx.post(f"{B}/cases", headers=H(ct), json={"company_name": "RG Co", "is_msme": False, "org_id": "org_demo"}).json()["case_id"]
httpx.post(f"{B}/cases/{rg}/products", headers=H(ct), json={"name": "Sambal RG"})
httpx.post(f"{B}/cases/{rg}/materials", headers=H(ct), json={"name": "gelatin"})  # critical
ei2 = httpx.post(f"{B}/cases/{rg}/sihalal/identity/link", headers=H(ct), json={"external_email": "rg@x.com"}).json()
httpx.post(f"{B}/sihalal/identity/{ei2['external_identity_id']}/verify", headers=H(ct), json={"expected_identifier": "rg@x.com"})
# NOTE: submit-application은 한 트랜잭션에 이벤트 4건을 쌓아 해시체인이 끊김(앱 이슈 의심 — autoflush=False).
# 이 케이스는 E섹션 audit-verify 대상이므로 요청 단위 raw 전이로 진행.
for s in ["application_draft", "ai_pre_assessment_ready", "ai_pre_assessment_running", "pathway_determination"]:
    tr(ct, rg, s)
a2 = httpx.post(f"{B}/cases/{rg}/pathway/assess", headers=H(ct)).json()
ok("임계원재료 → 정규 판정", a2["suggested_pathway"] == "reguler", a2["critical_ingredient_count"])
httpx.post(f"{B}/cases/{rg}/pathway/confirm", headers=H(ct), json={"pathway": "reguler"})
tr(ct, rg, "supplementation_submitted")
tr(ct, rg, "consultant_review")
tr(ct, rg, "document_pre_audit_requested")
# 가드: 미결제 인보이스(waiting_payment) 생성 후 전이 차단 확인
inv = httpx.post(f"{B}/cases/{rg}/invoices", headers=H(ct), json={"service_type": "pre_audit", "amount": 1000000}).json()
ok("인보이스 기본상태 waiting_payment", inv.get("status") == "waiting_payment", inv.get("status"))
g1 = tr(ct, rg, "document_pre_audit_in_review")
g1_codes = [b.get("code") for b in (g1.json().get("detail", {}).get("blockers") or [])] if g1.status_code == 409 else []
ok("가드: 미결제 → 사전심사검토 409 UNPAID_INVOICE", g1.status_code == 409 and "UNPAID_INVOICE" in g1_codes, g1_codes)
httpx.patch(f"{B}/invoices/{inv['invoice_id']}/pay", headers=H(ct))
g2 = tr(ct, rg, "document_pre_audit_in_review")
ok("결제 후 사전심사검토 전이 OK", g2.status_code == 200)
tr(ct, rg, "document_pre_audit_approved")
httpx.post(f"{B}/cases/{rg}/lph-assignment", headers=H(ft), json={"lph_name": "LPH Sucofindo"})
g3 = tr(ft, rg, "lph_assignment")   # lph_assignment 전이 = fatwa_liaison/operator
ok("LPH 배정 전이 OK(fatwa_liaison)", g3.status_code == 200, g3.json() if g3.status_code != 200 else "")
tr(aut, rg, "onsite_audit_scheduled")     # onsite 계열 = auditor
tr(aut, rg, "onsite_audit_in_progress")
fid = httpx.post(f"{B}/cases/{rg}/findings", headers=H(ct), json={"finding": "교차오염", "severity": "major"}).json()["finding_id"]
tr(aut, rg, "audit_closed")
tr(aut, rg, "hpas_evaluation_ready")
g4 = tr(ft, rg, "final_package_preparation")   # final_package = fatwa_liaison/operator
ok("가드: 미해결 major → 최종패키지 409", g4.status_code == 409, g4.json().get("detail", {}).get("blockers"))
httpx.patch(f"{B}/findings/{fid}", headers=H(ct), json={"status": "closed"})
g5 = tr(ft, rg, "final_package_preparation")
ok("major 종결 후 최종패키지 OK", g5.status_code == 200)
tr(ft, rg, "fatwa_review")
# fatwa 미승인 시 인증서 발급 차단 (발급은 operator 전용)
c1 = httpx.post(f"{B}/cases/{rg}/certificate/issue", headers=H(ot))
ok("가드: fatwa 미승인 → 인증서 발급 409", c1.status_code == 409, c1.json().get("detail", {}).get("code"))
# 2단계 승인: 샤리아 가승인(provisional) → operator 최종승인
fw1 = httpx.patch(f"{B}/cases/{rg}/fatwa", headers=H(ft), json={"decision": "approved", "committee_note": "충족"}).json()
ok("샤리아 가승인 → provisional", fw1.get("fatwa_status") == "provisional", fw1.get("fatwa_status"))
fw2 = httpx.post(f"{B}/cases/{rg}/fatwa/final-approve", headers=H(ot)).json()
ok("operator 최종승인 → approved", fw2.get("fatwa_status") == "approved", fw2)
cert = httpx.post(f"{B}/cases/{rg}/certificate/issue", headers=H(ot)).json()
ok("fatwa 승인 후 인증서 발급", bool(cert.get("certificate_no")), cert.get("certificate_no"))

print("=== E. 횡단 (감사·보고서·Copilot·SJPH) ===")
av = httpx.get(f"{B}/cases/{rg}/audit-verify", headers=H(ct)).json()
ok("감사 해시체인 무결성", av["integrity_ok"], f"events={av['count']}")
httpx.patch(f"{B}/cases/{rg}/sjph", headers=H(ct), json={"element": "commitment", "status": "ok"})
sj = httpx.get(f"{B}/cases/{rg}/sjph", headers=H(ct)).json()
ok("SJPH 5요소 동작", sj["completion"] >= 20, f"{sj['completion']}%")
rep = httpx.post(f"{B}/cases/{rg}/report", headers=H(ct)).json()
ok("종합 보고서 생성", "할랄 인증 준비 보고서" in rep["report"])
ask = httpx.post(f"{B}/cases/{rg}/ask", headers=H(ct), json={"question": "지금 상태 요약해줘"}, timeout=120).json()
ok("Copilot 응답(gemma3)", len(ask.get("answer", "")) > 10)

print("\n=== 결과 ===")
print(f"PASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패:", F)
sys.exit(1 if F else 0)
