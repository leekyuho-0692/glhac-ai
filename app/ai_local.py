"""로컬 AI 어댑터 — Ollama(Qwen2.5) + PaddleOCR. 설계 24.16.
모델 교체 시 이 파일만 변경. M1에서는 /ai/health 외 호출 없음(추론은 M3)."""
import os
import sys
import json
import httpx

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# gemma3:12b — 한국어 성분명 garble 내성 우수(추출 정확도), 다국어 140+ (vs qwen2.5:7b 비교 결과)
MODEL = os.environ.get("GLHAC_LLM", "gemma3:12b")
_ocr = None

# 홍익AI/CHU-1 장기기억(장기 컨텍스트) — RAG 연동. 장애 시 로컬 폴백(차단 없음).
CHU1_URL = os.environ.get("CHU1_URL", "http://localhost:7600")
CHU1_CONTEXT = os.environ.get("CHU1_CONTEXT", "1") == "1"

# ── LLM 공급자 라우팅 ────────────────────────────────────────────────────
# 같은 소스로 세 가지 배포를 돌려야 한다: AI 없음 / 로컬 Ollama / 원격 API(GPT 호환).
# 그래서 어느 것을 쓸지는 코드가 아니라 **환경**이 정한다.
#
#   GLHAC_LLM_PROVIDER = auto | none | ollama | openai      (기본 auto)
#   auto 순서: 로컬 Ollama 가 떠 있으면 ollama → 원격 키가 있으면 openai → 없으면 none
#
# **로컬을 먼저 본다.** 인증 서류에는 업체의 대외비가 들어 있어, 원격 API 로 보내는 것은
# 우연이 아니라 결정이어야 한다. 실측: 이 기계의 셸에 OPENAI_API_KEY 가 이미 있어서
# 원격 우선으로 두면 Ollama 가 멀쩡히 떠 있는데도 서류가 밖으로 나갔다.
# 원격을 쓰려면 GLHAC_LLM_PROVIDER=openai 로 명시하거나 Ollama 를 끄면 된다.
#
# 원격은 OpenAI 호환 /chat/completions 규약이면 무엇이든 된다(Azure·vLLM·LiteLLM 등).
#   GLHAC_OPENAI_KEY(또는 OPENAI_API_KEY) · GLHAC_OPENAI_BASE · GLHAC_OPENAI_MODEL
PROVIDER = (os.environ.get("GLHAC_LLM_PROVIDER") or "auto").strip().lower()
OPENAI_KEY = os.environ.get("GLHAC_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY") or ""
OPENAI_BASE = (os.environ.get("GLHAC_OPENAI_BASE")
               or "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.environ.get("GLHAC_OPENAI_MODEL", "gpt-4o-mini")

_provider_cache = {"value": None, "at": 0.0}


def _ollama_up(timeout=2.5):
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=timeout)
        names = [m["name"] for m in r.json().get("models", [])]
        return any(n.startswith(MODEL.split(":")[0]) for n in names)
    except Exception:  # noqa: BLE001
        return False


def provider(refresh=False):
    """지금 어느 공급자를 쓰는가. auto 는 30초 캐시 — 호출마다 헬스체크하면 느려진다."""
    import time as _t
    if PROVIDER in ("none", "ollama", "openai"):
        return PROVIDER
    now = _t.time()
    if not refresh and _provider_cache["value"] and now - _provider_cache["at"] < 30:
        return _provider_cache["value"]
    val = "ollama" if _ollama_up() else ("openai" if OPENAI_KEY else "none")
    _provider_cache.update({"value": val, "at": now})
    return val


