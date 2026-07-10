"""S8 검증 — 미니토론 섹션필터 · 갱신케이스 파생 · CaseList 필터."""
import os
import sys
import sqlite3
import httpx

DB_PATH = "glhac.db"


def force_status(case_id, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE case_application SET status=? WHERE case_id=?", (status, case_id))
    conn.commit()
    conn.close()

B = os.environ.get("GLHAC_E2E_BASE", "http://127.0.0.1:8800")
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

# 테스트용 케이스 생성
c1 = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "S8 Test Co", "org_id": "org_demo"}).json()
ok("테스트 케이스 생성", "case_id" in c1, c1)
CID = c1.get("case_id", "")

# ── S8-1: 미니토론 섹션 target 필터 ───────────────────────────
print("\n=== S8-1 미니토론 섹션 target 필터 ===")

# 여러 섹션에 토론 등록
targets = ["documents", "sjphEvaluation", "fatwa", "certificate", "sjphIntegration", "materials", "audit"]
for tgt in targets:
    r = httpx.post(f"{B}/cases/{CID}/discussions", headers=HC,
                   json={"text": f"S8 test comment for {tgt}", "target": tgt, "kind": "comment"})
    ok(f"토론 추가 ({tgt})", r.status_code == 200, r.status_code)

# 섹션별 필터 조회
for tgt in targets:
    r = httpx.get(f"{B}/cases/{CID}/discussions?target={tgt}", headers=HC).json()
    items = r if isinstance(r, list) else r.get("items", r)
    filtered_ok = all(d.get("target") == tgt for d in items if d.get("target"))
    ok(f"target={tgt} 필터 오염 없음", filtered_ok, [d.get("target") for d in items])
    ok(f"target={tgt} ≥1건", len(items) >= 1, len(items))

# target 없이 전체 조회 → 모든 섹션 포함
all_disc = httpx.get(f"{B}/cases/{CID}/discussions", headers=HC).json()
all_items = all_disc if isinstance(all_disc, list) else all_disc.get("items", all_disc)
ok("전체 조회 ≥7건", len(all_items) >= 7, len(all_items))

# ── S8-2: 갱신 케이스 파생 (POST /cases/{id}/renew) ──────────
print("\n=== S8-2 갱신 케이스 파생 ===")

# 갱신 실행(certificate.renew)은 operator 전용 — consultant는 403
r_role = httpx.post(f"{B}/cases/{CID}/renew", headers=HC)
ok("consultant 갱신 실행 → 403", r_role.status_code == 403, r_role.status_code)

# certificate_issued 상태가 아니면 400
r_notissued = httpx.post(f"{B}/cases/{CID}/renew", headers=HO)
ok("미발급 케이스 갱신 → 400", r_notissued.status_code == 400, r_notissued.status_code)
body_err = r_notissued.json()
ok("에러 코드 NOT_ISSUED", "NOT_ISSUED" in str(body_err), body_err)

# 제품·원재료 추가 후 SQLite 직접 갱신으로 certificate_issued 강제 설정
httpx.post(f"{B}/cases/{CID}/products", headers=HC, json={"name": "ProductA"})
httpx.post(f"{B}/cases/{CID}/materials", headers=HC, json={"name": "MatA", "mat_type": "raw"})
httpx.post(f"{B}/cases/{CID}/materials", headers=HC, json={"name": "MatB", "mat_type": "additive"})
force_status(CID, "certificate_issued")

# 상태 확인
detail = httpx.get(f"{B}/cases/{CID}", headers=HC).json()
ok("케이스 certificate_issued 상태", detail.get("status") == "certificate_issued", detail.get("status"))

# 갱신 케이스 생성 (operator)
r_renew = httpx.post(f"{B}/cases/{CID}/renew", headers=HO)
ok("갱신 케이스 생성 → 200", r_renew.status_code == 200, r_renew.status_code)
rn = r_renew.json()
ok("new_case_id 반환", "new_case_id" in rn, rn)
ok("parent_case_id 일치", rn.get("parent_case_id") == CID, rn.get("parent_case_id"))
ok("products_copied ≥1", rn.get("products_copied", 0) >= 1, rn.get("products_copied"))
ok("materials_copied ≥2", rn.get("materials_copied", 0) >= 2, rn.get("materials_copied"))

