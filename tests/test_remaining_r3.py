"""남은 항목 묶음 R3(운영·보안) 테스트 — R3-1 키 분리 · R3-2 의존성 lock · R3-3 연락처.

모든 테스트는 실제 함수/엔드포인트를 호출한다.
실행: <venv>/bin/python -m pytest tests/test_remaining_r3.py -q
"""
import hashlib
import hmac
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_remaining_r3_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_RATE_LIMIT_PER_MIN", "0")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, models, state_machine as sm  # noqa: E402
from app import main as m  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402


def _uniq():
    return uuid.uuid4().hex[:8]


# ══ R3-1. 감사 해시체인·인증서 서명 키 분리 ══════════════════════════════
def _new_case(db):
    c = models.CaseApplication(company_name="R3Co" + _uniq(), org_id="org_demo", status="draft")
    db.add(c)
    db.flush()
    return c


def _events(db, case_id):
    return (db.query(models.WorkflowEvent).filter_by(case_id=case_id)
            .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())


def test_r31_미설정이면_기존과_동일(monkeypatch):
    monkeypatch.delenv("GLHAC_AUDIT_KEY", raising=False)
    h = sm.chain_row_hash("p", "b")
    assert h == hmac.new(auth.SECRET, b"pb", hashlib.sha256).hexdigest()
    assert not h.startswith("k2:")
    assert sm.chain_row_matches("p", "b", h)


def test_r31_섞인_체인_무결_SECRET_회전에도_k2구간_유지(monkeypatch):
    db = SessionLocal()
    try:
        case = _new_case(db)
        monkeypatch.delenv("GLHAC_AUDIT_KEY", raising=False)
        sm.record_event(db, case, None, "draft", "old1")
        sm.record_event(db, case, "draft", "x", "old2")
        monkeypatch.setenv("GLHAC_AUDIT_KEY", "audit-key-r31-" + "z" * 24)
        sm.record_event(db, case, "x", "y", "new1")
        sm.record_event(db, case, "y", "z", "new2")
        db.commit()
        evs = _events(db, case.case_id)
        assert [e.row_hash.startswith("k2:") for e in evs] == [False, False, True, True]
        r = m._verify_wf_chain(evs)
        assert r["integrity_ok"] and r["verified"] == 4 and r["status"] == "ok", r
        # 위조: k2 행 본문 수정 → 탐지
        evs[3].payload = {"hacked": 1}
        r = m._verify_wf_chain(evs)
        assert not r["integrity_ok"] and r["break_at"] == evs[3].event_id, r
        db.rollback()
        # 이전(무접두) 행 위조도 탐지
        evs = _events(db, case.case_id)
        evs[1].action = "hacked"
        assert not m._verify_wf_chain(evs)["integrity_ok"]
        db.rollback()
        # AUDIT_KEY 를 잃으면 k2 행은 검증 불가(위조 판정)
        monkeypatch.delenv("GLHAC_AUDIT_KEY")
        evs = _events(db, case.case_id)
        assert not m._verify_wf_chain(evs)["integrity_ok"]
    finally:
        db.close()


def test_r31_SECRET을_바꿔도_k2_감사체인_통과(monkeypatch):
    monkeypatch.setenv("GLHAC_AUDIT_KEY", "audit-key-r31b-" + "z" * 24)
    db = SessionLocal()
    try:
        case = _new_case(db)
        sm.record_event(db, case, None, "draft", "a")
        sm.record_event(db, case, "draft", "x", "b")
        user = {"uid": "u_r3", "role": "admin", "org_id": "org_demo"}
        r1 = m._audit(db, user, "r3.audit.one", "case", case.case_id, case.case_id, {"k": 1})
        r2 = m._audit(db, user, "r3.audit.two", "case", case.case_id, case.case_id, {"k": 2})
        assert r1.meta["_chain"]["row"].startswith("k2:")
        monkeypatch.setattr(auth, "SECRET", b"rotated-login-secret-" + b"y" * 24)
        r = m._verify_wf_chain(_events(db, case.case_id))
        assert r["integrity_ok"] and r["verified"] == 2, r
        rows = [r1, r2]
        v = m._verify_audit_chain(rows, subset=True)
        assert v["integrity_ok"] and v["checked"] == 2 and v["tampered_count"] == 0, v
        # 위조(행 수정) 탐지 유지
        meta = dict(r2.meta)
        meta["k"] = 999
        r2.meta = meta
        v2 = m._verify_audit_chain([r1, r2], subset=True)
        assert v2["integrity_ok"] is False and r2.id in v2["tampered"], v2
        db.rollback()
    finally:
        db.close()


