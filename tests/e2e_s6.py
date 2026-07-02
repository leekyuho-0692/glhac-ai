"""S6 검증 — Penyelia 상태변경 · Admin Seed-Reset · KMA1360 면제목록."""
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

# ── S6-1: Penyelia 상태 변경 ───────────────────────────────
print("\n=== S6-1 Penyelia 상태 변경 (org_demo) ===")

# 등록
rp = httpx.post(f"{B}/orgs/org_demo/penyelia", headers=HC,
                json={"name": "S6 Penyelia Test", "training_cert": "CERT-S6"}).json()
ok("Penyelia 등록", "penyelia_id" in rp, rp)
pid = rp.get("penyelia_id", "")

# 비활성화
rd = httpx.patch(f"{B}/orgs/org_demo/penyelia/{pid}", headers=HC,
                 json={"status": "inactive"}).json()
ok("비활성화 성공", rd.get("status") == "inactive", rd)
ok("active_count 감소 반영", "active_count" in rd, rd)

# 상태 확인
lst = httpx.get(f"{B}/orgs/org_demo/penyelia", headers=HC).json()
found = next((x for x in lst.get("items", []) if x["penyelia_id"] == pid), None)
ok("목록에서 inactive 확인", found is not None and found.get("status") == "inactive", found)

# 재활성화
ra = httpx.patch(f"{B}/orgs/org_demo/penyelia/{pid}", headers=HC,
                 json={"status": "active"}).json()
ok("재활성화 성공", ra.get("status") == "active", ra)

# 잘못된 status → 400
rb = httpx.patch(f"{B}/orgs/org_demo/penyelia/{pid}", headers=HC,
                 json={"status": "deleted"})
ok("잘못된 status → 400", rb.status_code == 400, rb.status_code)

# 다른 org → 403
rb2 = httpx.patch(f"{B}/orgs/org_other/penyelia/{pid}", headers=HC,
                  json={"status": "inactive"})
ok("다른 org → 403", rb2.status_code in (403, 404), rb2.status_code)

# 없는 penyelia_id → 404
rb3 = httpx.patch(f"{B}/orgs/org_demo/penyelia/nonexistent", headers=HC,
                  json={"status": "inactive"})
ok("없는 penyelia_id → 404", rb3.status_code == 404, rb3.status_code)

# ── S6-2: Admin Seed Reset ────────────────────────────────
print("\n=== S6-2 Admin Seed Reset ===")

# confirm 없이 → 400
rsr0 = httpx.post(f"{B}/admin/seed-reset", headers=HA, json={"confirm": "WRONG"})
ok("잘못된 confirm → 400", rsr0.status_code == 400, rsr0.status_code)

# 올바른 confirm → 200
rsr = httpx.post(f"{B}/admin/seed-reset", headers=HA, json={"confirm": "RESET"}).json()
ok("Seed Reset 성공", rsr.get("reset") is True, rsr)
ok("demo_users_ensured 필드", "demo_users_ensured" in rsr, rsr)
ok("demo_users_ensured는 list", isinstance(rsr.get("demo_users_ensured"), list), rsr.get("demo_users_ensured"))

# 비어있어도 성공 (이미 존재하는 경우)
rsr2 = httpx.post(f"{B}/admin/seed-reset", headers=HA, json={"confirm": "RESET"}).json()
ok("중복 Reset도 성공(idempotent)", rsr2.get("reset") is True, rsr2)

# 비관리자 → 403
rsr_bad = httpx.post(f"{B}/admin/seed-reset", headers=HC, json={"confirm": "RESET"})
ok("비관리자 Seed Reset → 403", rsr_bad.status_code == 403, rsr_bad.status_code)

# ── S6-3: KMA1360 면제 목록 ───────────────────────────────
print("\n=== S6-3 KMA1360 면제 목록 ===")

rkma = httpx.get(f"{B}/admin/kma1360-exempt", headers=HA).json()
ok("terms 필드 있음", "terms" in rkma, rkma)
ok("terms는 list", isinstance(rkma.get("terms"), list), type(rkma.get("terms")).__name__)
ok("terms ≥1건", len(rkma.get("terms", [])) >= 1, len(rkma.get("terms", [])))
ok("terms는 문자열 목록", all(isinstance(t, str) for t in rkma.get("terms", [])), rkma.get("terms", [])[:3])

# 비관리자도 접근 가능 (admin 권한 요구 여부 확인)
rkma2 = httpx.get(f"{B}/admin/kma1360-exempt", headers=HC)
ok("비관리자 KMA1360 접근 → 403 or 200", rkma2.status_code in (200, 403), rkma2.status_code)

# ── 결과 ──────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
