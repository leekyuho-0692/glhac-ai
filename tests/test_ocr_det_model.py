"""모델 다운로드 없이 환경별 생성자 인자와 구버전 폴백을 검증한다."""
import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import ai_local


def mock_language_models(constructor):
    # 생성자 대역에도 실제 PaddleOCR의 언어 선택 API를 제공한다.
    models = {"korean": "korean_PP-OCRv5_mobile_rec", "ru": "eslav_PP-OCRv5_mobile_rec"}
    constructor._get_ocr_model_names = lambda self, lang, version: ("default_det", models[lang])


@pytest.fixture
def load_ocr(monkeypatch):
    def load(value):
        if value is None:
            monkeypatch.delenv("GLHAC_OCR_DET_MODEL", raising=False)
        else:
            monkeypatch.setenv("GLHAC_OCR_DET_MODEL", value)
        # 전역 모듈의 엔진·워커 상태를 건드리지 않고 시작 시 환경을 읽는다.
        spec = importlib.util.spec_from_file_location("app._test_ocr_det_model", ai_local.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return load


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), (" \t ", None),
    ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_det"),
    (" PP-OCRv5_mobile_det ", "PP-OCRv5_mobile_det"),
])
def test_detection_model_constructor_kwargs(load_ocr, monkeypatch, value, expected):
    ocr = load_ocr(value)
    constructor = Mock(return_value=object())
    mock_language_models(constructor)
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=constructor))

    engine = ocr._get_ocr_engine("korean")
    kwargs = dict(lang="korean", use_doc_orientation_classify=False,
                  use_doc_unwarping=False, use_textline_orientation=False,
                  enable_mkldnn=ocr._OCR_MKLDNN)
    if expected is not None:
        kwargs["text_detection_model_name"] = expected
        kwargs["text_recognition_model_name"] = "korean_PP-OCRv5_mobile_rec"
    constructor.assert_called_once_with(**kwargs)
    assert ocr._get_ocr_engine("korean") is engine
    assert list(ocr._ocr_engines) == ["korean"]
    assert constructor.call_count == 1


def test_legacy_constructor_rejecting_detection_model_falls_back(load_ocr, monkeypatch):
    ocr = load_ocr("PP-OCRv5_mobile_det")

    def legacy(**kwargs):
        if "text_detection_model_name" in kwargs:
            raise TypeError("unexpected keyword argument 'text_detection_model_name'")
        return object()

    constructor = Mock(side_effect=legacy)
    mock_language_models(constructor)
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=constructor))
    engine = ocr._get_ocr_engine("ru")
    assert constructor.call_args_list[0].kwargs["text_detection_model_name"] == "PP-OCRv5_mobile_det"
    constructor.assert_called_with(lang="ru")
    assert ocr._get_ocr_engine("ru") is engine
    assert list(ocr._ocr_engines) == ["ru"]
    assert constructor.call_count == 2


def test_detection_model_is_fixed_for_process_and_cache_stays_by_language(load_ocr, monkeypatch):
    ocr = load_ocr("PP-OCRv5_mobile_det")
    constructor = Mock(side_effect=lambda **kwargs: object())
    mock_language_models(constructor)
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=constructor))
    korean = ocr._get_ocr_engine("korean")
    monkeypatch.setenv("GLHAC_OCR_DET_MODEL", "PP-OCRv5_server_det")
    russian = ocr._get_ocr_engine("ru")

    assert russian is not korean
    assert ocr._get_ocr_engine("korean") is korean
    assert list(ocr._ocr_engines) == ["ru", "korean"]
    assert constructor.call_count == 2
    assert all(call.kwargs["text_detection_model_name"] == "PP-OCRv5_mobile_det"
               for call in constructor.call_args_list)
