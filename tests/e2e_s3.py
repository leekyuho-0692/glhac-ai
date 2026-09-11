"""S3 검증 — 현장체크리스트·파트와위원회·인증동결/언락·Excel·심사원풀·LPH레퍼런스."""
import os
import sys
import httpx
from _target import base   # 라이브(8800) 오염 방지 — 대상 서버는 GLHAC_E2E_BASE 로만 지정

B = base()
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:120]) if x else ""))


HC = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}
HO = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "operator1", "password": "pw"}).json()["token"]}
HF = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "fatwa1", "password": "pw"}).json()["token"]}

# is_msme — 뒤(S3-3)에서 자기선언 경로로 전진시켜야 인증서 발급 가드를 통과한다.
cid = httpx.post(f"{B}/cases", headers=HC,
                 json={"company_name": "S3Test", "is_msme": True,
                       "org_id": "org_demo"}).json()["case_id"]
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

r_fw = httpx.patch(f"{B}/cases/{cid}/fatwa", headers=HA, json={
    "decision": "approved",
    "committee_head": "Dr. Ahmad",
    "committee_secretary": "Budi",
    "committee_members": ["Siti", "Rahman", "Dewi"],
    "product_scope": [pid],
}).json()
ok("PATCH fatwa 성공", "decision" in r_fw or r_fw.get("ok") is True, r_fw)

fw = httpx.get(f"{B}/cases/{cid}/fatwa", headers=HA).json()
ok("committee_head 저장", fw.get("committee_head") == "Dr. Ahmad", fw.get("committee_head"))
ok("committee_secretary 저장", fw.get("committee_secretary") == "Budi", fw.get("committee_secretary"))
ok("committee_members 3명", isinstance(fw.get("committee_members"), list) and
   len(fw["committee_members"]) == 3, fw.get("committee_members"))
ok("product_scope 제품 포함", pid in (fw.get("product_scope") or []), fw.get("product_scope"))

# ── S3-3: 인증서 발급 → 동결 → 언락 ───────────────────────
print(f"\n=== S3-3 인증서 동결/언락 (case={cid[:8]}) ===")
# 인증서는 현장심사(또는 자기선언 위원회확인)를 거친 케이스에만 나간다 — 2026-08-14 발급 가드.
# 이 시나리오가 검증하려는 것은 동결/언락이므로, 자기선언 경로로 케이스를 정상 전진시켜 놓는다.
#   (종전에는 onboarding 상태로 곧장 발급을 시도해 STATE_NOT_READY 로 막혔다.)
httpx.post(f"{B}/orgs/org_demo/penyelia", headers=HC,
           json={"name": "Budi", "training_cert": "PH-1"})
httpx.post(f"{B}/cases/{cid}/materials", headers=HC, json={"name": "citric acid"})
_ei = httpx.post(f"{B}/cases/{cid}/sihalal/identity/link", headers=HC,
                 json={"external_email": "s3@x.com"}).json()
httpx.post(f"{B}/sihalal/identity/{_ei['external_identity_id']}/verify", headers=HC,
           json={"expected_identifier": "s3@x.com"})
# 사업자 식별번호는 업체마다 달라야 한다(입력검증 P0). e2e.py 가 같은 값을 먼저 쓰면
# 여기서 NIB_ALREADY_USED 로 막혀 submit-application 부터 줄줄이 실패했다
# (CI 는 한 DB 에서 스크립트를 순서대로 돌린다). 실행마다 고유한 값을 쓴다.
import time as _t
_S3_NIB = "987654321" + str(int(_t.time()))[-4:]
httpx.patch(f"{B}/cases/{cid}/profile", headers=HC, json={"nib": _S3_NIB})
httpx.post(f"{B}/cases/{cid}/submit-application", headers=HC)
httpx.post(f"{B}/cases/{cid}/pathway/assess", headers=HC)
httpx.post(f"{B}/cases/{cid}/pathway/confirm", headers=HC, json={"pathway": "self_declare"})
httpx.post(f"{B}/cases/{cid}/transition", headers=HC, json={"to_state": "sjph_lite_prepared"})
_users = httpx.get(f"{B}/admin/users", headers=HA).json()
_pp = next(u["user_id"] for u in _users if u["username"] == "pendamping1")
httpx.post(f"{B}/cases/{cid}/pendamping/assign", headers=HC, json={"pendamping_id": _pp})
httpx.post(f"{B}/cases/{cid}/transition", headers=HC, json={"to_state": "pendamping_verification"})
HP = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "pendamping1", "password": "pw"}).json()["token"]}
httpx.post(f"{B}/cases/{cid}/pendamping/verify", headers=HP, json={"decision": "verified"})
httpx.post(f"{B}/cases/{cid}/transition", headers=HO, json={"to_state": "committee_verification"})
httpx.post(f"{B}/cases/{cid}/committee/decide", headers=HF,
           json={"decision": "approve", "reason": "S3 동결 검증용"})
