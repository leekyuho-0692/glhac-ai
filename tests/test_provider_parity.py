"""공급자가 달라도 같은 서류면 같은 데이터 — 코드로 못 박는다.

배경(실측): 같은 16개 파일을 없음/로컬/원격으로 돌렸더니 GPT 만 두 건에서 더 뽑았다.
그중 하나가 제품×원재료 **매트릭스**에서 원재료 21건이었다 — 코드가 "이걸 원재료표로
읽으면 안 된다"고 가드를 둔 바로 그 파일이다. 그때는 집계 게이트(doc_type)가 우연히
막아 최종 결과가 같았지만, 우연에 기대면 안 된다.

규칙: **구조 파서가 낸 목록이 있으면 그 필드는 LLM 값을 받지 않는다.**
구조 파서가 아무것도 못 낸 문서는 LLM 값을 그대로 쓴다(기능을 죽이지 않는다).

실행: <venv>/bin/python -m pytest tests/test_provider_parity.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_parity_test.db")

from app import intake   # noqa: E402

STRUCT = {"doc_type": "material_list",
          "fields": {"material_names": ["Gula Pasir", "Garam"],
                     "product_names": ["Bumbu Instan"]},
          "field_sources": {"material_names": "structural",
                            "product_names": "structural"}}
LLM = {"doc_type": "material_list",
       "fields": {"material_names": ["LLM 원료 A", "LLM 원료 B"],
                  "product_names": ["LLM 제품"]},
       "field_sources": {"material_names": "llm", "product_names": "llm"}}


def test_구조_파서가_있으면_LLM_목록은_들어오지_않는다():
    only = intake.aggregate_fields([STRUCT])
    both = intake.aggregate_fields([STRUCT, LLM])
    assert both["materials"] == only["materials"] == ["Gula Pasir", "Garam"]
    assert both["products"] == only["products"] == ["Bumbu Instan"]


def test_문서_순서가_바뀌어도_같다():
    """LLM 문서가 먼저 오면 먼저 담기던 순서 의존을 막는다."""
    a = intake.aggregate_fields([STRUCT, LLM])
    b = intake.aggregate_fields([LLM, STRUCT])
    assert a["materials"] == b["materials"]
    assert a["products"] == b["products"]


def test_구조_파서가_없으면_확인_대기로_간다():
    """정책 변경(사용자 결정): 예전에는 LLM 값을 그대로 반영했다. 이제는 대기로 돌린다.
    스캔 PDF 는 LLM 밖에 길이 없지만, 그 값이 배포마다 달라 심사 대상이 갈렸다.
    버리지는 않는다 — 사람이 원본과 대조해 승인하면 들어간다."""
    only = intake.aggregate_fields([LLM])
    assert only["materials"] == []
    assert sorted(p["value"] for p in only["pending"] if p["kind"] == "material_names") \
        == ["LLM 원료 A", "LLM 원료 B"]


def test_필드별로_따로_판단한다():
    """원재료는 구조 값이 있어 자동 반영, 제품은 LLM 뿐이라 확인 대기.
    필드를 뭉뚱그리면 한쪽 때문에 다른 쪽이 통째로 사라진다."""
    half = {"doc_type": "material_list",
            "fields": {"material_names": ["Gula Pasir"]},
            "field_sources": {"material_names": "structural"}}
    agg = intake.aggregate_fields([half, LLM])
    assert agg["materials"] == ["Gula Pasir"]        # 구조가 이긴다
    assert agg["products"] == []                     # 제품은 자동 반영하지 않는다
    assert [p["value"] for p in agg["pending"] if p["kind"] == "product_names"] \
        == ["LLM 제품"]


def test_구조_파서_결과에_출처가_기록된다():
    """xlsx 전성분표 — 구조 마커가 있으면 LLM 을 부르지 않고 잠근다."""
    text = "[시트/제품명: Bumbu]\n원료명\tINS No.\n설탕\t-\n소금\t-\n"
    r = intake.classify("전성분표.xlsx", text)
    if r.get("field_sources", {}).get("material_names") == "structural":
        assert "material_names" in (r.get("locked_fields") or [])
    else:                    # 이 텍스트로 구조 마커가 안 잡히면 최소한 출처는 남아야 한다
        assert "field_sources" in r


def test_매트릭스는_원재료표로_읽히지_않는다():
    """제품×원재료 매트릭스를 원재료표로 읽으면 제품 목록이 통째로 사라진다."""
    rows = [["Nama Bahan", "Produk A", "Produk B", "Produk C"],
            ["Gula", "V", "V", "V"],
            ["Garam", "V", "", "V"],
            ["Air", "V", "V", ""]]
    assert intake._looks_like_matrix(rows, 0, 0) is True
    assert intake._id_material_rows(rows) is None


def test_매트릭스_파일은_3종_모두_목록을_내지_않는다():
    """실측 회귀: 이웃 헤더 개수(attrs)로 먼저 거르면 오른쪽이 전부 제품 열인
    매트릭스는 attrs=0 이라 매트릭스 검사에 닿지도 못했다. 그 사이 LLM 이 목록을
    지어냈고(로컬 제품 35건 · GPT 원재료 21건) 실행마다 값이 달랐다."""
    rows = [["Daftar Bahan yang Digunakan"], ["Source: ..."], [],
            ["NO", "Nama Bahan", "1 - SEMUR", "2 - RENDANG", "3 - CAH", "4 - ROLADE"],
            [1, "Gula", "-", "-", "V", "-"],
            [2, "Garam", "V", "-", "-", "V"],
            [3, "Air", "-", "V", "V", "-"]]
    seen = {}
    assert intake._id_material_rows(rows, seen) is None
    assert seen.get("matrix") is True        # 판정 사실이 밖으로 전달돼야 한다


def test_매트릭스_마커가_있으면_LLM_목록을_버린다():
    text = intake.MATRIX_MARK + "\nNO Nama Bahan 1-SEMUR 2-RENDANG\n1 Gula - V\n"
    r = intake.classify("Bahan vs Produk matriks.xlsx", text)
    f = r.get("fields") or {}
    assert not f.get("material_names"), f
    assert not f.get("product_names"), f
    assert r["field_sources"]["material_names"] == "structural_reject"
    agg = intake.aggregate_fields([{"doc_type": r["doc_type"], "fields": f,
                                    "field_sources": r["field_sources"]}])
    assert agg["materials"] == [] and agg["products"] == []


# ── LLM 단독 추출은 자동 반영하지 않는다 ────────────────────────────────
def test_LLM_단독_목록은_자동_반영되지_않는다():
    """결정: 재현성 > 자동화 편의. LLM 출력은 공급자마다, 같은 모델에서도 실행마다
    달라서 그대로 넣으면 배포에 따라 심사 대상이 달라진다."""
    agg = intake.aggregate_fields([LLM])
    assert agg["materials"] == [] and agg["products"] == []
    kinds = {p["kind"] for p in agg["pending"]}
    assert kinds == {"material_names", "product_names"}
    assert all(p["source"] == "llm" for p in agg["pending"])


def test_구조_파서_값은_그대로_자동_반영된다():
    """확인 대기로 돌리는 건 LLM 값만이다 — 구조 파서까지 막으면 자동화가 죽는다."""
    agg = intake.aggregate_fields([STRUCT])
    assert agg["materials"] == ["Gula Pasir", "Garam"]
    assert agg["pending"] == []


def test_매트릭스_거부값은_대기에도_올리지_않는다():
    """구조 파서가 '목록 아님'이라 판정한 문서다 — 확인 대기에 올리면 사람이
    지어낸 목록을 승인하게 된다."""
    rej = {"doc_type": "product_list",
           "fields": {"material_names": ["환각A"], "product_names": ["환각B"]},
           "field_sources": {"material_names": "structural_reject",
                             "product_names": "structural_reject"}}
    agg = intake.aggregate_fields([rej])
    assert agg["materials"] == [] and agg["products"] == []
    assert agg["pending"] == []


def test_문서에_저장된_출처로도_판별한다():
    """재처리 뒤에는 field_sources 가 fields._sources 로 문서에 저장된다."""
    saved = {"doc_type": "material_list",
             "fields": {"material_names": ["Gula Pasir"],
                        "_sources": {"material_names": "structural"}}}
    agg = intake.aggregate_fields([saved])
    assert agg["materials"] == ["Gula Pasir"]
    assert agg["pending"] == []
