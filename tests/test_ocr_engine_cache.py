"""모델 다운로드 없이 inproc와 실제 상주 워커의 언어별 엔진 수명을 검증한다."""
import gc
import json
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app import ai_local


_TEXTS = {"korean": "한국어 판독", "ru": "Русский текст", "id": "Bahasa Indonesia"}

# 같은 가짜 생성자를 부모와 자식에 주입한다. live는 엔진 참조를 붙들지 않는다.
_FAKE_PADDLE = '''
import gc
import json
import os
import weakref

live = weakref.WeakSet()
created = {}
references = []

def released(lang, instance, trace_path):
    with open(trace_path, "a", encoding="utf-8") as trace:
        trace.write(json.dumps({"lang": lang, "instance": instance,
                                "pid": os.getpid()}) + "\\n")

class Result(dict):
    pass

class BadScore:
    def __float__(self):
        raise RuntimeError("poisoned predictor")

class PaddleOCR:
    def __init__(self, lang, **kwargs):
        cyclic = os.environ.get("GLHAC_TEST_OCR_CYCLE") == "1"
        if cyclic:
            gc.disable()
        with open(os.environ["GLHAC_TEST_OCR_TRACE"], "a", encoding="utf-8") as trace:
            trace.write(json.dumps({"lang": lang, "pid": os.getpid(),
                                    "live_before": len(live), "kwargs": kwargs}) + "\\n")
        if os.environ.get("GLHAC_TEST_OCR_LEGACY") == "1" and kwargs:
            raise TypeError("legacy constructor")
        if lang == "broken":
            raise ValueError("model unavailable")
        self.lang = lang
        created[lang] = created.get(lang, 0) + 1
        instance = created[lang]
        self.failure = (os.environ.get("GLHAC_TEST_OCR_FAILURE", "")
                        if lang == "korean" and instance == 1 else "")
        if cyclic:
            self.cycle = self
            trace_path = os.environ["GLHAC_TEST_OCR_RELEASE_TRACE"]
            references.append(weakref.ref(
                self, lambda ref: released(lang, instance, trace_path)))
        live.add(self)

    def predict(self, path):
        if self.failure == "predict":
            raise RuntimeError("poisoned predictor")
        return self.results()

    def results(self):
        text = {"korean": "한국어 판독", "ru": "Русский текст", "id": "Bahasa Indonesia"}
        result = Result(rec_texts=[text[self.lang]],
                        rec_scores=[BadScore() if self.failure == "decode" else 0.98],
                        rec_polys=[[[1, 2], [11, 2], [11, 12], [1, 12]]])
        result.engine = self
        yield result
        if self.failure == "iterate":
            raise RuntimeError("poisoned predictor")
'''


@pytest.fixture(params=["inproc", "worker"])
def cached_ocr(request, monkeypatch, tmp_path):
    """worker에서는 _worker_start/_worker_call/ocr_worker를 대체하지 않는다."""
    ai_local._worker_stop(force=True)
    trace = tmp_path / "constructors.jsonl"
    release_trace = tmp_path / "releases.jsonl"
    monkeypatch.setenv("GLHAC_TEST_OCR_TRACE", str(trace))
    monkeypatch.setenv("GLHAC_TEST_OCR_RELEASE_TRACE", str(release_trace))
    monkeypatch.delenv("GLHAC_OCR_ENGINE_CACHE_SIZE", raising=False)
    monkeypatch.delenv("GLHAC_TEST_OCR_LEGACY", raising=False)
    monkeypatch.delenv("GLHAC_TEST_OCR_CYCLE", raising=False)
    monkeypatch.delenv("GLHAC_TEST_OCR_FAILURE", raising=False)
    monkeypatch.setattr(ai_local, "_ocr_engines", {})
    monkeypatch.setattr(ai_local, "OCR_MODE", request.param)
    monkeypatch.setattr(ai_local, "OCR_TIMEOUT", 10)
    monkeypatch.setattr(ai_local, "OCR_IDLE_SEC", 180)
    fake = ModuleType("paddleocr")
    exec(compile(_FAKE_PADDLE, "fake_paddleocr.py", "exec"), fake.__dict__)
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    (tmp_path / "paddleocr.py").write_text(_FAKE_PADDLE, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [
        str(tmp_path), os.environ.get("PYTHONPATH"),
    ])))

    def recognize(lang, *, multi=False):
        if multi:
            result = ai_local.ocr_text_multi("fake.png", [lang])
            assert result == {"ok": True, "text": _TEXTS[lang], "langs_used": [lang]}
        else:
            result = ai_local.ocr_image("fake.png", lang)
            assert result == {"ok": True, "lines": [{
                "text": _TEXTS[lang], "confidence": 0.98, "box": [1, 2, 11, 12],
            }]}
        if request.param == "worker":
            assert ai_local._worker_alive(), "워커 실패 후 inproc 폴백으로 성공하면 안 된다"
            assert ai_local._ocr_engines == {}, "부모가 모델을 생성하면 안 된다"
        return result

    def events():
        return [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]

    def releases():
        if not release_trace.exists():
            return []
        return [json.loads(line) for line in release_trace.read_text(encoding="utf-8").splitlines()]

    try:
        yield SimpleNamespace(recognize=recognize, events=events, releases=releases,
                              references=fake.references, mode=request.param)
    finally:
        ai_local._worker_stop(force=True)


