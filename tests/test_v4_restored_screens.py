"""v4 복원 화면(항목 7) — 새 인증 신청 · 청구/결제 · 인증서 갱신 · 사용자/스태프 역할 관리.

v4 에서 빠져 있던 기능을 js/case-new.js · invoice.js · renew.js · users.js 로 되살렸다.
여기서는 그 화면이 호출하는 API 경로·페이로드가 실제로 동작하는지와, 모듈이 index.html 에
연결돼 있는지를 확인한다.

실행: <venv>/bin/python -m pytest tests/test_v4_restored_screens.py -q
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

from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

V4 = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "static", "v4")
MODULES = ["case-new.js", "invoice.js", "renew.js", "users.js"]


def _h(t):
    return {"Authorization": "Bearer " + t}


def _tok(c, u, p):
    return c.post("/auth/login", json={"username": u, "password": p}).json()["token"]


def _items(j):
    return j if isinstance(j, list) else (j.get("items") or [])


def _set_status(cid, st):
    db = SessionLocal()
    try:
        db.get(models.CaseApplication, cid).status = st
        db.commit()
    finally:
        db.close()


def _status(cid):
    db = SessionLocal()
    try:
        return db.get(models.CaseApplication, cid).status
    finally:
        db.close()


def _confirm_contract(cid):
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, cid)
        db.add(models.Contract(case_id=cid, org_id=c.org_id, status="confirmed", fee=1000))
        db.commit()
    finally:
        db.close()


def _new_case(c, tok, **kw):
    body = {"company_name": "PT 복원" + uuid.uuid4().hex[:6], "scheme": "product", "is_msme": True}
    body.update(kw)
    r = c.post("/cases", json=body, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


# ── 1. 새 인증 신청 ──
def test_새_신청_생성_후_생성자_목록에_보인다():
    with TestClient(app) as c:
        for user in ("applicant1", "consultant1"):
            t = _tok(c, user, "pw")
            d = _new_case(c, t)
            ids = [x["case_id"] for x in _items(c.get("/cases", headers=_h(t)).json())]
            assert d["case_id"] in ids
            assert d["status"] == "onboarding"


def test_물류_신청은_jasa_범위가_저장된다():
    with TestClient(app) as c:
        t = _tok(c, "applicant1", "pw")
        d = _new_case(c, t, scheme="logistics", logistics_scope=["penyimpanan", "pendistribusian"])
        assert d.get("scheme") == "logistics"
        db = SessionLocal()
        try:
            assert db.get(models.CaseApplication, d["case_id"]).logistics_scope == ["penyimpanan", "pendistribusian"]
        finally:
            db.close()


def test_새_신청은_오디터가_만들_수_없다():
    with TestClient(app) as c:
        t = _tok(c, "auditor1", "pw")
        assert c.post("/cases", json={"company_name": "X"}, headers=_h(t)).status_code == 403


# ── 2. 청구서 발행 → 결제 → paid + 모의심사 진입 ──
def test_청구서_발행_결제_후_paid_및_상담검토_케이스_전이():
    with TestClient(app) as c:
        ct = _tok(c, "consultant1", "pw")
        cid = _new_case(c, ct)["case_id"]
        items = [{"name": "기본 심사료", "qty": 1, "unit_price": 1000, "amount": 1000},
                 {"name": "출장비", "qty": 2, "unit_price": 250, "amount": 500}]
        # 계약 미확정이면 막힌다
        r = c.post("/cases/%s/invoices" % cid, json={"service_type": "pre_audit", "amount": 1500, "line_items": items}, headers=_h(ct))
        assert r.status_code == 409
        _confirm_contract(cid)
        # 신청자는 발행 불가(컨설턴트 전용)
        at = _tok(c, "applicant1", "pw")
        r = c.post("/cases/%s/invoices" % cid, json={"service_type": "pre_audit", "amount": 1500}, headers=_h(at))
        assert r.status_code == 403
        r = c.post("/cases/%s/invoices" % cid, json={"service_type": "pre_audit", "amount": 1500, "line_items": items}, headers=_h(ct))
        assert r.status_code == 200, r.text
        inv = r.json()
        assert inv["total"] == 1665.0 and inv["status"] == "waiting_payment"
        _set_status(cid, "consultant_review")
        r = c.post("/invoices/%s/payment" % inv["invoice_id"],
                   json={"method": "va", "amount": inv["total"], "depositor_name": "PT 복원", "reference": "TRX-1"},
                   headers=_h(ct))
        assert r.status_code == 200, r.text
        assert r.json()["invoice_status"] == "paid"
        invs = c.get("/cases/%s/invoices" % cid, headers=_h(ct)).json()
        assert [i["status"] for i in invs] == ["paid"]
        pays = c.get("/cases/%s/payments" % cid, headers=_h(ct)).json()
        assert len(pays) == 1 and pays[0]["status"] == "confirmed" and pays[0]["reference"] == "TRX-1"
        assert _status(cid) == "document_pre_audit_requested"


def test_계좌이체는_검증대기_후_운영자_확정으로_paid():
    with TestClient(app) as c:
        ct = _tok(c, "consultant1", "pw")
        cid = _new_case(c, ct)["case_id"]
        _confirm_contract(cid)
        inv = c.post("/cases/%s/invoices" % cid, json={"service_type": "onsite", "amount": 100}, headers=_h(ct)).json()
        _set_status(cid, "consultant_review")
        op = _tok(c, "operator1", "pw")
        r = c.post("/invoices/%s/payment" % inv["invoice_id"], json={"method": "bank_transfer", "amount": 50}, headers=_h(op))
        assert r.status_code == 200 and r.json()["invoice_status"] == "need_verification"
        assert _status(cid) == "consultant_review"
        adm = [i for i in c.get("/admin/payments", headers=_h(op)).json()["items"] if i["invoice_id"] == inv["invoice_id"]]
        assert adm and adm[0]["case_id"] == cid
        r = c.patch("/invoices/%s/status" % inv["invoice_id"], json={"status": "paid", "reason": "입금 확인"}, headers=_h(op))
        assert r.status_code == 200, r.text
        assert _status(cid) == "document_pre_audit_requested"


def test_간이_결제표시():
    with TestClient(app) as c:
        ct = _tok(c, "consultant1", "pw")
        nc = _new_case(c, ct)
        cid = nc["case_id"]
        _confirm_contract(cid)
        inv = c.post("/cases/%s/invoices" % cid, json={"service_type": "pre_audit", "amount": 10}, headers=_h(ct)).json()
        op = _tok(c, "operator1", "pw")
        assert c.patch("/invoices/%s/pay" % inv["invoice_id"], json={}, headers=_h(op)).status_code == 403
        # 신청은 업체 전용 조직에 있다 → 그 업체 계정으로 결제(데모 조직 applicant1 은 남의 업체라 403)
        acc = c.post("/orgs/%s/accounts" % nc["org_id"], json={"username": "pay" + cid[:8]}, headers=_h(ct)).json()
        at = _tok(c, acc["username"], acc["temp_password"])
        r = c.patch("/invoices/%s/pay" % inv["invoice_id"], json={}, headers=_h(at))
        assert r.status_code == 200 and r.json()["status"] == "paid"


# ── 3. 인증서 갱신: 신청 → 승인 ──
def test_갱신_신청_후_운영자_승인():
    with TestClient(app) as c:
        at = _tok(c, "applicant1", "pw")
        cid = _new_case(c, at)["case_id"]
        # 발급 전에는 신청 불가
        assert c.post("/cases/%s/renew/request" % cid, json={}, headers=_h(at)).status_code == 400
        _set_status(cid, "certificate_issued")
        r = c.post("/cases/%s/renew/request" % cid, json={"reason": "만료 임박"}, headers=_h(at))
        assert r.status_code == 200 and r.json()["renewal_requested"] is True
        # 신청자는 승인 불가, 운영자는 신청 불가(신청↔승인 분리)
        assert c.post("/cases/%s/renew" % cid, headers=_h(at)).status_code == 403
        op = _tok(c, "operator1", "pw")
        assert c.post("/cases/%s/renew/request" % cid, json={}, headers=_h(op)).status_code == 403
        r = c.post("/cases/%s/renew" % cid, headers=_h(op))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["parent_status"] == "renewal_preparation"
        ids = [x["case_id"] for x in _items(c.get("/cases", headers=_h(at)).json())]
        assert d["new_case_id"] in ids


# ── 4. 사용자·스태프 역할 관리 ──
def test_관리자_사용자_생성_역할변경_삭제():
    with TestClient(app) as c:
        ad = _tok(c, "admin", "admin")
        un = "u" + uuid.uuid4().hex[:8]
        r = c.post("/admin/users", json={"username": un, "password": "Passw0rd!x", "role": "auditor", "org_id": "org_demo"}, headers=_h(ad))
        assert r.status_code == 200, r.text
        uid = r.json()["user_id"]
        assert c.post("/admin/users", json={"username": un, "password": "Passw0rd!x", "role": "auditor"}, headers=_h(ad)).status_code == 409
        r = c.patch("/admin/users/%s" % uid, json={"role": "fatwa_liaison"}, headers=_h(ad))
        assert r.status_code == 200 and r.json()["role"] == "fatwa_liaison"
        assert c.patch("/admin/users/%s" % uid, json={"role": "nope"}, headers=_h(ad)).status_code == 422
        # 운영관리자는 사용자 관리 불가
        op = _tok(c, "operator1", "pw")
        assert c.post("/admin/users", json={"username": "x" + un, "password": "Passw0rd!x", "role": "applicant"}, headers=_h(op)).status_code == 403
        assert c.patch("/admin/users/%s" % uid, json={"role": "admin"}, headers=_h(op)).status_code == 403
        assert c.delete("/admin/users/%s" % uid, headers=_h(op)).status_code == 403
        assert c.delete("/admin/users/%s" % uid, headers=_h(ad)).status_code == 200
        users = c.get("/admin/users", headers=_h(ad)).json()
        assert uid not in [u["user_id"] for u in _items(users)]


def test_스태프_가입_오디터_신청_후_역할지정_승인():
    with TestClient(app) as c:
        un = "s" + uuid.uuid4().hex[:8] + "@ex.com"
        r = c.post("/auth/staff-signup", json={"username": un, "password": "Passw0rd!x", "requested_role": "auditor", "display_name": "심사원"})
        assert r.status_code == 200 and r.json()["requested_role"] == "auditor"
        rid = r.json()["request_id"]
        # 허용 외 역할은 거부
        assert c.post("/auth/staff-signup", json={"username": "z" + un, "password": "Passw0rd!x", "requested_role": "operator"}).status_code == 422
        ad = _tok(c, "admin", "admin")
        r = c.post("/admin/staff-signups/%s/approve" % rid, json={"role": "fatwa_liaison"}, headers=_h(ad))
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "fatwa_liaison"
        login = c.post("/auth/login", json={"username": un, "password": "Passw0rd!x"}).json()
        assert login["role"] == "fatwa_liaison"


# ── 5. 정적: 모듈 연결 ──
def test_복원_모듈이_index에_연결돼_있다():
    html = open(os.path.join(V4, "index.html"), encoding="utf-8").read()
    for m in MODULES:
        assert os.path.exists(os.path.join(V4, "js", m)), m
        assert '<script src="js/%s"></script>' % m in html, m
    # 메인 스크립트·기존 모듈 뒤에 로드돼야 VIEWS 래핑이 마지막에 적용된다
    assert html.index('js/legacy-link.js') < html.index('js/case-new.js')
    src = {m: open(os.path.join(V4, "js", m), encoding="utf-8").read() for m in MODULES}
    assert "apiFetch('/cases', {method:'POST'" in src["case-new.js"]
    assert "'/invoices/'+iid+'/payment'" in src["invoice.js"] and "/renew/request" in src["renew.js"]
    assert "'/admin/users'" in src["users.js"] and "staff-signups/'+id+'/approve'" in src["users.js"]
