"""언어별 인식 모델 보존과 옵션 폴백 경고. 실제 설정 검증은 다운로드 없이 실행한다."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import ai_local


@pytest.fixture
def detection_override(monkeypatch):
    monkeypatch.setattr(ai_local, "_OCR_DET_MODEL", "PP-OCRv5_mobile_det")
    monkeypatch.setattr(ai_local, "_ocr_engines", {})


@pytest.fixture(scope="module")
def real_configs(tmp_path_factory):
    # 별도 프로세스라 다른 테스트의 가짜 paddleocr와 전역 캐시가 섞이지 않는다.
    cache = tmp_path_factory.mktemp("paddleocr_config")
    env = os.environ.copy()
    env.update(PADDLE_PDX_CACHE_HOME=str(cache),
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
               MPLCONFIGDIR=str(cache / "matplotlib"),
               GLHAC_OCR_DET_MODEL="PP-OCRv5_mobile_det")
    script = '''
import json
from paddleocr import PaddleOCR
from app import ai_local

class ConfigOnlyOCR(PaddleOCR):
    # 생성자, 언어 선택, YAML 로딩, override 병합은 설치 패키지 그대로 실행.
    # 병합 이후의 가중치 다운로드/추론 엔진 생성만 하지 않는다.
    def _create_paddlex_pipeline(self):
        return None

results = {}
for lang in ("korean", "ru"):
    baseline = ConfigOnlyOCR(lang=lang)._merged_paddlex_config
    kwargs = ai_local._ocr_kwargs(lang)
    configured = ConfigOnlyOCR(**kwargs)._merged_paddlex_config
    det_only = ConfigOnlyOCR(
        lang=lang, text_detection_model_name="PP-OCRv5_mobile_det"
    )._merged_paddlex_config
    results[lang] = {"baseline": baseline["SubModules"],
                     "configured": configured["SubModules"],
                     "det_only": det_only["SubModules"], "kwargs": kwargs}
print(json.dumps(results))
'''
    result = subprocess.run(
        [sys.executable, "-c", script], env=env,
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(cache.rglob("*.pdiparams")), "설정 검증 중 모델을 내려받으면 안 된다"
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("lang,expected", [
    ("korean", "korean_PP-OCRv5_mobile_rec"),
    ("ru", "eslav_PP-OCRv5_mobile_rec"),
])
def test_installed_paddleocr_keeps_language_recognition_model(real_configs, lang, expected):
    result = real_configs[lang]
    assert result["baseline"]["TextDetection"]["model_name"] == "PP-OCRv5_server_det"
    assert result["configured"]["TextDetection"]["model_name"] == "PP-OCRv5_mobile_det"
    assert result["baseline"]["TextRecognition"]["model_name"] == expected
    assert result["configured"]["TextRecognition"] == result["baseline"]["TextRecognition"]
    assert result["kwargs"]["text_recognition_model_name"] == expected
    # 검출 모델만 주는 종전 코드로 되돌아가면 실제 병합에서 언어를 잃는다.
    assert result["det_only"]["TextRecognition"]["model_name"] == "PP-OCRv6_medium_rec"


def install_constructor(monkeypatch, *, models=("default_det", "language_rec"), **kwargs):
    constructor = Mock(**kwargs)
    constructor._get_ocr_model_names = Mock(return_value=models)
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=constructor))
    return constructor


def test_recognition_selection_uses_installed_default_version(detection_override, monkeypatch):
    constructor = install_constructor(monkeypatch)
    ai_local._get_ocr_engine("custom_language")
    constructor._get_ocr_model_names.assert_called_once_with(None, "custom_language", None)
    assert constructor.call_args.kwargs["text_recognition_model_name"] == "language_rec"
    assert constructor.call_args.kwargs["text_detection_model_name"] == "PP-OCRv5_mobile_det"


def test_unset_override_does_not_resolve_or_pin_models(monkeypatch):
    monkeypatch.setattr(ai_local, "_OCR_DET_MODEL", "")
    constructor = install_constructor(monkeypatch)
    kwargs = ai_local._ocr_kwargs("korean")
    constructor._get_ocr_model_names.assert_not_called()
    assert "text_detection_model_name" not in kwargs
    assert "text_recognition_model_name" not in kwargs
    assert kwargs["lang"] == "korean"


@pytest.mark.parametrize("models", [(None, None), ("det", ""), ("det", object()), None])
def test_unresolved_language_ignores_override_and_warns(
        detection_override, monkeypatch, caplog, models):
    constructor = install_constructor(monkeypatch, models=models)
    with caplog.at_level(logging.WARNING, logger="app.ai_local"):
        ai_local._get_ocr_engine("unknown")
    kwargs = constructor.call_args.kwargs
    assert kwargs["lang"] == "unknown"
    assert "text_detection_model_name" not in kwargs
    assert "text_recognition_model_name" not in kwargs
    assert "Ignoring GLHAC_OCR_DET_MODEL='PP-OCRv5_mobile_det'" in caplog.text
    assert "lang='unknown'" in caplog.text


@pytest.mark.parametrize("error", [AttributeError("missing API"),
                                   TypeError("new signature"), ValueError("unsupported")])
def test_internal_api_change_keeps_language_defaults(
        detection_override, monkeypatch, caplog, error):
    constructor = install_constructor(monkeypatch)
    constructor._get_ocr_model_names.side_effect = error
    with caplog.at_level(logging.WARNING, logger="app.ai_local"):
        kwargs = ai_local._ocr_kwargs("korean")
    assert kwargs["lang"] == "korean"
    assert "text_detection_model_name" not in kwargs
    assert "text_recognition_model_name" not in kwargs
    assert str(error) in caplog.text


def test_missing_internal_api_keeps_language_defaults(detection_override, monkeypatch, caplog):
    constructor = install_constructor(monkeypatch)
    del constructor._get_ocr_model_names
    with caplog.at_level(logging.WARNING, logger="app.ai_local"):
        kwargs = ai_local._ocr_kwargs("ru")
    assert "text_detection_model_name" not in kwargs
    assert "text_recognition_model_name" not in kwargs
    assert "keeping PaddleOCR language defaults" in caplog.text


@pytest.mark.parametrize("override", ["", "PP-OCRv5_mobile_det"])
def test_constructor_typeerror_warns_about_discarded_options(
        detection_override, monkeypatch, caplog, override):
    monkeypatch.setattr(ai_local, "_OCR_DET_MODEL", override)
    engine = object()
    constructor = install_constructor(
        monkeypatch, side_effect=[TypeError("unexpected keyword argument"), engine])
    with caplog.at_level(logging.WARNING, logger="app.ai_local"):
        assert ai_local._get_ocr_engine("ru") is engine
    assert constructor.call_count == 2
    constructor.assert_called_with(lang="ru")
    assert ai_local._get_ocr_engine("ru") is engine
    assert constructor.call_count == 2
    assert "TypeError" in caplog.text
    assert "unexpected keyword argument" in caplog.text
    assert "discarding options" in caplog.text
    assert "enable_mkldnn" in caplog.text
    if override:
        assert override in caplog.text
        assert "text_recognition_model_name" in caplog.text


def test_invalid_detection_model_is_not_retried_or_cached(detection_override, monkeypatch):
    monkeypatch.setattr(ai_local, "_OCR_DET_MODEL", "invalid_model")
    constructor = install_constructor(monkeypatch, side_effect=ValueError("invalid model"))
    with pytest.raises(ValueError, match="invalid model"):
        ai_local._get_ocr_engine("korean")
    assert constructor.call_count == 1
    assert constructor.call_args.kwargs["text_detection_model_name"] == "invalid_model"
    assert ai_local._ocr_engines == {}
