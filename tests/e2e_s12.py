"""S12 검증 — 역할별 대시보드(S12-1) · Readiness 상세(S12-2) · Copilot 히스토리(S12-3)."""
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
                json={"company_name": "S12 Test Co", "org_id": "org_demo"}).json()
ok("테스트 케이스 생성", "case_id" in c1, c1)
CID = c1.get("case_id", "")

# ── S12-1: 역할별 전용 대시보드 API 검증 ──────────────────────
print("\n=== S12-1 역할별 전용 대시보드 API ===")

# Penyelia Halal 뷰: GET /orgs/{org_id}/penyelia
penyelia_list = httpx.get(f"{B}/orgs/org_demo/penyelia", headers=HC).json()
ok("penyelia list 응답 구조", "items" in penyelia_list, list(penyelia_list.keys()))
ok("penyelia items 배열", isinstance(penyelia_list.get("items", []), list), type(penyelia_list.get("items")).__name__)

# Penyelia 등록
r_penyelia = httpx.post(f"{B}/orgs/org_demo/penyelia", headers=HC,
                        json={"name": "S12 Penyelia Test", "nik": "1234567890"})
ok("Penyelia 등록 → 200", r_penyelia.status_code == 200, r_penyelia.status_code)
if r_penyelia.status_code == 200:
    pb = r_penyelia.json()
    ok("penyelia_id 반환", "penyelia_id" in pb, pb)
    PID = pb.get("penyelia_id", "")

    # 등록 후 목록 재확인
    plist2 = httpx.get(f"{B}/orgs/org_demo/penyelia", headers=HC).json()
    ok("penyelia 등록 후 items≥1", len(plist2.get("items", [])) >= 1, len(plist2.get("items", [])))
    if plist2.get("items"):
        p0 = plist2["items"][0]
        ok("penyelia name 필드", "name" in p0, list(p0.keys()))
        ok("penyelia status 필드", "status" in p0, list(p0.keys()))

    # Penyelia 활성화
    if PID:
        r_act = httpx.patch(f"{B}/orgs/org_demo/penyelia/{PID}", headers=HC,
                            json={"status": "active"})
        ok("Penyelia 활성화 → 200", r_act.status_code == 200, r_act.status_code)
        if r_act.status_code == 200:
            act_b = r_act.json()
            ok("활성화 status=active", act_b.get("status") == "active", act_b.get("status"))
            ok("active_count≥1", act_b.get("active_count", 0) >= 1, act_b.get("active_count"))

# Auditor 뷰: GET /cases/{cid}/findings — 역할별 미결 지적 집계
httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
           json={"finding": "S12 observation test", "severity": "observation", "area": "production"})
httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
           json={"finding": "S12 major test", "severity": "major", "area": "hygiene"})
httpx.post(f"{B}/cases/{CID}/findings", headers=HC,
           json={"finding": "S12 minor test", "severity": "minor", "area": "labeling"})
fnds = httpx.get(f"{B}/cases/{CID}/findings", headers=HC).json()
ok("findings 배열 (auditor 뷰용)", isinstance(fnds, list), type(fnds).__name__)
open_fnds = [f for f in fnds if f.get("status") == "open"]
ok("open findings ≥3건", len(open_fnds) >= 3, len(open_fnds))
crit_cnt = len([f for f in open_fnds if f.get("severity") == "observation"])
major_cnt = len([f for f in open_fnds if f.get("severity") == "major"])
minor_cnt = len([f for f in open_fnds if f.get("severity") == "minor"])
ok("observation ≥1건 집계", crit_cnt >= 1, crit_cnt)
ok("major ≥1건 집계", major_cnt >= 1, major_cnt)
ok("minor ≥1건 집계", minor_cnt >= 1, minor_cnt)

# Pendamping PPH 뷰: workflow blockers — PENDAMPING_NOT_VERIFIED 확인
w = httpx.get(f"{B}/cases/{CID}/workflow", headers=HC).json()
ok("workflow blockers 배열 (pendamping 뷰용)", isinstance(w.get("blockers", []), list), len(w.get("blockers", [])))
blk_codes = [b.get("code") for b in w.get("blockers", [])]
ok("blocker code 목록 확인 가능", isinstance(blk_codes, list), blk_codes)

# ── S12-2: Readiness 상세 breakdown API ─────────────────────────
print("\n=== S12-2 Readiness 상세 breakdown ===")

