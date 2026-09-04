"""AI 없이도 업무가 돌아가는가 — 그리고 '조용히' 돌지는 않는가.

배포 환경에 Ollama·PaddleOCR 이 없을 수 있다. 그때 시스템이 멈추면 안 되고,
반대로 아무 말 없이 반쯤 동작해도 안 된다. 자동으로 채워질 줄 알았던 칸이 비었는데
이유를 아무도 모르는 상태가 제일 나쁘다.

실행: <venv>/bin/python -m pytest tests/test_no_ai_mode.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_noai_test.db")

from fastapi.testclient import TestClient   # noqa: E402

import app.ai_local as ai_local             # noqa: E402
import app.intake as intake                 # noqa: E402
import app.main as m                        # noqa: E402
from app.main import app                    # noqa: E402


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _no_llm(monkeypatch):
    monkeypatch.setattr(ai_local, "health", lambda: {"ollama": "down", "error": "refused"})
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: None)
    monkeypatch.setattr(ai_local, "llm_text", lambda *a, **k: "")


# ── 역량 보고 ────────────────────────────────────────────────────────────
def test_AI_없으면_수기모드로_보고한다(monkeypatch):
    _no_llm(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/system/capabilities").json()
        assert r["ai_ready"] is False
        assert r["mode"] == "manual"
        assert r["capabilities"]["llm"]["ok"] is False


def test_꺼진_기능마다_대신할_일을_알려준다(monkeypatch):
    """'안 됩니다'로 끝나면 담당자가 멈춘다 — 무엇을 손으로 하면 되는지까지 줘야 한다."""
    _no_llm(monkeypatch)
    with TestClient(app) as c:
        steps = c.get("/system/capabilities").json()["manual_steps"]
        keys = {s["key"] for s in steps}
        assert {"doc_classify", "field_extract"} <= keys
        for s in steps:
            assert s["feature"] and s["instead"], s      # 빈 안내를 내보내지 않는다


def test_AI_없이_되는_업무를_함께_알린다(monkeypatch):
    """꺼진 것만 나열하면 '못 쓰는 시스템'으로 읽힌다."""
    _no_llm(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/system/capabilities").json()
        assert len(r["works_without_ai"]) >= 5


def test_AI_있으면_배너를_띄우지_않는다(monkeypatch):
    monkeypatch.setattr(ai_local, "health",
                        lambda: {"ollama": "up", "models": ["gemma3:12b"],
                                 "configured": "gemma3:12b", "model_ready": True})
    monkeypatch.setattr(ai_local, "ocr_available", lambda: True)
    with TestClient(app) as c:
        r = c.get("/system/capabilities").json()
        assert r["ai_ready"] is True and r["mode"] == "full"
        assert r["manual_steps"] == []


def test_OCR만_없어도_구분해_알린다(monkeypatch):
    monkeypatch.setattr(ai_local, "health",
                        lambda: {"ollama": "up", "models": ["gemma3:12b"],
                                 "configured": "gemma3:12b", "model_ready": True})
    monkeypatch.setattr(ai_local, "ocr_available", lambda: False)
    with TestClient(app) as c:
        r = c.get("/system/capabilities").json()
        assert r["mode"] == "partial"
        assert [s["key"] for s in r["manual_steps"]] == ["ocr"]


# ── AI 없이도 되는 일 ────────────────────────────────────────────────────
def test_LLM_없어도_서류유형은_파일명으로_판정된다(monkeypatch):
    """실측: CV. CITRA 제출 16개 파일 전부 유형이 맞았다."""
    _no_llm(monkeypatch)
    cases = {"Halal_Policy.pdf": "sjph_manual",
             "Pork free Statement.pdf": "supplier_declaration",
             "Diagram alir proses produksi.PNG": "process_flow",
             "Catatan pembelian barang.PNG": "other"}
    for name, want in cases.items():
        r = intake.classify(name, "본문 텍스트가 조금 있습니다")
        assert r["doc_type"] == want, (name, r["doc_type"])


def test_LLM_없어도_원재료_판정은_동작한다(monkeypatch):
    """온톨로지 사전 판정이라 LLM과 무관하다 — 이게 멈추면 업무가 선다."""
    _no_llm(monkeypatch)
    from app import screening
    r = screening.screen_merged("lard")
    assert r["status"] == "haram"
    r2 = screening.screen_merged("Bahan Tidak Dikenal", cert_no="ID001")
    assert r2["status"] == "halal" and r2["cert_promoted"] is True


def test_LLM_없어도_신청_제출까지_간다(monkeypatch):
    _no_llm(monkeypatch)
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Tanpa AI"}, headers=h).json()["case_id"]
        assert c.patch(f"/cases/{cid}/profile",
                       json={"nib": "1234567890123"}, headers=h).status_code == 200
        assert c.post(f"/cases/{cid}/products",
                      json={"name": "Bumbu", "description": "설명"},
                      headers=h).status_code == 200
        assert c.post(f"/cases/{cid}/materials",
                      json={"name": "Gula", "mat_type": "BAHAN BAKU"},
                      headers=h).status_code == 200
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["status"], "판정이 비었다"
        assert c.get(f"/cases/{cid}/doc-checklist", headers=h).status_code == 200


def test_서류_유형을_손으로_고칠_수_있다(monkeypatch):
    """파일명 판정이 틀렸을 때 되돌릴 길이 없으면 수기 모드가 성립하지 않는다."""
    _no_llm(monkeypatch)
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Reclass"}, headers=h).json()["case_id"]
        did = c.post(f"/cases/{cid}/documents",
                     json={"filename": "x.pdf", "file_b64": "JVBERi0xLjQK",
                           "doc_type": "other"}, headers=h).json()["document_id"]
        r = c.patch(f"/documents/{did}/reclassify",
                    json={"doc_type": "halal_policy"}, headers=h)
        assert r.status_code == 200, r.text


# ── 회귀: 서류 체크리스트 500 ────────────────────────────────────────────
def test_공급사_인증번호가_있어도_체크리스트가_열린다():
    """실측 결함: 사전 키에 %d 가 있는데 코드가 %d 없는 키로 찾아 TypeError.
    공급사 인증번호가 있는 케이스는 3개 언어 모두 500 이었다(2주간)."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "PT Cert"}, headers=h).json()["case_id"]
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Gelatin"},
                     headers=h).json()["material_id"]
        c.patch(f"/materials/{mid}", json={"cert_no": "ID00410000001"}, headers=h)
        for lang in ("ko", "en", "id"):
            r = c.get(f"/cases/{cid}/doc-checklist?lang={lang}", headers=h)
            assert r.status_code == 200, (lang, r.status_code)
            note = next((x.get("note") for x in r.json()["checklist"] if x.get("note")), "")
            assert "1" in note, (lang, note)      # 건수가 실제로 들어갔는지
