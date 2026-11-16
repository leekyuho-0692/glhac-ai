"""쪽별 키릴 샘플링과 기존 언어별 점수 선택. 실제 모델은 호출하지 않는다."""
import io
from concurrent.futures import CancelledError
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from PIL import Image, ImageOps

from app import ai_local, intake


def line(text="English document", confidence=0.9, box=(10, 20, 200, 40)):
    x0, y0, x1, y1 = box
    return {"text": text, "confidence": confidence, "box": list(box),
            "poly": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def image_bytes(format="PNG", frames=1):
    images = [Image.new("RGB", (1000, 300), color) for color in ("white", "gray")[:frames]]
    with io.BytesIO() as output:
        images[0].save(output, format=format, save_all=frames > 1, append_images=images[1:])
        return output.getvalue()


@pytest.fixture
def ocr(monkeypatch):
    state = SimpleNamespace(korean=[line()], russian=[line("Русский текст", 0.99)],
                            probe={"ok": True, "has_cyrillic": False},
                            calls=[], samples=[], paths=[], image=image_bytes())
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE", "1")
    monkeypatch.delenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", raising=False)
    monkeypatch.setattr(intake, "_OCR_FALLBACK_LANGS", ["ru"])

    def recognize(path, lang="korean"):
        assert Path(path).exists()
        state.paths.append(path)
        state.calls.append(lang)
        return {"ok": True, "lines": state.korean if lang == "korean" else state.russian}

    def probe(path, polys):
        assert Path(path).exists()
        state.samples.append(polys)
        if isinstance(state.probe, Exception):
            raise state.probe
        return state.probe

    monkeypatch.setattr(ai_local, "ocr_image", recognize)
    monkeypatch.setattr(ai_local, "ocr_cyrillic_probe", probe)
    return state


def test_hangul_never_probes_or_repeats_ocr(ocr):
    ocr.korean = [line("영문 English 혼합")]
    assert intake._ocr_bytes(ocr.image, "png") == "영문 English 혼합"
    assert ocr.calls == ["korean"] and ocr.samples == []


@pytest.mark.parametrize("has_cyrillic,expected,calls", [
    (False, "English", ["korean"]),
    (True, "Русский текст", ["korean", "ru"]),
])
def test_low_confidence_hangul_still_enters_fallback_probe(ocr, has_cyrillic, expected, calls):
    ocr.korean = [line("English"), line("한글", 0.1)]
    ocr.probe = {"ok": True, "has_cyrillic": has_cyrillic}
    assert intake._ocr_bytes(ocr.image, "png") == expected
    assert ocr.calls == calls
    assert ocr.samples == [[ocr.korean[0]["poly"]]]


def test_empty_kept_text_with_low_confidence_hangul_uses_full_russian_ocr(ocr):
    ocr.korean = [line("  "), line("한글", 0.1)]
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


def test_negative_probe_keeps_korean_lines_and_sink(ocr):
    sink = []
    result = intake._ocr_bytes(ocr.image, "png", sink)
    assert result == "English document" and result.parse_fields == {}
    assert ocr.calls == ["korean"]
    assert ocr.samples == [[ocr.korean[0]["poly"]]]
    assert sink == ocr.korean
    assert not any(Path(p).exists() for p in ocr.paths)


@pytest.mark.parametrize("score,expected", [(0.99, "Русский текст"),
                                            (0.9, "English document"),
                                            (0.8, "English document")])
def test_positive_probe_preserves_score_comparison(ocr, score, expected):
    ocr.probe = {"ok": True, "has_cyrillic": True}
    ocr.russian = [line("Русский текст", score)]
    sink = []
    assert intake._ocr_bytes(ocr.image, "png", sink) == expected
    assert ocr.calls == ["korean", "ru"] and len(ocr.samples) == 1
    assert sink[0]["text"] == expected


@pytest.mark.parametrize("case", ["empty", "blank", "missing-box", "missing-poly", "small-box", "disabled"])
def test_unprobeable_page_uses_full_russian_ocr(ocr, monkeypatch, case):
    if case == "empty":
        ocr.korean = []
    elif case == "blank":
        ocr.korean = [line("  ")]
    elif case == "missing-box":
        ocr.korean[0].pop("box")
    elif case == "missing-poly":
        ocr.korean[0].pop("poly")
    elif case == "small-box":
        ocr.korean[0]["box"] = [0, 0, 200, 7]
    else:
        monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE", "0")
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("height", [0.01, 7, 7.99])
def test_small_valid_box_among_ordinary_lines_uses_full_russian_ocr(ocr, height, reverse):
    ocr.korean = [line("English", box=(0, 0, 200, 20)),
                  line("MonoKo", box=(0, 40, 200, 40 + height)),
                  line("MonoKo", box=(0, 80, 200, 80 + height))]
    if reverse:
        ocr.korean.reverse()
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


@pytest.mark.parametrize("failure", [
    {"ok": False, "error": "WORKER_TIMEOUT"},
    {"ok": False, "error": "TextRecognition unavailable"},
    {"ok": True}, None, [],
    {"ok": True, "has_cyrillic": False, "error": "invalid output"},
    RuntimeError("predict failed"), TimeoutError("probe timeout"),
])
def test_failed_probe_conservatively_runs_full_russian_ocr(ocr, failure):
    ocr.probe = failure
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and len(ocr.samples) == 1


@pytest.mark.parametrize("error", [CancelledError(), intake.ArchiveError("deadline")])
def test_probe_cancellation_propagates_and_removes_tempfile(ocr, error):
    ocr.probe = error
    with pytest.raises(type(error)):
        intake._ocr_bytes(ocr.image, "png")
    assert ocr.calls == ["korean"]
    assert not any(Path(p).exists() for p in ocr.paths)


def test_other_fallback_languages_still_run_and_probe_uses_korean_polygons(ocr, monkeypatch):
    monkeypatch.setattr(intake, "_OCR_FALLBACK_LANGS", ["id", "ru", "en"])
    ocr.russian = [line("Alternative", 0.99, (30, 70, 300, 95))]
    assert intake._ocr_bytes(ocr.image, "png") == "Alternative"
    assert ocr.calls == ["korean", "id", "en"]
    assert ocr.samples == [[ocr.korean[0]["poly"]]]


@pytest.mark.parametrize("langs", [[], ["id", "en"]])
def test_no_ru_fallback_never_probes(ocr, monkeypatch, langs):
    monkeypatch.setattr(intake, "_OCR_FALLBACK_LANGS", langs)
    intake._ocr_bytes(ocr.image, "png")
    assert ocr.calls == ["korean"] + langs and ocr.samples == []


def test_mixed_language_pdf_is_decided_for_each_page(ocr, monkeypatch):
    pages = iter([line("한국어 첫쪽"), line("한국어 둘째쪽"), line("Hpumopckm sunman")])

    def recognize(path, lang="korean"):
        ocr.calls.append(lang)
        return {"ok": True, "lines": [next(pages)] if lang == "korean" else ocr.russian}

    monkeypatch.setattr(ai_local, "ocr_image", recognize)
    ocr.probe = {"ok": True, "has_cyrillic": True}
    with fitz.open() as pdf:
        for _ in range(3):
            pdf.new_page(width=300, height=200)
        raw = pdf.tobytes()
    text = intake.parse_file("mixed.pdf", raw)
    assert all(t in text for t in ("한국어 첫쪽", "한국어 둘째쪽", "Русский текст"))
    assert ocr.calls == ["korean", "korean", "korean", "ru"]
    assert len(ocr.samples) == 1


@pytest.mark.parametrize("limit", [1, 3, 12, 17, 999])
def test_selection_is_bounded_and_spans_vertical_bands(monkeypatch, limit):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", str(limit))
    limit = min(12, limit)
    # 상단 줄일수록 넓어도 하단 구간을 놓치지 않아야 한다. 입력 순서에도 무관.
    lines = [line(box=(0, i * 10, 1000 - i, i * 10 + 8)) for i in range(120)]
    boxes = [row["box"] for row in intake._cyrillic_probe_lines(list(reversed(lines)), page_width=1000)]
    assert len(boxes) == limit
    if limit > 1:
        assert min(b[1] for b in boxes) < 1200 / limit
        assert max(b[1] for b in boxes) >= 1190 * (limit - 1) / limit
        assert len({min(limit - 1, int(b[1] / 1190 * limit)) for b in boxes}) == limit


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("limit", [1, 4, 12])
def test_single_column_with_varied_line_lengths_keeps_vertical_coverage(monkeypatch, reverse, limit):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", str(limit))
    widths = (80, 900, 240, 499, 500, 120, 760)
    lines = []
    for i in range(48):
        x0 = 20 + (i % 3) * 10
        lines.append(line(box=(x0, i * 20, x0 + widths[i % len(widths)], i * 20 + 12)))
    samples = intake._cyrillic_probe_lines(lines[::-1] if reverse else lines, page_width=1000)
    assert len(samples) == limit
    if limit > 1:
        ys = [row["box"][1] for row in samples]
        assert min(ys) == 0 and max(ys) == 940
        assert {min(limit - 1, int(y / 940 * limit)) for y in ys} == set(range(limit))


def test_selection_is_independent_of_latin_text_and_input_order(monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "4")
    lines = [line("12345", box=(0, y, 80, y + 10)) for y in range(0, 240, 10)]
    selected = [row["box"] for row in intake._cyrillic_probe_lines(lines, page_width=80)]
    for row in lines:
        row["text"] = "12345" if row["box"] in selected else "Latin preferred before the fix"
    assert [row["box"] for row in intake._cyrillic_probe_lines(list(reversed(lines)), page_width=80)] == selected


@pytest.mark.parametrize("reverse", [False, True])
def test_twelve_rows_sample_both_english_and_misread_russian_columns(ocr, monkeypatch, reverse):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "12")
    ocr.korean = []
    for y in range(0, 240, 20):
        ocr.korean.extend([line("English document", box=(0, y, 270, y + 12)),
                           line("12345", box=(300, y, 540, y + 12))])
    if reverse:
        ocr.korean.reverse()

    def probe(path, polys):
        ocr.samples.append(polys)
        # 이전 라틴 우선 선택은 왼쪽 12개만 읽고 ru 전체 OCR을 생략했다.
        return {"ok": True, "has_cyrillic": any(poly[0][0] >= 300 for poly in polys)}

    monkeypatch.setattr(ai_local, "ocr_cyrillic_probe", probe)
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"]
    sampled = ocr.samples[0]
    assert len(sampled) == 12
    assert sum(poly[0][0] == 0 for poly in sampled) == 6
    assert sum(poly[0][0] == 300 for poly in sampled) == 6
    for x in (0, 300):
        ys = [poly[0][1] for poly in sampled if poly[0][0] == x]
        assert min(ys) == 0 and max(ys) >= 200


