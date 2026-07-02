"""스모크 테스트 — 자기선언/정규 두 경로 + 가드 차단. 설계 24.2.6/24.11."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_test.db")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

ORG = "org_test"


def _walk(client, cid, states):
    for s in states:
        r = client.post(f"/cases/{cid}/transition", json={"to_state": s})
        assert r.status_code == 200, (s, r.json())


def test_self_declare_happy_path():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        client.post(f"/orgs/{ORG}/penyelia", json={"name": "Budi", "training_cert": "PH-1"})
        cid = client.post("/cases", json={"org_id": ORG, "company_name": "ABC", "is_msme": True}).json()["case_id"]
        # 저위험 원재료(citric acid=halal)만
        client.post(f"/cases/{cid}/materials", json={"name": "citric acid", "e_number": "E330"})
        # SIHALAL 동일아이디 연동 (1회 등록 → 동일성 검증)
        ei = client.post(f"/cases/{cid}/sihalal/identity/link", json={"external_email": "abc@x.com"}).json()
        v = client.post(f"/sihalal/identity/{ei['external_identity_id']}/verify",
                        json={"expected_identifier": "abc@x.com"}).json()
        assert v["identifier_match"] is True, v
        _walk(client, cid, ["application_draft", "ai_pre_assessment_ready",
                            "ai_pre_assessment_running", "pathway_determination"])
        a = client.post(f"/cases/{cid}/pathway/assess").json()
        assert a["suggested_pathway"] == "self_declare", a
        client.post(f"/cases/{cid}/pathway/confirm", json={"pathway": "self_declare"})
        _walk(client, cid, ["sjph_lite_prepared", "pendamping_verification"])
        client.post(f"/cases/{cid}/pendamping/assign", json={"pendamping_id": "pp_1"})
        client.post(f"/cases/{cid}/pendamping/verify", json={"decision": "verified"})
        _walk(client, cid, ["self_declaration_submitted", "committee_verification", "certificate_issued"])
        assert client.get(f"/cases/{cid}").json()["status"] == "certificate_issued"


def test_haram_blocks_selfdeclare():
    with TestClient(app) as client:
        client.post(f"/orgs/{ORG}/penyelia", json={"name": "Budi"})
        cid = client.post("/cases", json={"org_id": ORG, "is_msme": True}).json()["case_id"]
        client.post(f"/cases/{cid}/materials", json={"name": "lard"})  # haram → BLOCK
        _walk(client, cid, ["application_draft", "ai_pre_assessment_ready",
                            "ai_pre_assessment_running", "pathway_determination"])
        a = client.post(f"/cases/{cid}/pathway/assess").json()
        assert a["suggested_pathway"] == "reguler"
        # 자기선언 시도 → 가드 차단(409)
        r = client.post(f"/cases/{cid}/pathway/confirm", json={"pathway": "self_declare"})
        assert r.status_code == 409
        assert any(b["code"] == "HAS_CRITICAL_MATERIAL" for b in r.json()["detail"]["blockers"])


def test_selfdeclare_submit_blocked_without_sihalal():
    with TestClient(app) as client:
        client.post(f"/orgs/{ORG}/penyelia", json={"name": "Budi"})
        cid = client.post("/cases", json={"org_id": ORG, "is_msme": True}).json()["case_id"]
        client.post(f"/cases/{cid}/materials", json={"name": "sugar"})  # unknown→비임계
        _walk(client, cid, ["application_draft", "ai_pre_assessment_ready",
                            "ai_pre_assessment_running", "pathway_determination"])
        client.post(f"/cases/{cid}/pathway/confirm", json={"pathway": "self_declare"})
        _walk(client, cid, ["sjph_lite_prepared", "pendamping_verification"])
        client.post(f"/cases/{cid}/pendamping/assign", json={"pendamping_id": "pp_1"})
        client.post(f"/cases/{cid}/pendamping/verify", json={"decision": "verified"})
        # SIHALAL 미연동 → 제출 차단
        r = client.post(f"/cases/{cid}/transition", json={"to_state": "self_declaration_submitted"})
        assert r.status_code == 409
        assert any(b["code"] == "SIHALAL_IDENTITY_UNVERIFIED" for b in r.json()["detail"]["blockers"])


if __name__ == "__main__":
    test_self_declare_happy_path()
    test_haram_blocks_selfdeclare()
    test_selfdeclare_submit_blocked_without_sihalal()
    print("ALL SMOKE TESTS PASSED")