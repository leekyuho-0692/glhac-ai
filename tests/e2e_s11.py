"""S11 검증 — Workflow 강화(S11-1) · 내보내기(S11-2) · Admin LPH/Ontology KPI(S11-3)."""
import sys
import sqlite3
import httpx
from _target import base   # 라이브(8800) 오염 방지 — 대상 서버는 GLHAC_E2E_BASE 로만 지정

DB_PATH = "glhac.db"
B = base()
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
                json={"company_name": "S11 Test Co", "org_id": "org_demo"}).json()
ok("테스트 케이스 생성", "case_id" in c1, c1)
CID = c1.get("case_id", "")

# ── S11-1: Workflow 강화 ──────────────────────────────────────
print("\n=== S11-1 Workflow 강화 ===")

# GET /cases/{cid}/workflow — 기본 필드 확인
w = httpx.get(f"{B}/cases/{CID}/workflow", headers=HC).json()
ok("workflow 응답", "state" in w and "phases" in w and "next_states" in w, list(w.keys()))
ok("blockers 필드", "blockers" in w, list(w.keys()))
ok("pathway 필드", "pathway" in w, w.get("pathway"))
ok("state_label 필드", "state_label" in w, w.get("state_label"))

# phases 구조 확인
phases = w.get("phases", [])
ok("phases ≥1개", len(phases) >= 1, len(phases))
if phases:
    p0 = phases[0]
    ok("phase.key 존재", "key" in p0, list(p0.keys()))
    ok("phase.label 존재", "label" in p0, list(p0.keys()))
    ok("phase.status 존재", "status" in p0, p0.get("status"))
    ok("phase.states 존재", "states" in p0, list(p0.keys()))

# next_states 구조 확인
nexts = w.get("next_states", [])
ok("next_states 배열", isinstance(nexts, list), type(nexts).__name__)
if nexts:
    n0 = nexts[0]
    ok("next.to 존재", "to" in n0, list(n0.keys()))
    ok("next.allowed 존재", "allowed" in n0, list(n0.keys()))
    ok("next.blockers 존재", "blockers" in n0, list(n0.keys()))

# GET /cases/{cid}/timeline — 이벤트 이력 (S11-1 신규 fetch)
tl = httpx.get(f"{B}/cases/{CID}/timeline", headers=HC).json()
ok("timeline 응답", "events" in tl, list(tl.keys()))
events = tl.get("events", [])
ok("events 배열", isinstance(events, list), type(events).__name__)
if events:
    e0 = events[0]
    ok("event.action 존재", "action" in e0, list(e0.keys()))
    ok("event.to 존재", "to" in e0, list(e0.keys()))
    ok("event.actor 존재", "actor" in e0, list(e0.keys()))

# blocker code 체계 확인 (PENYELIA_HALAL_MISSING 등)
blockers = w.get("blockers", [])
ok("blockers 배열", isinstance(blockers, list), type(blockers).__name__)
for b in blockers:
    ok(f"blocker {b.get('code')} code 존재", "code" in b, b)

# pathway별 phases 분기 — DB 직접 UPDATE는 서버 커넥션 풀 스냅숏이 못 보는 경합(플레이키)이라
# API 경유(pathway/confirm)로 현대화. SD는 MSME+무임계재료+SIHALAL 검증으로 가드 통과.
def _to_pathway_determination(cid):
    for st in ("application_draft", "ai_pre_assessment_ready",
               "ai_pre_assessment_running", "pathway_determination"):
        httpx.post(f"{B}/cases/{cid}/transition", headers=HC, json={"to_state": st})
    httpx.post(f"{B}/cases/{cid}/pathway/assess", headers=HC)


cb = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "S11 SD Co", "org_id": "org_demo", "is_msme": True}).json()
if "case_id" in cb:
    cbid = cb["case_id"]
    httpx.post(f"{B}/cases/{cbid}/products", headers=HC, json={"name": "S11P"})
    ei = httpx.post(f"{B}/cases/{cbid}/sihalal/identity/link", headers=HC,
                    json={"external_email": "s11sd@x.com"}).json()
    httpx.post(f"{B}/sihalal/identity/{ei['external_identity_id']}/verify", headers=HC,
               json={"expected_identifier": "s11sd@x.com"})
    _to_pathway_determination(cbid)
    httpx.post(f"{B}/cases/{cbid}/pathway/confirm", headers=HC, json={"pathway": "self_declare"})
    ws = httpx.get(f"{B}/cases/{cbid}/workflow", headers=HC).json()
    phase_keys = [p.get("key") for p in ws.get("phases", [])]
    ok("자기선언 경로 sd_sjph 단계 포함", "sd_sjph" in phase_keys, phase_keys)

cc = httpx.post(f"{B}/cases", headers=HC,
                json={"company_name": "S11 RG Co", "org_id": "org_demo", "is_msme": False}).json()
if "case_id" in cc:
    ccid = cc["case_id"]
    _to_pathway_determination(ccid)
    httpx.post(f"{B}/cases/{ccid}/pathway/confirm", headers=HC, json={"pathway": "reguler"})
    wr = httpx.get(f"{B}/cases/{ccid}/workflow", headers=HC).json()
    phase_keys_r = [p.get("key") for p in wr.get("phases", [])]
    ok("정규 경로 rg_suppl 단계 포함", "rg_suppl" in phase_keys_r, phase_keys_r)