def _make_cert(db, signed_at=None, tamper=False):
    case = _new_case(db)
    cert = models.HalalCertificate(case_id=case.case_id, certificate_no="CN-" + _uniq(), scope=["P"],
                                   issue_date="2026-10-01", expiry_date="2099-01-01", status="active",
                                   qr_token="qr" + _uniq())
    db.add(cert)
    db.flush()
    ph, sigv = m._sign_payload(m._cert_canonical(cert))
    db.add(models.Signature(case_id=case.case_id, subject_type="certificate", subject_id=cert.id,
                            signer="t", payload_hash=ph, signature_value=("bad" + sigv) if tamper else sigv,
                            signed_at=signed_at or datetime.utcnow()))
    db.commit()
    return cert.qr_token, sigv


def test_r31_인증서_서명키_분리(monkeypatch):
    monkeypatch.delenv("GLHAC_CERT_SIGN_KEY", raising=False)
    db = SessionLocal()
    try:
        with TestClient(app) as c:
            old_tok, old_sig = _make_cert(db)
            assert not old_sig.startswith("k2:")
            monkeypatch.setenv("GLHAC_CERT_SIGN_KEY", "cert-key-r31-" + "z" * 24)
            new_tok, new_sig = _make_cert(db)
            bad_tok, _ = _make_cert(db, tamper=True)
            assert new_sig.startswith("k2:")
            for t in (old_tok, new_tok):
                r = c.get("/verify/" + t).json()
                assert r["signature_status"] == "valid", r
            assert c.get("/verify/" + bad_tok).json()["signature_status"] == "invalid"
            # 로그인 키 회전: 신규(k2) 는 통과, 구분자 없는 이전 서명은 옛 키가 필요해 더는 검증 안 됨
            monkeypatch.setattr(auth, "SECRET", b"rotated-login-secret-" + b"y" * 24)
            assert c.get("/verify/" + new_tok).json()["signature_status"] == "valid"
            assert c.get("/verify/" + old_tok).json()["signature_status"] == "invalid"
            # 서명 키를 잃으면 k2 서명은 검증 불가(위조 취급)
            monkeypatch.delenv("GLHAC_CERT_SIGN_KEY")
            assert c.get("/verify/" + new_tok).json()["signature_status"] == "invalid"
    finally:
        db.close()


# ══ R3-2. 의존성 고정(requirements.lock) ═════════════════════════════════
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(n):
    import re
    return re.sub(r"[-_.]+", "-", n).lower()


