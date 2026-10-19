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