@pytest.mark.parametrize("orientation", [None, 1, 2, 3, 4, 5, 6, 7, 8])
def test_exif_orientation_samples_both_columns_in_ocr_coordinates(ocr, monkeypatch, orientation):
    size = (1000, 400) if orientation in (5, 6, 7, 8) else (400, 1000)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    with io.BytesIO() as output:
        Image.new("RGB", size, "white").save(output, format="JPEG", exif=exif)
        raw = output.getvalue()
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == size
        assert ImageOps.exif_transpose(image).size == (400, 1000)

    # 회전 후 너비 400의 1%보다 크지만 원본 너비 1000의 1%보다 작은 8px 열 간격.
    ocr.korean = [line(text, box=(x, y, x + 190, y + 12))
                  for y in range(0, 960, 80)
                  for text, x in (("English", 0), ("MonoKo", 198))]

    def probe(path, polys):
        ocr.samples.append(polys)
        return {"ok": True, "has_cyrillic": any(poly[0][0] == 198 for poly in polys)}

    monkeypatch.setattr(ai_local, "ocr_cyrillic_probe", probe)
    assert intake._ocr_bytes(raw, "jpg") == "Русский текст"
    assert ocr.calls == ["korean", "ru"]
    assert len(ocr.samples) == 1 and len(ocr.samples[0]) == 12
    assert sum(poly[0][0] == 0 for poly in ocr.samples[0]) == 6
    assert sum(poly[0][0] == 198 for poly in ocr.samples[0]) == 6