def _lock_pins():
    import re
    pins = {}
    for ln in open(os.path.join(_ROOT, "requirements.lock"), encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        mm = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", ln)
        assert mm, "lock 은 정확한 == 고정만 허용: " + ln
        pins[_norm(mm.group(1))] = mm.group(2)
    return pins


def test_r32_lock이_requirements_전체를_포함():
    import re
    pins = _lock_pins()
    assert len(pins) > 50
    names = []
    for ln in open(os.path.join(_ROOT, "requirements.txt"), encoding="utf-8"):
        ln = re.sub(r"\s*#.*$", "", ln).strip()
        if ln:
            names.append(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", ln).group(0))
    assert names
    missing = [n for n in names if _norm(n) not in pins]
    assert not missing, missing


def test_r32_테스트전용은_dev로_분리():
    reqs = open(os.path.join(_ROOT, "requirements.txt"), encoding="utf-8").read().lower()
    dev = open(os.path.join(_ROOT, "requirements-dev.txt"), encoding="utf-8").read().lower()
    assert "pytest" not in reqs and "pytest" in dev


def test_r32_CI용_고정출력은_lock_버전():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pin_from_lock", os.path.join(_ROOT, "scripts", "pin_from_lock.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.pin(skip=["paddle"])
    pins = _lock_pins()
    assert out and not any(o.lower().startswith("paddle") for o in out)
    for o in out:
        name, _, ver = o.partition("==")
        assert ver == pins[_norm(name.split("[")[0])], o
    assert any(o.startswith("uvicorn[standard]==") for o in out)


# ══ R3-3. 스태프 연락처 필드 + 알림 수신 ═════════════════════════════════
from app import notify  # noqa: E402


def _login(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mk_user(c, adm, role, org="org_demo"):
    name = "r3%s_%s" % (role[:4], _uniq())
    r = c.post("/admin/users", json={"username": name, "password": "halal-test-1", "role": role, "org_id": org},
               headers=adm)
    assert r.status_code == 200, r.text
    return name, r.json()["user_id"], _login(c, name, "halal-test-1")


def _capture(monkeypatch):
    seen = []

    def make(ch):
        def fn(contacts, text, notification=None):
            seen.append((ch, dict(contacts)))
            if not contacts.get("phone" if ch == "sms" else "email"):
                return {"channel": ch, "ok": False, "reason": "no_contact"}
            return {"channel": ch, "ok": True}
        return fn
    monkeypatch.setitem(notify.PROVIDERS, "sms", make("sms"))
    monkeypatch.setitem(notify.PROVIDERS, "email", make("email"))
    return seen


def _assigned_notif(auditor_uid, role="auditor"):
    db = SessionLocal()
    try:
        case = _new_case(db)
        if auditor_uid:
            sm.record_event(db, case, None, "draft", "ops.auditor_assigned", "operator", "op",
                            {"auditor_id": auditor_uid, "auditor_name": "x"})
        n = models.Notification(case_id=case.case_id, org_id=case.org_id, role=role, event_type="t",
                                channels=["sms", "email"], title="T", body="B", status="unsent")
        db.add(n)
        db.commit()
        return n.notification_id
    finally:
        db.close()


def _drain(nid):
    db = SessionLocal()
    try:
        db.query(models.Notification).filter(models.Notification.notification_id != nid,
                                             models.Notification.status == "unsent"
                                             ).update({"status": "skipped"}, synchronize_session=False)
        db.commit()
        m.drain_notifications(db)
        db.expire_all()
        return db.get(models.Notification, nid)
    finally:
        db.close()


def test_r33_오디터_연락처_있으면_그_주소로_없으면_no_contact(monkeypatch):
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        _, uid_a, ha = _mk_user(c, adm, "auditor")
        _, uid_b, hb = _mk_user(c, adm, "auditor")
        r = c.patch("/me/contact", json={"contact_email": "aud-a@x.test", "contact_phone": "+62 812-3456-7890"}, headers=ha)
        assert r.status_code == 200 and r.json()["contact_email"] == "aud-a@x.test"
        seen = _capture(monkeypatch)
        n = _drain(_assigned_notif(uid_a))
        assert n.status == "sent", (n.status, n.last_error)
        by = dict(seen)
        assert by["email"]["email"] == "aud-a@x.test" and by["sms"]["phone"] == "+62 812-3456-7890"
        # 연락처 없는 배정 오디터 → no_contact(업체로 새지 않음)
        seen.clear()
        n = _drain(_assigned_notif(uid_b))
        assert n.status == "skipped" and "no_contact" in (n.last_error or "")
        assert all(not cc.get("email") and not cc.get("phone") for _, cc in seen)
        # 배정이 없으면 아무에게도 안 간다
        seen.clear()
        n = _drain(_assigned_notif(None))
        assert n.status == "skipped" and all(not cc.get("email") for _, cc in seen)


def test_r33_운영자_역할_알림은_해당_역할_연락처(monkeypatch):
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        name, uid, h = _mk_user(c, adm, "fatwa_liaison")
        assert c.patch("/admin/users/" + uid, json={"contact_email": "sha@x.test"}, headers=adm).status_code == 200
        seen = _capture(monkeypatch)
        n = _drain(_assigned_notif(None, role="fatwa_liaison"))
        assert n.status == "sent", (n.status, n.last_error)
        assert dict(seen)["email"]["email"] == "sha@x.test"


def test_r33_연락처_수정_권한과_검증():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        _, uid_a, ha = _mk_user(c, adm, "auditor")
        _, uid_b, hb = _mk_user(c, adm, "auditor")
        assert c.patch("/me/contact", json={"contact_email": "a@x.test"}, headers=ha).status_code == 200
        # b 가 /me/contact 로 바꿔도 b 자신만 바뀐다
        assert c.patch("/me/contact", json={"contact_email": "b@x.test"}, headers=hb).status_code == 200
        db = SessionLocal()
        try:
            assert db.get(models.User, uid_a).contact_email == "a@x.test"
            assert db.get(models.User, uid_b).contact_email == "b@x.test"
        finally:
            db.close()
        # 비운영자는 남의 연락처를 관리자 경로로도 못 바꾼다
        assert c.patch("/admin/users/" + uid_a, json={"contact_email": "evil@x.test"}, headers=hb).status_code == 403
        assert c.patch("/me/contact", json={"contact_email": "not-an-email"}, headers=ha).status_code == 422
        assert c.patch("/me/contact", json={"contact_phone": "abc"}, headers=ha).status_code == 422
        # 빈 문자열=삭제
        r = c.patch("/me/contact", json={"contact_email": ""}, headers=ha)
        assert r.status_code == 200 and r.json()["contact_email"] is None
        # 인증 없이는 불가
        assert c.patch("/me/contact", json={"contact_email": "x@x.test"}).status_code in (401, 403)


# ══ R3-4. 내 정보 · 운영자 사용자 목록/상세 ═══════════════════════════════
def test_r34a_내정보_조회_수정과_권한필드_거부():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        name, uid, h = _mk_user(c, adm, "auditor")
        p = c.get("/me/profile", headers=h).json()
        assert p["username"] == name and p["role"] == "auditor" and p["org_id"] == "org_demo"
        assert p["status"] == "active" and p["created_at"] and "password_hash" not in p
        assert "assigned_case_count" in p["summary"]
        r = c.patch("/me/profile", json={"contact_email": "me@x.test", "contact_phone": "010-1234-5678",
                                         "display_name": "표기명R3"}, headers=h)
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["contact_email"] == "me@x.test" and p["display_name"] == "표기명R3"
        # 권한 필드는 422 이고 실제로 바뀌지 않는다
        for bad in ({"role": "admin"}, {"org_id": "org_x"}, {"status": "active"}, {"username": "zz"}):
            assert c.patch("/me/profile", json=bad, headers=h).status_code == 422, bad
        p = c.get("/me/profile", headers=h).json()
        assert p["role"] == "auditor" and p["org_id"] == "org_demo"
        assert c.patch("/me/profile", json={"contact_email": "bad"}, headers=h).status_code == 422
        # 기업 계정은 표기명 수정 불가, 담당 컨설턴트 표기 필드 존재
        ename, _, eh = _mk_user(c, adm, "applicant")
        assert c.patch("/me/profile", json={"display_name": "x"}, headers=eh).status_code == 422
        pe = c.get("/me/profile", headers=eh).json()
        assert "consultant_name" in pe and "case_count" in pe["summary"]
        assert c.get("/me/profile").status_code in (401, 403)


def test_r34a_남의_profile은_못_바꾼다():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        _, uid_a, ha = _mk_user(c, adm, "auditor")
        _, uid_b, hb = _mk_user(c, adm, "auditor")
        # 대상 id 를 받는 경로 자체가 없고, 본문에 user_id 를 넣어도 422
        assert c.patch("/me/profile", json={"user_id": uid_a, "contact_email": "evil@x.test"}, headers=hb).status_code == 422
        assert c.patch("/me/profile/" + uid_a, json={"contact_email": "e@x.test"}, headers=hb).status_code in (404, 405)
        assert c.patch("/me/profile", json={"contact_email": "b@x.test"}, headers=hb).status_code == 200
        assert c.get("/me/profile", headers=ha).json()["contact_email"] is None


def test_r34b_목록_검색_필터_페이지_마스킹():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        tag = _uniq()
        made = []
        for i in range(5):
            r = c.post("/admin/users", json={"username": "lst%s_%d" % (tag, i), "password": "halal-test-1",
                                             "role": "auditor" if i < 3 else "consultant", "org_id": "org_demo"}, headers=adm)
            made.append(r.json()["user_id"])
        assert c.patch("/admin/users/" + made[0], json={"contact_email": "secretmail@x.test",
                                                       "contact_phone": "+6281234567890"}, headers=adm).status_code == 200
        assert c.post("/admin/users/%s/suspend" % made[1], json={"reason": "t"}, headers=adm).status_code == 200
        j = c.get("/admin/users", params={"q": "lst" + tag, "page": 1, "size": 2, "sort": "username"}, headers=adm).json()
        assert j["total"] == 5 and len(j["items"]) == 2 and j["pages"] == 3 and j["page"] == 1
        j3 = c.get("/admin/users", params={"q": "lst" + tag, "page": 3, "size": 2}, headers=adm).json()
        assert len(j3["items"]) == 1
        j = c.get("/admin/users", params={"q": "lst" + tag, "role": "consultant", "page": 1}, headers=adm).json()
        assert j["total"] == 2 and all(i["role"] == "consultant" for i in j["items"])
        j = c.get("/admin/users", params={"q": "lst" + tag, "status": "suspended", "page": 1}, headers=adm).json()
        assert [i["user_id"] for i in j["items"]] == [made[1]]
        j = c.get("/admin/users", params={"q": "lst" + tag, "status": "active", "page": 1}, headers=adm).json()
        assert made[1] not in [i["user_id"] for i in j["items"]] and j["total"] == 4
        j = c.get("/admin/users", params={"q": "lst" + tag, "sort": "username", "dir": "desc", "page": 1}, headers=adm).json()
        assert j["items"][0]["username"].endswith("_4")
        row = c.get("/admin/users", params={"q": "lst%s_0" % tag, "page": 1}, headers=adm).json()["items"][0]
        assert row["contact_email"] == "se***@x.test" and "secretmail" not in str(row)
        assert row["contact_phone"] == "+628***" and "1234567890" not in str(row)
        raw = c.get("/admin/users", params={"q": "lst" + tag, "page": 1}, headers=adm).text
        assert "password" not in raw and "token" not in raw.lower()
        # 비운영자는 목록 403
        _, _, hb = _mk_user(c, adm, "auditor")
        assert c.get("/admin/users", headers=hb).status_code == 403
        # 기존 호출 형태(리스트 응답)는 그대로
        assert isinstance(c.get("/admin/users", params={"q": "lst" + tag}, headers=adm).json(), list)


def test_r34c_상세_권한_원문연락처_감사로그_404():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        name, uid, h = _mk_user(c, adm, "auditor")
        assert c.patch("/admin/users/" + uid, json={"contact_email": "orig@x.test"}, headers=adm).status_code == 200
        _, _, hother = _mk_user(c, adm, "consultant")
        assert c.get("/admin/users/" + uid, headers=hother).status_code == 403
        assert c.get("/admin/users/" + uid, headers=h).status_code == 403
        assert c.post("/admin/users/%s/suspend" % uid, json={"reason": "점검"}, headers=adm).status_code == 200
        assert c.get("/admin/users/no-such-uid", headers=adm).status_code == 404

        def views():
            db = SessionLocal()
            try:
                return db.query(models.AuditLog).filter_by(action="user.view", resource_id=uid).count()
            finally:
                db.close()
        assert views() == 0
        r = c.get("/admin/users/" + uid, headers=adm)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["contact_email"] == "orig@x.test" and d["status"] == "suspended"
        assert d["status_history"][0]["status"] == "suspended" and d["status_history"][0]["reason"] == "점검"
        assert any(a["action"] == "admin.user.suspend" for a in d["admin_actions"])
        assert isinstance(d["recent_audit"], list) and isinstance(d["related_cases"], list)
        assert "password" not in r.text and "token_version" not in r.text
        assert views() == 1
        # 열람 기록이 체인에 들어가 있다
        db = SessionLocal()
        try:
            row = db.query(models.AuditLog).filter_by(action="user.view", resource_id=uid).first()
            assert row.meta["_chain"]["row"]
            assert row.actor_role == "admin"
        finally:
            db.close()


def test_r34_operator는_조회만_수정삭제는_admin_전용():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin")
        _, uid, _ = _mk_user(c, adm, "auditor")
        assert c.patch("/admin/users/" + uid, json={"contact_email": "t@x.test"}, headers=adm).status_code == 200
        _, _, hop = _mk_user(c, adm, "operator")
        _, _, hcons = _mk_user(c, adm, "consultant")
        # 목록·상세 200 (연락처: 목록 마스킹, 상세 원문)
        r = c.get("/admin/users", params={"page": 1, "size": 5}, headers=hop)
        assert r.status_code == 200 and "items" in r.json()
        r = c.get("/admin/users/" + uid, headers=hop)
        assert r.status_code == 200 and r.json()["contact_email"] == "t@x.test"
        # user.view 감사로그에 operator 열람이 남는다
        db = SessionLocal()
        try:
            row = db.query(models.AuditLog).filter_by(action="user.view", resource_id=uid, actor_role="operator").first()
            assert row is not None
        finally:
            db.close()
        # 수정·정지·삭제는 operator 403 (그리고 실제로 안 바뀜)
        assert c.patch("/admin/users/" + uid, json={"role": "admin"}, headers=hop).status_code == 403
        assert c.patch("/admin/users/" + uid, json={"contact_email": "z@x.test"}, headers=hop).status_code == 403
        assert c.post("/admin/users/%s/suspend" % uid, json={}, headers=hop).status_code == 403
        assert c.delete("/admin/users/" + uid, headers=hop).status_code == 403
        d = c.get("/admin/users/" + uid, headers=adm).json()
        assert d["role"] == "auditor" and d["contact_email"] == "t@x.test" and d["status"] == "active"
        # 그 밖의 역할은 목록·상세 모두 403
        assert c.get("/admin/users", headers=hcons).status_code == 403
        assert c.get("/admin/users/" + uid, headers=hcons).status_code == 403
