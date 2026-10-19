"""R2-2 — FK 도입: 모델 FK 구조·정책 · 연결별 PRAGMA · GLHAC_SQLITE_FK 되돌리기 · 삭제 경로 동작.

실제 앱·실제 SQLite 임시 DB 를 쓴다(목 없음). 도구는 test_fk_orphans.py · test_fk_rebuild.py.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GLHAC_DEV", "1")

from app import models  # noqa: E402
from app.db import Base  # noqa: E402


def _relations():
    """모델 메타데이터의 FK → (자식, 컬럼, 부모, 정책, NULL허용) — 도구와 같은 출처(하드코딩 없음)."""
    out = []
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_key_constraints:
            col = fk.columns[0]
            out.append(type('R', (), dict(child=t.name, ccol=col.name, ondelete=(fk.ondelete or 'RESTRICT').upper(),
                                          nullable=bool(col.nullable))))
    return out


def test_모델에_FK가_선언돼_있고_정책이_의미대로다():
    rels = _relations()
    assert len(rels) >= 60
    by = {(r.child, r.ccol): r for r in rels}
    # 감사·법적 기록은 CASCADE 로 지우면 안 된다 → RESTRICT 또는 FK 제외
    for t in ("invoice", "payment", "refund", "halal_certificate", "contract", "fatwa_decision", "signature"):
        assert by[(t, "case_id")].ondelete == "RESTRICT", t
    assert by[("payment", "invoice_id")].ondelete == "RESTRICT"
    assert not any(r.child in ("audit_log", "workflow_event") for r in rels)
    # 순수 종속 행은 CASCADE · 선택 참조는 SET NULL
    assert by[("product_material", "product_id")].ondelete == "CASCADE"
    assert by[("document_asset", "material_id")].ondelete == "SET NULL"


def test_SET_NULL_컬럼은_모두_NULL_허용이다():
    for r in _relations():
        if r.ondelete == "SET NULL":
            assert r.nullable, "%s.%s: SET NULL 인데 NOT NULL" % (r.child, r.ccol)


def test_의사키_컬럼은_FK_대상이_아니다():
    names = {(r.child, r.ccol) for r in _relations()}
    for pseudo in (("workflow_event", "case_id"), ("audit_log", "case_id"), ("audit_log", "actor_id"),
                   ("signature", "subject_id"), ("sys_menu_permission", "target_id"),
                   ("generated_document", "case_id"),       # 'reports:<org>' 의사 키(기간 보고서)
                   ("corrective_action", "finding_id")):    # 'eval:<요소>' 의사 키(평가 판정 CAR)
        assert pseudo not in names


def test_새_DB는_FK가_생기고_연결마다_PRAGMA가_켜진다(tmp_path):
    from app.db import engine
    with engine.connect() as conn:
        assert conn.exec_driver_sql("pragma foreign_keys").scalar() == 1
        fks = conn.exec_driver_sql("pragma foreign_key_list(payment)").fetchall()
        assert {(r[3], r[2]) for r in fks} == {("case_id", "case_application"), ("invoice_id", "invoice")}
    # 자식을 실제 부모 없이 넣으면 거부
    from app.db import SessionLocal
    from sqlalchemy.exc import IntegrityError
    db = SessionLocal()
    try:
        db.add(models.Product(case_id="없는케이스", name="x"))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_GLHAC_SQLITE_FK_0_이면_끈다(tmp_path):
    code = ("from app.db import engine\n"
            "print(engine.connect().exec_driver_sql('pragma foreign_keys').scalar())")
    base = dict(os.environ, GLHAC_DB_URL="sqlite:///%s/x.db" % tmp_path)
    off = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=dict(base, GLHAC_SQLITE_FK="0"),
                         capture_output=True, text=True)
    on = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=base, capture_output=True, text=True)
    assert off.stdout.strip() == "0", off.stderr
    assert on.stdout.strip() == "1", on.stderr




# ───────────────────────── 삭제 경로 (FK ON 에서 실제 호출) ─────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _login(c, u="admin", p="admin"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_케이스_purge는_인보이스_결제_RESTRICT를_자식먼저_순서로_지운다():
    """이름순으로 지우면 invoice 가 payment 보다 먼저 지워져 payment.invoice_id(RESTRICT)에 걸린다."""
    from app.db import SessionLocal
    with _client() as c:
        h = _login(c)
        keep = c.post("/cases", json={"company_name": "보존", "nib": "9" * 13}, headers=h).json()["case_id"]
        drop = c.post("/cases", json={"company_name": "삭제", "nib": "9" * 13}, headers=h).json()["case_id"]
        db = SessionLocal()
        try:
            inv = models.Invoice(case_id=drop, total=100.0)
            db.add(inv)
            db.flush()
            db.add(models.Payment(invoice_id=inv.invoice_id, case_id=drop, amount=100.0))
            db.add(models.Contract(case_id=drop))
            db.commit()
        finally:
            db.close()
        r = c.post("/admin/cases/purge", headers=h, json={
            "keep": [keep], "dry_run": False, "confirm": "PURGE", "reason": "FK 순서 검증"})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            assert db.get(models.CaseApplication, drop) is None
            assert db.query(models.Invoice).filter_by(case_id=drop).count() == 0
            assert db.query(models.Payment).filter_by(case_id=drop).count() == 0
            assert db.get(models.CaseApplication, keep) is not None
            # 감사로그는 보존(case_id 는 사라진 케이스를 가리키지만 FK 가 아니므로 남는다)
            assert db.query(models.AuditLog).filter_by(case_id=drop, action="case.purge").count() == 1
        finally:
            db.close()


def test_케이스를_직접_지우려_하면_RESTRICT가_막는다():
    from app.db import SessionLocal
    from sqlalchemy.exc import IntegrityError
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id="org_fk", company_name="인보이스 있는 케이스")
        db.add(c)
        db.flush()
        db.add(models.Invoice(case_id=c.case_id, total=1.0))
        db.add(models.Product(case_id=c.case_id, name="종속 제품"))
        db.commit()
        cid = c.case_id
        db.delete(db.get(models.CaseApplication, cid))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 인보이스를 먼저 치우면 나머지(제품)는 CASCADE 로 따라간다
        db.query(models.Invoice).filter_by(case_id=cid).delete()
        db.delete(db.get(models.CaseApplication, cid))
        db.commit()
        assert db.query(models.Product).filter_by(case_id=cid).count() == 0
    finally:
        db.close()


def test_지급기록이_있는_사용자_삭제는_409이고_없으면_지워진다():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        u1 = models.User(username="fk_payee", password_hash="x", role="consultant", org_id="org_demo")
        u2 = models.User(username="fk_plain", password_hash="x", role="consultant", org_id="org_demo")
        db.add_all([u1, u2])
        db.flush()
        db.add(models.ConsultantProfile(consultant_id=u2.user_id))
        db.add(models.ConsultantPayout(consultant_id=u1.user_id, amount=1.0))
        db.commit()
        id1, id2 = u1.user_id, u2.user_id
    finally:
        db.close()
    with _client() as c:
        h = _login(c)
        r = c.delete("/admin/users/" + id1, headers=h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "USER_HAS_RECORDS", r.text
        assert c.delete("/admin/users/" + id2, headers=h).status_code == 200
    db = SessionLocal()
    try:
        assert db.get(models.User, id1) is not None
        assert db.get(models.User, id2) is None
        assert db.get(models.ConsultantProfile, id2) is None      # CASCADE
    finally:
        db.close()


def test_없는_제품_원재료로_연결하면_404이고_고아가_생기지_않는다():
    with _client() as c:
        h = _login(c)
        cid = c.post("/cases", json={"company_name": "링크", "nib": "9" * 13}, headers=h).json()["case_id"]
        r = c.post("/cases/%s/matrix/link" % cid, headers=h,
                   json={"product_id": "없는제품", "material_id": "없는원료", "linked": True})
        assert r.status_code == 404, r.text
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        assert db.query(models.ProductMaterial).filter_by(case_id=cid).count() == 0
    finally:
        db.close()


def test_FK_위반은_500이_아니라_409로_돌려준다():
    """호출부가 막지 못한 FK 위반(없는 부모 참조)은 서버 오류가 아니라 409 FK_CONSTRAINT."""
    from app.main import app
    from sqlalchemy.exc import IntegrityError

    @app.get("/__fk_probe")
    def _probe():
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            db.add(models.Product(case_id="없는케이스", name="x"))
            db.commit()
        finally:
            db.close()

    from fastapi.testclient import TestClient
    r = TestClient(app, raise_server_exceptions=False).get("/__fk_probe")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "FK_CONSTRAINT", r.text