def _openai_chat(system, user, timeout, want_json, model=None):
    """OpenAI 호환 chat/completions. 실패는 예외로 올려 호출부가 기존처럼 처리한다."""
    body = {"model": model or OPENAI_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if want_json:
        body["response_format"] = {"type": "json_object"}
        body["temperature"] = 0
    r = httpx.post(f"{OPENAI_BASE}/chat/completions", timeout=timeout,
                   headers={"Authorization": "Bearer %s" % OPENAI_KEY},
                   json=body)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def search_context(query, top_k=3, timeout=3):
    """CHU-1 /search 장기기억 시맨틱 검색. 비활성/장애/타임아웃 시 [] 반환(폴백)."""
    if not CHU1_CONTEXT or not query or not str(query).strip():
        return []
    try:
        r = httpx.post(f"{CHU1_URL}/search", timeout=timeout,
                       json={"query": str(query)[:2000], "top_k": top_k})
        r.raise_for_status()
        return r.json().get("hits", []) or []
    except Exception:
        return []


def context_health():
    """CHU-1 장기기억 인덱스 상태(연결 여부 포함)."""
    if not CHU1_CONTEXT:
        return {"ok": False, "enabled": False}
    try:
        r = httpx.get(f"{CHU1_URL}/search/health", timeout=4)
        d = r.json()
        d["enabled"] = True
        return d
    except Exception as e:
        return {"ok": False, "enabled": True, "error": str(e)}


def health():
    """LLM 가용성 — 지금 선택된 공급자 기준으로 답한다.

    'ollama' 키는 화면·기존 호출부 호환으로 남긴다(원격을 쓰면 up 으로 보고한다).
    무엇을 쓰는지는 provider/base/model 로 구분한다."""
    pv = provider()
    if pv == "openai":
        base = {"provider": "openai", "endpoint": OPENAI_BASE, "configured": OPENAI_MODEL}
        try:
            r = httpx.get(f"{OPENAI_BASE}/models", timeout=5,
                          headers={"Authorization": "Bearer %s" % OPENAI_KEY})
            ok = r.status_code < 400
            return {**base, "ollama": "up" if ok else "down", "model_ready": ok,
                    "models": [], **({} if ok else {"error": "HTTP %d" % r.status_code})}
        except Exception as e:  # noqa: BLE001
            # 목록 조회를 막아둔 게이트웨이도 있다 — 키가 있으면 준비된 것으로 본다.
            return {**base, "ollama": "up", "model_ready": True, "models": [],
                    "note": "모델 목록 조회 불가(%s)" % type(e).__name__}
    if pv == "none":
        return {"provider": "none", "ollama": "down", "error": "LLM 미설정",
                "configured": None, "model_ready": False}
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=4)
        names = [m["name"] for m in r.json().get("models", [])]
        return {"provider": "ollama", "ollama": "up", "models": names, "configured": MODEL,
                "endpoint": OLLAMA,
                "model_ready": any(n.startswith(MODEL.split(":")[0]) for n in names)}
    except Exception as e:  # noqa: BLE001
        return {"provider": "ollama", "ollama": "down", "error": str(e),
                "endpoint": OLLAMA, "model_ready": False}


# PaddleOCR 모델 캐시. 첫 호출 때 여기로 내려받는다(약 230MB, 네트워크 필요).
# 배포 직후 오프라인이면 패키지는 있는데 추론이 안 된다 — import 만 보면 그걸 놓친다.
OCR_MODEL_DIR = os.environ.get(
    "PADDLE_PDX_MODEL_SOURCE_DIR",
    os.path.join(os.path.expanduser("~"), ".paddlex", "official_models"))


def ocr_status():
    """OCR 준비 상태 — 패키지·모델 캐시를 나눠 본다(엔진은 띄우지 않는다).

    엔진을 만들어 확인하면 배너 한 번 그리는 데 수십 초가 든다. 그래서 설치 여부와
    모델 캐시 존재만 본다. 모델이 없으면 '첫 문서에서 내려받는다'는 사실을 알려야
    오프라인 배포에서 조용히 실패하지 않는다."""
    import importlib.util
    pkg = importlib.util.find_spec("paddleocr") is not None
    models = []
    if os.path.isdir(OCR_MODEL_DIR):
        models = [d for d in os.listdir(OCR_MODEL_DIR)
                  if os.path.isdir(os.path.join(OCR_MODEL_DIR, d))]
    return {"installed": pkg, "models_cached": len(models), "model_dir": OCR_MODEL_DIR,
            "ready": pkg and bool(models),
            "note": None if (pkg and models) else
                    ("모델 미다운로드 — 첫 문서 처리 때 약 230MB를 내려받습니다(네트워크 필요)"
                     if pkg else "paddleocr 미설치")}


def ocr_available():
    """설치 + 모델 캐시까지 준비됐는가(배너·역량 판단용)."""
    return bool(ocr_status()["ready"])


