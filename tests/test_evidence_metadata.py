"""P0 갭 — 증거 귀속 메타데이터(감사 A08) 격리 테스트.

모의(mock_evidence_*)·현장(onsite_evidence_*) 증거 업로드 시
uploaded_by·uploader_role·file_hash(sha256)·captured_at(EXIF DateTimeOriginal)·
lat/lng(EXIF GPS)가 저장·조회되는지 검증. 고유 DB(smoke DB와 격리).

실행: <venv>/bin/python tests/test_evidence_metadata.py
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# smoke DB와 반드시 분리 — 직접 대입(setdefault 아님)
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_evmeta_test.db"
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

# EXIF(JPEG) 상수 — GPS 37.5665,126.9780(서울) + DateTimeOriginal "2026:07:10 09:30:15".
# piexif로 사전 생성한 바이트를 base64 리터럴로 고정(런타임 생성 회피, 재현성 보장).
EXIF_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/4QCyRXhpZgAATU0AKgAAAAgAAodpAAQAAAABAAAAJoglAAQA"
    "AAABAAAASAAAAAAAAZADAAIAAAAUAAAANDIwMjY6MDc6MTAgMDk6MzA6MTUAAAQAAQACAAAAAk4A"
    "AAAAAgAFAAAAAwAAAHoAAwACAAAAAkUAAAAABAAFAAAAAwAAAJIAAAAlAAAAAQAAACEAAAABAAAX"
    "NAAAAGQAAAB+AAAAAQAAADoAAAABAAAP7wAAAGT/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
    "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgy"
    "IRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR"
    "CAAEAAQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgED"
    "AwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRol"
    "JicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWW"
    "l5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3"
    "+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3"
    "AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaan"
    "qKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIR"
    "AxEAPwDPooor6w1P/9k="
)

# EXIF 없는 1x1 PNG(귀속 메타는 채워지되 captured_at/gps는 None)
PLAIN_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
                 "C0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def test_helper_parses_exif_gps_and_datetime():
    """EXIF 헬퍼 단위검증 — GPS·DateTimeOriginal 파싱(상수 이미지)."""
    from app.main import _exif_gps, _exif_datetime
    gps = _exif_gps(EXIF_JPEG_B64)
    assert gps is not None, "EXIF GPS 파싱 실패"
    assert abs(gps[0] - 37.5665) < 0.01 and abs(gps[1] - 126.978) < 0.01, gps
    assert _exif_datetime(EXIF_JPEG_B64) == "2026:07:10 09:30:15"
    # 평문 PNG는 둘 다 None
    assert _exif_gps(PLAIN_PNG_B64) is None
    assert _exif_datetime(PLAIN_PNG_B64) is None


def test_mock_evidence_attribution_metadata():
    """모의 증거(EXIF JPEG) 업로드 → uploaded_by/uploader_role/file_hash/captured_at/GPS 저장·조회."""
    import hashlib
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "EvMeta Mock Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        r = c.post(f"/cases/{cid}/documents",
                   json={"filename": "storage.jpg", "file_b64": EXIF_JPEG_B64,
                         "doc_type": "mock_evidence_material_storage"}, headers=_h(adm))
        assert r.status_code == 200, r.text
        docs = c.get(f"/cases/{cid}/documents", headers=_h(adm)).json()
        d = next(x for x in docs if x["doc_type"] == "mock_evidence_material_storage")
        # 누가
        assert d["uploaded_by"], "uploaded_by 미기록"
        assert d["uploader_role"] == "admin", d["uploader_role"]
        # 무결성 — sha256 64 hex, 실제 콘텐츠 해시와 일치
        expect = hashlib.sha256(base64.b64decode(EXIF_JPEG_B64)).hexdigest()
        assert d["file_hash"] == expect, "file_hash 불일치"
        assert len(d["file_hash"]) == 64
        # 언제 — EXIF DateTimeOriginal
        assert d["captured_at"] == "2026:07:10 09:30:15", d["captured_at"]
        # 어디서 — EXIF GPS
        assert d["lat"] is not None and d["lng"] is not None, "GPS 미기록"
        assert abs(d["lat"] - 37.5665) < 0.01 and abs(d["lng"] - 126.978) < 0.01
        assert d["geo_source"] == "exif"


def test_onsite_evidence_attribution_no_exif():
    """현장 증거(EXIF 없는 PNG) 업로드 → 귀속·해시는 채워지고 captured_at/GPS는 None."""
    import hashlib
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "EvMeta Onsite Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        r = c.post(f"/cases/{cid}/documents",
                   json={"filename": "line.png", "file_b64": PLAIN_PNG_B64,
                         "doc_type": "onsite_evidence_material_flow"}, headers=_h(adm))
        assert r.status_code == 200, r.text
        docs = c.get(f"/cases/{cid}/documents", headers=_h(adm)).json()
        d = next(x for x in docs if x["doc_type"] == "onsite_evidence_material_flow")
        assert d["uploaded_by"] and d["uploader_role"] == "admin"
        assert d["file_hash"] == hashlib.sha256(base64.b64decode(PLAIN_PNG_B64)).hexdigest()
        assert d["captured_at"] is None and d["lat"] is None and d["lng"] is None


def test_non_evidence_upload_skips_attribution():
    """비증거 doc_type('other')은 귀속 메타를 채우지 않음(범위 최소화 — 증거만)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "EvMeta Other Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        c.post(f"/cases/{cid}/documents",
               json={"filename": "misc.png", "file_b64": PLAIN_PNG_B64, "doc_type": "other"},
               headers=_h(adm))
        docs = c.get(f"/cases/{cid}/documents", headers=_h(adm)).json()
        d = next(x for x in docs if x["doc_type"] == "other")
        assert d["uploaded_by"] is None  # A08 귀속(촬영자)은 증거 전용 · file_hash는 이제 전 문서 기록(무결성·중복방지)


if __name__ == "__main__":
    tests = [test_helper_parses_exif_gps_and_datetime,
             test_mock_evidence_attribution_metadata,
             test_onsite_evidence_attribution_no_exif,
             test_non_evidence_upload_skips_attribution]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {t.__name__}: {e}")
            raise
    print(f"\n{passed}/{len(tests)} passed")
