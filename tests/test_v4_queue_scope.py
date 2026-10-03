"""v4 연결 P1 — 업무 큐 범위 · 위원장 서명 형식.

인증기관 스태프(오디터·샤리아·컨설턴트)는 org_demo 소속이고, 업체 케이스는 가입한 업체의
조직에 있다. 큐 API 가 org_id 로 거르면서 다음이 실측됐다:
  · 오디터 배정함이 배정 후에도 비어 수락할 수 없었다(nav-counts 는 1)
  · 샤리아 파트와 큐가 fatwa_review 케이스를 못 봤다
  · 컨설턴트 문의함에 담당 고객 문의는 빠지고 같은 조직 다른 스태프의 문의가 보였다
그리고 v4 서명은 member 에 사용자명을 넣어 정족수의 '위원장 서명'이 성립하지 않았다.

실행: <venv>/bin/python -m pytest tests/test_v4_queue_scope.py -q
"""
import logging
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import main as M  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

PNG = "data:image/png;base64,iVBORw0KGgo="


def _h(t):
    return {"Authorization": "Bearer " + t}


def _tok(c, u, p):
    return c.post("/auth/login", json={"username": u, "password": p}).json()["token"]


def _new_company(c, invite=None):
    """업체 가입 → 자기 조직의 케이스 1건. (case_id, org_id, token)"""
    u = "co" + uuid.uuid4().hex[:8]
    body = {"username": u, "password": "EntPass!123", "company_name": "업체" + u}
    if invite:
        body["invite_code"] = invite
    j = c.post("/auth/register", json=body).json()
    cases = c.get("/cases", headers=_h(j["token"])).json()
    items = cases if isinstance(cases, list) else cases.get("items") or cases.get("cases")
    return items[0]["case_id"], j["org_id"], j["token"]


def _set_status(cid, st):
    db = SessionLocal()
    try:
        db.get(models.CaseApplication, cid).status = st
        db.commit()
    finally:
        db.close()


def _uid(username):
    db = SessionLocal()
    try:
        return db.query(models.User).filter_by(username=username).first().user_id
    finally:
        db.close()


def test_오디터_배정함에_다른_조직_업체_배정건이_보인다():
    with TestClient(app) as c:
        cid, org, _ = _new_company(c)
        assert org != "org_demo"
        _set_status(cid, "document_pre_audit_requested")
        op = _tok(c, "operator1", "pw")
        r = c.post("/ops/cases/%s/assign-auditor" % cid, json={"auditor_id": _uid("auditor1")}, headers=_h(op))
        assert r.status_code == 200, r.text
        au = _tok(c, "auditor1", "pw")
        j = c.get("/auditor/assignments?status=all", headers=_h(au)).json()
        assert cid in [i["case_id"] for i in j["items"]], j
        assert j["pending_count"] >= 1
        # 배정받지 않은 오디터에게는 안 보인다
        j2 = c.get("/auditor/assignments?status=all", headers=_h(_tok(c, "auditor2", "pw"))).json()
        assert cid not in [i["case_id"] for i in j2["items"]]


def test_샤리아_파트와_큐에_다른_조직_케이스가_보인다():
    with TestClient(app) as c:
        cid, _, _ = _new_company(c)
        _set_status(cid, "fatwa_review")
        q = c.get("/fatwa/queue", headers=_h(_tok(c, "fatwa1", "pw"))).json()
        assert cid in [i["case_id"] for i in q], q
        d = c.get("/fatwa/dashboard", headers=_h(_tok(c, "fatwa1", "pw"))).json()
        assert d is not None


def test_컨설턴트_문의함은_담당_고객_문의만_본다():
    with TestClient(app) as c:
        cons = _tok(c, "consultant1", "pw")
        inv = c.post("/consultant/invites", json={"company_name": "초대업체", "max_uses": 1},
                     headers=_h(cons)).json()
        code = inv.get("code") or (inv.get("invite") or {}).get("code")
        assert code, inv
        _, _, ent = _new_company(c, invite=code)
        c.post("/consultations", json={"subject": "고객문의", "message": "CoA 양식 문의"}, headers=_h(ent))
        c.post("/consultations", json={"subject": "직원문의", "message": "내부 메모"},
               headers=_h(_tok(c, "auditor1", "pw")))
        subjects = [r["subject"] for r in c.get("/consultations", headers=_h(cons)).json()]
        assert "고객문의" in subjects, subjects
        assert "직원문의" not in subjects, subjects


def test_위원장_서명_형식이면_정족수가_성립한다():
    with TestClient(app) as c:
        cid, _, _ = _new_company(c)
        fw = _tok(c, "fatwa1", "pw")
        # v4 수정 후 형식: 위원장은 member='chairman', 위원은 'member:이름', 이름은 name 으로
        for m, n in (("chairman", "위원장A"), ("member:위원B", "위원B")):
            r = c.post("/cases/%s/fatwa/sign" % cid, json={"member": m, "name": n, "image": PNG}, headers=_h(fw))
            assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            ok, missing, ctx = M._fatwa_quorum_ok(db, cid)
        finally:
            db.close()
        assert ok, (missing, ctx)


def test_예전_v4_서명_형식은_위원장이_없다():
    """회귀 방지용 대조 — 사용자명을 member 로 보내면 NO_CHAIRMAN."""
    with TestClient(app) as c:
        cid, _, _ = _new_company(c)
        fw = _tok(c, "fatwa1", "pw")
        for m in ("fatwa1", "operator1"):
            c.post("/cases/%s/fatwa/sign" % cid, json={"member": m, "image": PNG}, headers=_h(fw))
        db = SessionLocal()
        try:
            ok, missing, _ = M._fatwa_quorum_ok(db, cid)
        finally:
            db.close()
        assert not ok and "NO_CHAIRMAN" in missing
