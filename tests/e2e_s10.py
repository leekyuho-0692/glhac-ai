"""S10 검증 — 증빙 현황(S10-1) · 사후관리 전환(S10-2) · 검색필터 데이터(S10-3)."""
import os
import sys
import sqlite3
import httpx

DB_PATH = "glhac.db"
B = "http://127.0.0.1:%s" % os.environ.get("GLHAC_PORT", "8800")
P, F = [], []


def ok(n, c, x=""):
    (P if c else F).append(n)
    print(("  ✅ " if c else "  ❌ ") + n + ((" — " + str(x)[:120]) if x else ""))


def force_status(case_id, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE case_application SET status=? WHERE case_id=?", (status, case_id))
    conn.commit()
    conn.close()


HC = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "consultant1", "password": "pw"}).json()["token"]}
HA = {"Authorization": "Bearer " + httpx.post(f"{B}/auth/login",
      json={"username": "admin", "password": "admin"}).json()["token"]}

# 테스트 케이스 생성
c1 = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "S10 Test Co", "org_id": "org_demo"}).json()
ok("테스트 케이스 생성", "case_id" in c1, c1)
CID = c1.get("case_id", "")

# ── S10-1: 원재료 증빙 파일 목록뷰 ─────────────────────────────
print("\n=== S10-1 원재료 증빙 파일 목록뷰 ===")

# 원재료 추가
m1 = httpx.post(f"{B}/cases/{CID}/materials", headers=HC,
                json={"name": "Palm Oil", "mat_type": "raw"}).json()
m2 = httpx.post(f"{B}/cases/{CID}/materials", headers=HC,
                json={"name": "E471 Emulsifier", "e_number": "E471", "mat_type": "additive"}).json()
ok("원재료 Palm Oil 추가", "material_id" in m1, m1)
ok("원재료 E471 추가", "material_id" in m2, m2)
MID1 = m1.get("material_id", "")
MID2 = m2.get("material_id", "")

# 원재료 목록 — evidence_count 필드 확인
mats = httpx.get(f"{B}/cases/{CID}/materials", headers=HC).json()
ok("원재료 목록 ≥2건", len(mats) >= 2, len(mats))
ok("evidence_count 필드 존재", all("evidence_count" in m for m in mats), [list(m.keys()) for m in mats[:2]])
ok("초기 evidence_count=0", all((m.get("evidence_count") or 0) == 0 for m in mats), [m.get("evidence_count") for m in mats])

# 증빙 목록 조회 (아직 없음)
if MID1:
    evlist = httpx.get(f"{B}/cases/{CID}/materials/{MID1}/evidence", headers=HC).json()
    ok("증빙 없을 때 빈 배열", isinstance(evlist, list) and len(evlist) == 0, evlist)

# AI 스크리닝으로 NEEDS_EVIDENCE 상태 만들기 (직접 DB 업데이트)
if MID2:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE material SET screen_status=?, screen_result=? WHERE material_id=?",
                 ("mushbooh", "NEEDS_EVIDENCE", MID2))
    conn.commit()
    conn.close()
    # 재조회 → result 확인
    mats2 = httpx.get(f"{B}/cases/{CID}/materials", headers=HC).json()
    needs = [m for m in mats2 if m.get("result") == "NEEDS_EVIDENCE"]
    ok("NEEDS_EVIDENCE 원재료 조회됨", len(needs) >= 1, [m.get("name") for m in needs])
    ok("NEEDS_EVIDENCE 원재료 name 확인", any("E471" in (m.get("name") or "") for m in needs), [m.get("name") for m in needs])

# 증빙 업로드 (base64 더미)
if MID2:
    import base64
    dummy_b64 = base64.b64encode(b"dummy-pdf-content").decode()
    r_evup = httpx.post(f"{B}/cases/{CID}/materials/{MID2}/evidence", headers=HC,
                        json={"evidence_type": "halal_cert", "file_b64": dummy_b64, "filename": "cert.pdf"})
    ok("증빙 업로드", r_evup.status_code == 200, r_evup.status_code)
    if r_evup.status_code == 200:
        body_ev = r_evup.json()
        ok("업로드 후 screen_result 반환", "screen_result" in body_ev, body_ev)
        # evidence_count 갱신 확인
        mats3 = httpx.get(f"{B}/cases/{CID}/materials", headers=HC).json()
        mat_e471 = next((m for m in mats3 if MID2 and m.get("material_id") == MID2), None)
        ok("업로드 후 evidence_count≥1", mat_e471 and (mat_e471.get("evidence_count") or 0) >= 1,
           mat_e471.get("evidence_count") if mat_e471 else "-")

# ── S10-2: 사후관리 전환 ─────────────────────────────────────
print("\n=== S10-2 사후 관리 (post_certification_monitoring) 전환 ===")

