"""P0-4 정본(canonical) 3개국어 문서 + 취합본(D-18) 생성/검증.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_canonical_docs.py -q

주의(테스트 격리): 고유 DB(glhac_canonical_test.db)를 app import 전에 강제 대입해 격리한다.
"""
import os
import sys
import tempfile
import hashlib
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_canonical_test.db"   # setdefault 금지 — 격리 강제
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드(테스트 전용)
os.environ.setdefault("GLHAC_UPLOAD_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mkcase(c, admin):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "AuditRepCo"},
                  headers=_h(admin)).json()["case_id"]


def _notif_count(case_id, role):
    db = SessionLocal()
    try:
        return db.query(models.Notification).filter_by(case_id=case_id, role=role).count()
    finally:
        db.close()


def _event_count(case_id, action):
    db = SessionLocal()
    try:
        return db.query(models.WorkflowEvent).filter_by(case_id=case_id, action=action).count()
    finally:
        db.close()


def _make_approved_report(c, admin, cid):
    """audit-report 생성 → approve → gen_doc_id 반환."""
    r = c.post(f"/cases/{cid}/audit-report", headers=_h(admin))
    assert r.status_code == 200, r.text
    gid = r.json()["gen_doc_id"]
    ap = c.post(f"/gen-docs/{gid}/approve", headers=_h(admin))
    assert ap.status_code == 200, ap.text
    return gid


def _make_sjph_complete(c, admin, cid):
    """HPAS 5요소 ok + PenyeliaHalal active — SJPH complete 게이트 충족."""
    for el in ("commitment", "materials", "process", "product", "monitoring"):
        rr = c.patch(f"/cases/{cid}/sjph", json={"element": el, "status": "ok"}, headers=_h(admin))
        assert rr.status_code == 200, rr.text
    pr = c.post("/orgs/org_demo/penyelia", json={"name": "Pen Halal A"}, headers=_h(admin))
    assert pr.status_code == 200, pr.text


def _send_fatwa(c, aud, cid):
    c.post(f"/cases/{cid}/audit-report/sign", json={"name": "Auditor Kim"}, headers=_h(aud))
    return c.post(f"/cases/{cid}/audit-report/send-fatwa", headers=_h(aud))


def test_상정시_3개국어_정본과_취합본():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        _make_approved_report(c, admin, cid)
        _make_sjph_complete(c, admin, cid)
        r = _send_fatwa(c, aud, cid)
        assert r.status_code == 200, r.text
        body = r.json()
        can = body["canonical"]
        assert can["doc"] == "D-17"
        assert can["langs"] == ["en", "id", "ko"]
        assert can["canonical_lang"] == "en"
        assert set(can["sha256"].keys()) == {"en", "id", "ko"}
        fp = body["final_package"]
        assert fp["doc"] == "D-18"
        assert fp["gen_doc_id"]
        assert fp["sha256"]


def test_정본_다운로드_지문_일치_권한():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        con = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin)

        r0 = c.get(f"/cases/{cid}/canonical/D-17.docx", params={"lang": "en"}, headers=_h(admin))
        assert r0.status_code == 404, r0.text
        assert r0.json()["detail"]["code"] == "CANONICAL_NOT_ISSUED"

        _make_approved_report(c, admin, cid)
        _make_sjph_complete(c, admin, cid)
        assert _send_fatwa(c, aud, cid).status_code == 200

        canon = c.get(f"/cases/{cid}/canonical", headers=_h(admin)).json()
        d17 = canon["d17"]
        for lang in ("en", "id", "ko"):
            dl = c.get(f"/cases/{cid}/canonical/D-17.docx?lang={lang}", headers=_h(admin))
            assert dl.status_code == 200, dl.text
            assert "wordprocessingml" in dl.headers["content-type"]
            assert hashlib.sha256(dl.content).hexdigest() == d17["sha256"][lang]

        bad = c.get(f"/cases/{cid}/canonical/D-17.docx?lang=xx", headers=_h(admin))
        assert bad.status_code == 422, bad.text

        forb = c.get(f"/cases/{cid}/canonical/D-17.docx?lang=en", headers=_h(con))
        assert forb.status_code == 403, forb.text
        assert forb.json()["detail"]["code"] == "DOC_VIEW_ONLY"


def test_기업_확인_서명_검증():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        app_tok = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)

        early = c.post(f"/cases/{cid}/audit-report/client-sign",
                       json={"name": "Boss", "image": "data:image/png;base64,iVBORw0KGgo="},
                       headers=_h(app_tok))
        assert early.status_code == 409, early.text
        assert early.json()["detail"]["code"] == "REPORT_NOT_APPROVED"

        _make_approved_report(c, admin, cid)

        noimg = c.post(f"/cases/{cid}/audit-report/client-sign",
                       json={"name": "Boss"}, headers=_h(app_tok))
        assert noimg.status_code == 422, noimg.text
        assert noimg.json()["detail"]["code"] == "NAME_AND_IMAGE_REQUIRED"

        ok = c.post(f"/cases/{cid}/audit-report/client-sign",
                    json={"name": "Boss", "image": "data:image/png;base64,iVBORw0KGgo="},
                    headers=_h(app_tok))
        assert ok.status_code == 200, ok.text

        _make_sjph_complete(c, admin, cid)
        assert _send_fatwa(c, aud, cid).status_code == 200

        canon = c.get(f"/cases/{cid}/canonical", headers=_h(admin)).json()
        assert canon["client_signed"] is True

        fd = json.loads(c.get(f"/gen-docs/{canon['d18']['gen_doc_id']}", headers=_h(admin)).json()["content"])
        assert fd["doc"] == "D-18"
        assert any(it.get("doc_type") == "audit_report_canonical" for it in fd["items"])
        assert fd["notice"]["en"]


def test_SRS모드_기업서명_게이트():
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            aud = _tok(c, "auditor1", "pw")
            app_tok = _tok(c, "applicant1", "pw")
            cid = _mkcase(c, admin)
            _make_approved_report(c, admin, cid)
            _make_sjph_complete(c, admin, cid)

            blocked = _send_fatwa(c, aud, cid)
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["detail"]["code"] == "FATWA_GATE_BLOCKED"
            assert "CLIENT_SIGN_REQUIRED" in blocked.json()["detail"]["missing"]

            sg = c.post(f"/cases/{cid}/audit-report/client-sign",
                        json={"name": "Boss", "image": "data:image/png;base64,iVBORw0KGgo="},
                        headers=_h(app_tok))
            assert sg.status_code == 200, sg.text

            ok = _send_fatwa(c, aud, cid)
            assert ok.status_code == 200, ok.text
    finally:
        os.environ.pop("GLHAC_FORMAL_FLOW", None)


def test_재상정_버전증가():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        _make_approved_report(c, admin, cid)
        _make_sjph_complete(c, admin, cid)

        assert _send_fatwa(c, aud, cid).status_code == 200
        assert _send_fatwa(c, aud, cid).status_code == 200

        canon = c.get(f"/cases/{cid}/canonical", headers=_h(admin)).json()
        assert canon["d17"]["version"] == 2

        v1 = c.get(f"/cases/{cid}/canonical/D-17.docx?lang=en&version=1", headers=_h(admin))
        assert v1.status_code == 200, v1.text
