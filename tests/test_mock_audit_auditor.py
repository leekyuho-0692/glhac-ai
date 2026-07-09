"""P0-5 모의심사 오디터 뷰 3종 — WorkflowEvent(latest-wins) 저장 검증.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_mock_audit_auditor.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")
os.environ.setdefault("GLHAC_DEV", "1")   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mkcase(c, admin):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "MockAuditCo"},
                  headers=_h(admin)).json()["case_id"]


def test_manual_reject_round_increments_and_approve_recorded():
    """① manual reject 왕복 시 round 1→2 누적, approve 기록·최신 상태 반영."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # reject 1회차
        r1 = c.post(f"/cases/{cid}/mock-audit/manual",
                    json={"decision": "reject", "comment": "보완 필요1"}, headers=_h(aud))
        assert r1.status_code == 200, r1.text
        assert r1.json()["round"] == 1, r1.json()
        # reject 2회차 → round 증가
        r2 = c.post(f"/cases/{cid}/mock-audit/manual",
                    json={"decision": "reject", "comment": "보완 필요2"}, headers=_h(aud))
        assert r2.status_code == 200 and r2.json()["round"] == 2, r2.text
        # reject는 사유 필수(422)
        rbad = c.post(f"/cases/{cid}/mock-audit/manual",
                      json={"decision": "reject", "comment": ""}, headers=_h(aud))
        assert rbad.status_code == 422 and rbad.json()["detail"]["code"] == "REASON_REQUIRED", rbad.text
        # 잘못된 decision(422)
        rdec = c.post(f"/cases/{cid}/mock-audit/manual",
                      json={"decision": "maybe", "comment": "x"}, headers=_h(aud))
        assert rdec.status_code == 422 and rdec.json()["detail"]["code"] == "INVALID_DECISION", rdec.text
        # approve 기록 → 최신 상태 approve
        ra = c.post(f"/cases/{cid}/mock-audit/manual",
                    json={"decision": "approve", "comment": ""}, headers=_h(aud))
        assert ra.status_code == 200, ra.text
        d = c.get(f"/cases/{cid}/mock-audit/detail", headers=_h(aud)).json()
        assert d["manual"]["decision"] == "approve", d["manual"]
        # 코멘트 이력에 reject 2건 포함
        rejects = [h for h in d["manual_history"] if h["decision"] == "reject"]
        assert len(rejects) == 2, d["manual_history"]


def test_evidence_verdict_latest_wins_and_validation():
    """② 섹션별 latest-wins(같은 섹션 2번 저장→최신 반영), 잘못된 section/verdict 422."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # facility 섹션 nonconformity → comply 로 갱신(latest-wins)
        assert c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
                      json={"section": "facility", "verdict": "nonconformity",
                            "corrective_action": "환기 개선"}, headers=_h(aud)).status_code == 200
        assert c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
                      json={"section": "facility", "verdict": "comply"}, headers=_h(aud)).status_code == 200
        # hygiene 섹션 nonconformity
        assert c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
                      json={"section": "hygiene", "verdict": "nonconformity",
                            "corrective_action": "손세정"}, headers=_h(aud)).status_code == 200
        d = c.get(f"/cases/{cid}/mock-audit/detail", headers=_h(aud)).json()
        secs = {s["section"]: s for s in d["sections"]}
        assert len(d["sections"]) == 5, d["sections"]
        assert secs["facility"]["verdict"] == "comply", secs["facility"]   # 최신 반영
        assert secs["hygiene"]["verdict"] == "nonconformity", secs["hygiene"]
        assert secs["hygiene"]["corrective_action"] == "손세정", secs["hygiene"]
        # 판정 없는 섹션은 None
        assert secs["material_storage"]["verdict"] is None, secs["material_storage"]
        # 잘못된 section(422)
        rs = c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
                    json={"section": "nope", "verdict": "comply"}, headers=_h(aud))
        assert rs.status_code == 422 and rs.json()["detail"]["code"] == "BAD_SECTION", rs.text
        # 잘못된 verdict(422)
        rv = c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
                    json={"section": "facility", "verdict": "ok"}, headers=_h(aud))
        assert rv.status_code == 422 and rv.json()["detail"]["code"] == "INVALID_VERDICT", rv.text


def test_ai_report_run_deterministic_fields():
    """③ ai-report/run 결정적 경로 200 + 집계 필드 반환(LLM 미의존 — 텍스트 내용 단언 회피)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # 증거 판정 2건(comply 1·nonconformity 1) 세팅
        c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
               json={"section": "facility", "verdict": "comply"}, headers=_h(aud))
        c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
               json={"section": "hygiene", "verdict": "nonconformity",
                     "corrective_action": "청소"}, headers=_h(aud))
        r = c.post(f"/cases/{cid}/mock-audit/ai-report/run", json={}, headers=_h(aud))
        assert r.status_code == 200, r.text
        rep = r.json()["report"]
        # 결정적 집계 필드 존재·값 검증
        for k in ("summary", "recommendations", "verdict_overall", "comply", "nonconformity",
                  "sections", "sjph_completion", "evidence_docs", "manual_status"):
            assert k in rep, (k, rep)
        assert rep["comply"] == 1 and rep["nonconformity"] == 1, rep
        assert rep["verdict_overall"] == "부적합", rep   # nonconformity 존재
        assert len(rep["sections"]) == 5, rep["sections"]
        # detail 로도 최신 리포트 조회 가능
        d = c.get(f"/cases/{cid}/mock-audit/detail", headers=_h(aud)).json()
        assert d["ai_report"] and d["ai_report"]["comply"] == 1, d["ai_report"]


def test_mock_audit_endpoints_rbac_403_for_applicant():
    """오디터 뷰 판정 라우트는 신청자에게 403(기존 audit.mock_decide 권한 재사용)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        assert c.post(f"/cases/{cid}/mock-audit/manual",
                      json={"decision": "approve"}, headers=_h(ap)).status_code == 403
        assert c.post(f"/cases/{cid}/mock-audit/evidence-verdict",
                      json={"section": "facility", "verdict": "comply"},
                      headers=_h(ap)).status_code == 403
        assert c.post(f"/cases/{cid}/mock-audit/ai-report/run",
                      json={}, headers=_h(ap)).status_code == 403
