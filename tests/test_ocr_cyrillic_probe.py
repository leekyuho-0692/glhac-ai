"""실제 워커 JSON 왕복과 inproc에서 가짜 TextRecognition으로 샘플 인식을 검증한다."""
import gc
import hashlib
import json
import os
import sys
import threading
import time
import weakref
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import importlib.util

import pytest

# 실제 OCR 스택(numpy·cv2·paddlex 의 CropByPolys)을 쓰는 테스트다. CI 는 무거운 paddle 을 빼고 설치하므로
# (ci.yml '무거운 OCR 은 제외') 거기선 건너뛰고, 스택이 있는 로컬·운영 venv 에서 돈다.
_MISSING_OCR = [m for m in ("numpy", "cv2", "paddlex") if importlib.util.find_spec(m) is None]
# 모듈 단위 skip 은 '수집 0건'(pytest 종료코드 5)이라 파일별 격리 러너가 실패로 센다 — 테스트별로 건너뛴다.
pytestmark = pytest.mark.skipif(bool(_MISSING_OCR), reason="OCR 스택 미설치(%s) — paddle 포함 venv 에서 실행"
                                % ", ".join(_MISSING_OCR))

from PIL import Image  # noqa: E402
if not _MISSING_OCR:
    import numpy as np  # noqa: E402

from app import ai_local, document_extraction as extraction, intake


_FAKE_PADDLE = r'''
import hashlib
import json
import os
import time
import weakref

live = weakref.WeakSet()
references = []
created = 0

def record(**data):
    with open(os.environ["GLHAC_TEST_PROBE_TRACE"], "a", encoding="utf-8") as out:
        out.write(json.dumps(dict(pid=os.getpid(), **data)) + "\n")

class PaddleOCR:
    def __init__(self, **kwargs):
        raise AssertionError("probe must not create a detection pipeline")

    def _get_ocr_model_names(self, lang, version):
        assert lang == "ru" and version is None
        return "unused_detector", "eslav_PP-OCRv5_mobile_rec"

class TextRecognition:
    def __init__(self, **kwargs):
        global created
        created += 1
        record(op="construct", kwargs=kwargs, live_before=len(live))
        self.failure = os.environ.get("GLHAC_TEST_PROBE_FAILURE", "") if created == 1 else ""
        self.cycle = self
        live.add(self)
        references.append(weakref.ref(self))
        record(op="alive", count=len(live))
        if self.failure == "construct":
            raise RuntimeError("recognizer constructor failed")

    def predict(self, *, input):
        assert isinstance(input, list)
        record(op="predict", shapes=[list(c.shape) for c in input],
               pixels=[c[0, 0].tolist() for c in input],
               digests=[hashlib.sha256(c.tobytes()).hexdigest() for c in input],
               contiguous=all(c.flags.c_contiguous for c in input))
        if self.failure == "predict":
            raise RuntimeError("recognizer predict failed")
        # 설치된 PaddleOCR predict()도 결과를 list로 모두 소비한 뒤 반환한다.
        return list(self.results(input))

    def results(self, crops):
        if self.failure == "sleep":
            time.sleep(30)
        raw = os.environ.get("GLHAC_TEST_PROBE_RESULTS")
        if raw is not None:
            yield from json.loads(raw)
            return
        texts = json.loads(os.environ.get("GLHAC_TEST_PROBE_TEXTS", "null"))
        for text in (texts if texts is not None else ["English document"] * len(crops)):
            result = {"rec_text": text, "rec_score": 0.01}
            if os.environ.get("GLHAC_TEST_PROBE_JSON") == "1":
                from types import SimpleNamespace
                result = SimpleNamespace(json={"res": result})
            yield result
            if self.failure == "iterate":
                raise RuntimeError("recognizer iteration failed")
'''