def test_exif_inspection_failure_uses_full_russian_ocr(ocr, monkeypatch):
    def broken_exif(image):
        raise OSError("EXIF metadata unavailable")

    monkeypatch.setattr(Image.Image, "getexif", broken_exif)
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


@pytest.mark.parametrize("width", [None, 0, -1, float("nan"), float("inf")])
def test_unavailable_page_width_does_not_infer_width_from_korean_boxes(ocr, monkeypatch, width):
    monkeypatch.setattr(Image.Image, "width", property(lambda image: width))
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


def test_selection_fills_empty_bands_without_losing_sparse_bottom(monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "12")
    lines = [line(box=(0, y, 500, y + 8)) for y in range(80)]
    lines += [line("Bottom", box=(0, 1000, 100, 1010))]
    boxes = [row["box"] for row in intake._cyrillic_probe_lines(lines, page_width=500)]
    assert len(boxes) == 12 and lines[-1]["box"] in boxes


@pytest.mark.parametrize("setting,expected", [(None, 12), ("bad", 12), ("", 12), ("0", 1),
                                            ("-1", 1), ("17", 12), ("999", 12)])
def test_probe_limit_default_and_invalid_values(ocr, monkeypatch, setting, expected):
    if setting is not None:
        monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", setting)
    lines = [line(box=(0, i * 12, 100, i * 12 + 10)) for i in range(30)]
    assert len(intake._cyrillic_probe_lines(lines, page_width=100)) == expected


