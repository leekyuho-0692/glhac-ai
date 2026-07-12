"""v3 백엔드 RBAC 강제 검증 — 역할 × 엔드포인트 403/허용 매트릭스 (회의 6역할 + operator).

v3 변경: LPH배정·인증서발급·파트와결정 = fatwa_liaison·operator (consultant 아님) · operator(최고운영자) 신설 · mockAudit.
실행: 테스트 서버 기동(:8801) 후 <venv>/bin/python tests/e2e_rbac.py
      대상은 GLHAC_TEST_URL 로 지정(기본 8801). ⚠️ 정본(8800)에는 실행 금지 — 테스트 케이스가 정본 DB를 오염시킴.
"""
import os
import sys
import httpx

B = os.environ.get("GLHAC_TEST_URL", "http://127.0.0.1:8801")
ROLES = {"consultant1": "pw", "applicant1": "pw", "penyelia1": "pw", "pendamping1": "pw",
         "auditor1": "pw", "fatwa1": "pw", "operator1": "pw", "admin": "admin"}
RN = {"consultant1": "consultant", "applicant1": "applicant", "penyelia1": "penyelia_halal",
      "pendamping1": "pendamping_pph", "auditor1": "auditor", "fatwa1": "fatwa_liaison",
      "operator1": "operator", "admin": "admin"}


def tok(u, p):
    return httpx.post(f"{B}/auth/login", json={"username": u, "password": p}).json()["token"]


T = {u: tok(u, p) for u, p in ROLES.items()}
H = {u: {"Authorization": "Bearer " + t} for u, t in T.items()}
# 공용 케이스(consultant, org_demo)
CID = httpx.post(f"{B}/cases", headers=H["consultant1"],
                 json={"company_name": "RBAC Test", "is_msme": True, "org_id": "org_demo"}).json()["case_id"]

# (라벨, method, path, body, 허용역할집합)
CASES = [
    ("케이스 생성", "POST", "/cases", {"company_name": "x", "org_id": "org_demo"}, {"consultant", "applicant", "admin"}),
    ("원재료 추가", "POST", f"/cases/{CID}/materials", {"name": "gula"}, {"consultant", "applicant", "penyelia_halal", "admin"}),
    ("SJPH 편집", "PATCH", f"/cases/{CID}/sjph", {"element": "commitment", "status": "ok"}, {"consultant", "applicant", "penyelia_halal", "admin"}),
    # A05(설계 정본): 오디터도 문서 단위 판정(승인/반려/재요청) 수행 — rbac document.review={consultant,auditor}
    ("문서 검수", "PATCH", "/documents/nope/review", {"review_status": "approved"}, {"consultant", "auditor", "admin"}),
    ("청구 생성", "POST", f"/cases/{CID}/invoices", {"service_type": "pre_audit", "amount": 1000}, {"consultant", "admin"}),
    ("경로 확정", "POST", f"/cases/{CID}/pathway/confirm", {"pathway": "self_declare"}, {"consultant", "admin"}),
    ("SIHALAL 검증", "POST", "/sihalal/identity/nope/verify", {"expected_identifier": "x"}, {"consultant", "admin"}),
    ("심사 지적", "POST", f"/cases/{CID}/findings", {"finding": "x", "severity": "minor"}, {"consultant", "auditor", "admin"}),
    ("LPH 배정", "POST", f"/cases/{CID}/lph-assignment", {"lph_name": "x"}, {"fatwa_liaison", "operator", "admin"}),
    # SoD(직무분리): 파트와 가승인은 fatwa_liaison만, 최종 인증서 발급은 operator만(+admin 우회)
    ("파트와 결정", "PATCH", f"/cases/{CID}/fatwa", {"decision": "approved"}, {"fatwa_liaison", "admin"}),
    ("인증서 발급", "POST", f"/cases/{CID}/certificate/issue", None, {"operator", "admin"}),
    ("모의심사 큐", "GET", "/mock-audit/queue", None, {"auditor", "fatwa_liaison", "operator", "admin"}),
    ("모의심사 결정", "POST", f"/cases/{CID}/mock-audit/decision", {"result": "pass"}, {"auditor", "fatwa_liaison", "operator", "admin"}),
    ("자기선언 검증", "POST", f"/cases/{CID}/pendamping/verify", {"decision": "verified"}, {"pendamping_pph", "admin"}),
    ("결제 표시", "PATCH", "/invoices/nope/pay", None, {"consultant", "applicant", "admin"}),
    ("admin 사용자목록", "GET", "/admin/users", None, {"admin"}),
    ("admin 사용자생성", "POST", "/admin/users", {"username": "z", "password": "z", "role": "auditor"}, {"admin"}),
    ("admin 전체케이스", "GET", "/admin/cases", None, {"admin"}),
    ("admin 온톨로지", "GET", "/admin/ontology/stats", None, {"admin"}),
]

P, F = [], []


def call(u, method, path, body):
    fn = getattr(httpx, method.lower())
    kw = {"headers": H[u]}
    if body is not None:
        kw["json"] = body
    return fn(f"{B}{path}", **kw).status_code


print("=== 역할 × 엔드포인트 403/허용 매트릭스 ===")
for label, method, path, body, allowed in CASES:
    line = f"  {label:<14} "
    for u in ROLES:
        role = RN[u]
        sc = call(u, method, path, body)
        should_allow = role in allowed
        # 허용역할=403 아님 / 미허용=403
        good = (sc != 403) if should_allow else (sc == 403)
        (P if good else F).append(f"{label}/{role}")
        mark = "·" if good else "✗"
        line += f"{role[:4]}:{sc}{mark} "
    print(line)

# admin이 생성한 z 정리
for x in httpx.get(f"{B}/admin/users", headers=H["admin"]).json():
    if x["username"] == "z":
        httpx.delete(f"{B}/admin/users/{x['user_id']}", headers=H["admin"])

print(f"\n=== 결과 === PASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패:", F[:20])
sys.exit(1 if F else 0)
