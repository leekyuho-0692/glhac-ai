"""S7 검증 — 2뎁스 네비 + 케이스 스위처 (백엔드 데이터 확인)."""
import os
import sys
import httpx

B = "http://127.0.0.1:%s" % os.environ.get("GLHAC_PORT", "8800")
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:120]) if x else ""))


HC = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

# ── S7-1: 케이스 스위처 데이터 소스 (/cases) ─────────────────
print("\n=== S7-1 케이스 스위처 데이터 소스 ===")

# 케이스 2건 이상 생성
c1 = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "SwCase A", "org_id": "org_demo"}).json()
c2 = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "SwCase B", "org_id": "org_demo"}).json()
ok("케이스 A 생성", "case_id" in c1, c1)
ok("케이스 B 생성", "case_id" in c2, c2)

# /cases 목록 조회 — 케이스 스위처 바 데이터 소스
cases = httpx.get(f"{B}/cases", headers=HC).json()["items"]
ok("/cases 목록 배열", isinstance(cases, list), type(cases).__name__)
ok("케이스 ≥2건 반환", len(cases) >= 2, len(cases))

# 각 케이스 항목 필드 확인 (스위처 바에서 사용하는 필드)
if cases:
    item = cases[0]
    ok("case_id 필드", "case_id" in item, item)
    ok("company_name 필드", "company_name" in item, item)
    ok("status 필드", "status" in item, item)
    ok("pathway 필드", "pathway" in item, item)

# ── S7-2: 케이스 전환 (GLHAC_CASE 변경 검증) ─────────────────
print("\n=== S7-2 케이스 별 콘텐츠 분리 확인 ===")

cid_a = c1["case_id"]
cid_b = c2["case_id"]

# A 케이스 원재료 추가
ma = httpx.post(f"{B}/cases/{cid_a}/materials", headers=HC,
                json={"name": "MatA_Only", "mat_type": "raw"}).json()
ok("A 케이스 원재료 추가", "material_id" in ma, ma)

# B 케이스 원재료 추가 (다른 이름)
mb = httpx.post(f"{B}/cases/{cid_b}/materials", headers=HC,
                json={"name": "MatB_Only", "mat_type": "additive"}).json()
ok("B 케이스 원재료 추가", "material_id" in mb, mb)

# A 케이스 조회 → MatA_Only만
mats_a = httpx.get(f"{B}/cases/{cid_a}/materials", headers=HC).json()
names_a = [m["name"] for m in mats_a]
ok("A 케이스에 MatA_Only 존재", "MatA_Only" in names_a, names_a)
ok("A 케이스에 MatB_Only 없음", "MatB_Only" not in names_a, names_a)

# B 케이스 조회 → MatB_Only만
mats_b = httpx.get(f"{B}/cases/{cid_b}/materials", headers=HC).json()
names_b = [m["name"] for m in mats_b]
ok("B 케이스에 MatB_Only 존재", "MatB_Only" in names_b, names_b)
ok("B 케이스에 MatA_Only 없음", "MatA_Only" not in names_b, names_b)

# ── S7-3: 관리자 케이스 목록 (전체 조회) ─────────────────────
print("\n=== S7-3 관리자 전체 케이스 목록 ===")

all_cases = httpx.get(f"{B}/cases", headers=HA).json()["items"]
ok("관리자 /cases 응답 배열", isinstance(all_cases, list), type(all_cases).__name__)
# 관리자는 org 구분 없이 전체 케이스 볼 수 있어야 함 (org_demo 포함)
ids = [c["case_id"] for c in all_cases]
ok("방금 생성된 A 케이스 포함", cid_a in ids, len(ids))

# ── S7-4: 케이스 상세 조회 (스위처 탭 클릭 시 사용) ──────────
print("\n=== S7-4 케이스 상세 /cases/{id} ===")

ra = httpx.get(f"{B}/cases/{cid_a}", headers=HC).json()
ok("A 케이스 상세 조회", ra.get("case_id") == cid_a, ra.get("case_id"))
ok("company_name 일치", ra.get("company_name") == "SwCase A", ra.get("company_name"))
ok("pathway 필드 존재", "pathway" in ra, ra)
ok("status 필드 존재", "status" in ra, ra)

rb = httpx.get(f"{B}/cases/{cid_b}", headers=HC).json()
ok("B 케이스 상세 조회", rb.get("case_id") == cid_b, rb.get("case_id"))
ok("company_name 일치", rb.get("company_name") == "SwCase B", rb.get("company_name"))

# 없는 케이스 → 404
bad = httpx.get(f"{B}/cases/nonexistent-case-id", headers=HC)
ok("없는 케이스 → 404", bad.status_code == 404, bad.status_code)

# ── 결과 ──────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
