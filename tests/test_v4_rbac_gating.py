"""v4 RBAC 게이트 — 403 나는 버튼을 그리지 않는다 + 동반자(PPH) 전용 역할.

· GET /rbac/actions 가 시드 역할별로 기대한 action 맵을 준다(UI CAN() 의 원천)
· /pendamping/assignments — pendamping1 이 컨설턴트가 배정한 건을 본다, 신청기업은 403
· 정적 계약: js/rbac.js SERVER_ROLES_FOR 표가 main.py 의 require_roles/require_action 과 일치
· 정적: 게이트 대상 버튼 코드가 canCall/CAN 을 거친다, pendamping_pph → 'pen' 역할

실행: <venv>/bin/python -m pytest tests/test_v4_rbac_gating.py -q
"""
import logging
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import rbac  # noqa: E402
from app.main import app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V4 = os.path.join(ROOT, "app", "static", "v4")
SEED = {"admin": ("admin", "admin"), "applicant": ("applicant1", "pw"), "consultant": ("consultant1", "pw"),
        "auditor": ("auditor1", "pw"), "fatwa_liaison": ("fatwa1", "pw"), "operator": ("operator1", "pw"),
        "penyelia_halal": ("penyelia1", "pw"), "pendamping_pph": ("pendamping1", "pw")}


def _h(t):
    return {"Authorization": "Bearer " + t}


def _tok(c, role):
    u, p = SEED[role]
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


# ---------- 1. 서버 매트릭스 ----------
def test_rbac_actions_per_seed_role():
    with TestClient(app) as c:
        for role in SEED:
            d = c.get("/rbac/actions", headers=_h(_tok(c, role))).json()
            assert d["role"] == role
            acts = d["actions"]
            assert set(acts) == set(rbac.ACTION_ROLES)
            for a, roles in rbac.ACTION_ROLES.items():
                assert acts[a] == (role == "admin" or role in roles), (role, a)
        # 대표 기대값(버튼 게이트가 기대는 것)
        get = lambda role: c.get("/rbac/actions", headers=_h(_tok(c, role))).json()["actions"]  # noqa: E731
        assert get("pendamping_pph")["pendamping.verify"] is True
        assert get("consultant")["pendamping.verify"] is False
        assert get("auditor")["material.add"] is False
        assert get("penyelia_halal")["material.add"] is True
        assert get("operator")["admin.user.manage"] is False
        assert all(get("admin").values())


# ---------- 2. 동반자 인박스 ----------
def test_pendamping_assignments_inbox():
    with TestClient(app) as c:
        cons, pen, app_t = _tok(c, "consultant"), _tok(c, "pendamping_pph"), _tok(c, "applicant")
        r = c.get("/pendamping/assignments", headers=_h(pen))
        assert r.status_code == 200 and "items" in r.json()
        assert c.get("/pendamping/assignments", headers=_h(app_t)).status_code == 403
        cid = c.post("/cases", json={"company_name": "PenCo"}, headers=_h(app_t)).json()["case_id"]
        r = c.post(f"/cases/{cid}/pendamping/assign", json={"pendamping_id": "pendamping1"}, headers=_h(cons))
        assert r.status_code == 200, r.text
        items = c.get("/pendamping/assignments", headers=_h(pen)).json()["items"]
        assert any(i["case_id"] == cid and i["decision"] is None for i in items)
        # 검증은 동반자만 — 컨설턴트 403, 동반자 200(본문 schemas.PendampingVerify)
        body = {"decision": "rework", "note": "보완", "signature_ref": None}
        assert c.post(f"/cases/{cid}/pendamping/verify", json=body, headers=_h(cons)).status_code == 403
        r = c.post(f"/cases/{cid}/pendamping/verify", json=body, headers=_h(pen))
        assert r.status_code == 200, r.text
        items = c.get("/pendamping/assignments", headers=_h(pen)).json()["items"]
        assert [i["decision"] for i in items if i["case_id"] == cid] == ["rework"]