def quad(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


@pytest.fixture(params=["inproc", "worker"])
def probe(request, monkeypatch, tmp_path):
    ai_local._worker_stop(force=True)
    trace = tmp_path / "probe.jsonl"
    path = tmp_path / "page.png"
    Image.new("RGB", (240, 160), (12, 34, 56)).save(path)
    monkeypatch.setenv("GLHAC_TEST_PROBE_TRACE", str(trace))
    monkeypatch.setenv("GLHAC_OCR_MKLDNN", "0")
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(tmp_path / "paddlex"))
    monkeypatch.setenv("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    monkeypatch.delenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", raising=False)
    for key in ("FAILURE", "TEXTS", "RESULTS", "JSON"):
        monkeypatch.delenv("GLHAC_TEST_PROBE_" + key, raising=False)
    monkeypatch.setattr(ai_local, "OCR_MODE", request.param)
    monkeypatch.setattr(ai_local, "OCR_TIMEOUT", 30)
    monkeypatch.setattr(ai_local, "_OCR_MKLDNN", False)
    monkeypatch.setattr(ai_local, "_cyrillic_recognizer", None)
    monkeypatch.setattr(ai_local, "_ocr_engines", {})
    fake = ModuleType("paddleocr")
    exec(compile(_FAKE_PADDLE, "fake_probe_paddleocr.py", "exec"), fake.__dict__)
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    (tmp_path / "paddleocr.py").write_text(_FAKE_PADDLE, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [
        str(tmp_path), os.environ.get("PYTHONPATH"),
    ])))

    def events():
        return [json.loads(row) for row in trace.read_text().splitlines()] if trace.exists() else []

    def run(polys=None):
        return ai_local.ocr_cyrillic_probe(str(path), polys if polys is not None else [
            quad(10, 20, 200, 40), quad(10, 70, 220, 90),
        ])

    try:
        yield SimpleNamespace(run=run, events=events, mode=request.param, fake=fake, path=path)
    finally:
        ai_local._worker_stop(force=True)
        ai_local._discard_cyrillic_recognizer()


def test_real_worker_roundtrip_and_inproc_reuse_recognizer_only(probe):
    for _ in range(2):
        assert probe.run() == {"ok": True, "has_cyrillic": False, "sampled_lines": 2,
                               "cyrillic_lines": 0, "cyrillic_chars": 0}
    events = probe.events()
    constructors = [e for e in events if e["op"] == "construct"]
    assert len(constructors) == 1
    assert constructors[0]["kwargs"] == {"model_name": "eslav_PP-OCRv5_mobile_rec", "enable_mkldnn": False}
    assert max(e["count"] for e in events if e["op"] == "alive") == 1
    assert ai_local._ocr_engines == {}
    if probe.mode == "worker":
        assert ai_local._worker_alive()
        assert ai_local._cyrillic_recognizer is None
        assert {e["pid"] for e in events} == {ai_local._w["proc"].pid}
    else:
        assert {e["pid"] for e in events} == {os.getpid()}
        assert not ai_local._worker_alive()


@pytest.mark.parametrize("texts,expected,lines,chars", [
    (["English", "Latin"], False, 0, 0),
    (["я", "ю"], True, 2, 2),
    (["Я" * 9, "Latin"], False, 1, 9),
    (["Я" * 10, "Latin"], True, 1, 10),
    (["\u0400", "\u04ff"], True, 2, 2),
    (["\u03ff", "\u0500"], False, 0, 0),
])
def test_cyrillic_thresholds_ignore_recognition_confidence(probe, monkeypatch, texts, expected, lines, chars):
    monkeypatch.setenv("GLHAC_TEST_PROBE_TEXTS", json.dumps(texts))
    result = probe.run()
    assert result == {"ok": True, "has_cyrillic": expected, "sampled_lines": 2,
                      "cyrillic_lines": lines, "cyrillic_chars": chars}


def test_polygon_crops_are_passed_as_contiguous_bgr_arrays(probe):
    result = probe.run([quad(0, 0, 240, 15), quad(10, 40, 110, 55)])
    assert result["ok"] and result["sampled_lines"] == 2
    prediction = next(e for e in probe.events() if e["op"] == "predict")
    assert prediction["shapes"] == [[15, 240, 3], [15, 100, 3]]
    assert prediction["pixels"] == [[56, 34, 12], [56, 34, 12]]
    assert prediction["contiguous"]


def test_no_usable_crop_is_inconclusive_without_loading_a_model(probe):
    result = probe.run([quad(0, 0, 200, 7)])
    assert result["ok"] is False
    assert probe.events() == []


@pytest.mark.parametrize("invalid", [None, [], [0, 0, 200, 20],
                                     [[0, 0], [20, 0], [20, 20]],
                                     quad(0, 0, float("nan"), 30),
                                     quad(0, 0, float("inf"), 30)])
