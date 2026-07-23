"""B-5 조직도 교차검증 다국어 OCR — 회귀테스트.

- _reconcile_ocr_langs: 담당자 이름 스크립트로 OCR 언어 선택(한글→korean+latin, 인니→latin) + env 오버라이드
- ai_local.ocr_text_multi: 다국어 병합·graceful(엔진 부재 시 ok=False, 구조 유지)
- reconcile 응답에 ocr_langs 포함

실행: <venv>/bin/pytest tests/test_org_ocr_multilang_l5.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_org_l5_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, _reconcile_ocr_langs  # noqa: E402
from app import ai_local  # noqa: E402

_PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAA"
            "AAAEAAH2FzhVAAAAAElFTkSuQmCC")


def test_ocr_lang_selection_by_script():
    assert _reconcile_ocr_langs([{"name": "홍길동"}, {"name": "김철수"}]) == ["korean", "latin"]
    assert _reconcile_ocr_langs([{"name": "Budi Santoso"}, {"name": "Siti"}]) == ["latin"]
    assert _reconcile_ocr_langs([{"name": "홍길동"}, {"name": "Budi"}]) == ["korean", "latin"]


def test_ocr_lang_env_override():
    old = os.environ.get("GLHAC_OCR_LANGS")
    os.environ["GLHAC_OCR_LANGS"] = "en, korean"
    try:
        assert _reconcile_ocr_langs([{"name": "x"}]) == ["en", "korean"]
    finally:
        if old is None:
            os.environ.pop("GLHAC_OCR_LANGS", None)
        else:
            os.environ["GLHAC_OCR_LANGS"] = old


def test_ocr_text_multi_graceful_no_engine():
    # 엔진 미설치/잘못된 경로여도 구조 유지(ok=False, langs_used=[])
    r = ai_local.ocr_text_multi("/nonexistent/path/x.png", ["korean", "latin"])
    assert set(r) >= {"ok", "text", "langs_used"}
    assert r["ok"] is False and r["text"] == "" and r["langs_used"] == []


def test_ocr_text_multi_empty_langs():
    r = ai_local.ocr_text_multi("/whatever.png", [])
    assert r["ok"] is False and r["langs_used"] == []


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_reconcile_response_has_ocr_langs():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"company_name": "PT Sinar Halal"}, headers=h).json()["case_id"]
        c.patch(f"/cases/{cid}/profile", json={"responsible_person": "홍길동",
                "halal_supervisor": "김철수"}, headers=h)
        d = c.post(f"/cases/{cid}/documents",
                   json={"filename": "org.png", "file_b64": _PNG_1x1, "doc_type": "manual_section"},
                   headers=h).json()
        c.post(f"/cases/{cid}/sjph-manual/layout",
               json={"inserts": {"org_chart": {"document_id": d["document_id"], "filename": "org.png"}}}, headers=h)
        j = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h).json()
        assert "ocr_langs" in j and isinstance(j["ocr_langs"], list)


if __name__ == "__main__":
    test_ocr_lang_selection_by_script()
    test_ocr_lang_env_override()
    test_ocr_text_multi_graceful_no_engine()
    test_ocr_text_multi_empty_langs()
    test_reconcile_response_has_ocr_langs()
    print("✅ B-5 다국어 OCR 테스트 전부 통과")
