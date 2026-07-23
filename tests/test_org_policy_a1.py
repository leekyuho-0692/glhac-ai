"""A-1 하이브리드 정책 — 폼 파생 조직도로 org_chart 섹션 자동 완료 인정.

담당자(대표+할랄감독자) 입력만으로 조직도 서류 별도 업로드 없이 org_chart 섹션이 완료(auto)되는지 검증.

실행: <venv>/bin/pytest tests/test_org_policy_a1.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_org_a1_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _org_chart_section(layout):
    return next((s for s in layout["sections"] if s["key"] == "org_chart"), None)


def test_org_chart_incomplete_without_persons():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"company_name": "PT Kosong"}, headers=h).json()["case_id"]
        lay = c.get(f"/cases/{cid}/sjph-manual/layout", headers=h).json()
        s = _org_chart_section(lay)
        assert s is not None and s["complete"] is False and s.get("auto") is False


def test_org_chart_auto_complete_with_persons():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"company_name": "PT Sinar Halal"}, headers=h).json()["case_id"]
        # 대표 + 할랄감독자 입력 → 파생 조직도 성립
        c.patch(f"/cases/{cid}/profile", json={"responsible_person": "홍길동",
                "halal_supervisor": "김철수"}, headers=h)
        lay = c.get(f"/cases/{cid}/sjph-manual/layout", headers=h).json()
        s = _org_chart_section(lay)
        assert s["complete"] is True and s["auto"] is True, s
        # 이미지 없이도 완료로 집계
        assert s["image"] is None


if __name__ == "__main__":
    test_org_chart_incomplete_without_persons()
    test_org_chart_auto_complete_with_persons()
    print("✅ A-1 하이브리드 자동완료 테스트 통과")