def test_one_missing_or_invalid_polygon_fails_the_whole_probe(probe, invalid):
    result = probe.run([quad(10, 20, 200, 40), invalid])
    assert result["ok"] is False and "has_cyrillic" not in result
    assert probe.events() == []


def test_unavailable_paddlex_crop_fails_without_loading_a_recognizer(probe, monkeypatch):
    if probe.mode == "worker":
        (probe.path.parent / "paddlex.py").write_text("raise ImportError('crop unavailable')\n")
    else:
        monkeypatch.setitem(sys.modules,
                            "paddlex.inference.pipelines.components.common.crop_image_regions", None)
    result = probe.run()
    assert result["ok"] is False and "has_cyrillic" not in result
    assert probe.events() == []


def test_skewed_and_vertical_crops_match_installed_full_pipeline(probe):
    import cv2
    from paddlex.inference.pipelines.components.common.crop_image_regions import CropByPolys

    # 흰 이미지는 보정 차이를 가리므로 픽셀마다 다른 이미지를 사용한다.
    pixels = np.arange(160 * 240 * 3, dtype=np.uint8).reshape(160, 240, 3)
    Image.fromarray(pixels).save(probe.path)
    polys = [quad(10, 15, 30, 135),
             [[45, 30], [205, 55], [200, 80], [40, 50]],
             [[70, 10], [90, 15], [65, 140], [45, 130]]]
    expected = CropByPolys(det_box_type="quad")(cv2.imread(str(probe.path)), polys)
    assert expected[0].shape == (20, 120, 3)  # bbox crop의 120×20과 반대
    assert probe.run(polys)["ok"] is True
    prediction = next(e for e in probe.events() if e["op"] == "predict")
    assert prediction["shapes"] == [list(crop.shape) for crop in expected]
    assert prediction["digests"] == [hashlib.sha256(crop.tobytes()).hexdigest() for crop in expected]
    assert prediction["contiguous"]


@pytest.mark.parametrize("setting,expected", [(None, 12), ("999", 12), ("17", 12),
                                            ("3", 3), ("0", 1), ("-1", 1), ("bad", 12)])
def test_worker_and_inproc_inputs_are_clamped_to_one_through_twelve(probe, monkeypatch, setting, expected):
    if setting is not None:
        monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", setting)
    # 인테이크 선택을 우회해 24개를 워커에 직접 넘긴다.
    polys = [quad(i * 8, 20, i * 8 + 7, 40) for i in range(24)]
    result = probe.run(polys)
    assert result["ok"] is True and result["sampled_lines"] == expected
    prediction = next(e for e in probe.events() if e["op"] == "predict")
    assert len(prediction["shapes"]) == expected


@pytest.mark.parametrize("source", ["rec_polys", "dt_polys"])
def test_full_ocr_preserves_original_polygons_and_existing_boxes(monkeypatch, source):
    polys = [[[5, 4], [100, 17], [96, 40], [1, 27]], quad(120, 10, 140, 130)]
    data = {"rec_texts": ["English", "Document"], "rec_scores": [0.9, 0.8],
            source: np.array(polys)}
    if source == "rec_polys":
        data["dt_polys"] = [quad(0, 0, 1, 1)]
    else:
        data["rec_polys"] = np.array([])
    engine = SimpleNamespace(predict=lambda path: [data])
    monkeypatch.setattr(ai_local, "_get_ocr_engine", lambda lang: engine)
    result = ai_local._ocr_image_inproc("page.png")
    assert result == {"ok": True, "lines": [
        {"text": "English", "confidence": 0.9, "box": [1, 4, 100, 40], "poly": polys[0]},
        {"text": "Document", "confidence": 0.8, "box": [120, 10, 140, 130], "poly": polys[1]},
    ]}
    assert json.loads(json.dumps(result)) == result