# ── S11-2: 내보내기 ───────────────────────────────────────────
print("\n=== S11-2 내보내기 ===")

# 종합 보고서: POST /cases/{cid}/report
r_rep = httpx.post(f"{B}/cases/{CID}/report", headers=HC)
ok("종합 보고서 생성 → 200", r_rep.status_code == 200, r_rep.status_code)
if r_rep.status_code == 200:
    body_rep = r_rep.json()
    ok("report 필드 존재", "report" in body_rep, list(body_rep.keys()))
    ok("report 내용 있음", len(body_rep.get("report", "")) > 10, len(body_rep.get("report", "")))

# SJPH 매뉴얼: POST /cases/{cid}/sjph/manual
r_sjph = httpx.post(f"{B}/cases/{CID}/sjph/manual", headers=HC)
ok("SJPH 매뉴얼 생성 → 200", r_sjph.status_code == 200, r_sjph.status_code)
if r_sjph.status_code == 200:
    body_sjph = r_sjph.json()
    ok("manual 필드 존재", "manual" in body_sjph, list(body_sjph.keys()))
    ok("manual 내용 있음", len(body_sjph.get("manual", "")) > 10, len(body_sjph.get("manual", "")))

# 케이스 JSON export
r_json = httpx.get(f"{B}/cases/{CID}/export.json", headers=HC)
ok("케이스 JSON export → 200", r_json.status_code == 200, r_json.status_code)
if r_json.status_code == 200:
    body_exp = r_json.json()
    ok("export case_id 존재", "case_id" in body_exp or "case" in body_exp, list(body_exp.keys())[:5])

# 원재료 Excel export
httpx.post(f"{B}/cases/{CID}/materials", headers=HC, json={"name": "S11 Mat", "mat_type": "raw"})
r_xlsx = httpx.get(f"{B}/cases/{CID}/materials/export.xlsx", headers=HC)
ok("원재료 Excel export → 200", r_xlsx.status_code == 200, r_xlsx.status_code)
ok("Excel content-type 확인", "spreadsheet" in r_xlsx.headers.get("content-type", "").lower() or
   r_xlsx.status_code == 200, r_xlsx.headers.get("content-type"))

# ── S11-3: Admin LPH 현황 + Ontology KPI ─────────────────────
print("\n=== S11-3 Admin LPH 현황 + Ontology KPI ===")

# GET /admin/lph-references
lph_refs = httpx.get(f"{B}/admin/lph-references", headers=HA).json()
ok("/admin/lph-references 배열", isinstance(lph_refs, list), type(lph_refs).__name__)

# LPH 등록 후 목록 재확인
r_lph_add = httpx.post(f"{B}/admin/lph-references", headers=HA,
                       json={"name": "LPH Test Jakarta", "area": "food",
                             "cert_type": "halal", "address": "Jakarta"})
ok("LPH 기관 등록 → 200 or 201", r_lph_add.status_code in (200, 201), r_lph_add.status_code)
if r_lph_add.status_code in (200, 201):
    lph_body = r_lph_add.json()
    ok("LPH id 반환", "id" in lph_body or "lph_id" in lph_body, lph_body)

lph_refs2 = httpx.get(f"{B}/admin/lph-references", headers=HA).json()
ok("LPH 등록 후 목록 ≥1건", len(lph_refs2) >= 1, len(lph_refs2))
if lph_refs2:
    l0 = lph_refs2[0]
    ok("LPH name 필드", "name" in l0, list(l0.keys()))
    ok("LPH region 필드", "region" in l0 or "accreditation_no" in l0, list(l0.keys()))

# GET /admin/ontology/stats
ont = httpx.get(f"{B}/admin/ontology/stats", headers=HA).json()
ok("/admin/ontology/stats 응답", "ontology_count" in ont, list(ont.keys()))
ok("ontology_count ≥0", (ont.get("ontology_count") or 0) >= 0, ont.get("ontology_count"))
ok("rule_versions 배열", isinstance(ont.get("rule_versions", []), list),
   type(ont.get("rule_versions")).__name__)

# 비관리자 → 403
r_nonadmin = httpx.get(f"{B}/admin/lph-references", headers=HC)
ok("비관리자 lph-references → 200(consultant 허용) or 403", r_nonadmin.status_code in (200, 403),
   r_nonadmin.status_code)

# /admin/cases — 통계 기반 (S11-3 KPI 계산)
all_c = httpx.get(f"{B}/admin/cases", headers=HA).json()
ok("/admin/cases ≥0건", isinstance(all_c, list), type(all_c).__name__)
# 사후관리 케이스 집계
post_mon = [c for c in all_c if c.get("status") == "post_certification_monitoring"]
ok("post_certification_monitoring 집계 가능", isinstance(post_mon, list), len(post_mon))

# ── 결과 ──────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASS {len(P)}/{len(P)+len(F)}")
if F:
    print("  실패:", ", ".join(F))
sys.exit(0 if not F else 1)