# certificate_issued 상태로 강제 전환
force_status(CID, "certificate_issued")
detail = httpx.get(f"{B}/cases/{CID}", headers=HC).json()
ok("certificate_issued 강제 전환", detail.get("status") == "certificate_issued", detail.get("status"))

# POST /cases/{cid}/transition → post_certification_monitoring
r_mon = httpx.post(f"{B}/cases/{CID}/transition", headers=HC,
                   json={"to_state": "post_certification_monitoring", "action": "cert.start_monitoring"})
ok("사후 모니터링 착수 전환 → 200", r_mon.status_code == 200, r_mon.status_code)
if r_mon.status_code == 200:
    body_mon = r_mon.json()
    ok("from=certificate_issued", body_mon.get("from") == "certificate_issued", body_mon.get("from"))
    ok("to=post_certification_monitoring", body_mon.get("to") == "post_certification_monitoring", body_mon.get("to"))

# 현재 상태 확인
det2 = httpx.get(f"{B}/cases/{CID}", headers=HC).json()
ok("케이스 status=post_certification_monitoring", det2.get("status") == "post_certification_monitoring", det2.get("status"))

# post_cert_monitoring → renewal_preparation 전환
r_renp = httpx.post(f"{B}/cases/{CID}/transition", headers=HC,
                    json={"to_state": "renewal_preparation", "action": "monitoring.start_renewal"})
ok("갱신 준비 전환 → 200", r_renp.status_code == 200, r_renp.status_code)
if r_renp.status_code == 200:
    ok("to=renewal_preparation", r_renp.json().get("to") == "renewal_preparation", r_renp.json().get("to"))

# 잘못된 전환 → 409
force_status(CID, "onboarding")
r_bad = httpx.post(f"{B}/cases/{CID}/transition", headers=HC,
                   json={"to_state": "post_certification_monitoring"})
ok("onboarding→post_mon 잘못된 전환 → 409", r_bad.status_code == 409, r_bad.status_code)

# ── S10-3: Materials / Audit 검색·필터 데이터 ─────────────────
print("\n=== S10-3 Materials / Audit 검색·필터 데이터 확인 ===")

# Materials: 필터에 필요한 필드 (name, mat_type, result) 확인
mats_all = httpx.get(f"{B}/cases/{CID}/materials", headers=HC).json()
ok("Materials 배열", isinstance(mats_all, list), type(mats_all).__name__)
if mats_all:
    m0 = mats_all[0]
    ok("name 필드", "name" in m0, list(m0.keys()))
    ok("mat_type 필드", "mat_type" in m0, list(m0.keys()))
    ok("result 필드", "result" in m0, list(m0.keys()))

# Audit: severity/status 필드 확인 (기존 findings 활용)
# 새 케이스에 findings 추가
fnd_a = httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
                   json={"finding": "S10 major test", "severity": "major", "area": "production"})
fnd_b = httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
                   json={"finding": "S10 minor test", "severity": "minor", "area": "hygiene"})
ok("major 지적 추가", fnd_a.status_code == 200, fnd_a.status_code)
ok("minor 지적 추가", fnd_b.status_code == 200, fnd_b.status_code)

fnds = httpx.get(f"{B}/cases/{CID}/findings", headers=HC).json()
ok("Audit findings 배열", isinstance(fnds, list), type(fnds).__name__)
if fnds:
    f0 = fnds[0]
    ok("severity 필드", "severity" in f0, list(f0.keys()))
    ok("status 필드", "status" in f0, list(f0.keys()))
    ok("area 필드", "area" in f0, list(f0.keys()))

# severity 필터 시뮬레이션 (클라이언트 사이드)
majors = [f for f in fnds if f.get("severity") == "major"]
minors = [f for f in fnds if f.get("severity") == "minor"]
ok("major 지적 ≥1건", len(majors) >= 1, len(majors))
ok("minor 지적 ≥1건", len(minors) >= 1, len(minors))

# status 필터: open 기본값
opens = [f for f in fnds if f.get("status") == "open"]
ok("open 상태 지적 ≥1건", len(opens) >= 1, len(opens))

# closed 상태 필터 확인 — 하나 종결
if fnd_a.status_code == 200:
    fid_a = fnd_a.json().get("finding_id", "")
    if fid_a:
        httpx.patch(f"{B}/findings/{fid_a}", headers=HC, json={"status": "closed"})
        fnds2 = httpx.get(f"{B}/cases/{CID}/findings", headers=HC).json()
        closes = [f for f in fnds2 if f.get("status") == "closed"]
        ok("closed 필터 시뮬 ≥1건", len(closes) >= 1, len(closes))
        mixed = [f for f in fnds2 if f.get("status") == "open"]
        ok("open/closed 혼재 시뮬 가능", len(closes) >= 1 and len(mixed) >= 1,
           f"closed={len(closes)} open={len(mixed)}")

# ── 결과 ──────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
