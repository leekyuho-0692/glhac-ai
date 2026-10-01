"""macOS 압축(아카이브.zip) 파일명 — UTF-8 플래그 없는 NFD 한글.

Finder '압축하기'는 파일명을 UTF-8(NFD, 자모 분해)로 쓰고 0x800 플래그를 세우지 않는다.
NFD 그대로 두면 '사업자등록증' 규칙과 문자열이 달라 파일명 판정이 기타로 떨어졌다(실측).
"""
import io
import unicodedata
import zipfile

from app import intake


class _RawNameInfo(zipfile.ZipInfo):
    """파일명 바이트를 그대로 쓰고 UTF-8 플래그(0x800)를 세우지 않는다.
    zipfile 은 비ASCII 이름에 플래그를 자동으로 붙이므로 macOS/Windows ZIP 을 흉내내려면 필요하다."""

    def __init__(self, raw: bytes):
        super().__init__(raw.decode("cp437"))
        self._raw = raw

    def _encodeFilenameFlags(self):
        return self._raw, self.flag_bits


def _zip_raw(raw_names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for raw in raw_names:
            z.writestr(_RawNameInfo(raw), b"%PDF-1.4 dummy")
        z.writestr("__MACOSX/._dummy", b"x")
    data = buf.getvalue()
    for zi in zipfile.ZipFile(io.BytesIO(data)).infolist():
        assert not zi.flag_bits & 0x800   # 실제 macOS/Windows ZIP 과 같은 조건인지 확인
    return data


def _mac_zip(names):
    """macOS 아카이브 유틸리티와 같은 형태: NFD UTF-8 바이트, 플래그 0x800 없음."""
    return _zip_raw([unicodedata.normalize("NFD", n).encode("utf-8") for n in names])


def test_mac_zip_names_are_nfc():
    names = ["부인산업 사업자등록증 (2).pdf", "부인산업 공장등록증 (1).pdf"]
    got = [n for n, _ in intake.extract_zip(_mac_zip(names))]
    assert got == names
    assert all(unicodedata.is_normalized("NFC", n) for n in got)


def test_windows_cp949_zip_still_decodes():
    data = _zip_raw(["사업자등록증.pdf".encode("cp949")])
    assert [n for n, _ in intake.extract_zip(data)] == ["사업자등록증.pdf"]


def test_nfd_filename_classified_by_filename_rule():
    for name, dt in [("부인산업 사업자등록증 (2).pdf", "nib_business_license"),
                     ("부인산업 공장등록증 (1).pdf", "factory_registration")]:
        nfd = unicodedata.normalize("NFD", name)
        assert intake.refine_doctype_reason(nfd, None)[0] == dt
        assert intake.classify(nfd, "")["doc_type"] == dt   # 본문 없는 스캔본도 파일명으로 살린다