@pytest.fixture
def cyclic_ocr(cached_ocr, monkeypatch):
    """자동 GC가 우연히 순환 참조를 회수해 테스트가 통과하지 않게 한다."""
    enabled = gc.isenabled()
    monkeypatch.setenv("GLHAC_TEST_OCR_CYCLE", "1")
    try:
        yield cached_ocr
    finally:
        ai_local._ocr_engines.clear()
        gc.collect()
        if enabled:
            gc.enable()


@pytest.mark.parametrize("multi", [False, True])
def test_alternating_languages_reuse_two_engines(cached_ocr, multi):
    process = None
    for lang in ["korean", "ru", "korean", "ru"]:
        cached_ocr.recognize(lang, multi=multi)
        if cached_ocr.mode == "worker":
            process = process or ai_local._w["proc"]
            assert ai_local._w["proc"] is process
            assert process.poll() is None
    events = cached_ocr.events()
    assert [e["lang"] for e in events] == ["korean", "ru"]
    assert {e["pid"] for e in events} == {process.pid if process else os.getpid()}


@pytest.mark.parametrize("multi", [False, True])
def test_limit_one_replaces_engine_before_each_creation(cached_ocr, monkeypatch, multi):
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", "1")
    langs = ["korean", "ru", "korean", "ru"]
    for lang in langs:
        cached_ocr.recognize(lang, multi=multi)
    events = cached_ocr.events()
    assert [e["lang"] for e in events] == langs
    assert all(e["live_before"] == 0 for e in events)


def test_single_and_multi_language_paths_share_cache(cached_ocr):
    for lang in ["korean", "ru"]:
        cached_ocr.recognize(lang)
    for lang in ["korean", "ru"]:
        cached_ocr.recognize(lang, multi=True)
    assert [e["lang"] for e in cached_ocr.events()] == ["korean", "ru"]


def test_cache_hit_updates_lru_across_both_paths(cached_ocr):
    # korean hit가 없으면 FIFO는 id 추가 때 korean을 잘못 축출한다.
    for i, lang in enumerate(["korean", "ru", "korean", "id", "korean", "ru"]):
        cached_ocr.recognize(lang, multi=bool(i % 2))
    events = cached_ocr.events()
    assert [e["lang"] for e in events] == ["korean", "ru", "id", "ru"]
    assert max(e["live_before"] for e in events) == 1


