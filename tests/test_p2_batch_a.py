"""P2 배치 A 격리 테스트 — P2-1 현장캘린더 후보일 택1 수락 · P2-2 입금증빙(deposit_proof).

주의(테스트 격리): tests 일괄 실행 시 공유 DB 상호오염 방지를 위해 app import 전에
고유 DB(glhac_p2a_test.db)를 강제 대입한다(setdefault 금지).
실행: <venv>/bin/python -m pytest tests/test_p2_batch_a.py -q
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p2a_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                   # 데모 계정 시드

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402

_PDF_B64 = base64.b64encode(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF").decode()


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mkcase(c, admin):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "P2BatchACo"},
                  headers=_h(admin)).json()["case_id"]


def _count(case_id, model, **filt):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(case_id=case_id, **filt).count()
    finally:
        db.close()


def test_a_deposit_proof_upload_and_filter():
    """(a) 입금증빙 업로드(doc_type=deposit_proof) → documents 필터 조회 + 원본 다운로드."""
    with TestClient(app) as c:
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        ap = _tok(c, "applicant1", "pw")
        r = c.post(f"/cases/{cid}/documents",
                   json={"filename": "transfer.pdf", "file_b64": _PDF_B64,
                         "doc_type": "deposit_proof"}, headers=_h(ap))
        assert r.status_code == 200, r.text
        assert r.json()["doc_type"] == "deposit_proof", r.json()
        did = r.json()["document_id"]
        docs = c.get(f"/cases/{cid}/documents", headers=_h(ap)).json()
        proofs = [d for d in docs if d["doc_type"] == "deposit_proof"]
        assert len(proofs) == 1 and proofs[0]["has_file"], docs
        # DocumentAsset로 저장(스키마 무변경 재사용)
        assert _count(cid, models.DocumentAsset, doc_type="deposit_proof") == 1
        # 원본 파일 다운로드 가능
        f = c.get(f"/documents/{did}/file", headers=_h(ap))
        assert f.status_code == 200 and f.content[:5] == b"%PDF-", f.status_code


def test_b_onsite_accept_proposed_confirms_schedule():
    """(b) 클라 제시 → 오디터 재제안 → 클라 후보일 택1 수락 → 확정 이벤트 + 오디터 알림."""
    with TestClient(app) as c:
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        ap = _tok(c, "applicant1", "pw")
        au = _tok(c, "auditor1", "pw")
        # 클라 제시
        c.post(f"/cases/{cid}/onsite-schedule/propose",
               json={"dates": ["2026-08-01"]}, headers=_h(ap))
        # 오디터 재제안(2개)
        rr = c.post(f"/cases/{cid}/onsite-schedule/reschedule",
                    json={"dates": ["2026-08-10", "2026-08-12"]}, headers=_h(au))
        assert rr.json()["status"] == "reproposed", rr.text
        n_before = _count(cid, models.Notification, role="auditor")
        # 클라 택1 수락 → 확정
        r = c.post(f"/cases/{cid}/onsite-schedule/accept-proposed",
                   json={"date": "2026-08-12", "time": "10:00"}, headers=_h(ap))
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["status"] == "confirmed", st
        assert st["confirmed"]["date"] == "2026-08-12", st
        # confirm 이벤트 기록 + 오디터 알림 증가
        assert _count(cid, models.WorkflowEvent, action="onsite_schedule.confirm") == 1
        assert _count(cid, models.Notification, role="auditor") == n_before + 1
        # 후보일에 없는 날짜 수락 시도 → 400 (임의 확정 방지) — 새 케이스로 재현
        cid2 = _mkcase(c, _tok(c, "admin", "admin"))
        c.post(f"/cases/{cid2}/onsite-schedule/propose",
               json={"dates": ["2026-09-01"]}, headers=_h(ap))
        c.post(f"/cases/{cid2}/onsite-schedule/reschedule",
               json={"dates": ["2026-09-10"]}, headers=_h(au))
        bad = c.post(f"/cases/{cid2}/onsite-schedule/accept-proposed",
                     json={"date": "2026-09-99"}, headers=_h(ap))
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "DATE_NOT_PROPOSED", bad.text


def test_c_accept_proposed_requires_reproposed_state():
    """(c) 재제안 이전(none/proposed) 상태에서 수락 시도 → 409."""
    with TestClient(app) as c:
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        ap = _tok(c, "applicant1", "pw")
        # 아무 제시/재제안 없음 → 409
        r = c.post(f"/cases/{cid}/onsite-schedule/accept-proposed",
                   json={"date": "2026-08-01"}, headers=_h(ap))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "NO_PROPOSED_DATES", r.text


def test_d_accept_proposed_forbidden_for_auditor():
    """(d) 비권한(오디터측)이 클라 수락 라우트 호출 → 403 (가드=클라측 역할)."""
    with TestClient(app) as c:
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        ap = _tok(c, "applicant1", "pw")
        au = _tok(c, "auditor1", "pw")
        c.post(f"/cases/{cid}/onsite-schedule/propose",
               json={"dates": ["2026-08-01"]}, headers=_h(ap))
        c.post(f"/cases/{cid}/onsite-schedule/reschedule",
               json={"dates": ["2026-08-10"]}, headers=_h(au))
        r = c.post(f"/cases/{cid}/onsite-schedule/accept-proposed",
                   json={"date": "2026-08-10"}, headers=_h(au))
        assert r.status_code == 403, r.text


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