def test_exif_rotated_photo_uses_same_coordinates_as_full_ocr(probe):
    path = probe.path.with_suffix(".jpg")
    exif = Image.Exif()
    exif[274] = 6  # OpenCV/PaddleX는 시계 방향 90도 회전한 80×120 좌표를 반환한다.
    Image.new("RGB", (120, 80), "white").save(path, exif=exif)
    result = ai_local.ocr_cyrillic_probe(str(path), [quad(0, 90, 80, 120)])
    assert result["ok"] and result["sampled_lines"] == 1
    prediction = next(e for e in probe.events() if e["op"] == "predict")
    assert prediction["shapes"] == [[30, 80, 3]]


def test_result_json_wrapper_is_supported(probe, monkeypatch):
    monkeypatch.setenv("GLHAC_TEST_PROBE_JSON", "1")
    monkeypatch.setenv("GLHAC_TEST_PROBE_TEXTS", json.dumps(["я", "ю"]))
    assert probe.run()["has_cyrillic"] is True


@pytest.mark.parametrize("failure", ["construct", "predict", "iterate"])
def test_failed_recognizer_is_collected_before_recreation(probe, monkeypatch, failure):
    monkeypatch.setenv("GLHAC_TEST_PROBE_FAILURE", failure)
    # 기존 언어 캐시는 샘플 인식기의 실패·재생성과 무관하다.
    engines = {"korean": object(), "ru": object()}
    ai_local._ocr_engines.update(engines)
    assert probe.run()["ok"] is False
    assert ai_local._cyrillic_recognizer is None
    assert probe.run()["ok"] is True
    constructors = [e for e in probe.events() if e["op"] == "construct"]
    assert len(constructors) == 2
    assert [e["live_before"] for e in constructors] == [0, 0]
    assert ai_local._ocr_engines == engines


@pytest.mark.parametrize("outputs", [
    [], [{"rec_text": "English", "rec_score": 1}],
    [{"rec_text": "", "rec_score": 1}] * 2,
    [{"rec_text": "English", "rec_score": 1}, {"rec_text": "", "rec_score": 1}],
    [{"rec_text": "English"}] * 2,
    [{"rec_text": None, "rec_score": 1}] * 2,
    [{"rec_text": "English", "rec_score": float("nan")}] * 2,
    [{"rec_text": "English", "rec_score": 1}] * 3,
])
def test_malformed_or_incomplete_results_cannot_produce_negative_probe(probe, monkeypatch, outputs):
    monkeypatch.setenv("GLHAC_TEST_PROBE_RESULTS", json.dumps(outputs))
    assert probe.run()["ok"] is False
    assert ai_local._cyrillic_recognizer is None


@pytest.mark.parametrize("texts", [["English", "", ""], ["English", " ", "\n"],
                                  ["Русский текст", "English", ""]])
def test_partial_recognition_failure_runs_full_russian_ocr(probe, monkeypatch, texts):
    monkeypatch.setenv("GLHAC_TEST_PROBE_TEXTS", json.dumps(texts))
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE", "1")
    monkeypatch.setattr(intake, "_OCR_FALLBACK_LANGS", ["ru"])
    polys = [quad(10, y, 200, y + 15) for y in (20, 70, 110)]
    korean = [{"text": "English", "confidence": 0.9,
               "box": [poly[0][0], poly[0][1], poly[2][0], poly[2][1]], "poly": poly}
              for poly in polys]
    calls = []

    def recognize(path, lang="korean"):
        calls.append(lang)
        return {"ok": True, "lines": korean if lang == "korean" else [
            {"text": "Русский текст", "confidence": 0.99}]}

    monkeypatch.setattr(ai_local, "ocr_image", recognize)
    assert intake._ocr_bytes(probe.path.read_bytes(), "png") == "Русский текст"
    assert calls == ["korean", "ru"]
    assert len([e for e in probe.events() if e["op"] == "predict"]) == 1


@pytest.mark.parametrize("probe", ["inproc"], indirect=True)
@pytest.mark.parametrize("missing", ["TextRecognition", "language-model"])
def test_unsupported_paddleocr_version_is_inconclusive(probe, monkeypatch, missing):
    if missing == "TextRecognition":
        monkeypatch.delattr(probe.fake, "TextRecognition")
    else:
        monkeypatch.setattr(probe.fake.PaddleOCR, "_get_ocr_model_names", lambda *a: (None, None))
    assert probe.run()["ok"] is False
    assert probe.events() == []


