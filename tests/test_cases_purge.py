"""심사데이터 초기화 API — 감사 흔적을 남기고 감사로그를 보존하는지 검증.

배경: 2026-08-04 초기화를 스크립트로 DB에 직접 걸어 감사로그까지 지웠고, 전역 HMAC 체인의
prev가 사라져 영구 단절이 남았다. 초기화는 케이스 데이터를 지우는 것이지 '지웠다는 사실'을
지우는 것이 아니다. 이 테스트가 그 규약을 고정한다.
실행: <venv>/bin/python -m pytest tests/test_cases_purge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_purge_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _h(c, username="admin", password="admin"):
    r = c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, h, name):
    r = c.post("/cases", json={"company_name": name, "nib": "9" * 13}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["case_id"]


def _audit_count(action=None):
    from app import models
    from app.db import SessionLocal
    db = SessionLocal()
    q = db.query(models.AuditLog)
    if action:
        q = q.filter(models.AuditLog.action == action)
    n = q.count()
    db.close()
    return n


def _orphan_state():
    """감사체인에서 '선행 로그 삭제'로 인한 단절 수와 본문 변조 지점."""
    from app import models
    from app.db import SessionLocal
    from app.main import _audit_chain_hashes, _verify_audit_chain
    db = SessionLocal()
    rows = (db.query(models.AuditLog)
            .order_by(models.AuditLog.created_at, models.AuditLog.id).all())
    rep = _verify_audit_chain(rows, known_hashes=_audit_chain_hashes(db))
    db.close()
    return len(rep.get("deleted_predecessor", [])), rep["break_at"]


def test_purge_dry_run_changes_nothing():
    with TestClient(app) as c:
        h = _h(c)
        keep = _mkcase(c, h, "보존사")
        drop = _mkcase(c, h, "삭제사")
        before = _audit_count()
        r = c.post("/admin/cases/purge", json={"keep": [keep]}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        ids = [x["case_id"] for x in body["cases"]]
        assert drop in ids and keep not in ids

        from app import models
        from app.db import SessionLocal
        db = SessionLocal()
        assert db.query(models.CaseApplication).filter_by(case_id=drop).first() is not None
        db.close()
        assert _audit_count() == before   # 미리보기는 감사로그도 늘리지 않는다


def test_purge_requires_confirm_reason_and_count():
    with TestClient(app) as c:
        h = _h(c)
        keep = _mkcase(c, h, "가드보존")
        base = {"keep": [keep], "dry_run": False}
        assert c.post("/admin/cases/purge", json=base, headers=h).status_code == 400
        assert c.post("/admin/cases/purge", json={**base, "confirm": "PURGE"},
                      headers=h).status_code == 400
        assert c.post("/admin/cases/purge",
                      json={**base, "confirm": "PURGE", "reason": "r", "expect_delete": 9999},
                      headers=h).status_code == 409


def test_purge_preserves_audit_log_and_records_deletion():
    with TestClient(app) as c:
        h = _h(c)
        keep = _mkcase(c, h, "존치사")
        drop = _mkcase(c, h, "폐기사")
        before_audit = _audit_count()
        n = c.post("/admin/cases/purge", json={"keep": [keep]}, headers=h).json()["delete_count"]
        r = c.post("/admin/cases/purge", headers=h,
                   json={"keep": [keep], "dry_run": False, "confirm": "PURGE",
                         "reason": "시연 준비", "expect_delete": n})
        assert r.status_code == 200, r.text

        from app import models
        from app.db import SessionLocal
        db = SessionLocal()
        assert db.query(models.CaseApplication).filter_by(case_id=drop).first() is None
        assert db.query(models.CaseApplication).filter_by(case_id=keep).first() is not None
        # 핵심: 삭제된 케이스의 감사로그가 남아 있어야 한다(체인 보존)
        assert db.query(models.AuditLog).filter_by(case_id=drop).count() > 0
        db.close()
        assert _audit_count() > before_audit          # 줄지 않고 늘어난다
        assert _audit_count("case.purge") > 0         # 삭제 사실이 기록된다
        assert _audit_count("admin.cases.purge") > 0


def test_purge_keeps_audit_chain_unbroken():
    """초기화 후에도 '선행 로그 삭제' 단절이 새로 생기지 않아야 한다."""
    with TestClient(app) as c:
        h = _h(c)
        keep = _mkcase(c, h, "체인보존")
        _mkcase(c, h, "체인폐기")
        before, _ = _orphan_state()
        n = c.post("/admin/cases/purge", json={"keep": [keep]}, headers=h).json()["delete_count"]
        c.post("/admin/cases/purge", headers=h,
               json={"keep": [keep], "dry_run": False, "confirm": "PURGE",
                     "reason": "체인 검증", "expect_delete": n})
        after, break_at = _orphan_state()
        assert after == before, "초기화가 감사로그를 지워 체인을 끊었다"
        assert break_at is None, "감사로그 본문 무결성이 깨졌다: %s" % break_at


def test_purge_removes_uploaded_content_and_reclaims_file():
    """업로드 원본은 DB blob에 있다 — 행 삭제로 내용이 사라지고 VACUUM으로 파일에서도 회수된다."""
    import base64

    from app import models
    from app.db import SessionLocal

    with TestClient(app) as c:
        h = _h(c)
        keep = _mkcase(c, h, "파일보존")
        drop = _mkcase(c, h, "파일폐기")
        blob = base64.b64encode(b"%PDF-1.4 " + b"x" * 300000).decode()   # 300KB 더미
        r = c.post("/cases/%s/documents" % drop, headers=h,
                   json={"filename": "spec.pdf", "file_b64": blob, "doc_type": "other"})
        assert r.status_code in (200, 201), r.text

        db = SessionLocal()
        assert db.query(models.DocumentAsset).filter_by(case_id=drop).count() == 1
        db.close()

        pre = c.post("/admin/cases/purge", json={"keep": [keep]}, headers=h).json()
        assert pre["files"]["blob_bytes"] > 0          # 지울 원본 용량을 미리 보여준다
        assert pre["files"]["stored_in"].startswith("db(")

        r = c.post("/admin/cases/purge", headers=h,
                   json={"keep": [keep], "dry_run": False, "confirm": "PURGE",
                         "reason": "파일 회수 검증", "expect_delete": pre["delete_count"]})
        assert r.status_code == 200, r.text
        f = r.json()["files"]
        assert f.get("vacuum_ok") is True, f
        assert f["db_file_reclaimed_bytes"] > 0, f     # 파일이 실제로 줄어야 한다

        db = SessionLocal()
        assert db.query(models.DocumentAsset).filter_by(case_id=drop).count() == 0
        db.close()


def test_purge_sweep_uploads_stays_in_sandbox():
    """sweep_uploads는 GLHAC_UPLOAD_DIR 안에서만 지운다(기본 off)."""
    sandbox = os.environ.get("GLHAC_UPLOAD_DIR")
    if not sandbox or not os.path.isdir(sandbox):
        return   # 샌드박스가 없는 환경이면 건너뛴다
    outside = os.path.join(os.path.dirname(sandbox.rstrip("/")), "glhac_outside_probe.txt")
    with open(outside, "w") as fh:
        fh.write("건드리면 안 되는 파일")
    inside = os.path.join(sandbox, "staged_probe.bin")
    with open(inside, "wb") as fh:
        fh.write(b"z" * 1000)
    try:
        with TestClient(app) as c:
            h = _h(c)
            keep = _mkcase(c, h, "샌드박스")
            n = c.post("/admin/cases/purge", json={"keep": [keep]}, headers=h).json()["delete_count"]
            r = c.post("/admin/cases/purge", headers=h,
                       json={"keep": [keep], "dry_run": False, "confirm": "PURGE",
                             "reason": "샌드박스 정리", "expect_delete": n,
                             "sweep_uploads": True}).json()
            assert r["files"]["sandbox_removed"] >= 1, r["files"]
            assert not os.path.exists(inside), "샌드박스 안 파일이 안 지워졌다"
            assert os.path.exists(outside), "샌드박스 밖 파일을 지웠다"
    finally:
        for p in (inside, outside):
            if os.path.exists(p):
                os.remove(p)


def test_purge_forbidden_for_non_admin():
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "operator1", "password": "pw"})
        if r.status_code != 200:
            return   # 데모 계정이 없는 환경이면 건너뛴다
        h = {"Authorization": "Bearer " + r.json()["token"]}
        assert c.post("/admin/cases/purge", json={"keep": []}, headers=h).status_code == 403