# ---------- 3. 정적 계약: SERVER_ROLES_FOR ↔ main.py ----------
def _route_deps():
    """main.py: 'METHOD /path/{}' -> ('roles', set) | ('action', id) | ('any', None)."""
    src = _read(ROOT, "app", "main.py").split("\n")
    out = {}
    for i, line in enumerate(src):
        m = re.match(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', line)
        if not m:
            continue
        sig = " ".join(src[i + 1:i + 8])
        sig = sig[:sig.find("):") + 1] if "):" in sig else sig
        key = m.group(1).upper() + " " + re.sub(r"\{[^}]*\}", "{}", m.group(2))
        rr = re.search(r"require_roles\(([^)]*)\)", sig)
        ra = re.search(r'require_action\("([^"]+)"\)', sig)
        if ra:
            out[key] = ("action", ra.group(1))
        elif rr:
            out[key] = ("roles", set(re.findall(r'"(\w+)"', rr.group(1))) - {"admin"})
        else:
            out[key] = ("any", None)
    return out


def _client_table():
    js = _read(V4, "js", "rbac.js")
    body = js[js.index("const SERVER_ROLES_FOR"):js.index("};", js.index("const SERVER_ROLES_FOR"))]
    tab = {}
    for m in re.finditer(r"'([A-Z]+ [^']+)':\s*(\[[^\]]*\]|'[^']+')", body):
        v = m.group(2)
        tab[m.group(1)] = v.strip("'") if v.startswith("'") else set(re.findall(r"'(\w+)'", v))
    return tab


def test_client_gating_table_matches_server_routes():
    routes, tab = _route_deps(), _client_table()
    assert len(tab) >= 40
    for key, want in tab.items():
        assert key in routes, "rbac.js 에 있는데 main.py 에 없는 엔드포인트: " + key
        kind, val = routes[key]
        if isinstance(want, str):
            assert (kind, val) == ("action", want), (key, routes[key])
        else:
            assert kind == "roles" and val == want, (key, want, routes[key])


# ---------- 4. 정적: 버튼 경로가 게이트를 거친다 ----------
GATED = [  # (파일, 버튼/함수 표식, 게이트 표현)
    ("js/ent-apply-more.js", "App.entaRefund(", "canCall('POST','/invoices/{}/refund/request')"),
    ("js/ent-apply-more.js", "App.entaSaveDraft()", "canCall('POST','/cases/{}/save-draft')"),
    ("js/ent-apply-more.js", "enta-zip", "canCall('POST','/cases/{}/intake-zip-stream')"),
    ("js/ent-logistics.js", "App.entlSign(", "canCall('POST','/cleanings/{}/sign')"),
    ("js/ent-logistics.js", "App.entlAdd()", "canCall('POST','/orgs/{}/vehicles')"),
    ("js/ent-profile.js", "App.entpSaveProfile()", "canCall('PATCH','/cases/{}/profile')"),
    ("js/ent-facilities.js", "App.entfOrgSave(", "canCall('PUT','/cases/{}/halal-org')"),
    ("index.html", "App.docUpload(", "canCall('POST','/cases/{}/parse-file')"),
    ("index.html", "App.entAddProduct(", "canCall('POST','/cases/{}/products')"),
    ("index.html", "App.entSubmitApp(", "canCall('POST','/cases/{}/submit-application')"),
    ("index.html", "App.entResubmit(", "canCall('POST','/cases/{}/preassess/resubmit')"),
    ("index.html", "App.finalBundle(", "canCall('GET','/cases/{}/evidence-bundle.zip')"),
    ("index.html", "App.matAddR = ", "canCall('POST','/cases/{}/materials')"),
    ("index.html", "App.ccaRequestSignature(", "canCall('POST', '/cases/{}/contract/request-signature')"),
    ("js/aud-docproc.js", "App.auddRepro(", "canCall('POST','/documents/{}/reprocess')"),
    ("js/aud-docproc.js", "App.auddRecl(", "canCall('PATCH','/documents/{}/reclassify')"),
    ("js/aud-docproc.js", "App.auddApplyAll(", "canCall('POST','/cases/{}/pending-extractions/apply')"),
    ("js/aud-quant.js", "App.audqRename(", "canCall('PATCH','/materials/{}/rename')"),
    ("js/cons-contract-actions.js", "App.ccaRequestSignature'", "canCall('POST','/cases/{}/contract/request-signature')"),
    ("js/cons-contract-actions.js", "App.ccaReceive", "canCall('POST','/cases/{}/contract/receive')"),
    ("js/cons-contract-actions.js", "App.ccaConfirm'", "canCall('POST','/cases/{}/contract/confirm')"),
    ("js/adm-cert-more.js", "App.admcmLink(", "canCall('POST','/cases/{}/sihalal/identity/link')"),
    ("js/adm-cert-more.js", "App.admcmVerify(", "canCall('POST','/sihalal/identity/{}/verify')"),
    ("js/self-declare.js", "App.sdcConfirm(", "canCall('POST','/cases/{}/pathway/confirm')"),
    ("js/self-declare.js", "App.sdcAssign(", "canCall('POST','/cases/{}/pendamping/assign')"),
    ("js/self-declare.js", "App.sdcVerify(", "canCall('POST','/cases/{}/pendamping/verify')"),
    ("js/ops-org.js", "function OPSO_orgSection", "canCall('GET','/admin/orgs')"),
    ("js/ops-tenant.js", "App.opstActivateDomain(", "canCall('POST','/admin/orgs/{}/activate-domain')"),
    ("js/ops-audit.js", "OPSA_renderLogs()", "canCall('GET','/admin/audit-logs')"),
    ("js/ops-ref.js", "App.opsfNewLph()", "canCall('POST','/admin/lph-references')"),
    ("js/ops-ref.js", "App.opsfReseedConfirm()", "canCall('GET','/admin/ontology/stats')"),
    ("js/approvals.js", "App.aprvApprove(", "APRV_canDecide(it)"),
]


def test_gated_buttons_reference_helper():
    for path, marker, gate in GATED:
        src = _read(V4, path)
        assert marker in src, (path, marker)
        assert gate in src, (path, gate)
        # 게이트는 표식(버튼·함수) 가까이(같은 렌더 블록·함수 본문)에 있어야 한다
        pos = [m.start() for m in re.finditer(re.escape(marker), src)]
        gpos = [m.start() for m in re.finditer(re.escape(gate), src)]
        assert any(abs(p - g) < 2500 for p in pos for g in gpos), (path, marker, gate)
    # 승인함: 배지는 결정 가능한 건만
    assert "filter(APRV_canDecide).length" in _read(V4, "js", "approvals.js")


def test_rbac_loader_wired_and_pen_role():
    html = _read(V4, "index.html")
    # rbac.js 는 메인 인라인 스크립트 바로 다음 첫 모듈
    tail = html[html.index("</script>", html.index("const AUTH = {")):]
    first = re.search(r'<script src="(js/[^"]+)"', tail).group(1)
    assert first == "js/rbac.js"
    assert '<script src="js/pen-workspace.js"></script>' in html
    # 로그인·새로고침 공통(enterRole)에서 데이터보다 먼저 매트릭스를 불러온다
    er = html[html.index("async function enterRole"):html.index("async function restoreSession")]
    assert er.index("loadRbac") < er.index("loadAud")
    assert "pendamping_pph:'pen'" in html
    assert "pen:['pen-home']" in html
    assert "code:'pen'" in html and "home:'pen-home'" in html
    js = _read(V4, "js", "rbac.js")
    for name in ("function CAN(", "function canCall(", "async function loadRbac(", "/rbac/actions"):
        assert name in js
    pen = _read(V4, "js", "pen-workspace.js")
    assert "VIEWS['pen-home']" in pen and "/pendamping/assignments" in pen and "async function loadPen" in pen
    assert "'nav.pen-home'" in pen and "'ws.pen'" in pen
    # 관리자 회원 화면은 admin 전용으로 사이드바에서 숨김
    assert re.search(r"'adm-members':\s*\[\]", js)
