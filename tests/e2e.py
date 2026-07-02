"""GL-HAC AI 전체 E2E 점검 — 인프라·인증·RBAC/ABAC·이중경로 생애주기·가드·감사·보고서·Copilot."""
import sys
import httpx

B = "http://127.0.0.1:8800"
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
ok("consultant/applicant/admin 로그인", all([ct, at, adt]))
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
for s in ["application_draft", "ai_pre_assessment_ready", "ai_pre_assessment_running", "pathway_determination"]:
    tr(ct, sd, s)
a = httpx.post(f"{B}/cases/{sd}/pathway/assess", headers=H(ct)).json()
ok("자기선언 적격 판정", a["suggested_pathway"] == "self_declare", a["risk_category"])
httpx.post(f"{B}/cases/{sd}/pathway/confirm", headers=H(ct), json={"pathway": "self_declare"})
tr(ct, sd, "sjph_lite_prepared")
tr(ct, sd, "pendamping_verification")
httpx.post(f"{B}/cases/{sd}/pendamping/assign", headers=H(ct), json={"pendamping_id": "pp1"})
# pendamping verify는 pendamping_pph 역할 필요
pt = login("pendamping1", "pw")
httpx.post(f"{B}/cases/{sd}/pendamping/verify", headers=H(pt), json={"decision": "verified"})
r = tr(ct, sd, "self_declaration_submitted")
ok("SD 제출 전이(가드 통과)", r.status_code == 200, r.json())
tr(ct, sd, "committee_verification")
r = tr(ct, sd, "certificate_issued")
ok("SD 인증서 단계 도달", r.status_code == 200 and httpx.get(f"{B}/cases/{sd}", headers=H(ct)).json()["status"] == "certificate_issued")

print("=== D. 정규(Reguler) 생애주기 + 가드 ===")
rg = httpx.post(f"{B}/cases", headers=H(ct), json={"company_name": "RG Co", "is_msme": False, "org_id": "org_demo"}).json()["case_id"]
httpx.post(f"{B}/cases/{rg}/products", headers=H(ct), json={"name": "Sambal RG"})
httpx.post(f"{B}/cases/{rg}/materials", headers=H(ct), json={"name": "gelatin"})  # critical
ei2 = httpx.post(f"{B}/cases/{rg}/sihalal/identity/link", headers=H(ct), json={"external_email": "rg@x.com"}).json()
httpx.post(f"{B}/sihalal/identity/{ei2['external_identity_id']}/verify", headers=H(ct), json={"expected_identifier": "rg@x.com"})
for s in ["application_draft", "ai_pre_assessment_ready", "ai_pre_assessment_running", "pathway_determination"]:
    tr(ct, rg, s)
a2 = httpx.post(f"{B}/cases/{rg}/pathway/assess", headers=H(ct)).json()
ok("임계원재료 → 정규 판정", a2["suggested_pathway"] == "reguler", a2["critical_ingredient_count"])
httpx.post(f"{B}/cases/{rg}/pathway/confirm", headers=H(ct), json={"pathway": "reguler"})
tr(ct, rg, "supplementation_submitted")
tr(ct, rg, "consultant_review")
tr(ct, rg, "document_pre_audit_requested")
# 가드: 미결제 인보이스 생성 후 전이 차단 확인
inv = httpx.post(f"{B}/cases/{rg}/invoices", headers=H(ct), json={"service_type": "pre_audit", "amount": 1000000}).json()
g1 = tr(ct, rg, "document_pre_audit_in_review")
ok("가드: 미결제 → 사전심사검토 409", g1.status_code == 409, g1.json().get("detail", {}).get("blockers"))
httpx.patch(f"{B}/invoices/{inv['invoice_id']}/pay", headers=H(ct))
g2 = tr(ct, rg, "document_pre_audit_in_review")
ok("결제 후 사전심사검토 전이 OK", g2.status_code == 200)
tr(ct, rg, "document_pre_audit_approved")
httpx.post(f"{B}/cases/{rg}/lph-assignment", headers=H(ct), json={"lph_name": "LPH Sucofindo"})
g3 = tr(ct, rg, "lph_assignment")
ok("LPH 배정 전이 OK", g3.status_code == 200)
tr(ct, rg, "onsite_audit_scheduled")
tr(ct, rg, "onsite_audit_in_progress")
fid = httpx.post(f"{B}/cases/{rg}/findings", headers=H(ct), json={"finding": "교차오염", "severity": "major"}).json()["finding_id"]
tr(ct, rg, "audit_closed")
tr(ct, rg, "hpas_evaluation_ready")
g4 = tr(ct, rg, "final_package_preparation")
ok("가드: 미해결 major → 최종패키지 409", g4.status_code == 409, g4.json().get("detail", {}).get("blockers"))
httpx.patch(f"{B}/findings/{fid}", headers=H(ct), json={"status": "closed"})
g5 = tr(ct, rg, "final_package_preparation")
ok("major 종결 후 최종패키지 OK", g5.status_code == 200)
tr(ct, rg, "fatwa_review")
# fatwa 미승인 시 인증서 발급 차단
c1 = httpx.post(f"{B}/cases/{rg}/certificate/issue", headers=H(ct))
ok("가드: fatwa 미승인 → 인증서 발급 409", c1.status_code == 409)
httpx.patch(f"{B}/cases/{rg}/fatwa", headers=H(ct), json={"decision": "approved", "committee_note": "충족"})
cert = httpx.post(f"{B}/cases/{rg}/certificate/issue", headers=H(ct)).json()
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