@pytest.mark.parametrize("error", ["WORKER_TIMEOUT", "WORKER_FAILED: broken pipe"])
def test_worker_failure_never_loads_probe_in_web_process(monkeypatch, error):
    monkeypatch.setattr(ai_local, "OCR_MODE", "worker")
    monkeypatch.setattr(ai_local, "_worker_call", lambda req: {"ok": False, "error": error})
    monkeypatch.setattr(ai_local, "_ocr_cyrillic_probe_inproc", lambda *a: pytest.fail("web model load"))
    assert ai_local.ocr_cyrillic_probe("page.png", [quad(0, 0, 20, 20)]) == {"ok": False, "error": error}


@pytest.mark.parametrize("probe", ["inproc"], indirect=True)
def test_inproc_timeout_discards_recognizer(probe, monkeypatch):
    assert probe.run()["ok"] is True
    reference = weakref.ref(ai_local._cyrillic_recognizer)
    original = probe.fake.TextRecognition.predict

    def slow(self, **kwargs):
        time.sleep(0.03)
        return original(self, **kwargs)

    monkeypatch.setattr(probe.fake.TextRecognition, "predict", slow)
    monkeypatch.setattr(ai_local, "OCR_TIMEOUT", 0.01)
    result = probe.run()
    assert result == {"ok": False, "error": "CYRILLIC_PROBE_TIMEOUT"}
    assert ai_local._cyrillic_recognizer is None and not probe.fake.live
    assert reference() is None


@pytest.mark.parametrize("probe", ["worker"], indirect=True)
def test_probe_worker_timeout_kills_worker(probe, monkeypatch):
    monkeypatch.setenv("GLHAC_TEST_PROBE_FAILURE", "sleep")
    assert ai_local._worker_call({"op": "ping"}) == {"ok": True}
    process = ai_local._w["proc"]
    monkeypatch.setattr(ai_local, "OCR_TIMEOUT", 0.8)
    assert probe.run() == {"ok": False, "error": "WORKER_TIMEOUT"}
    assert process.poll() is not None and not ai_local._worker_alive()
    assert ai_local._cyrillic_recognizer is None


@pytest.mark.parametrize("cancel", [True, False], ids=["cancel", "deadline"])
def test_probe_lock_wait_observes_cancellation_and_deadline(probe, cancel):
    stopped = threading.Event()
    entered = threading.Event()
    lock = ai_local._wlock() if probe.mode == "worker" else ai_local._ocr_lock

    def run():
        token = intake._ARCHIVE_CONTROL.set((stopped, None if cancel else time.monotonic() + 0.15))
        entered.set()
        try:
            return probe.run()
        finally:
            intake._ARCHIVE_CONTROL.reset(token)

    with ThreadPoolExecutor(1) as pool:
        with lock:
            future = pool.submit(run)
            assert entered.wait(1)
            if cancel:
                stopped.set()
            with pytest.raises(CancelledError if cancel else intake.ArchiveError):
                future.result(timeout=2)
    assert probe.events() == []


@pytest.mark.parametrize("probe", ["inproc"], indirect=True)
def test_cancellation_during_recognition_releases_model_with_retained_exception(probe, monkeypatch):
    stopped = threading.Event()
    original = probe.fake.TextRecognition.predict

    def cancel(self, **kwargs):
        stopped.set()
        return original(self, **kwargs)

    monkeypatch.setattr(probe.fake.TextRecognition, "predict", cancel)
    token = extraction._ACTIVE_STOPPED.set(stopped)
    try:
        with pytest.raises(CancelledError) as retained:
            probe.run()
        assert ai_local._cyrillic_recognizer is None
        assert retained.value.__traceback__ is not None
        gc.collect()
        assert probe.fake.references and all(ref() is None for ref in probe.fake.references)
    finally:
        extraction._ACTIVE_STOPPED.reset(token)