def llm_json(system, user, timeout=90):
    """구조화 출력(JSON) — 설계 C.4 계약. 실패 시 {'error':...}.

    공급자(none/ollama/openai)는 환경이 정한다. 없으면 호출 자체를 하지 않는다 —
    죽은 주소로 매번 붙어보면 문서 한 건당 수십 초가 그냥 날아간다(실측)."""
    pv = provider()
    if pv == "none":
        return {"error": "LLM_UNAVAILABLE"}
    try:
        if pv == "openai":
            return json.loads(_openai_chat(system, user, timeout, True))
        r = httpx.post(f"{OLLAMA}/api/chat", timeout=timeout, json={
            "model": MODEL, "format": "json", "stream": False,
            "options": {"temperature": 0},   # 문서 추출/분류는 결정적이어야(재현성·필드 누락 방지)
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]})
        return json.loads(r.json()["message"]["content"])
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def llm_text(system, user, timeout=120, model=None):
    """평문(prose) 응답 — 설명·요약용. model 지정 시 해당 공급자 모델 사용. 실패 시 ''."""
    pv = provider()
    if pv == "none":
        return ""
    try:
        if pv == "openai":
            return _openai_chat(system, user, timeout, False, model).strip()
        r = httpx.post(f"{OLLAMA}/api/chat", timeout=timeout, json={
            "model": model or MODEL, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]})
        return r.json()["message"]["content"].strip()
    except Exception:  # noqa: BLE001
        return ""


def ocr_image(path, lang="korean"):
    """PaddleOCR 추출. 기본은 워커 프로세스에서 돈다(웹이 모델을 이고 있지 않게).

    인테이크(서류 접수)가 이 함수를 쓴다 — 여기가 워커를 안 타면 분리한 의미가 없다."""
    if OCR_MODE == "inproc":
        return _ocr_image_inproc(path, lang)
    r = _worker_call({"op": "ocr_lines", "path": path, "lang": lang})
    if isinstance(r, dict) and str(r.get("error", "")).startswith("WORKER_FAILED"):
        return _ocr_image_inproc(path, lang)
    return r


def _ocr_image_inproc(path, lang="korean"):
    """워커 안에서 실제로 도는 본체(그리고 GLHAC_OCR_MODE=inproc 경로)."""
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
            # 좌표를 함께 넘긴다. 표 사진은 좌표 없이 열이 섞여 행 복원이 불가능하다
            # (실측: 인니 구매·검사·보관 기록 사진에서 재료명과 담당자가 엉켰다).
            polys = data.get("rec_polys") or data.get("dt_polys") or []
            for i, (t, sc) in enumerate(zip(texts, scores)):
                item = {"text": t, "confidence": float(sc)}
                if i < len(polys):
                    try:
                        xs = [float(pt[0]) for pt in polys[i]]
                        ys = [float(pt[1]) for pt in polys[i]]
                        item["box"] = [min(xs), min(ys), max(xs), max(ys)]
                    except Exception:  # noqa: BLE001
                        pass
                lines.append(item)
        return {"ok": True, "lines": lines}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ── B-5: 언어별 엔진 캐시 + 다국어 OCR 병합 (조직도 인니/한글 혼재 대응) ──
# 기존 전역 _ocr(단일 엔진·최초 lang 고정)와 분리 — reconcile가 korean/latin을 함께 인식.
_ocr_engines = {}


def ocr_image_lang(path, lang):
    """언어별 엔진 캐시로 OCR(전역 _ocr 미오염). 미설치/실패 시 graceful.
    워커 프로세스 안에서 호출된다 — 여기서 다시 워커를 부르면 무한 재귀다."""
    try:
        eng = _ocr_engines.get(lang)
        if eng is None:
            from paddleocr import PaddleOCR
            try:
                eng = PaddleOCR(lang=lang, use_doc_orientation_classify=False,
                                use_doc_unwarping=False, use_textline_orientation=False)
            except TypeError:
                eng = PaddleOCR(lang=lang)
            _ocr_engines[lang] = eng
        result = eng.predict(path)
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


