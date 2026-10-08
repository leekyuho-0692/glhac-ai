"""SRS FORMAL FLOW — 샤리아 위원회 정족수·투표·서명·위원장 확정."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_sharia_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_FORMAL_FLOW", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


client = TestClient(app)


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, admin):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "ShariaCo"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _set_status(cid, status):
    db = SessionLocal()
    try:
        ca = db.query(models.CaseApplication).filter_by(case_id=cid).first()
        ca.status = status
        db.commit()
    finally:
        db.close()


def _srs(on=True):
    os.environ["GLHAC_FORMAL_FLOW"] = "1" if on else "0"


def _committee3(c, fatwa):
    r = c.post("/fatwa/committee", headers=fatwa, json={"members": [
        {"role": "chair", "name": "Ahmad"},
        {"role": "member", "name": "Budi"},
        {"role": "member", "name": "Citra"},
    ]})
    assert r.status_code == 200, r.text
    return r.json()


def test_위원회_3인_고정():
    c = client
    admin = _tok(c, "admin", "admin")
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(True)
    try:
        # 2인 → 422 BAD_MEMBERS
        r = c.post("/fatwa/committee", headers=fatwa, json={"members": [
            {"role": "chair", "name": "Ahmad"},
            {"role": "member", "name": "Budi"},
        ]})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "BAD_MEMBERS", r.text
        # 4인 → 422 BAD_MEMBERS
        r = c.post("/fatwa/committee", headers=fatwa, json={"members": [
            {"role": "chair", "name": "Ahmad"},
            {"role": "member", "name": "Budi"},
            {"role": "member", "name": "Citra"},
            {"role": "member", "name": "Dedi"},
        ]})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "BAD_MEMBERS", r.text
        # chair 없음 → 422 CHAIR_REQUIRED
        r = c.post("/fatwa/committee", headers=fatwa, json={"members": [
            {"role": "member", "name": "Ahmad"},
            {"role": "member", "name": "Budi"},
            {"role": "member", "name": "Citra"},
        ]})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "CHAIR_REQUIRED", r.text
        # 정확히 3인 → 200
        out = _committee3(c, fatwa)
        assert out is not None
    finally:
        _srs(False)


def test_투표_검증_위원키_의견종류_사유():
    c = client
    admin = _tok(c, "admin", "admin")
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(True)
    try:
        _committee3(c, fatwa)
        cid = _mkcase(c, admin)
        _set_status(cid, "fatwa_review")
        # 위원 키 아님 → 422 NOT_COMMITTEE_MEMBER
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "nope", "vote": "approve"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "NOT_COMMITTEE_MEMBER", r.text
        # abstain → 422 OPINION_REQUIRED
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "chairman", "vote": "abstain"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "OPINION_REQUIRED", r.text
        # conditional 노트 없음 → 422 REASON_REQUIRED
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "member:Budi", "vote": "conditional"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "REASON_REQUIRED", r.text
        # reject 노트 없음 → 422 REASON_REQUIRED
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "member:Budi", "vote": "reject"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "REASON_REQUIRED", r.text
    finally:
        _srs(False)


def test_과반_결과_3가지():
    c = client
    admin = _tok(c, "admin", "admin")
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(True)
    try:
        _committee3(c, fatwa)
        # approve 2 + reject 1 → passed
        cid = _mkcase(c, admin)
        _set_status(cid, "fatwa_review")
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "chairman", "vote": "approve"})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "member:Budi", "vote": "approve"})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa,
                   json={"member": "member:Citra", "vote": "reject", "note": "미비"})
        assert r.status_code == 200, r.text
        assert r.json()["result"] == "passed", r.text
        # approve 1 + conditional 1 + reject 1 → conditional
        cid2 = _mkcase(c, admin)
        _set_status(cid2, "fatwa_review")
        r = c.post("/cases/%s/fatwa/vote" % cid2, headers=fatwa,
                   json={"member": "chairman", "vote": "approve"})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/fatwa/vote" % cid2, headers=fatwa,
                   json={"member": "member:Budi", "vote": "conditional", "note": "보완"})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/fatwa/vote" % cid2, headers=fatwa,
                   json={"member": "member:Citra", "vote": "reject", "note": "미비"})
        assert r.status_code == 200, r.text
        g = c.get("/cases/%s/fatwa/votes" % cid2, headers=fatwa)
        assert g.status_code == 200, g.text
        assert g.json()["result"] == "conditional", g.text
        # reject 2 + approve 1 → rejected
        cid3 = _mkcase(c, admin)
        _set_status(cid3, "fatwa_review")
        r = c.post("/cases/%s/fatwa/vote" % cid3, headers=fatwa,
                   json={"member": "member:Citra", "vote": "reject", "note": "미비"})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/fatwa/vote" % cid3, headers=fatwa,
                   json={"member": "member:Budi", "vote": "reject", "note": "미비"})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/fatwa/vote" % cid3, headers=fatwa,
                   json={"member": "chairman", "vote": "approve"})
        assert r.status_code == 200, r.text
        g = c.get("/cases/%s/fatwa/votes" % cid3, headers=fatwa)
        assert g.status_code == 200, g.text
        assert g.json()["result"] == "rejected", g.text
    finally:
        _srs(False)


def test_위원장_확정_선행조건과_D19():
    c = client
    admin = _tok(c, "admin", "admin")
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(True)
    try:
        _committee3(c, fatwa)
        cid = _mkcase(c, admin)
        _set_status(cid, "fatwa_review")
        # 의견 3개 미만 → 409 VOTES_INCOMPLETE
        r = c.post("/cases/%s/fatwa/confirm" % cid, headers=fatwa, json={"note": ""})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "VOTES_INCOMPLETE", r.text
        # 투표 3개
        c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa, json={"member": "chairman", "vote": "approve"})
        c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa, json={"member": "member:Budi", "vote": "approve"})
        c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa, json={"member": "member:Citra", "vote": "reject", "note": "미비"})
        # 서명 3개 미만 → 409 SIGNATURES_INCOMPLETE
        r = c.post("/cases/%s/fatwa/confirm" % cid, headers=fatwa, json={"note": ""})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "SIGNATURES_INCOMPLETE", r.text
        # 서명 3개
        for m in ("chairman", "member:Budi", "member:Citra"):
            r = c.post("/cases/%s/fatwa/sign" % cid, headers=fatwa,
                       json={"member": m, "image": "data:image/png;base64,iVBORw0KGgo=", "name": m})
            assert r.status_code == 200, r.text
        # 확정 성공
        r = c.post("/cases/%s/fatwa/confirm" % cid, headers=fatwa, json={"note": ""})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["decision"] in ("approved", "conditional", "rejected"), out
        assert out["decision_no"].startswith("FD-"), out
        assert out["gen_doc_id"], out
        assert out["fatwa_status"], out
        r = c.get("/cases/%s/gen-docs" % cid, headers=fatwa)
        assert r.status_code == 200, r.text
        docs = r.json()["items"] if isinstance(r.json(), dict) else r.json()
        assert any(d.get("doc_type") == "fatwa_decree" for d in docs), docs
        if out["decision"] == "approved":
            assert out["fatwa_status"] == "provisional", out
        if out["decision"] == "rejected":
            assert out["fatwa_status"] == "rejected", out
    finally:
        _srs(False)


def test_patch_fatwa_는_SRS_모드에서_차단():
    c = client
    admin = _tok(c, "admin", "admin")
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(True)
    try:
        _committee3(c, fatwa)
        cid = _mkcase(c, admin)
        _set_status(cid, "fatwa_review")
        r = c.patch("/cases/%s/fatwa" % cid, headers=fatwa, json={"decision": "approved"})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "USE_CHAIR_CONFIRM", r.text
    finally:
        _srs(False)


def test_안건_이력():
    c = client
    admin = _tok(c, "admin", "admin")
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(True)
    try:
        _committee3(c, fatwa)
        cid = _mkcase(c, admin)
        _set_status(cid, "fatwa_review")
        r = c.get("/sharia/agenda", headers=fatwa)
        assert r.status_code == 200, r.text
        ag = r.json()
        assert ag["quorum"] == 3, ag
        assert isinstance(ag["items"], list), ag
        assert any(it["case_id"] == cid for it in ag["items"]), ag
        # 확정까지
        c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa, json={"member": "chairman", "vote": "approve"})
        c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa, json={"member": "member:Budi", "vote": "approve"})
        c.post("/cases/%s/fatwa/vote" % cid, headers=fatwa, json={"member": "member:Citra", "vote": "approve"})
        for m in ("chairman", "member:Budi", "member:Citra"):
            c.post("/cases/%s/fatwa/sign" % cid, headers=fatwa,
                   json={"member": m, "image": "data:image/png;base64,iVBORw0KGgo=", "name": m})
        r = c.post("/cases/%s/fatwa/confirm" % cid, headers=fatwa, json={"note": ""})
        assert r.status_code == 200, r.text
        dno = r.json()["decision_no"]
        r = c.get("/sharia/history", headers=fatwa)
        assert r.status_code == 200, r.text
        hist = r.json()
        assert any(it.get("decision_no") == dno for it in hist["items"]), hist
    finally:
        _srs(False)


def test_플래그_off_면_기존동작():
    c = client
    fatwa = _tok(c, "fatwa1", "pw")
    _srs(False)
    r = c.post("/fatwa/committee", headers=fatwa, json={"members": [
        {"role": "chair", "name": "Ahmad"},
        {"role": "member", "name": "Budi"},
    ]})
    assert r.status_code == 200, r.text