def test_larger_limit_keeps_three_languages(cached_ocr, monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", "3")
    for lang in ["korean", "ru", "id"] * 2:
        cached_ocr.recognize(lang)
    events = cached_ocr.events()
    assert [e["lang"] for e in events] == ["korean", "ru", "id"]
    assert max(e["live_before"] for e in events) == 2


@pytest.mark.parametrize("value,expected", [("bad", 2), ("", 2), ("0", 4), ("-2", 4)])
def test_invalid_or_nonpositive_limit_is_bounded(cached_ocr, monkeypatch, value, expected):
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", value)
    for lang in ["korean", "ru"] * 2:
        cached_ocr.recognize(lang)
    assert len(cached_ocr.events()) == expected


def test_constructor_failure_is_not_cached_or_replaced_with_another_language(cached_ocr):
    cached_ocr.recognize("korean")
    for _ in range(2):
        result = ai_local.ocr_image("fake.png", "broken")
        assert result == {"ok": False, "error": "model unavailable"}
    cached_ocr.recognize("korean")
    assert [e["lang"] for e in cached_ocr.events()] == ["korean", "broken", "broken"]


def test_legacy_constructor_fallback_still_caches_by_language(cached_ocr, monkeypatch):
    monkeypatch.setenv("GLHAC_TEST_OCR_LEGACY", "1")
    for lang in ["korean", "ru"] * 2:
        cached_ocr.recognize(lang)
    events = cached_ocr.events()
    assert [e["lang"] for e in events] == ["korean", "korean", "ru", "ru"]
    assert [bool(e["kwargs"]) for e in events] == [True, False, True, False]


@pytest.mark.parametrize("cached_ocr", ["inproc"], indirect=True)
def test_smaller_limit_releases_lru_engine_on_hit(cached_ocr, monkeypatch):
    cached_ocr.recognize("korean")
    cached_ocr.recognize("ru")
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", "1")
    cached_ocr.recognize("korean")
    assert list(ai_local._ocr_engines) == ["korean"]
    cached_ocr.recognize("ru")
    assert cached_ocr.events()[-1]["live_before"] == 0


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("failure", ["predict", "iterate", "decode"])
def test_failed_engine_is_collected_and_recreated(cyclic_ocr, monkeypatch, multi, failure):
    monkeypatch.setenv("GLHAC_TEST_OCR_FAILURE", failure)
    first = (ai_local.ocr_text_multi("fake.png", ["korean"]) if multi
             else ai_local.ocr_image("fake.png", "korean"))
    assert first == ({"ok": False, "text": "", "langs_used": []} if multi else
                     {"ok": False, "error": "poisoned predictor"})
    # weakref 콜백은 실패 응답 직후, 다음 요청이나 테스트 측 GC 전에 실행돼야 한다.
    assert [(e["lang"], e["instance"]) for e in cyclic_ocr.releases()] == [("korean", 1)]
    if cyclic_ocr.mode == "inproc":
        assert cyclic_ocr.references[0]() is None
        assert "korean" not in ai_local._ocr_engines
    process = ai_local._w["proc"] if cyclic_ocr.mode == "worker" else None
    outcomes = [first["ok"]]
    for lang in ["ru", "korean", "ru"]:
        outcomes.append(cyclic_ocr.recognize(lang, multi=multi)["ok"])
        if cyclic_ocr.mode == "worker":
            assert process is not None and process.poll() is None
            assert ai_local._w["proc"] is process
    assert outcomes == [False, True, True, True]
    events = cyclic_ocr.events()
    assert [e["lang"] for e in events] == ["korean", "ru", "korean"]
    assert [e["live_before"] for e in events] == [0, 0, 1]
    assert {e["pid"] for e in events} == {process.pid if process else os.getpid()}


@pytest.mark.parametrize("multi", [False, True])
def test_failure_preserves_other_cached_language(cyclic_ocr, monkeypatch, multi):
    monkeypatch.setenv("GLHAC_TEST_OCR_FAILURE", "iterate")
    cyclic_ocr.recognize("ru", multi=multi)
    process = ai_local._w["proc"] if cyclic_ocr.mode == "worker" else None
    result = (ai_local.ocr_text_multi("fake.png", ["korean"]) if multi
              else ai_local.ocr_image("fake.png", "korean"))
    assert result["ok"] is False
    assert [e["lang"] for e in cyclic_ocr.releases()] == ["korean"]
    cyclic_ocr.recognize("ru", multi=multi)
    cyclic_ocr.recognize("korean", multi=multi)
    assert [e["lang"] for e in cyclic_ocr.events()] == ["ru", "korean", "korean"]
    if cyclic_ocr.mode == "worker":
        assert ai_local._w["proc"] is process


@pytest.mark.parametrize("multi", [False, True])
def test_eviction_collects_cyclic_engine_before_replacement(cyclic_ocr, monkeypatch, multi):
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", "1")
    langs = ["korean", "ru", "korean", "ru"]
    for index, lang in enumerate(langs):
        cyclic_ocr.recognize(lang, multi=multi)
        assert [e["lang"] for e in cyclic_ocr.releases()] == langs[:index]
        if cyclic_ocr.mode == "inproc":
            assert all(ref() is None for ref in cyclic_ocr.references[:-1])
            assert cyclic_ocr.references[-1]() is not None
    events = cyclic_ocr.events()
    assert [e["lang"] for e in events] == langs
    assert all(e["live_before"] == 0 for e in events)


@pytest.mark.parametrize("cached_ocr", ["inproc"], indirect=True)
def test_smaller_limit_collects_cyclic_lru_engine_on_hit(cyclic_ocr, monkeypatch):
    cyclic_ocr.recognize("korean")
    cyclic_ocr.recognize("ru")
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", "1")
    cyclic_ocr.recognize("korean")
    assert list(ai_local._ocr_engines) == ["korean"]
    assert [e["lang"] for e in cyclic_ocr.releases()] == ["ru"]
    assert cyclic_ocr.references[0]() is not None
    assert cyclic_ocr.references[1]() is None


@pytest.mark.parametrize("cached_ocr", ["inproc"], indirect=True)
@pytest.mark.parametrize("entry,args,kwargs", [
    (ai_local._ocr_image_inproc, ("fake.png",), {}),
    (ai_local._ocr_image_inproc, (), {"path": "fake.png", "lang": "korean"}),
    (ai_local.ocr_image_lang, (), {"path": "fake.png", "lang": "korean"}),
])
def test_failed_engine_cleanup_accepts_default_and_keyword_language(
        cyclic_ocr, monkeypatch, entry, args, kwargs):
    monkeypatch.setenv("GLHAC_TEST_OCR_FAILURE", "predict")
    assert entry(*args, **kwargs) == {"ok": False, "error": "poisoned predictor"}
    assert cyclic_ocr.references[0]() is None
    assert entry(*args, **kwargs)["ok"] is True
    assert [e["lang"] for e in cyclic_ocr.events()] == ["korean", "korean"]


@pytest.mark.parametrize("cached_ocr", ["inproc"], indirect=True)
def test_successful_cache_hits_do_not_collect(cyclic_ocr, monkeypatch):
    collections = []
    collect = gc.collect

    def record_collection():
        collections.append(True)
        return collect()

    monkeypatch.setattr(gc, "collect", record_collection)
    for index, lang in enumerate(["korean", "ru"] * 2):
        cyclic_ocr.recognize(lang, multi=bool(index % 2))
    assert collections == []
    assert cyclic_ocr.releases() == []