# ── OCR 워커 ────────────────────────────────────────────────────────────
# 무거운 모델(인니어 정착 2.4GB · 한국어 병용 피크 7.8GB)을 웹 프로세스 밖에 둔다.
# 상주시켜 묶음 작업의 반복 로딩을 피하되, 유휴가 지나면 종료해 메모리를 반납한다.
#   GLHAC_OCR_MODE      worker(기본) | inproc      — inproc 는 예전 동작(디버깅용)
#   GLHAC_OCR_IDLE_SEC  유휴 종료 시간(기본 180초)
#   GLHAC_OCR_TIMEOUT   한 건 제한시간(기본 180초)
OCR_MODE = (os.environ.get("GLHAC_OCR_MODE") or "worker").strip().lower()
OCR_IDLE_SEC = float(os.environ.get("GLHAC_OCR_IDLE_SEC", "180"))
OCR_TIMEOUT = float(os.environ.get("GLHAC_OCR_TIMEOUT", "180"))

_w = {"proc": None, "last": 0.0, "reaper": None}
_w_lock = None


def _wlock():
    global _w_lock
    if _w_lock is None:
        import threading
        _w_lock = threading.Lock()
    return _w_lock


def _worker_alive():
    p = _w["proc"]
    return p is not None and p.poll() is None


def _worker_start():
    import subprocess
    import threading
    import time as _t
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _w["proc"] = subprocess.Popen(
        [sys.executable, "-m", "app.ocr_worker"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)
    _w["last"] = _t.time()

    def reap():
        while True:
            _t.sleep(15)
            with _wlock():
                if not _worker_alive():
                    _w["reaper"] = None
                    return
                if _t.time() - _w["last"] > OCR_IDLE_SEC:
                    _worker_stop()
                    _w["reaper"] = None
                    return
    if _w["reaper"] is None:
        _w["reaper"] = threading.Thread(target=reap, daemon=True)
        _w["reaper"].start()


def _worker_stop():
    p = _w["proc"]
    _w["proc"] = None
    if p is None or p.poll() is not None:
        return
    try:
        p.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        p.wait(timeout=5)
    except Exception:  # noqa: BLE001
        p.kill()


def ocr_worker_status():
    """워커 상태 — 진단·테스트용."""
    import time as _t
    return {"mode": OCR_MODE, "alive": _worker_alive(),
            "idle_sec": OCR_IDLE_SEC,
            "idle_for": round(_t.time() - _w["last"], 1) if _w["last"] else None}


def _worker_call(req):
    """워커에 한 건 보내고 결과를 받는다. 죽어 있으면 띄우고, 실패하면 되살린다."""
    import json as _j
    import time as _t
    with _wlock():
        if not _worker_alive():
            _worker_start()
        p = _w["proc"]
        try:
            p.stdin.write(_j.dumps(req, ensure_ascii=False) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("worker closed")
            _w["last"] = _t.time()
            return _j.loads(line)
        except Exception as e:  # noqa: BLE001
            _worker_stop()
            return {"ok": False, "error": "WORKER_FAILED: %s" % type(e).__name__}


def _ocr_text_multi_inproc(path, langs):
    """워커 안에서 실제로 도는 본체(그리고 GLHAC_OCR_MODE=inproc 일 때의 경로)."""
    any_ok, seen, out, used = False, set(), [], []
    for lg in (langs or []):
        r = ocr_image_lang(path, lg)
        if r.get("ok"):
            any_ok = True
            used.append(lg)
            for ln in r.get("lines", []):
                t = (ln.get("text") or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return {"ok": any_ok, "text": "\n".join(out), "langs_used": used}


def ocr_text_multi(path, langs):
    """여러 언어로 OCR → 텍스트 병합. 기본은 별도 워커 프로세스에서 돈다.

    웹 프로세스가 모델을 이고 있지 않게 하려는 것이다(실측 2.4~7.8GB).
    워커가 죽거나 못 뜨면 in-process 로 떨어진다 — 서류 접수가 멈추는 것보다 낫다."""
    if OCR_MODE == "inproc":
        return _ocr_text_multi_inproc(path, langs)
    r = _worker_call({"op": "ocr", "path": path, "langs": list(langs or [])})
    if isinstance(r, dict) and str(r.get("error", "")).startswith("WORKER_FAILED"):
        log_msg = r.get("error")
        try:
            import logging
            logging.getLogger("glhac").warning("OCR 워커 실패 — in-process 로 대체: %s", log_msg)
        except Exception:  # noqa: BLE001
            pass
        return _ocr_text_multi_inproc(path, langs)
    return r