"""qwen2.5:7b vs gemma3:12b — 우리 과제(다국어 성분판정) 직접 비교.
① 성분 추출 정확도(OCR garble 내성) ② 다국어 설명(ko/id/en) ③ 속도."""
import json
import time
import httpx

OLLAMA = "http://localhost:11434"
MODELS = ["qwen2.5:7b", "gemma3:12b"]

# 실제 OCR 원문(레시틴 등 — qwen이 과거 '레시チン'으로 깬 케이스)
OCR_TEXT = (
    "전성분(INGREDIENTS)\n정제수,글리세린(Glycerin),젤라틴(Gelatin),\n"
    "부틸렌글라이콜,레시틴(Lecithin),\n구연산(Citric Acid),향료(Fragrance)"
)
EXTRACT_SYS = ('식품/화장품 라벨 성분 추출기. OCR 텍스트에서 성분명만 JSON으로. '
               '형식: {"ingredients":["글리세린","젤라틴",...]}. 제품명/용량/제조사 제외.')

EXPLAIN_SYS = "할랄 인증 컨설턴트. 아래 성분의 할랄 위험 사유를 해당 언어로 2문장 이내."
EXPLAIN_CASES = [
    ("ko", "젤라틴 (동물성 원료, 인증서·출처 증빙 필요)"),
    ("id", "gelatin (bahan hewani, perlu sertifikat halal dan deklarasi sumber)"),
    ("en", "gelatin (animal-derived, requires halal certificate and source declaration)"),
]


def chat(model, system, user, fmt=None, timeout=180):
    body = {"model": model, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if fmt:
        body["format"] = fmt
    t = time.time()
    r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
    dt = time.time() - t
    return r.json()["message"]["content"], dt


def main():
    for model in MODELS:
        print(f"\n{'='*60}\n### {model}\n{'='*60}")
        # ① 성분 추출 (JSON)
        try:
            out, dt = chat(model, EXTRACT_SYS, OCR_TEXT, fmt="json")
            try:
                ings = json.loads(out).get("ingredients", [])
            except Exception:
                ings = [f"(JSON 파싱실패) {out[:120]}"]
            leck = any("레시틴" in str(x) for x in ings)
            print(f"[①추출 {dt:.1f}s] {ings}")
            print(f"   레시틴 정확추출: {'O' if leck else 'X (garble?)'}")
        except Exception as e:
            print(f"[①추출] ERROR {e}")
        # ② 다국어 설명
        for lang, ing in EXPLAIN_CASES:
            try:
                out, dt = chat(model, EXPLAIN_SYS, ing)
                print(f"[②설명 {lang} {dt:.1f}s] {out.strip()[:160]}")
            except Exception as e:
                print(f"[②설명 {lang}] ERROR {e}")


if __name__ == "__main__":
    main()