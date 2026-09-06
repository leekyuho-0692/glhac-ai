"""OCR 워커 분리 — 무거운 모델을 웹 프로세스 밖에 둔다.

실측 배경:
    앱 유휴            31 MB
    OCR(인니어) 정착  2,369 MB
    OCR(+한국어) 피크 7,780 MB
웹 안에서 돌리면 이 메모리가 계속 상주해 8GB 서버에서 한국어를 켜면 터진다.
워커로 빼면 웹은 30MB대를 유지하고, 유휴가 지나면 워커가 죽어 반납한다.

실행: <venv>/bin/python -m pytest tests/test_ocr_worker.py -q
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_ocrw_test.db")

import pytest   # noqa: E402

from app import ai_local   # noqa: E402


def _png(tmp_path, text="CATATAN PEMBELIAN BAHAN"):
    from PIL import Image, ImageDraw
    p = tmp_path / "s.png"
    im = Image.new("RGB", (760, 200), (255, 255, 255))
    ImageDraw.Draw(im).text((30, 80), text, fill=(0, 0, 0))
    im.save(p)
    return str(p)


@pytest.fixture(autouse=True)
def _stop_worker():
    yield
    ai_local._worker_stop()


# ── 규약 ────────────────────────────────────────────────────────────────
def test_기본은_워커모드다():
    """웹이 모델을 이고 있지 않게 하는 것이 기본값이어야 한다."""
    assert ai_local.OCR_MODE in ("worker", "inproc")
    st = ai_local.ocr_worker_status()
    assert set(st) >= {"mode", "alive", "idle_sec"}


def test_워커가_실제로_별도_프로세스다(tmp_path):
    if not ai_local.ocr_available():
        pytest.skip("OCR 미설치")
    ai_local.ocr_text_multi(_png(tmp_path), ["id"])
    p = ai_local._w["proc"]
    assert p is not None and p.pid != os.getpid()
    assert ai_local._worker_alive()


def test_워커를_거쳐도_결과_규약은_같다(tmp_path):
    if not ai_local.ocr_available():
        pytest.skip("OCR 미설치")
    img = _png(tmp_path)
    a = ai_local.ocr_text_multi(img, ["id"])
    b = ai_local._ocr_text_multi_inproc(img, ["id"])
    assert set(a) >= {"ok", "text", "langs_used"}
    assert a["ok"] == b["ok"] and a["langs_used"] == b["langs_used"]
    assert a["text"] == b["text"]        # 같은 이미지·같은 언어면 같은 글자


def test_인테이크가_쓰는_ocr_image도_워커를_탄다(tmp_path):
    """여기가 워커를 안 타면 분리한 의미가 없다 — 서류 접수가 가장 무겁다."""
    if not ai_local.ocr_available():
        pytest.skip("OCR 미설치")
    ai_local._worker_stop()
    r = ai_local.ocr_image(_png(tmp_path), "id")
    assert r.get("ok") is True
    assert set(r) >= {"ok", "lines"}
    assert ai_local._worker_alive(), "ocr_image 가 워커를 띄우지 않았다"


def test_워커가_죽으면_인프로세스로_떨어진다(tmp_path, monkeypatch):
    """서류 접수가 멈추는 것보다 느리게라도 되는 편이 낫다."""
    if not ai_local.ocr_available():
        pytest.skip("OCR 미설치")
    monkeypatch.setattr(ai_local, "_worker_start", lambda: None)
    ai_local._w["proc"] = None
    r = ai_local.ocr_text_multi(_png(tmp_path), ["id"])
    assert r.get("ok") is True          # 폴백이 답을 냈다


def test_유휴가_지나면_워커가_종료된다(tmp_path, monkeypatch):
    """죽지 않으면 메모리를 반납하지 못한다 — 분리의 절반이 사라진다."""
    if not ai_local.ocr_available():
        pytest.skip("OCR 미설치")
    import time
    monkeypatch.setattr(ai_local, "OCR_IDLE_SEC", 1.0)
    ai_local.ocr_text_multi(_png(tmp_path), ["id"])
    assert ai_local._worker_alive()
    ai_local._w["last"] = time.time() - 100      # 유휴로 만든다
    for _ in range(40):
        if not ai_local._worker_alive():
            break
        time.sleep(1)
    assert not ai_local._worker_alive(), "유휴인데 워커가 살아 있다"


def test_inproc_모드로_되돌릴_수_있다(tmp_path, monkeypatch):
    """워커에 문제가 생겼을 때 환경변수만으로 예전 동작으로 돌아갈 길."""
    if not ai_local.ocr_available():
        pytest.skip("OCR 미설치")
    monkeypatch.setattr(ai_local, "OCR_MODE", "inproc")
    ai_local._w["proc"] = None
    r = ai_local.ocr_text_multi(_png(tmp_path), ["id"])
    assert r.get("ok") is True
    assert ai_local._w["proc"] is None, "inproc 인데 워커를 띄웠다"
