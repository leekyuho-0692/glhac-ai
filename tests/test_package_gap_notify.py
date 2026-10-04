"""인증 패키지(업체·제품·성분·공장) 점검과 오디터 통보.

실측(아미라 밀크, 2026-09-23): 손글씨 기록물 등 9건이 신뢰도 0 으로 판독되지 않았는데 아무에게도
통보되지 않았다. 그리고 알림을 만들었더라도 조회가 '자기 조직'만이라 org_demo 소속 오디터는 업체
조직의 알림을 볼 수 없었다.

실행: <venv>/bin/python -m pytest tests/test_package_gap_notify.py -q
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


def _h(t):
    return {"Authorization": "Bearer " + t}


def _tok(c, u, p):
    return c.post("/auth/login", json={"username": u, "password": p}).json()["token"]


def _uid(name):
    db = SessionLocal()
    try:
        return db.query(models.User).filter_by(username=name).first().user_id
    finally:
        db.close()


def _case(c):
    """컨설턴트가 업체 신청 생성 → 업체 전용 조직(오디터·컨설턴트와 다른 조직)."""
    cons = _tok(c, "consultant1", "pw")
    j = c.post("/cases", json={"company_name": "아미라" + uuid.uuid4().hex[:6]}, headers=_h(cons)).json()
    return j["case_id"], j["org_id"], cons


def _add_doc(cid, name, conf):
    db = SessionLocal()
    try:
        d = models.DocumentAsset(case_id=cid, filename=name, doc_type="other", confidence=conf, review_status="pending")
        db.add(d)
        db.commit()
        return d.document_id
    finally:
        db.close()


def _run_check(cid):
    db = SessionLocal()
    try:
        chk = M._notify_package_gaps(db, db.get(models.CaseApplication, cid))
        db.commit()
        return chk
    finally:
        db.close()


def _gap_notifs(c, tok):
    return [n for n in c.get("/notifications", headers=_h(tok)).json() if n["event_type"] == "package.gap"]


def test_빈_신청은_네_항목이_모두_누락():
    with TestClient(app) as c:
        cid, org, cons = _case(c)
        d = c.get("/cases/%s/package-check" % cid, headers=_h(cons)).json()
        assert set(d["gaps"]) == {"company", "product", "material", "factory"} - (
            {"company"} if not d["items"]["company"]["missing"] else set())
        assert not d["complete"]


def test_판독불가_서류는_오디터에게_통보되고_오디터가_본다():
    with TestClient(app) as c:
        cid, org, cons = _case(c)
        assert org != "org_demo"
        op = _tok(c, "operator1", "pw")
        c.post("/ops/cases/%s/assign-auditor" % cid, json={"auditor_id": _uid("auditor1")}, headers=_h(op))
        _add_doc(cid, "18 생산결과기록_손글씨.pdf", 0.0)
        chk = _run_check(cid)
        assert len(chk["unreadable"]) == 1
        au = _tok(c, "auditor1", "pw")
        ns = _gap_notifs(c, au)
        assert ns and "판독 실패" in ns[0]["title"], ns          # org_demo 오디터가 업체 조직 알림을 본다
        assert not _gap_notifs(c, _tok(c, "auditor2", "pw"))   # 다른 오디터에게는 안 보인다
        assert _gap_notifs(c, cons)                              # 담당 컨설턴트도 받는다
        # 같은 내용은 반복 통보하지 않는다
        before = len(_gap_notifs(c, au))
        _run_check(cid)
        assert len(_gap_notifs(c, au)) == before


def test_오디터가_없으면_운영자에게_그리고_배정되면_오디터에게():
    with TestClient(app) as c:
        cid, _, _ = _case(c)
        _add_doc(cid, "손글씨.jpg", 0.1)
        _run_check(cid)
        assert _gap_notifs(c, _tok(c, "operator1", "pw"))
        op = _tok(c, "operator1", "pw")
        c.post("/ops/cases/%s/assign-auditor" % cid, json={"auditor_id": _uid("auditor1")}, headers=_h(op))
        assert any(cid == n["case_id"] for n in _gap_notifs(c, _tok(c, "auditor1", "pw")))


def test_수기_입력과_수기_확인으로_패키지_완비():
    with TestClient(app) as c:
        cid, _, cons = _case(c)
        doc = _add_doc(cid, "손글씨 재료구매기록.pdf", 0.0)
        H = _h(cons)
        assert c.patch("/cases/%s/profile" % cid, json={"nib": "1234567890", "responsible_person": "대표",
                                                        "address": "서울시 강남구 테헤란로 1 지엘빌딩", "factory_address": "경기도 광주시 곤지암읍 신만로 275-43"},
                       headers=H).status_code == 200
        assert c.post("/cases/%s/products" % cid, json={"name": "아이란"}, headers=H).status_code == 200
        assert c.post("/cases/%s/materials" % cid, json={"name": "원유"}, headers=H).status_code == 200
        d = c.get("/cases/%s/package-check" % cid, headers=H).json()
        assert d["gaps"] == [] and len(d["unreadable"]) == 1 and not d["complete"]
        op = _tok(c, "operator1", "pw")
        c.post("/ops/cases/%s/assign-auditor" % cid, json={"auditor_id": _uid("auditor1")}, headers=_h(op))
        au = _tok(c, "auditor1", "pw")
        assert c.patch("/documents/%s/review" % doc, json={"review_status": "approved"}, headers=_h(au)).status_code == 200
        assert c.get("/cases/%s/package-check" % cid, headers=_h(au)).json()["complete"]


def test_보완_요청():
    with TestClient(app) as c:
        cid, org, cons = _case(c)
        op = _tok(c, "operator1", "pw")
        c.post("/ops/cases/%s/assign-auditor" % cid, json={"auditor_id": _uid("auditor1")}, headers=_h(op))
        au = _tok(c, "auditor1", "pw")
        r = c.post("/cases/%s/package-check/request" % cid, json={"note": "원재료명을 입력해 주세요"}, headers=_h(au))
        assert r.status_code == 200, r.text
        assert any(n["event_type"] == "package.supplement_request" for n in c.get("/notifications", headers=_h(cons)).json())
        assert c.post("/cases/%s/package-check/request" % cid, json={}, headers=_h(cons)).status_code == 403


def test_화면_연결():
    root = os.path.dirname(os.path.dirname(__file__))
    html = open(os.path.join(root, "app/static/v4/index.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "app/static/v4/js/package-check.js"), encoding="utf-8").read()
    assert 'src="js/package-check.js"' in html
    for v in ("ent-home", "ent-apply", "cons-status", "aud-pre", "aud-detail"):
        assert "wrap('%s'" % v in js


def _add_raw_doc(cid, name, content_b64=None):
    """판독 전 서류(confidence 없음) — 판독 기능 이전 업로드 재현."""
    db = SessionLocal()
    try:
        d = models.DocumentAsset(case_id=cid, filename=name, doc_type="other", confidence=None,
                                 review_status="pending", content_b64=content_b64)
        db.add(d)
        db.commit()
        return d.document_id
    finally:
        db.close()


def test_미판독과_판독실패를_구분한다():
    with TestClient(app) as c:
        cid, _, cons = _case(c)
        _add_raw_doc(cid, "예전업로드1.pdf")
        _add_raw_doc(cid, "예전업로드2.pdf")
        _add_doc(cid, "손글씨.pdf", 0.0)
        d = c.get("/cases/%s/package-check" % cid, headers=_h(cons)).json()
        assert len(d["unprocessed"]) == 2 and len(d["unreadable"]) == 1
        chk = _run_check(cid)
        n = _gap_notifs(c, _tok(c, "operator1", "pw"))
        t = [x["title"] for x in n if x["case_id"] == cid][0]
        assert "미판독 서류 2건" in t and "판독 실패 서류 1건" in t, t


def test_미판독_일괄_재처리_큐():
    import base64
    with TestClient(app) as c:
        cid, _, cons = _case(c)
        _add_raw_doc(cid, "원본있음.txt", "data:text/plain;base64," + base64.b64encode("원재료: 원유".encode()).decode())
        _add_raw_doc(cid, "원본없음.pdf", None)
        r = c.post("/cases/%s/documents/reprocess-pending" % cid, headers=_h(cons))
        assert r.status_code == 200 and r.json()["found"] == 1, r.text
        assert c.post("/cases/%s/documents/reprocess-pending" % cid,
                      headers=_h(_tok(c, "applicant1", "pw"))).status_code == 403
