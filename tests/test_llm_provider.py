"""LLM 공급자 라우팅 — 같은 소스로 세 배포가 돈다.

  ① AI 없음      ② 로컬 Ollama      ③ 원격 API(GPT 호환)

셋을 오가는 데 소스를 고치면 안 된다. 어느 것을 쓸지는 환경이 정하고, 코드는
그 결정을 따르기만 한다. 그리고 **결정적 판정(온톨로지·구조 파서)은 공급자와
무관하게 같은 답**을 내야 한다 — 그게 다르면 심사 결과가 배포마다 달라진다.

실행: <venv>/bin/python -m pytest tests/test_llm_provider.py -q
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_prov_test.db")

import app.ai_local as ai_local   # noqa: E402


def _reload(**env):
    """환경만 바꿔 모듈을 다시 읽는다 — 배포에서 env 만 바꾸는 것과 같은 조건."""
    old = {}
    for k in ("GLHAC_LLM_PROVIDER", "GLHAC_OPENAI_KEY", "OPENAI_API_KEY",
              "GLHAC_OPENAI_BASE", "GLHAC_OPENAI_MODEL"):
        old[k] = os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    mod = importlib.reload(ai_local)
    return mod, old


def _restore(old):
    for k in ("GLHAC_LLM_PROVIDER", "GLHAC_OPENAI_KEY", "OPENAI_API_KEY",
              "GLHAC_OPENAI_BASE", "GLHAC_OPENAI_MODEL"):
        os.environ.pop(k, None)
        if old.get(k) is not None:
            os.environ[k] = old[k]
    importlib.reload(ai_local)


# ── 명시 지정 ────────────────────────────────────────────────────────────
def test_none이면_호출하지_않고_바로_답한다():
    """죽은 주소로 매번 붙어보면 문서 한 건당 수십 초가 날아간다."""
    m, old = _reload(GLHAC_LLM_PROVIDER="none")
    try:
        assert m.provider() == "none"
        assert m.llm_json("s", "u") == {"error": "LLM_UNAVAILABLE"}
        assert m.llm_text("s", "u") == ""
        h = m.health()
        assert h["provider"] == "none" and h["model_ready"] is False
    finally:
        _restore(old)


def test_openai로_지정하면_원격을_본다():
    m, old = _reload(GLHAC_LLM_PROVIDER="openai", GLHAC_OPENAI_KEY="sk-x",
                     GLHAC_OPENAI_BASE="https://example.invalid/v1",
                     GLHAC_OPENAI_MODEL="gpt-4o-mini")
    try:
        assert m.provider() == "openai"
        h = m.health()
        assert h["provider"] == "openai"
        assert h["endpoint"] == "https://example.invalid/v1"
        assert h["configured"] == "gpt-4o-mini"
    finally:
        _restore(old)


def test_ollama로_지정하면_로컬을_본다():
    m, old = _reload(GLHAC_LLM_PROVIDER="ollama")
    try:
        assert m.provider() == "ollama"
        assert m.health()["provider"] == "ollama"
    finally:
        _restore(old)


# ── auto ────────────────────────────────────────────────────────────────
def test_auto는_로컬을_먼저_본다(monkeypatch):
    """인증 서류에는 대외비가 들어 있다 — 원격으로 나가는 건 우연이 아니라 결정이어야 한다.
    실측: 셸에 OPENAI_API_KEY 가 있어 원격 우선이면 Ollama 가 떠 있는데도 밖으로 나갔다."""
    m, old = _reload(OPENAI_API_KEY="sk-x")
    try:
        monkeypatch.setattr(m, "_ollama_up", lambda timeout=2.5: True)
        m._provider_cache.update({"value": None, "at": 0})
        assert m.provider(refresh=True) == "ollama"
    finally:
        _restore(old)


def test_auto는_로컬이_없으면_원격으로(monkeypatch):
    m, old = _reload(OPENAI_API_KEY="sk-x")
    try:
        monkeypatch.setattr(m, "_ollama_up", lambda timeout=2.5: False)
        m._provider_cache.update({"value": None, "at": 0})
        assert m.provider(refresh=True) == "openai"
    finally:
        _restore(old)


def test_auto는_둘_다_없으면_none(monkeypatch):
    m, old = _reload()
    try:
        monkeypatch.setattr(m, "_ollama_up", lambda timeout=2.5: False)
        m._provider_cache.update({"value": None, "at": 0})
        assert m.provider(refresh=True) == "none"
    finally:
        _restore(old)


# ── 공급자가 바뀌어도 같아야 하는 것 ─────────────────────────────────────
def test_원재료_판정은_공급자와_무관하다(tmp_path):
    """온톨로지 사전 판정 — 배포마다 심사 결과가 달라지면 안 된다.

    함정: load_ontology 를 부르지 않으면 매칭 캐시가 비어 **전부 mushbooh** 로 나온다.
    그 상태로 비교하면 '세 모드가 같다'는 결론이 나오는데, 실은 셋 다 아무것도 판정하지
    못한 것이다(실측으로 한 번 속았다). 그래서 사전을 실제로 적재하고 비교한다."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models, screening
    from app.ontology_seed import seed as seed_onto
    eng = create_engine("sqlite:///%s" % (tmp_path / "onto.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    seed_onto(db)
    screening.load_ontology(db)

    want = {}
    for pv in ("none", "ollama", "openai"):
        m, old = _reload(GLHAC_LLM_PROVIDER=pv, GLHAC_OPENAI_KEY="sk-x")
        try:
            got = {n: screening.screen_merged(n)["status"]
                   for n in ("lard", "gelatin", "Air Pam", "Bahan Tidak Dikenal")}
            if not want:
                want = got
            assert got == want, (pv, got, want)
        finally:
            _restore(old)
    # 사전이 실제로 적재됐는지 — 전부 mushbooh 면 위 비교가 무의미하다
    assert want["lard"] == "haram", want
    assert want["Air Pam"] == "halal", want


def test_파일명_판정은_공급자와_무관하다():
    """LLM 이 붙든 안 붙든 파일명이 확정하는 유형은 같아야 한다."""
    from app import intake
    want = {}
    for pv in ("none", "ollama", "openai"):
        m, old = _reload(GLHAC_LLM_PROVIDER=pv, GLHAC_OPENAI_KEY="sk-x")
        try:
            got = {f: intake.refine_doctype_reason(f, None)[0]
                   for f in ("Halal_Policy.pdf", "Pork free Statement.pdf",
                             "Diagram alir proses produksi.PNG")}
            if not want:
                want = got
            assert got == want, (pv, got, want)
        finally:
            _restore(old)


def test_공급자가_역량응답에_드러난다():
    from fastapi.testclient import TestClient
    import app.main as m
    with TestClient(m.app) as c:
        cap = c.get("/system/capabilities").json()
        assert "provider" in cap["capabilities"]["llm"]