def test_selection_ignores_invalid_and_duplicate_boxes_but_keeps_blank_text():
    valid = line(box=(0, 0, 80, 8))
    blank = line("   ")
    bad = [None, [], [1, 2, 3], [0, 0, 40, 0], [0, 10, 40, 0], [50, 0, 40, 20],
           [0, 0, float("nan"), 30], [0, 0, 40, float("inf")], ["bad", 0, 30, 10]]
    lines = [valid, valid, {"text": "ABC"}, blank]
    lines += [dict(text="ABC", box=b) for b in bad]
    assert intake._cyrillic_probe_lines(lines, page_width=200) == [blank, valid]


@pytest.mark.parametrize("ext", ["tif", "tiff", "TIF", "TIFF"])
def test_tiff_extension_bypasses_probe_even_for_one_frame(ocr, monkeypatch, ext):
    raw = image_bytes("TIFF")
    monkeypatch.setattr(Image, "open", lambda *a, **kw: pytest.fail("TIFF는 열지 않고 우회"))
    assert intake._ocr_bytes(raw, ext) == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


@pytest.mark.parametrize("format,ext", [("TIFF", "tiff"), ("TIFF", "png"),
                                        ("GIF", "gif"), ("PNG", "png")])
def test_multiple_frames_bypass_probe_even_with_repeated_page_boxes(ocr, format, ext):
    raw = image_bytes(format, frames=2)
    with Image.open(io.BytesIO(raw)) as image:
        assert image.n_frames == 2
    # 첫 쪽 영어와 둘째 쪽 러시아어 오독이 같은 좌표로 반환되는 경우.
    ocr.korean = [line("English"), line("MonoKo")]
    assert intake._ocr_bytes(raw, ext) == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


@pytest.mark.parametrize("stage", ["open", "frames"])
def test_image_frame_inspection_failure_uses_full_russian_ocr(ocr, monkeypatch, stage):
    class BrokenImage:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def n_frames(self):
            raise OSError("frame metadata unavailable")

    def open_image(*args, **kwargs):
        if stage == "open":
            raise OSError("unreadable image")
        return BrokenImage()

    monkeypatch.setattr(Image, "open", open_image)
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []
    assert not any(Path(p).exists() for p in ocr.paths)


@pytest.mark.parametrize("text", ["", "   ", None])
@pytest.mark.parametrize("limit", [2, 12])
def test_blank_korean_boxes_are_prioritized_and_can_trigger_russian_ocr(ocr, monkeypatch, text, limit):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", str(limit))
    ocr.korean = [line("English", box=(0, 0, 200, 12)),
                  line(text, 0, (0, 40, 200, 52)), line(text, 0, (0, 80, 200, 92))]
    blank_polys = [row["poly"] for row in ocr.korean[1:]]

    def probe(path, polys):
        ocr.samples.append(polys)
        return {"ok": True, "has_cyrillic": any(poly in blank_polys for poly in polys)}

    monkeypatch.setattr(ai_local, "ocr_cyrillic_probe", probe)
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"]
    assert len(ocr.samples[0]) == min(limit, 3)
    assert all(poly in blank_polys for poly in ocr.samples[0][:2])


