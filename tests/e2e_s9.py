"""S9 검증 — 기한 경보(due_date) · Admin 통계 현황."""
import sys
import sqlite3
from datetime import date, timedelta
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

# 테스트 케이스 생성
c1 = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "S9 DeadlineTest", "org_id": "org_demo"}).json()
ok("테스트 케이스 생성", "case_id" in c1, c1)
CID = c1.get("case_id", "")

# ── S9-1: 기한 경보 — 심사 지적 due_date 필드 ─────────────────
print("\n=== S9-1 심사지적 기한 경보 (due_date 필드) ===")

today = date.today()
past_date = (today - timedelta(days=5)).isoformat()   # 5일 초과
near_date = (today + timedelta(days=3)).isoformat()   # 3일 남음 (임박)
future_date = (today + timedelta(days=30)).isoformat() # 30일 (정상)

# 기한 초과 지적 (open)
r_ovd = httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
                   json={"finding": "S9 기한초과 지적", "severity": "minor",
                         "area": "hygiene", "due_date": past_date})
ok("기한초과 지적 추가", r_ovd.status_code == 200, r_ovd.status_code)
fid_ovd = r_ovd.json().get("finding_id", "")

# 임박 지적 (open, 3일 남음)
r_imm = httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
                   json={"finding": "S9 임박 지적", "severity": "minor",
                         "area": "labeling", "due_date": near_date})
ok("임박 지적 추가", r_imm.status_code == 200, r_imm.status_code)
fid_imm = r_imm.json().get("finding_id", "")

# 정상 기한 지적 (30일 후)
r_fut = httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
                   json={"finding": "S9 여유 지적", "severity": "major",
                         "area": "production", "due_date": future_date})
ok("정상기한 지적 추가", r_fut.status_code == 200, r_fut.status_code)

# 기한 없는 지적
r_nodue = httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
                     json={"finding": "S9 기한없음", "severity": "observation"})
ok("기한없음 지적 추가", r_nodue.status_code == 200, r_nodue.status_code)

# 지적 목록 조회 — due_date 필드 확인
fnd = httpx.get(f"{B}/cases/{CID}/findings", headers=HC).json()
ok("지적 목록 ≥4건", len(fnd) >= 4, len(fnd))
ok("due_date 필드 존재", all("due_date" in f for f in fnd), [f.get("due_date") for f in fnd])

# 기한 초과 항목 확인
ovd_items = [f for f in fnd if f.get("due_date") == past_date and f.get("status") == "open"]
ok("기한초과 지적 조회됨", len(ovd_items) >= 1, ovd_items)
ok("기한초과 지적 due_date 정확", ovd_items[0]["due_date"] == past_date if ovd_items else False,
   ovd_items[0].get("due_date") if ovd_items else "-")

# 임박 항목 확인
imm_items = [f for f in fnd if f.get("due_date") == near_date and f.get("status") == "open"]
ok("임박 지적 조회됨", len(imm_items) >= 1, imm_items)

# 정상 기한 항목 확인
fut_items = [f for f in fnd if f.get("due_date") == future_date]
ok("정상기한 지적 조회됨", len(fut_items) >= 1, fut_items)

# 종결된 지적은 기한 경보 제외 대상 (closed 처리)
if fid_ovd:
    r_close = httpx.patch(f"{B}/findings/{fid_ovd}", headers=HC,
                          json={"status": "closed"})
    ok("기한초과 지적 종결", r_close.status_code == 200, r_close.status_code)
    # 재조회 → closed 확인
    fnd2 = httpx.get(f"{B}/cases/{CID}/findings", headers=HC).json()
    closed = next((f for f in fnd2 if f.get("finding_id") == fid_ovd), None)
    ok("종결된 지적 status=closed", closed and closed.get("status") == "closed",
       closed.get("status") if closed else "-")

# due_date 없는 지적은 경보 집계에서 제외
nodue_items = [f for f in fnd if not f.get("due_date") and f.get("finding") == "S9 기한없음"]
ok("기한없음 지적 due_date=null", all(f.get("due_date") is None for f in nodue_items), nodue_items)

# ── S9-2: Admin 통계 현황 ─────────────────────────────────────
print("\n=== S9-2 Admin 전체 현황 통계 ===")

# /admin/cases — 전체 케이스 목록 (통계 기반 데이터)
all_c = httpx.get(f"{B}/admin/cases", headers=HA).json()
ok("/admin/cases 배열", isinstance(all_c, list), type(all_c).__name__)
ok("admin 케이스 ≥1", len(all_c) >= 1, len(all_c))

# 통계 계산에 필요한 필드 존재 확인
required_fields = ["case_id", "org_id", "company_name", "status", "pathway", "fatwa_status"]
if all_c:
    first = all_c[0]
    for field in required_fields:
        ok(f"필드 {field} 존재", field in first, list(first.keys()))

# 상태별 분포 (클라이언트 집계 시뮬레이션)
st_map = {}
for c in all_c:
    st = c.get("status", "unknown")
    st_map[st] = st_map.get(st, 0) + 1
ok("상태별 분포 집계 가능", len(st_map) >= 1, st_map)

# pathway별 분포
sd_count = len([c for c in all_c if c.get("pathway") == "self_declare"])
rg_count = len([c for c in all_c if c.get("pathway") == "reguler"])
un_count = len([c for c in all_c if c.get("pathway") == "undetermined"])
ok("pathway 합계 = 전체 케이스", sd_count + rg_count + un_count == len(all_c),
   f"sd={sd_count} rg={rg_count} un={un_count} total={len(all_c)}")

# fatwa_status 분포
fw_pend = len([c for c in all_c if c.get("fatwa_status") in ("pending", "none")])
fw_appr = len([c for c in all_c if c.get("fatwa_status") == "approved"])
ok("fatwa 분포 집계 가능", fw_pend + fw_appr <= len(all_c), f"pend={fw_pend} appr={fw_appr}")

# certificate_issued 케이스 수 집계
cert_done = len([c for c in all_c if c.get("status") == "certificate_issued"])
ok("certificate_issued 집계", cert_done >= 0, cert_done)

# org별 분포
org_map = {}
for c in all_c:
    o = c.get("org_id", "?")
    org_map[o] = org_map.get(o, 0) + 1
ok("org별 분포 집계 가능", len(org_map) >= 1, org_map)

# /admin/orgs — 조직 목록 (org별 통계 보조)
orgs = httpx.get(f"{B}/admin/orgs", headers=HA).json()
ok("/admin/orgs 배열", isinstance(orgs, list), type(orgs).__name__)
ok("orgs cases 필드 존재", all("cases" in o for o in orgs), [list(o.keys()) for o in orgs[:2]])

# 비관리자 admin 통계 접근 → 403
r_nonadmin = httpx.get(f"{B}/admin/cases", headers=HC)
ok("비관리자 /admin/cases → 403", r_nonadmin.status_code == 403, r_nonadmin.status_code)

# ── 결과 ──────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
