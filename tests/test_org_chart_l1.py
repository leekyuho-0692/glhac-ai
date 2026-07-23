"""L1 할랄팀 조직도 자동생성 — 데이터 계약 회귀테스트.

manualBuilder(프런트)가 org_chart 섹션에서 조직도 초안을 파생할 때 의존하는 API 경로가
담당자(대표·할랄감독자·PIC·CP)·Penyelia·org_chart 섹션을 정상 제공하는지 검증한다.
프런트 JS(deriveHalalOrg/renderOrgTree/orgToSvg/svgToPngFile)는 node/브라우저에서 별도 검증.

실행: <venv>/bin/python tests/test_org_chart_l1.py   (TestClient 기반, 서버 불필요)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_orgchart_test.db")
os.environ.setdefault("GLHAC_DEV", "1")   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_org_chart_l1_data_contract():
    """신청서 담당자·Penyelia·org_chart 섹션이 manualBuilder에 정상 공급되는지."""
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        # 케이스 생성
        r = c.post("/cases", json={"company_name": "PT Sinar Halal"}, headers=h)
        assert r.status_code in (200, 201), r.text
        cid = r.json()["case_id"]
        # 담당자 프로필 설정(대표·할랄감독자·PIC·CP)
        prof = {"company_name": "PT Sinar Halal", "responsible_person": "홍길동",
                "halal_supervisor": "김철수",
                "profile_ext": {"pic_name": "이영희", "pic_title": "생산부장",
                                "cp_name": "박민수", "cp_title": "구매"}}
        assert c.patch(f"/cases/{cid}/profile", json=prof, headers=h).status_code == 200
        # Penyelia 추가
        assert c.post("/orgs/org_demo/penyelia", json={"name": "최지훈"}, headers=h).status_code == 200
        # 케이스 GET — 담당자 복호화되어 조직도 파생 입력으로 제공
        d = c.get(f"/cases/{cid}", headers=h).json()
        assert d["responsible_person"] == "홍길동", "대표자 미제공"
        assert d["halal_supervisor"] == "김철수", "할랄감독자 미제공"
        assert (d.get("profile_ext") or {}).get("pic_name") == "이영희", "PIC 미제공"
        assert (d.get("profile_ext") or {}).get("cp_name") == "박민수", "CP 미제공"
        # Penyelia 목록 — 조직도 penyelia 노드 입력
        pj = c.get(f"/orgs/{d['org_id']}/penyelia", headers=h).json()
        assert any(x["name"] == "최지훈" for x in pj.get("items", [])), "Penyelia 목록 미제공"
        # sjph-manual layout — org_chart 섹션 존재(조직도 주입 지점)
        lj = c.get(f"/cases/{cid}/sjph-manual/layout", headers=h).json()
        keys = [s["key"] for s in lj.get("sections", [])]
        assert "org_chart" in keys, "org_chart 섹션 부재"


if __name__ == "__main__":
    test_org_chart_l1_data_contract()
    print("✅ test_org_chart_l1 통과 — 조직도 자동생성 데이터 계약 정상")