ok("발급 전 상태 = committee_verification",
   httpx.get(f"{B}/cases/{cid}", headers=HC).json().get("status") == "committee_verification",
   httpx.get(f"{B}/cases/{cid}", headers=HC).json().get("status"))
# 2단계 파트와: PATCH(approved)=가승인(provisional) → operator 최종승인 후에야 발급 가능
r_fa = httpx.post(f"{B}/cases/{cid}/fatwa/final-approve", headers=HO).json()
ok("파트와 최종승인(operator)", r_fa.get("fatwa_status") == "approved", r_fa)
# 인증서 발급은 2인 승인(maker-checker) 구조: operator가 요청 후 fatwa_liaison이 승인해야 실제 발급됨
rc = httpx.post(f"{B}/cases/{cid}/certificate/issue", headers=HO).json()
rc_dict = rc if isinstance(rc, dict) else {}
approval_id = rc_dict.get("approval_id")

# certificate_no가 이미 있으면 구경로(발급 완료), 없으면 approval_id로 checker 승인 요청
if "certificate_no" not in rc_dict and approval_id:
    ra = httpx.post(f"{B}/approvals/{approval_id}/approve", headers=HF).json()
    ra_dict = ra if isinstance(ra, dict) else {}
    result = ra_dict.get("result", ra_dict)
else:
    result = rc_dict

ok("인증서 발급", "certificate_no" in result if isinstance(result, dict) else False, result)

cert = httpx.get(f"{B}/cases/{cid}/certificate", headers=HC).json()
cert_dict = cert if isinstance(cert, dict) else {}
ok("frozen_product_ids 존재", "frozen_product_ids" in cert_dict, cert_dict)
ok("frozen_material_ids 존재", "frozen_material_ids" in cert_dict, cert_dict)

# unlock (operator 전용 + 사유 필수)
ru = httpx.post(f"{B}/cases/{cid}/certificate/unlock", headers=HO,
                json={"reason": "S3 재인증 언락 테스트"}).json()
ok("언락 성공", ru.get("ok") is True or "scope_frozen" in ru, ru)

# 이미 잠금 해제된 상태에서 재unlock 시도 → 409 (NOT_FROZEN)
ru2 = httpx.post(f"{B}/cases/{cid}/certificate/unlock", headers=HO,
                 json={"reason": "S3 중복 언락 시도"})
ok("중복 언락 → 409", ru2.status_code == 409, ru2.status_code)

# ── S3-4: Excel 내보내기 ─────────────────────────────────
print(f"\n=== S3-4 Excel 내보내기 (case={cid[:8]}) ===")
resp_xl = httpx.get(f"{B}/cases/{cid}/materials/export.xlsx", headers=HC)
ok("200 OK", resp_xl.status_code == 200, resp_xl.status_code)
ok("xlsx MIME", "spreadsheet" in resp_xl.headers.get("content-type", ""), resp_xl.headers.get("content-type"))
ok("파일 크기 > 0", len(resp_xl.content) > 0, len(resp_xl.content))

# ── S3-5: 심사원 풀 (max 3) ──────────────────────────────
print(f"\n=== S3-5 심사원 풀 (case={cid[:8]}) ===")
a1 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HO,
                json={"name": "Ahmad A", "role_in_team": "ketua", "cert_no": "LSH-001"}).json()
ok("심사원 1 추가", "id" in a1, a1)

a2 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HO,
                json={"name": "Budi B", "role_in_team": "anggota"}).json()
ok("심사원 2 추가", "id" in a2, a2)

a3 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HO,
                json={"name": "Cici C", "role_in_team": "anggota"}).json()
ok("심사원 3 추가", "id" in a3, a3)

pool = httpx.get(f"{B}/cases/{cid}/auditor-pool", headers=HC).json()
ok("풀 3명", len(pool) == 3, len(pool))

a4 = httpx.post(f"{B}/cases/{cid}/auditor-pool", headers=HO,
                json={"name": "Deni D", "role_in_team": "anggota"})
ok("4번째 추가 → 400/409", a4.status_code in (400, 409), a4.status_code)

# 삭제
pool_id = pool[0]["id"]
rd = httpx.delete(f"{B}/cases/{cid}/auditor-pool/{pool_id}", headers=HO).json()
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
