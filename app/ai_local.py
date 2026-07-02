"""로컬 AI 어댑터 — Ollama(Qwen2.5) + PaddleOCR. 설계 24.16.
모델 교체 시 이 파일만 변경. M1에서는 /ai/health 외 호출 없음(추론은 M3)."""
import os
import json
import httpx

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# gemma3:12b — 한국어 성분명 garble 내성 우수(추출 정확도), 다국어 140+ (vs qwen2.5:7b 비교 결과)
MODEL = os.environ.get("GLHAC_LLM", "gemma3:12b")
_ocr = None


def health():
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=4)
        names = [m["name"] for m in r.json().get("models", [])]
        return {"ollama": "up", "models": names, "configured": MODEL,
                "model_ready": any(n.startswith(MODEL.split(":")[0]) for n in names)}
    except Exception as e:  # noqa: BLE001
        return {"ollama": "down", "error": str(e)}


def llm_json(system, user, timeout=90):
    """구조화 출력(JSON) — 설계 C.4 계약. 실패 시 {'error':...}."""
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", timeout=timeout, json={
            "model": MODEL, "format": "json", "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]})
        return json.loads(r.json()["message"]["content"])
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def llm_text(system, user, timeout=120):
    """평문(prose) 응답 — 설명·요약용. 실패 시 ''."""
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", timeout=timeout, json={
            "model": MODEL, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]})
        return r.json()["message"]["content"].strip()
    except Exception:  # noqa: BLE001
        return ""


def ocr_image(path, lang="korean"):
    """PaddleOCR 3.x 추출 — 설계 C.1. 미설치 시 graceful 실패."""
    global _ocr
    try:
        if _ocr is None:
            from paddleocr import PaddleOCR
            try:
                _ocr = PaddleOCR(lang=lang, use_doc_orientation_classify=False,
                                 use_doc_unwarping=False, use_textline_orientation=False)
            except TypeError:
                _ocr = PaddleOCR(lang=lang)
        result = _ocr.predict(path)
        lines = []
        for r in result:
            data = r if isinstance(r, dict) else getattr(r, "json", {}) or {}
            texts = data.get("rec_texts", []) or []
            scores = data.get("rec_scores", []) or []
            for t, s in zip(texts, scores):
                lines.append({"text": t, "confidence": float(s)})
        return {"ok": True, "lines": lines}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}