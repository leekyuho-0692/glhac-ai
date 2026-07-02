"""S3 검증 — 현장체크리스트·파트와위원회·인증동결/언락·Excel·심사원풀·LPH레퍼런스."""
import sys
import httpx

B = "http://127.0.0.1:8800"
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:120]) if x else ""))


HC = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

cid = httpx.post(f"{B}/cases", headers=HC,
                 json={"company_name": "S3Test", "org_id": "org_demo"}).json()["case_id"]
print(f"공용 케이스: {cid[:8]}...")

# ── S3-1: 현장 체크리스트 16항목 ────────────────────────────
print(f"\n=== S3-1 현장 체크리스트 (case={cid[:8]}) ===")
cl = httpx.get(f"{B}/cases/{cid}/onsite-checklist", headers=HC).json()
ok("16항목 반환", len(cl.get("items", [])) == 16, len(cl.get("items", [])))
ok("comply 카운트 필드", "comply" in cl, cl)
ok("nonconformity 카운트 필드", "nonconformity" in cl, cl)

r1 = httpx.post(f"{B}/cases/{cid}/onsite-checklist", headers=HC,
                json={"item_key": "halal_policy", "result": "comply", "note": "정책 확인"}).json()
ok("첫 항목 comply 저장", r1.get("ok") is True, r1)

r2 = httpx.post(f"{B}/cases/{cid}/onsite-checklist", headers=HC,
                json={"item_key": "raw_material_control", "result": "nonconformity", "note": "서류 미흡"}).json()
ok("두 번째 항목 nonconformity 저장", r2.get("ok") is True, r2)

cl2 = httpx.get(f"{B}/cases/{cid}/onsite-checklist", headers=HC).json()
ok("comply count = 1", cl2.get("comply") == 1, cl2.get("comply"))
ok("nonconformity count = 1", cl2.get("nonconformity") == 1, cl2.get("nonconformity"))

# ── S3-2: 파트와 위원회 구성 + 제품 scope ──────────────────
print(f"\n=== S3-2 파트와 위원회 (case={cid[:8]}) ===")
# 제품 추가
pid = httpx.post(f"{B}/cases/{cid}/products", headers=HC,
                 json={"name": "HalalNoodle", "category": "food"}).json()["product_id"]

r_fw = httpx.patch(f"{B}/cases/{cid}/fatwa", headers=HC, json={
    "decision": "approved",
    "committee_head": "Dr. Ahmad",
    "committee_secretary": "Budi",
    "committee_members": ["Siti", "Rahman", "Dewi"],
    "product_scope": [pid],
}).json()
ok("PATCH fatwa 성공", "decision" in r_fw or r_fw.get("ok") is True, r_fw)

fw = httpx.get(f"{B}/cases/{cid}/fatwa", headers=HC).json()
ok("committee_head 저장", fw.get("committee_head") == "Dr. Ahmad", fw.get("committee_head"))
ok("committee_secretary 저장", fw.get("committee_secretary") == "Budi", fw.get("committee_secretary"))
ok("committee_members 3명", isinstance(fw.get("committee_members"), list) and
   len(fw["committee_members"]) == 3, fw.get("committee_members"))
ok("product_scope 제품 포함", pid in (fw.get("product_scope") or []), fw.get("product_scope"))

# ── S3-3: 인증서 발급 → 동결 → 언락 ───────────────────────
print(f"\n=== S3-3 인증서 동결/언락 (case={cid[:8]}) ===")
rc = httpx.post(f"{B}/cases/{cid}/certificate/issue", headers=HC).json()
ok("인증서 발급", "certificate_no" in rc or rc.get("ok") is True, rc)

cert = httpx.get(f"{B}/cases/{cid}/certificate", headers=HC).json()
ok("frozen_product_ids 존재", "frozen_product_ids" in cert, cert)
ok("frozen_material_ids 존재", "frozen_material_ids" in cert, cert)

# unlock (consultant)
ru = httpx.post(f"{B}/cases/{cid}/certificate/unlock", headers=HC).json()
ok("언락 성공", ru.get("ok") is True or "scope_frozen" in ru, ru)

# 이미 잠금 해제된 상태에서 재발급 후 다시 unlock → 409 (이미 해제)
# 단, 재unlock 시도 → 409
ru2 = httpx.post(f"{B}/cases/{cid}/certificate/unlock", headers=HC)
ok("중복 언락 → 409", ru2.status_code == 409, ru2.status_code)

# ── S3-4: Excel 내보내기 ─────────────────────────────────
print(f"\n=== S3-4 Excel 내보내기 (case={cid[:8]}) ===")
resp_xl = httpx.get(f"{B}/cases/{cid}/materials/export.xlsx", headers=HC)
ok("200 OK", resp_xl.status_code == 200, resp_xl.status_code)
ok("xlsx MIME", "spreadsheet" in resp_xl.headers.get("content-type", ""), resp_xl.headers.get("content-type"))
ok("파일 크기 > 0", len(resp_xl.content) > 0, len(resp_xl.content))

# ── S3-5: 심사원 풀 (max 3) ──────────────────────────────
print(f"\n=== S3-5 심사원 풀 (case={cid[:8]}) ===")
a1 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HC,
                json={"name": "Ahmad A", "role_in_team": "ketua", "cert_no": "LSH-001"}).json()
ok("심사원 1 추가", "id" in a1, a1)

a2 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HC,
                json={"name": "Budi B", "role_in_team": "anggota"}).json()
ok("심사원 2 추가", "id" in a2, a2)

a3 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HC,
                json={"name": "Cici C", "role_in_team": "anggota"}).json()
ok("심사원 3 추가", "id" in a3, a3)

pool = httpx.get(f"{B}/cases/{cid}/auditor-pool", headers=HC).json()
ok("풀 3명", len(pool) == 3, len(pool))

a4 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HC,
                json={"name": "Deni D", "role_in_team": "anggota"})
ok("4번째 추가 → 400/409", a4.status_code in (400, 409), a4.status_code)

# 삭제
pool_id = pool[0]["id"]
rd = httpx.delete(f"{B}/cases/{cid}/auditor-pool/{pool_id}", headers=HC).json()
ok("심사원 삭제", "deleted" in rd, rd)

pool2 = httpx.get(f"{B}/cases/{cid}/auditor-pool", headers=HC).json()
ok("삭제 후 2명", len(pool2) == 2, len(pool2))

# ── S3-6: LPH 레퍼런스 ────────────────────────────────────
print(f"\n=== S3-6 LPH 레퍼런스 ===")
rl1 = httpx.post(f"{B}/admin/lph-references", headers=HA,
                 json={"name": "LPH Jakarta", "accreditation_no": "LPH-001", "region": "Jakarta"}).json()
ok("LPH 1 생성 (admin)", rl1.get("ok") is True or "lph_id" in rl1, rl1)

rl2 = httpx.post(f"{B}/admin/lph-references", headers=HA,
                 json={"name": "LPH Surabaya", "accreditation_no": "LPH-002", "region": "East Java"}).json()
ok("LPH 2 생성 (admin)", rl2.get("ok") is True or "lph_id" in rl2, rl2)

refs = httpx.get(f"{B}/admin/lph-references", headers=HC).json()
ok("LPH 목록 ≥2", len(refs) >= 2, len(refs))
names = [x["name"] for x in refs]
ok("LPH Jakarta 포함", "LPH Jakarta" in names, names)
ok("LPH Surabaya 포함", "LPH Surabaya" in names, names)

# ── 결과 ──────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