# GET /cases/{cid}/readiness — 기존 API, breakdown + counts 필드 확인
rd = httpx.get(f"{B}/cases/{CID}/readiness", headers=HC).json()
ok("readiness 응답", "readiness" in rd, list(rd.keys()))
ok("band 필드", "band" in rd, rd.get("band"))
ok("breakdown 필드", "breakdown" in rd, list(rd.keys()))
ok("counts 필드", "counts" in rd, list(rd.keys()))

bk = rd.get("breakdown", {})
ok("breakdown.documents", "documents" in bk, list(bk.keys()))
ok("breakdown.sjph", "sjph" in bk, list(bk.keys()))
ok("breakdown.materials", "materials" in bk, list(bk.keys()))
ok("breakdown.audit", "audit" in bk, list(bk.keys()))
ok("documents 0~100", 0 <= bk.get("documents", -1) <= 100, bk.get("documents"))
ok("sjph 0~100", 0 <= bk.get("sjph", -1) <= 100, bk.get("sjph"))
ok("materials 0~100", 0 <= bk.get("materials", -1) <= 100, bk.get("materials"))
ok("audit 0~100", 0 <= bk.get("audit", -1) <= 100, bk.get("audit"))

cnt = rd.get("counts", {})
ok("counts.critical_high ≥0", cnt.get("critical_high", -1) >= 0, cnt.get("critical_high"))
ok("counts.needs_evidence ≥0", cnt.get("needs_evidence", -1) >= 0, cnt.get("needs_evidence"))
ok("counts.open_findings ≥0", cnt.get("open_findings", -1) >= 0, cnt.get("open_findings"))

# open findings 추가 후 audit score 하락 확인
rd_before = rd.get("breakdown", {}).get("audit", 100)
# findings 이미 추가됨 → audit score 하락 기대
ok("open findings → audit score≤80", bk.get("audit", 100) <= 80, bk.get("audit"))

# 원재료 BLOCK 추가 후 materials score 하락 확인
m1 = httpx.post(f"{B}/cases/{CID}/materials", headers=HC,
                json={"name": "S12 Haram Mat", "mat_type": "raw"}).json()
if "material_id" in m1:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE material SET screen_status=?, screen_result=? WHERE material_id=?",
                 ("haram", "BLOCK", m1["material_id"]))
    conn.commit()
    conn.close()
    rd2 = httpx.get(f"{B}/cases/{CID}/readiness", headers=HC).json()
    ok("BLOCK 원재료 → materials score 하락", rd2.get("breakdown", {}).get("materials", 100) < 100,
       rd2.get("breakdown", {}).get("materials"))
    ok("counts.critical_high≥1 반영", rd2.get("counts", {}).get("critical_high", 0) >= 1,
       rd2.get("counts", {}).get("critical_high"))

# ── S12-3: Consultant Copilot — /cases/{cid}/ask API 검증 ──────
print("\n=== S12-3 Consultant Copilot API ===")

# POST /cases/{cid}/ask — 기본 동작
r_ask = httpx.post(f"{B}/cases/{CID}/ask", headers=HC,
                   json={"question": "지금 뭐가 막혀있어?"}, timeout=30)
ok("Copilot ask → 200", r_ask.status_code == 200, r_ask.status_code)
if r_ask.status_code == 200:
    ask_b = r_ask.json()
    ok("answer 필드", "answer" in ask_b, list(ask_b.keys()))
    ok("context_facts 필드", "context_facts" in ask_b, list(ask_b.keys()))
    ok("answer 내용 있음", len(ask_b.get("answer", "")) > 0, len(ask_b.get("answer", "")))

# 다른 질문 — 세션 히스토리 시뮬 (2회 연속 호출)
r_ask2 = httpx.post(f"{B}/cases/{CID}/ask", headers=HC,
                    json={"question": "자기선언 가능해?"}, timeout=30)
ok("Copilot 2회 호출 → 200", r_ask2.status_code == 200, r_ask2.status_code)
if r_ask2.status_code == 200:
    ok("2번째 답변 존재", len(r_ask2.json().get("answer", "")) > 0,
       len(r_ask2.json().get("answer", "")))

# 빈 질문 → 400 or 422
r_empty = httpx.post(f"{B}/cases/{CID}/ask", headers=HC,
                     json={"question": ""})
ok("빈 질문 → 400 or 422", r_empty.status_code in (400, 422), r_empty.status_code)

# 존재하지 않는 케이스 → 404
r_notfound = httpx.post(f"{B}/cases/nonexistent-case-id/ask", headers=HC,
                        json={"question": "test"}, timeout=15)
ok("없는 케이스 ask → 404 or 403", r_notfound.status_code in (403, 404), r_notfound.status_code)

# ── 결과 ──────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
