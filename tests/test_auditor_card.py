"""P2-6 오디터 배정 카드 — 격리 테스트.

스키마 무변경(User 컬럼 추가 금지) 검증: 오디터 프로필(전문분야·언어)을
WorkflowEvent(action="auditor.profile") latest-wins sentinel(case_id=오디터 user_id)로 저장.

고유 DB(glhac_audcard_test.db) · GLHAC_DEV=1 · TestClient(서버 불필요).
실행: <venv>/bin/python -m pytest tests/test_auditor_card.py -q
검증: (a) 프로필 POST→_ops_auditors_data/대시보드에 specialty·languages 반영·latest-wins
      (b) load/load_pct 집계 (c) 비권한(applicant) 프로필 설정 403.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_audcard_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                       # 데모 계정 시드

_DBFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "glhac_audcard_test.db")
if os.path.exists(_DBFILE):
    os.remove(_DBFILE)

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


def _mk_case(status, company, org_id="org_demo", **extra):
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id=org_id, company_name=company, status=status, **extra)
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _auditor_id():
    db = SessionLocal()
    try:
        u = db.query(models.User).filter_by(role="auditor", org_id="org_demo").first()
        return u.user_id
    finally:
        db.close()


def _user_columns():
    return set(models.User.__table__.columns.keys())


def test_a_profile_reflected_latest_wins():
    """프로필 POST → 대시보드/집계에 specialty·languages 반영. 재설정 시 latest-wins."""
    with TestClient(app) as c:
        aid = _auditor_id()   # 테이블은 startup(=client 진입) 시 생성되므로 컨텍스트 내에서 조회
        tok = _tok(c, "operator1", "pw")
        # 최초 설정
        r = c.post("/ops/auditors/%s/profile" % aid,
                   json={"specialty": "식품 전문", "languages": ["id", "en"]}, headers=_h(tok))
        assert r.status_code == 200, r.text
        assert r.json()["profile"]["specialty"] == "식품 전문"

        def _row(payload):
            return next(a for a in payload["auditors"] if a["user_id"] == aid)

        auds = c.get("/ops/auditors", headers=_h(tok)).json()
        row = _row(auds)
        assert row["specialty"] == "식품 전문", row
        assert row["languages"] == ["id", "en"], row
        # 대시보드 경유도 동일
        dash = c.get("/ops/dashboard", headers=_h(tok)).json()
        drow = _row(dash["auditors"])
        assert drow["specialty"] == "식품 전문" and drow["languages"] == ["id", "en"], drow

        # 재설정 → latest-wins (덮어쓰기)
        r2 = c.post("/ops/auditors/%s/profile" % aid,
                    json={"specialty": "화장품 전문", "languages": ["ar"]}, headers=_h(tok))
        assert r2.status_code == 200, r2.text
        row2 = _row(c.get("/ops/auditors", headers=_h(tok)).json())
        assert row2["specialty"] == "화장품 전문", row2
        assert row2["languages"] == ["ar"], row2

    # 스키마 무변경: auditor.profile 이벤트가 누적되어 latest-wins(행 삭제 없음)
    db = SessionLocal()
    try:
        n = (db.query(models.WorkflowEvent)
             .filter_by(case_id=aid, action="auditor.profile").count())
        assert n == 2, "프로필 이벤트가 append(누적)되지 않음: %d" % n
    finally:
        db.close()


def test_b_load_and_load_pct_aggregated():
    """오디터 배정 후 load 증가 + load_pct 정규화(0<pct<=100) 노출."""
    with TestClient(app) as c:
        cid = _mk_case("document_pre_audit_requested", "PT LoadCo")
        aid = _auditor_id()
        tok = _tok(c, "operator1", "pw")
        r = c.post("/ops/cases/%s/assign-auditor" % cid,
                   json={"auditor_id": aid}, headers=_h(tok))
        assert r.status_code == 200, r.text
        row = next(a for a in c.get("/ops/auditors", headers=_h(tok)).json()["auditors"]
                   if a["user_id"] == aid)
        assert row["load"] >= 1, row
        assert "load_pct" in row and 0 < row["load_pct"] <= 100, row
        assert row["capacity"] >= 1, row


def test_c_applicant_forbidden_to_set_profile():
    """비권한 역할(applicant)은 오디터 프로필 설정 403."""
    with TestClient(app) as c:
        aid = _auditor_id()
        tok = _tok(c, "applicant1", "pw")
        r = c.post("/ops/auditors/%s/profile" % aid,
                   json={"specialty": "x", "languages": []}, headers=_h(tok))
        assert r.status_code == 403, r.text


def test_d_user_schema_unchanged():
    """User 모델에 specialty/languages 등 프로필 컬럼이 추가되지 않았음(스키마 무변경)."""
    cols = _user_columns()
    assert "specialty" not in cols and "languages" not in cols, cols
