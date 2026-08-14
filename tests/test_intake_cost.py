"""인테이크 비용 — 버릴 값을 계산하지 않는다.

배경(실측, Demo test files.zip 16개 파일):
  · 총 419.6초 중 분류 LLM 이 266.1초. 그런데 그 결과의 상당수가 곧바로 버려졌다.
  · 'Daftar bahan.xlsx' 는 136행짜리 표인데 헤더가 인니어('Nama')라 구조 파서가 놓쳤고,
    87초짜리 청크 LLM 으로 다시 읽어 134개를 건졌다. 표에 그대로 있는 값이다.
  · 기록물 사진(Catatan…)은 사전이 doc_type 을 확정하는데도 파일당 15~20초를 들여
    필드를 뽑았다. aggregate_fields 는 'other' 유형의 회사정보·제품·원재료를 전부
    무시하므로 그 값은 쓰이지 않는다.

여기서 지키는 성질: 파일명/구조로 결정할 수 있으면 LLM을 부르지 않는다. 단, 모르는
문서의 판정까지 뺏지는 않는다.

실행: <venv>/bin/python -m pytest tests/test_intake_cost.py -q
"""
import io
import os
import sys

import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import ai_local, intake  # noqa: E402


def _wb(rows, title="Sheet1"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return openpyxl.load_workbook(io.BytesIO(buf.getvalue()), read_only=True, data_only=True)


ID_LIST = [
    ["Daftar Bahan Halal (Pabrik)"],
    ["Source: 690209121"],
    [],
    ["No", "Nama", "Jenis Bahan", "Produsen", "Negara", "Supplier",
     "Lembaga Penerbit Sertifikat", "Nomor Sertifikat Halal"],
    ["1", "DAGING POTONG 1 KG", "BAHAN BAKU", "PT. AGRO", "INDONESIA", "Meat", "BPJPH", "ID004"],
    ["2", "Air mineral aqua", "BAHAN BAKU", "PT. Tirta", "INDONESIA", "SWALAYAN", "BPJPH", "ID005"],
    ["3", "Minyak Bimoli", "BAHAN BAKU", "PT. Salim", "INDONESIA", "SWALAYAN", "BPJPH", "ID006"],
]

MATRIX = [
    ["Daftar Bahan yang Digunakan"],
    ["Source: 690209121"],
    [],
    ["NO", "Nama Bahan", "1 - SEMUR BETAWI", "2 - RENDANG", "3 - CAH DAGING", "4 - ROLADE"],
    ["1", "DAGING POTONG 1 KG", "V", "V", "V", ""],
    ["2", "Air mineral aqua", "V", "V", "V", "V"],
    ["3", "Minyak Bimoli", "V", "", "V", "V"],
]


def test_indonesian_material_list_is_read_from_the_table():
    """인니어 원재료 목록은 표에서 그대로 읽는다 — LLM으로 다시 읽을 이유가 없다."""
    got = intake._xlsx_ingredient_table(_wb(ID_LIST, "Daftar Bahan Halal"))
    assert got is not None, "인니어 헤더('Nama')를 못 잡으면 87초짜리 LLM 경로로 떨어진다"
    products, materials = got
    assert materials == ["DAGING POTONG 1 KG", "Air mineral aqua", "Minyak Bimoli"]
    assert products == [], "표 제목('Daftar Bahan Halal …')을 제품명으로 쓰면 안 된다"


def test_product_matrix_is_not_a_material_list():
    """제품×원재료 매트릭스는 같은 헤더를 쓰지만 원재료표가 아니다.

    잘못 잡으면 doc_type 이 product_list → material_list 로 뒤집혀 제품 목록이 사라진다."""
    assert intake._xlsx_ingredient_table(_wb(MATRIX, "Bahan per Produk")) is None


def test_material_names_are_deduped():
    """같은 원재료가 여러 행에 나와도 한 번만 — 표에는 실제 중복이 있다(실측 136행/134종)."""
    rows = ID_LIST + [["4", "Air mineral aqua", "BAHAN BAKU", "PT. Tirta", "INDONESIA",
                       "SWALAYAN", "BPJPH", "ID005"]]
    _, materials = intake._xlsx_ingredient_table(_wb(rows, "Daftar Bahan Halal"))
    assert materials.count("Air mineral aqua") == 1


def test_bare_nama_header_without_context_is_ignored():
    """'Nama' 한 단어만 보고 원재료표로 단정하지 않는다 — 이웃 헤더가 근거다."""
    rows = [["Daftar Karyawan"], [], ["No", "Nama", "Jabatan"], ["1", "Budi", "Manajer"]]
    assert intake._xlsx_ingredient_table(_wb(rows, "Karyawan")) is None


# ── 기록물: 사전이 확정하면 LLM을 부르지 않는다 ─────────────────────────────

@pytest.fixture
def no_llm(monkeypatch):
    """LLM 을 부르면 즉시 실패시킨다 — '부르지 않는다'를 눈으로 확인할 방법이 없다."""
    def boom(*a, **k):
        raise AssertionError("LLM 을 불렀다 — 파일명으로 이미 결정된 문서다")
    monkeypatch.setattr(ai_local, "llm_json", boom)
    return boom


@pytest.mark.parametrize("name", [
    "Catatan pembelian barang.PNG", "Catatan hasil produksi.PNG",
    "Catatan penyimpanan barang.PNG", "Denah ruang produksi.pdf",
])
def test_dictionary_known_records_skip_the_llm(no_llm, name):
    """사전이 아는 운영 기록물은 LLM 없이 분류된다."""
    r = intake.classify(name, "Tanggal 01/07/2026 Nama bahan Gula 10 kg Budi")
    assert r["doc_type"] == "other"
    assert r["decided_by"] == "filename" and r["reason"]


@pytest.mark.parametrize("name", ["Halal_Policy.pdf", "Diagram alir proses produksi.PNG",
                                  "Pork free Statement.pdf"])
def test_name_decided_types_skip_the_llm(no_llm, name):
    """기존 파일명 확정 규칙은 그대로 — 회귀 방지."""
    assert intake.classify(name, "isi dokumen")["decided_by"] == "filename"


def test_unknown_document_still_goes_to_the_llm(monkeypatch):
    """모르는 이름은 LLM 이 판정한다 — 비용을 아끼려다 판정을 잃으면 안 된다."""
    called = []
    monkeypatch.setattr(ai_local, "llm_json",
                        lambda *a, **k: called.append(1) or {"doc_type": "coa_msds"})
    r = intake.classify("scan_2026_08_14_final.pdf", "Certificate of Analysis pH 6.5")
    assert called, "미지 문서까지 건너뛰면 분류가 죽는다"
    assert r["doc_type"] == "coa_msds"


def test_empty_text_never_calls_the_llm(no_llm):
    """빈 문서에 LLM을 쓰지 않는다(기존 동작)."""
    assert intake.classify("whatever.pdf", "   ")["doc_type"] == "other"


# ── OCR 재사용 ────────────────────────────────────────────────────────────

def test_ocr_lines_are_handed_back_for_caching(monkeypatch):
    """OCR 라인(좌표 포함)을 버리지 않고 넘긴다 — 안 그러면 나중에 같은 사진을 또 읽는다."""
    fake = {"ok": True, "lines": [{"text": "Gula", "confidence": 0.97, "box": [1, 2, 3, 4]},
                                  {"text": "10 kg", "confidence": 0.95, "box": [5, 2, 8, 4]}]}
    monkeypatch.setattr(ai_local, "ocr_image", lambda *a, **k: fake)
    sink = []
    txt = intake.parse_file("Catatan pembelian barang.png", b"\x89PNG fake", ocr_sink=sink)
    assert "Gula" in txt
    assert [l["text"] for l in sink] == ["Gula", "10 kg"]
    assert sink[0]["box"] == [1, 2, 3, 4], "좌표가 빠지면 표 행 복원이 불가능하다"


def test_parse_file_works_without_a_sink(monkeypatch):
    """sink 를 안 줘도 기존처럼 동작한다."""
    monkeypatch.setattr(ai_local, "ocr_image",
                        lambda *a, **k: {"ok": True, "lines": [{"text": "A", "confidence": 0.9}]})
    assert intake.parse_file("x.png", b"\x89PNG") == "A"
