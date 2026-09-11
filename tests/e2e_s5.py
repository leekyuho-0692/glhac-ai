"""S5 검증 — 원재료 일괄 스크리닝 · Pendamping 배정·검증."""
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
HP = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "pendamping1", "password": "pw"}).json()["token"]}

cid = httpx.post(f"{B}/cases", headers=HC,
                 json={"company_name": "S5Test", "org_id": "org_demo"}).json()["case_id"]
print(f"공용 케이스: {cid[:8]}...")

# ── S5-1: 원재료 일괄 스크리닝 ────────────────────────────
print(f"\n=== S5-1 원재료 일괄 스크리닝 (case={cid[:8]}) ===")

# 원재료 3건 추가
m1 = httpx.post(f"{B}/cases/{cid}/materials", headers=HC,
                json={"name": "Gelatin", "e_number": "E441", "mat_type": "additive"}).json()
m2 = httpx.post(f"{B}/cases/{cid}/materials", headers=HC,
                json={"name": "Lecithin", "e_number": "E322", "mat_type": "additive"}).json()
m3 = httpx.post(f"{B}/cases/{cid}/materials", headers=HC,
                # 'colorant' 는 사전에 없는 코드다 — 입력검증(P0) 이후 422 가 난다.
                # 카민은 색소지만 첨가물로 분류한다(사전 기준).
                json={"name": "Carmine", "e_number": "E120", "mat_type": "additive"}).json()
ok("원재료 3건 추가", all("material_id" in x for x in [m1, m2, m3]), [m1, m2, m3])

# 개별 스크리닝 API 동작 확인
for mid_obj, name in [(m1, "Gelatin"), (m2, "Lecithin"), (m3, "Carmine")]:
    mid = mid_obj.get("material_id")
    if not mid:
        ok(f"{name} screen skip (no id)", False, mid_obj)
        continue
    r = httpx.post(f"{B}/materials/{mid}/screen", headers=HC)
    ok(f"{name} screen 200", r.status_code == 200, r.status_code)
    if r.status_code == 200:
        d = r.json()
        ok(f"{name} result 필드 존재", "result" in d, d)
        ok(f"{name} result 유효값", d.get("result") in ("PASS", "BLOCK", "NEEDS_EVIDENCE", "CLEARED", "UNKNOWN"), d.get("result"))

# 스크리닝 후 목록 조회
mats = httpx.get(f"{B}/cases/{cid}/materials", headers=HC).json()
screened = [m for m in mats if m.get("result") and m["result"] != "UNKNOWN"]
ok("스크리닝 후 판정 결과 ≥1건", len(screened) >= 1, len(screened))

# E441(Gelatin) → 돼지 유래 → BLOCK 예상
g = next((m for m in mats if m.get("e_number") == "E441"), None)
if g:
    ok("E441(Gelatin) 스크리닝 결과 존재", bool(g.get("result")), g.get("result"))

# E322(Lecithin) → 식물성이면 PASS/NEEDS_EVIDENCE
l = next((m for m in mats if m.get("e_number") == "E322"), None)
if l:
    ok("E322(Lecithin) 스크리닝 결과 존재", bool(l.get("result")), l.get("result"))

# ── S5-2: Pendamping 배정 + 검증 ───────────────────────────
print(f"\n=== S5-2 Pendamping 배정·검증 (case={cid[:8]}) ===")

# 배정 (consultant)
ra = httpx.post(f"{B}/cases/{cid}/pendamping/assign", headers=HC,
                json={"pendamping_id": "pendamping1"}).json()
ok("동반자 배정 성공", "assignment_id" in ra, ra)

# 배정 비권한 (pendamping_pph → 403)
ra_bad = httpx.post(f"{B}/cases/{cid}/pendamping/assign", headers=HP,
                    json={"pendamping_id": "pendamping1"})
ok("pendamping_pph 배정 → 403", ra_bad.status_code == 403, ra_bad.status_code)

# 검증 (pendamping_pph) — approved
rv = httpx.post(f"{B}/cases/{cid}/pendamping/verify", headers=HP,
                json={"decision": "approved", "note": "이행사항 확인", "signature_ref": "SIG-001"}).json()
ok("검증 승인 성공", rv.get("decision") == "approved", rv)
ok("승인 시 switched_to_reguler=False", rv.get("switched_to_reguler") is False, rv.get("switched_to_reguler"))

# 두 번째 케이스: 거부 → 정규 전환
cid2 = httpx.post(f"{B}/cases", headers=HC,
                  json={"company_name": "S5RejectTest", "org_id": "org_demo"}).json()["case_id"]
httpx.post(f"{B}/cases/{cid2}/pendamping/assign", headers=HC,
           json={"pendamping_id": "pendamping1"})

rv2 = httpx.post(f"{B}/cases/{cid2}/pendamping/verify", headers=HP,
                 json={"decision": "rejected", "note": "불일치 발견"}).json()
ok("검증 거부 성공", rv2.get("decision") == "rejected", rv2)
ok("거부 시 switched_to_reguler=True", rv2.get("switched_to_reguler") is True, rv2.get("switched_to_reguler"))

# 배정 없이 검증 시도 → 404
cid3 = httpx.post(f"{B}/cases", headers=HC,
                  json={"company_name": "S5NoAssign", "org_id": "org_demo"}).json()["case_id"]
rv3 = httpx.post(f"{B}/cases/{cid3}/pendamping/verify", headers=HP,
                 json={"decision": "approved"})
ok("배정 없이 검증 → 404", rv3.status_code == 404, rv3.status_code)

# ── 결과 ──────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
