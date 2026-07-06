"""S1 검증 — /meta/enums 계약 · org 목록 · readiness · 증빙 자동재스크리닝(C1)."""
import base64
import os
import sys
import httpx

B = "http://127.0.0.1:%s" % os.environ.get("GLHAC_PORT", "8800")
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:70]) if x else ""))


H = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
     json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

print("=== S1-1 /meta/enums 계약 ===")
e = httpx.get(f"{B}/meta/enums", headers=H).json()
groups = set(e.keys())
need = {"material_type", "material_source", "material_cert", "doc_type", "review_status", "pathway",
        "role", "finding_severity", "sjph_element", "sjph_status", "invoice_service_type",
        "fatwa_decision", "change_type", "discussion_kind", "evidence_type"}
ok("필수 enum 그룹 존재", need <= groups, "누락=" + str(need - groups))
src = [x["value"] for x in e["material_source"]]
ok("material_source에 unknown 포함(P0 버그 수정)", "unknown" in src, src)
mt = [x["value"] for x in e["material_type"]]
ok("material_type 정합(processing_aid·sanitizer)", "processing_aid" in mt and "sanitizer" in mt, mt)
ev = [x["value"] for x in e["evidence_type"]]
ok("evidence_type(msds/coa/할랄인증서)", {"msds", "coa", "halal_certificate"} <= set(ev))

print("=== S1-2 org 목록(드롭다운 소스) ===")
orgs = httpx.get(f"{B}/admin/orgs", headers=HA).json()
ok("admin /admin/orgs 목록 반환", isinstance(orgs, list) and len(orgs) > 0, [o["org_id"] for o in orgs][:5])

print("=== S1-4 readiness ===")
cid = httpx.post(f"{B}/cases", headers=H,
                 json={"company_name": "S1T", "is_msme": True, "org_id": "org_demo"}).json()["case_id"]
r = httpx.get(f"{B}/cases/{cid}/readiness", headers=H).json()
ok("readiness 구조(점수·band·breakdown)",
   "readiness" in r and r["band"] in ("ready", "warn", "risk") and "documents" in r["breakdown"],
   f"{r['readiness']}% {r['band']}")

print("=== S1-3/S1-5 증빙 자동재스크리닝(C1) ===")
mid = httpx.post(f"{B}/cases/{cid}/materials", headers=H,
                 json={"name": "gelatin", "source": "unknown"}).json()["material_id"]
m0 = [m for m in httpx.get(f"{B}/cases/{cid}/materials", headers=H).json() if m["material_id"] == mid][0]
ok("원재료 초기 NEEDS_EVIDENCE", m0["result"] == "NEEDS_EVIDENCE", m0["result"])
ok("evidence_count 필드", m0.get("evidence_count") == 0)
b64 = base64.b64encode(b"HALAL CERT - fish gelatin").decode()
res = httpx.post(f"{B}/cases/{cid}/materials/{mid}/evidence", headers=H,
                 json={"evidence_type": "halal_certificate", "file_b64": b64, "filename": "c.txt"}).json()
ok("증빙 업로드 → 재스크리닝 CLEARED(C1)", res["screen_result"] == "CLEARED" and res["recleared"], res)
m1 = [m for m in httpx.get(f"{B}/cases/{cid}/materials", headers=H).json() if m["material_id"] == mid][0]
ok("원재료 CLEARED + evidence_count 1", m1["result"] == "CLEARED" and m1["evidence_count"] == 1)

print(f"\n=== 결과 === PASS {len(P)} / FAIL {len(F)}")
if F:
    print("실패:", F)
sys.exit(1 if F else 0)