@pytest.mark.parametrize("probe", ["inproc"], indirect=True)
@pytest.mark.parametrize("stage", ["construct", "predict", "iterate"])
@pytest.mark.parametrize("error_type", [CancelledError, intake.ArchiveError, RuntimeError])
def test_retained_exception_chain_does_not_keep_recognizer_alive(probe, monkeypatch, stage, error_type):
    retained = error_type("recognition interrupted")
    original = probe.fake.TextRecognition.__init__

    def fail(self):
        try:
            raise RuntimeError("predictor cause holds self")
        except RuntimeError as cause:
            raise retained from cause

    def construct(self, **kwargs):
        original(self, **kwargs)
        fail(self)

    def predict(self, **kwargs):
        if stage == "predict":
            fail(self)

        def results():
            # 구버전의 지연 결과도 self를 붙들고 있을 수 있다.
            yield {"rec_text": "English", "rec_score": 0.9}
            fail(self)

        return results()

    monkeypatch.setattr(probe.fake.TextRecognition,
                        "__init__" if stage == "construct" else "predict",
                        construct if stage == "construct" else predict)
    if error_type is RuntimeError:
        assert probe.run() == {"ok": False, "error": "recognition interrupted"}
    else:
        with pytest.raises(error_type) as caught:
            probe.run()
        assert caught.value is retained
    if error_type is not RuntimeError:
        assert retained.__traceback__ is not None  # 재전파 경로는 여전히 확인 가능
    assert str(retained.__cause__) == "predictor cause holds self"
    assert ai_local._cyrillic_recognizer is None
    gc.collect()
    assert probe.fake.references and all(ref() is None for ref in probe.fake.references)


@pytest.mark.parametrize("probe", ["worker"], indirect=True)
def test_cancellation_during_worker_probe_kills_worker(probe, monkeypatch):
    monkeypatch.setenv("GLHAC_TEST_PROBE_FAILURE", "sleep")
    assert ai_local._worker_call({"op": "ping"}) == {"ok": True}
    process = ai_local._w["proc"]
    stopped = threading.Event()

    def run():
        token = extraction._ACTIVE_STOPPED.set(stopped)
        try:
            return probe.run()
        finally:
            extraction._ACTIVE_STOPPED.reset(token)

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run)
        try:
            deadline = time.monotonic() + 5
            while not any(e["op"] == "predict" for e in probe.events()):
                assert time.monotonic() < deadline
                time.sleep(0.01)
        finally:
            stopped.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
    assert process.poll() is not None and not ai_local._worker_alive()


@pytest.mark.parametrize("probe", ["inproc"], indirect=True)
@pytest.mark.parametrize("limit", [1, 2])
def test_probe_and_full_ocr_share_lock_and_preserve_language_lru(probe, monkeypatch, limit):
    from contextlib import contextmanager

    state = {"active": 0, "peak": 0, "recognizers": 0}
    live_engines = weakref.WeakSet()
    counter_lock = threading.Lock()

    @contextmanager
    def busy():
        assert ai_local._ocr_lock.locked()
        assert len(live_engines) <= limit
        assert len(probe.fake.live) <= 1
        state["recognizers"] = max(state["recognizers"], len(probe.fake.live))
        with counter_lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(0.01)
            yield
        finally:
            with counter_lock:
                state["active"] -= 1

    class Engine:
        _get_ocr_model_names = probe.fake.PaddleOCR._get_ocr_model_names

        def __init__(self, lang, **kwargs):
            with busy():
                assert len(live_engines) < limit  # 언어 엔진은 생성 전에 축출
                self.lang = lang
                live_engines.add(self)

        def predict(self, path):
            with busy():
                yield {"rec_texts": [self.lang], "rec_scores": [0.9]}

    original = probe.fake.TextRecognition.predict

    def predict(self, **kwargs):
        with busy():
            return original(self, **kwargs)

    monkeypatch.setattr(probe.fake, "PaddleOCR", Engine)
    monkeypatch.setattr(probe.fake.TextRecognition, "predict", predict)
    monkeypatch.setenv("GLHAC_OCR_ENGINE_CACHE_SIZE", str(limit))

    def run(lang):
        return probe.run() if lang is None else ai_local._ocr_image_inproc(str(probe.path), lang)

    for lang in ("korean", None, "ru"):
        assert run(lang)["ok"]
    assert len(live_engines) == limit and len(probe.fake.live) == 1
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(run, [None, "korean", "ru", None, "id"] * 3))
    assert all(r["ok"] for r in results) and state["peak"] == 1
    assert len(ai_local._ocr_engines) == limit and len(live_engines) == limit
    assert state["recognizers"] == 1 and len(probe.fake.live) == 1
    assert len([e for e in probe.events() if e["op"] == "construct"]) == 1