@pytest.mark.parametrize("reverse", [False, True])
def test_blank_duplicate_box_keeps_unread_text_priority(monkeypatch, reverse):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "1")
    blank = line("", 0, (0, 100, 200, 112))
    lines = [line(box=(0, 0, 200, 12)), line(box=blank["box"]), blank]
    assert intake._cyrillic_probe_lines(lines[::-1] if reverse else lines, page_width=200) == [blank]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("title_interval", [
    pytest.param(None, id="no-title"),
    pytest.param((0, 499), id="width-499"),
    pytest.param((0, 500), id="width-500"),
    pytest.param((0, 850), id="table-width"),
    pytest.param((0, 1000), id="page-width"),
    pytest.param((0, 240), id="two-left-columns"),
    pytest.param((50, 680), id="two-middle-columns"),
    pytest.param((320, 850), id="two-right-columns"),
    pytest.param((0, 680), id="three-left-columns"),
    pytest.param((50, 850), id="three-right-columns"),
])
def test_four_column_table_samples_russian_column_and_runs_full_ocr(ocr, monkeypatch, reverse, title_interval):
    ocr.korean = [line(text, box=(x0, y, x1, y + 12))
                  for y in range(40, 280, 20)
                  for text, x0, x1 in (("1", 0, 30), ("MonoKo", 50, 240),
                                      ("English", 320, 680), ("10", 800, 850))]
    title = (line("English table title", box=(title_interval[0], 0, title_interval[1], 20))
             if title_interval else None)
    if title:
        ocr.korean.append(title)
    if reverse:
        ocr.korean.reverse()

    def probe(path, polys):
        ocr.samples.append(polys)
        return {"ok": True, "has_cyrillic": any(poly[0][0] == 50 and poly[0][1] >= 40
                                              for poly in polys)}

    monkeypatch.setattr(ai_local, "ocr_cyrillic_probe", probe)
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"]
    sampled = ocr.samples[0]
    assert len(sampled) == 12
    if title:
        assert title["poly"] in sampled
    for x in (0, 50, 320, 800):
        ys = [poly[0][1] for poly in sampled if poly[0][0] == x and poly[0][1] >= 40]
        assert len(ys) >= 1 and min(ys) == 40 and max(ys) >= 240
        if not title:
            assert len(ys) == 3


def test_spanning_title_and_columns_exceeding_sample_limit_bypass_probe(ocr, monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "4")
    ocr.korean = [line(box=(x, 40, x + 100, 52)) for x in (0, 250, 500, 750)]
    ocr.korean.append(line("English table title", box=(0, 0, 850, 20)))
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


@pytest.mark.parametrize("limit", [1, 3, 12, 999])
def test_more_columns_than_sample_limit_bypasses_probe(ocr, monkeypatch, limit):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", str(limit))
    count = min(limit, 12) + 1
    ocr.korean = [line(box=(x * 60, 0, x * 60 + 20, 12)) for x in range(count)]
    assert intake._ocr_bytes(ocr.image, "png") == "Русский текст"
    assert ocr.calls == ["korean", "ru"] and ocr.samples == []


def test_twelve_columns_each_receive_one_sample(ocr, monkeypatch):
    ocr.korean = [line(box=(x * 60, y, x * 60 + 20, y + 12))
                  for y in range(0, 240, 20) for x in range(12)]
    assert intake._ocr_bytes(ocr.image, "png") == " ".join(row["text"] for row in ocr.korean)
    assert ocr.calls == ["korean"]
    assert {poly[0][0] for poly in ocr.samples[0]} == {x * 60 for x in range(12)}


def test_blank_priority_preserves_other_column_coverage(monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "4")
    blanks = [line("", box=(0, y, 100, y + 12)) for y in range(0, 240, 20)]
    english = line(box=(300, 0, 500, 12))
    samples = intake._cyrillic_probe_lines([english] + blanks, page_width=500)
    assert len(samples) == 4 and english in samples
    assert all(row in blanks for row in samples[:3])


@pytest.mark.parametrize("scale", [1, 10])
def test_column_gap_is_relative_to_page_width(monkeypatch, scale):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "2")
    # 두 번째 구간은 첫 구간과 가깝고, 세 번째는 별개의 실제 열이다.
    lines = [line(box=tuple(v * scale for v in box)) for box in
             [(0, 0, 100, 12), (105, 40, 200, 52), (300, 80, 400, 92)]]
    samples = intake._cyrillic_probe_lines(lines, page_width=1000 * scale)
    assert len(samples) == 2 and lines[-1] in samples
    # 폭이 작아지면 5px 간격도 열 경계가 되므로 3열을 2개로 대표할 수 없다.
    assert intake._cyrillic_probe_lines(lines, page_width=400 * scale) == []


def test_revisited_vertical_band_does_not_only_sample_its_top(monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "4")
    dense = [line(box=(0, y, 200, y + 8)) for y in range(80)]
    bottom = line(box=(0, 1000, 200, 1008))
    samples = intake._cyrillic_probe_lines(dense + [bottom], page_width=200)
    assert bottom in samples and len(samples) == 4
    assert any(60 <= row["box"][1] <= 79 for row in samples)


def test_same_vertical_position_prefers_column_center_over_left(monkeypatch):
    monkeypatch.setenv("GLHAC_OCR_CYRILLIC_PROBE_LINES", "1")
    lines = [line(box=(x, 20, x + 100, 32)) for x in (0, 50, 100)]
    assert intake._cyrillic_probe_lines(lines, page_width=200) == [lines[1]]
