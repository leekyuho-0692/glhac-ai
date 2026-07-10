"""P0-4차: M06 규정·법령 관리 — WorkflowEvent(latest-wins) 저장 검증(신규 테이블 없음).
TestClient 기반(서버 불필요). 고유 DB로 격리.
실행: <venv>/bin/python -m pytest tests/test_regulation_mgmt.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_reg_test.db"
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드(테스트 전용)

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


def test_operator_create_list_detail_200():
    """(a) operator 법령 생성 → 목록·상세 200, 초기 상태 draft."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        r = c.post("/regulations", json={"title": "할랄 인증 시행규칙", "reg_number": "PP-2024-01",
                                         "effective_date": "2024-01-01", "category": "regulation",
                                         "summary": "인증 절차 규정"}, headers=_h(op))
        assert r.status_code == 200, r.text
        reg_id = r.json()["reg_id"]
        assert reg_id.startswith("reg_") and r.json()["state"] == "draft"
        lst = c.get("/regulations", headers=_h(op))
        assert lst.status_code == 200
        assert any(x["reg_id"] == reg_id for x in lst.json()["items"])
        det = c.get(f"/regulations/{reg_id}", headers=_h(op))
        assert det.status_code == 200, det.text
        assert det.json()["content"]["title"] == "할랄 인증 시행규칙"
        assert det.json()["state"] == "draft"


def test_edit_accumulates_two_versions_in_order():
    """(b) 수정 → 버전 이력 2건 시간순 누적."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        reg_id = c.post("/regulations", json={"title": "원본 제목"}, headers=_h(op)).json()["reg_id"]
        r2 = c.put(f"/regulations/{reg_id}", json={"title": "개정 제목", "summary": "개정 요약"},
                   headers=_h(op))
        assert r2.status_code == 200 and r2.json()["version"] == 2, r2.text
        det = c.get(f"/regulations/{reg_id}", headers=_h(op)).json()
        vers = det["versions"]
        assert len(vers) == 2, vers
        assert [v["version"] for v in vers] == [1, 2]
        assert vers[0]["title"] == "원본 제목" and vers[1]["title"] == "개정 제목"
        # 현재 내용 = 최신(latest-wins)
        assert det["content"]["title"] == "개정 제목"
        hist = c.get(f"/regulations/{reg_id}/history", headers=_h(op))
        assert hist.status_code == 200 and len(hist.json()["versions"]) == 2


def test_impact_mapping_roundtrip():
    """(c) 영향매핑 태그 저장·조회 라운드트립(심사단계·증거섹션)."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        payload = {"title": "영향 테스트", "impact_stages": ["mock_audit", "onsite"],
                   "impact_sections": ["material_storage", "hygiene"]}
        reg_id = c.post("/regulations", json=payload, headers=_h(op)).json()["reg_id"]
        det = c.get(f"/regulations/{reg_id}", headers=_h(op)).json()
        assert det["content"]["impact_stages"] == ["mock_audit", "onsite"]
        assert det["content"]["impact_sections"] == ["material_storage", "hygiene"]
        # 수정으로 매핑 갱신 → 최신 반영
        c.put(f"/regulations/{reg_id}", json={"title": "영향 테스트", "impact_stages": ["fatwa"],
                                              "impact_sections": []}, headers=_h(op))
        det2 = c.get(f"/regulations/{reg_id}", headers=_h(op)).json()
        assert det2["content"]["impact_stages"] == ["fatwa"]
        assert det2["content"]["impact_sections"] == []


def test_transition_draft_to_effective_notifies():
    """(d) 상태전이 draft→review→effective(발효) 시 이벤트 + 알림 생성."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        reg_id = c.post("/regulations", json={"title": "발효 대상", "reg_number": "X-1"},
                        headers=_h(op)).json()["reg_id"]
        # draft → review
        r1 = c.post(f"/regulations/{reg_id}/transition", json={"to_state": "review"}, headers=_h(op))
        assert r1.status_code == 200 and r1.json()["to"] == "review", r1.text
        # review → effective (발효)
        r2 = c.post(f"/regulations/{reg_id}/transition",
                    json={"to_state": "effective", "reason": "위원회 승인"}, headers=_h(op))
        assert r2.status_code == 200 and r2.json()["to"] == "effective", r2.text
        det = c.get(f"/regulations/{reg_id}", headers=_h(op)).json()
        assert det["state"] == "effective"
        actions = [h["action"] for h in det["history"]]
        assert "regulation.submitted" in actions and "regulation.approved" in actions
        # 발효 알림 생성 확인(전용 테이블 없음 — Notification 큐)
        db = SessionLocal()
        try:
            notes = (db.query(models.Notification)
                     .filter(models.Notification.event_type == "regulation.effective").all())
            assert len(notes) >= 2, "operator·auditor 알림 2건 이상 기대"
            assert {n.role for n in notes} >= {"operator", "auditor"}
        finally:
            db.close()
        # 잘못된 전이(effective→review 불가)는 409
        bad = c.post(f"/regulations/{reg_id}/transition", json={"to_state": "review"}, headers=_h(op))
        assert bad.status_code == 409, bad.text


def test_applicant_create_forbidden_403():
    """(e) 비권한(applicant) 생성 403."""
    with TestClient(app) as c:
        ap = _tok(c, "applicant1", "pw")
        r = c.post("/regulations", json={"title": "무단 등록"}, headers=_h(ap))
        assert r.status_code == 403, r.text
        # 목록 조회도 차단
        assert c.get("/regulations", headers=_h(ap)).status_code == 403
