"""P33 부족 서류 요청서 엔드포인트 — 미충족 증빙만 나열하고, 사유와 제출 가능 서류를
언어별로 돌려주는지 확인한다.

실행: <venv>/bin/python -m pytest tests/test_doc_request_api.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_docreq_test.db")

from fastapi.testclient import TestClient   # noqa: E402

from app.main import app                    # noqa: E402


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _case(c, h):
    return c.post("/cases", json={"company_name": "PT Bahan Uji"}, headers=h).json()["case_id"]


def _add(c, cid, h, name):
    r = c.post(f"/cases/{cid}/materials", json={"name": name}, headers=h)
    assert r.status_code in (200, 201), r.text


def test_요청서는_빈_케이스에서도_형식을_지킨다():
    """원재료가 없는 케이스에서도 요청서 JSON 형식이 유지된다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        r = c.get(f"/cases/{cid}/document-requests", headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ["lang", "groups", "material_count", "request_count",
                  "by_reason", "case_id", "skipped"]:
            assert k in d, k
        assert d["request_count"] == 0
        assert d["material_count"] == 0
        assert d["groups"] == []


def test_증빙필요_원재료가_요청서에_나온다():
    """Gelatin 같은 증빙 필요 원재료가 요청서 그룹에 나온다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        _add(c, cid, h, "Gelatin")
        r = c.get(f"/cases/{cid}/document-requests", headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["request_count"] >= 1
        assert d["material_count"] >= 1
        assert len(d["groups"]) >= 1
        names = []
        for g in d["groups"]:
            assert g["reason_code"]
            assert g["reason_text"]
            for m in g["materials"]:
                names.append(m["name"])
        assert "Gelatin" in names


def test_언어별로_사유문구가_바뀐다():
    """lang 에 따라 사유 문구가 번역되어 다르게 나온다 — 고정 템플릿이 3언어로 있다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        _add(c, cid, h, "Gelatin")
        rk = c.get(f"/cases/{cid}/document-requests", params={"lang": "ko"}, headers=h)
        ri = c.get(f"/cases/{cid}/document-requests", params={"lang": "id"}, headers=h)
        assert rk.status_code == 200 and ri.status_code == 200
        dk, di = rk.json(), ri.json()
        assert dk["lang"] == "ko"
        assert di["lang"] == "id"
        sk = {g["reason_text"] for g in dk["groups"]}
        si = {g["reason_text"] for g in di["groups"]}
        assert sk
        assert si
        assert sk != si


def test_평문_엔드포인트는_텍스트를_돌려준다():
    """.txt 엔드포인트가 평문으로 원재료명을 포함해 돌려준다 — 고객 발송·출력용."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        _add(c, cid, h, "Gelatin")
        r = c.get(f"/cases/{cid}/document-requests.txt", headers=h)
        assert r.status_code == 200, r.text
        assert "text/plain" in r.headers["content-type"]
        assert r.text
        assert "Gelatin" in r.text


def test_인증없이는_거부된다():
    """토큰 없이 요청서를 조회하면 거부된다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        r = c.get(f"/cases/{cid}/document-requests")
        assert r.status_code in (401, 403)


def test_없는_케이스는_404():
    """존재하지 않는 케이스의 요청서는 404를 돌려준다."""
    with TestClient(app) as c:
        h = _tok(c)
        r = c.get("/cases/nope-does-not-exist/document-requests", headers=h)
        assert r.status_code == 404
