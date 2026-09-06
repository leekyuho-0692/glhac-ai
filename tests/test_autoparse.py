"""업로드 후 자동 파싱 — 올려두고 값이 안 채워지는 서류를 없앤다.

실측 배경: POST /cases/{id}/documents 는 0.0초에 저장만 하고 끝났다. 파싱·OCR 은
사람이 재처리를 눌러야 돌았다. 컨설턴트가 붙은 데모에서는 안 드러나지만, 클라이언트가
혼자 올리면 doc_type=other 인 채로 남아 회사명·원재료가 비어 있었다.

업로드 응답은 여전히 즉시 끝나야 한다(OCR 은 10초씩 걸린다) — 그래서 큐다.

실행: <venv>/bin/python -m pytest tests/test_autoparse.py -q
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_autoparse_test.db")

from fastapi.testclient import TestClient   # noqa: E402

import app.main as m                        # noqa: E402
from app.main import app                    # noqa: E402

PDF = "JVBERi0xLjQK"


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _drain(timeout=30):
    """큐가 빌 때까지 기다린다(백그라운드 스레드)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        q = m._autoparse_q
        if q is not None and q.unfinished_tasks == 0:
            return True
        time.sleep(0.2)
    return False


def test_업로드가_큐에_들어간다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Auto"}, headers=h).json()["case_id"]
        r = c.post(f"/cases/{cid}/documents",
                   json={"filename": "Halal_Policy.pdf", "file_b64": PDF}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["autoparse_queued"] is True


def test_업로드_응답은_파싱을_기다리지_않는다():
    """OCR 은 10초씩 걸린다 — 업로드가 그걸 기다리면 화면이 멈춘 것처럼 보인다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Fast"}, headers=h).json()["case_id"]
        t0 = time.time()
        c.post(f"/cases/{cid}/documents",
               json={"filename": "Halal_Team.pdf", "file_b64": PDF}, headers=h)
        assert time.time() - t0 < 3.0


def test_큐가_문서를_실제로_처리한다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Done"}, headers=h).json()["case_id"]
        before = m._autoparse_state["done"]
        c.post(f"/cases/{cid}/documents",
               json={"filename": "Pork free Statement.pdf", "file_b64": PDF}, headers=h)
        assert _drain(), "큐가 비지 않았다"
        assert m._autoparse_state["done"] > before


def test_유형을_안_주면_자동_파싱이_확정한다():
    """저장 시 유형이 없으면 other 로 들어간다 — 큐가 파일명·본문을 보고 확정한다."""
    import app.models as models
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Fix"}, headers=h).json()["case_id"]
        did = c.post(f"/cases/{cid}/documents",
                     json={"filename": "Halal_Policy.pdf", "file_b64": PDF},
                     headers=h).json()["document_id"]
        assert _drain()
        db = m.SessionLocal()
        try:
            d = db.get(models.DocumentAsset, did)
            assert d.doc_type == "sjph_manual", d.doc_type
            assert d.confidence and d.confidence > 0
        finally:
            db.close()


def test_사용자가_정한_유형은_덮지_않는다():
    """무엇을 낸 서류인지는 올린 사람이 안다. 모의심사 증거·입금증처럼 본문으로는
    알 수 없는 것도 있어, 자동 파싱이 유형을 바꾸면 편철이 어긋난다."""
    import app.models as models
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Keep"}, headers=h).json()["case_id"]
        did = c.post(f"/cases/{cid}/documents",
                     json={"filename": "Halal_Policy.pdf", "file_b64": PDF,
                           "doc_type": "deposit_proof"}, headers=h).json()["document_id"]
        assert _drain()
        db = m.SessionLocal()
        try:
            d = db.get(models.DocumentAsset, did)
            assert d.doc_type == "deposit_proof", d.doc_type   # 파일명이 달라도 유지
        finally:
            db.close()


def test_상태_조회로_밀린_건수를_본다():
    """서류를 올렸는데 값이 안 채워질 때 여기부터 본다."""
    with TestClient(app) as c:
        h = _tok(c)
        st = c.get("/system/autoparse", headers=h).json()
        assert set(st) >= {"enabled", "pending", "done", "failed"}
        assert st["enabled"] is True


def test_한_건_실패해도_큐는_계속_돈다(monkeypatch):
    """자동 파싱이 실패해도 업로드는 유효하다 — 사람이 재처리를 누르면 된다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Err"}, headers=h).json()["case_id"]
        boom = [True]
        orig = m._reprocess_doc

        def flaky(db, d, cc, **kw):
            if boom[0]:
                boom[0] = False
                raise RuntimeError("의도된 실패")
            return orig(db, d, cc, **kw)
        monkeypatch.setattr(m, "_reprocess_doc", flaky)
        f0 = m._autoparse_state["failed"]
        c.post(f"/cases/{cid}/documents",
               json={"filename": "a.pdf", "file_b64": PDF}, headers=h)
        c.post(f"/cases/{cid}/documents",
               json={"filename": "Halal_Team.pdf", "file_b64": PDF}, headers=h)
        assert _drain()
        assert m._autoparse_state["failed"] > f0      # 실패는 세어 둔다
        assert m._autoparse_q.unfinished_tasks == 0   # 그래도 큐는 살아 있다


def test_본문이_비어도_파일명_유형은_살린다():
    """이미지 PDF·OCR 미가동이면 본문이 빈다. 그때 파일명 사전이 답을 알고 있는데도
    other 로 떨어뜨리면, AI 없는 배포에서 유일한 판정 수단을 버리는 것이다."""
    from app import intake
    r = intake.classify("Halal_Policy.pdf", "")
    assert r["doc_type"] == "sjph_manual"
    assert r["empty"] is True and r["decided_by"] == "filename"
    # 파일명도 모르는 문서는 여전히 other
    assert intake.classify("unknown_scan.pdf", "")["doc_type"] == "other"