NEW_CID = rn.get("new_case_id", "")

# 갱신 케이스 상태 = onboarding
if NEW_CID:
    nd = httpx.get(f"{B}/cases/{NEW_CID}", headers=HC).json()
    ok("갱신 케이스 status=onboarding", nd.get("status") == "onboarding", nd.get("status"))
    ok("갱신 케이스 company_name Renewal 포함", "Renewal" in (nd.get("company_name") or ""), nd.get("company_name"))
    # 원재료 복사 확인
    mats = httpx.get(f"{B}/cases/{NEW_CID}/materials", headers=HC).json()
    ok("갱신 케이스 원재료 복사됨", len(mats) >= 2, len(mats))
    names = [m["name"] for m in mats]
    ok("MatA 복사됨", "MatA" in names, names)

# admin은 B.4.2 설계상 항상 통과 → 200 (renew 역할 제한 없음)
r_adm = httpx.post(f"{B}/cases/{CID}/renew", headers=HA)
ok("admin 갱신 시도 → 200 (B.4.2 admin bypass)", r_adm.status_code == 200, r_adm.status_code)

# 갱신 중복: 다시 renew 호출 → 400 (원본 케이스가 certificate_issued 이므로 다시 성공도 OK — 멱등 확인)
r_renew2 = httpx.post(f"{B}/cases/{CID}/renew", headers=HO)
ok("갱신 재호출 → 200 or 400", r_renew2.status_code in (200, 400), r_renew2.status_code)

# ── S8-3: CaseList 필터 (백엔드 /cases?pathway=&status= 또는 FE 클라이언트 필터) ──
print("\n=== S8-3 CaseList 필터 검증 ===")

# 다양한 케이스 생성
ca = httpx.post(f"{B}/cases", headers=HC, json={"company_name": "FilterSelf", "org_id": "org_demo"}).json()
cb = httpx.post(f"{B}/cases", headers=HC, json={"company_name": "FilterReg", "org_id": "org_demo"}).json()
ok("필터 케이스 A 생성", "case_id" in ca, ca)
ok("필터 케이스 B 생성", "case_id" in cb, cb)

# pathway 확정 — v3: /pathway/confirm은 가드 필요 → 필터 검증 목적상 DB에 직접 설정(자립 테스트)
_pconn = sqlite3.connect(DB_PATH)
if "case_id" in ca:
    _pconn.execute("UPDATE case_application SET pathway=? WHERE case_id=?", ("self_declare", ca["case_id"]))
if "case_id" in cb:
    _pconn.execute("UPDATE case_application SET pathway=? WHERE case_id=?", ("reguler", cb["case_id"]))
_pconn.commit()
_pconn.close()

# /cases 목록 — 봉투 {total,items}. FE 필터 기반이므로 items 필드 확인
env = httpx.get(f"{B}/cases", headers=HC, params={"limit": 500}).json()
ok("/cases 봉투 응답(total/items)", isinstance(env, dict) and "items" in env and "total" in env,
   list(env.keys()) if isinstance(env, dict) else type(env).__name__)
all_c = env.get("items", [])
ok("pathway 필드 있음", all(("pathway" in c) for c in all_c), len(all_c))
ok("status 필드 있음", all(("status" in c) for c in all_c), len(all_c))

# 클라이언트 사이드 필터 시뮬레이션 (self_declare 필터)
self_dec = [c for c in all_c if c.get("pathway") == "self_declare"]
ok("self_declare 케이스 ≥1", len(self_dec) >= 1, len(self_dec))

reguler = [c for c in all_c if c.get("pathway") == "reguler"]
ok("reguler 케이스 ≥1", len(reguler) >= 1, len(reguler))

# onboarding 상태 필터
onboarding = [c for c in all_c if c.get("status") == "onboarding"]
ok("onboarding 상태 케이스 존재", len(onboarding) >= 1, len(onboarding))

# certificate_issued 필터 (CID)
cert_done = [c for c in all_c if c.get("status") == "certificate_issued"]
ok("certificate_issued 케이스 존재", len(cert_done) >= 1, len(cert_done))

# ── 결과 ──────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
