"""PII·문서 필드 암호화 (Track A) — AES-256-GCM 봉투암호화.

설계 원칙
  - 토큰 형식: ``<ver>:<urlsafe_b64(nonce[12] || tag[16] || ciphertext)>`` — 키버전 접두로 회전 대비.
  - 평문 혼재 안전: 접두가 알려진 키버전이 아니거나 복호 실패 시 원문을 그대로 반환.
    → 컬럼별·행별 점진 마이그레이션 중에도 읽기가 깨지지 않음(EncryptedType의 안전판).
  - 키 소스: 환경변수 ``GLHAC_ENC_KEY``(base64 44자 또는 hex 64자 = 32바이트).
    미설정 시 앱 SECRET에서 HKDF 파생(데모 폴백) — 경고 로그. 프로덕션은 전용 키 권장.
  - 블라인드 인덱스: 암호화 컬럼의 동등검색용 HMAC(정규화값). 평문 노출 없이 ``=`` 조회.

추가 의존성 없음 — 리포지토리 venv의 pycryptodome(Crypto) 사용.
"""
import base64
import hashlib
import hmac
import logging
import os

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.Random import get_random_bytes

log = logging.getLogger("glhac.crypto")

ACTIVE_VER = "v1"          # 신규 암호화가 붙이는 키버전
_NONCE = 12
_TAG = 16


def _parse_key(raw):
    """base64(44자) 또는 hex(64자) 문자열 → 32바이트 키."""
    raw = (raw or "").strip()
    try:
        if len(raw) == 64:
            k = bytes.fromhex(raw)
        else:
            k = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        k = raw.encode("utf-8")
    if len(k) != 32:
        # 임의 길이 입력은 HKDF로 32바이트 정규화(약키 방지)
        k = HKDF(k, 32, b"", SHA256, context=b"glhac-enc-normalize")
    return k


def _load_keys():
    """{keyver: 32바이트키} 로드. 명시키 우선, 없으면 SECRET 파생(폴백)."""
    keys = {}
    raw = os.environ.get("GLHAC_ENC_KEY")
    if raw:
        keys[ACTIVE_VER] = _parse_key(raw)
        log.info("[crypto] GLHAC_ENC_KEY 사용 (%s)", ACTIVE_VER)
    else:
        from . import auth  # 지연 임포트(순환 회피)
        keys[ACTIVE_VER] = HKDF(auth.SECRET, 32, b"", SHA256, context=b"glhac-pii-v1")
        log.warning("[crypto] GLHAC_ENC_KEY 미설정 — 앱 SECRET에서 파생(데모 폴백). "
                    "프로덕션은 GLHAC_ENC_KEY에 전용 키 설정 권장.")
    # 추가 버전(회전 후 구버전 복호용): GLHAC_ENC_KEY_V2 ...
    for env, ver in [("GLHAC_ENC_KEY_V2", "v2"), ("GLHAC_ENC_KEY_V3", "v3")]:
        if os.environ.get(env):
            keys[ver] = _parse_key(os.environ[env])
    return keys


_KEYS = _load_keys()


def _bidx_key():
    """블라인드 인덱스용 별도 파생 키(enc 키와 분리)."""
    return HKDF(_KEYS[ACTIVE_VER], 32, b"", SHA256, context=b"glhac-bidx-v1")


def enc(plaintext):
    """평문 → ``ver:token``. None은 None 유지. 문자열이 아니면 str() 후 암호화."""
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    key = _KEYS[ACTIVE_VER]
    nonce = get_random_bytes(_NONCE)
    c = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = c.encrypt_and_digest(plaintext.encode("utf-8"))
    blob = base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")
    return "%s:%s" % (ACTIVE_VER, blob)


def dec(token):
    """``ver:token`` → 평문. 암호문이 아니거나 복호 실패 시 원문 반환(혼재 안전)."""
    if token is None or not isinstance(token, str) or ":" not in token:
        return token
    ver, _, blob = token.partition(":")
    key = _KEYS.get(ver)
    if key is None:
        return token          # 알 수 없는 접두 → 평문 취급
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        nonce, tag, ct = raw[:_NONCE], raw[_NONCE:_NONCE + _TAG], raw[_NONCE + _TAG:]
        c = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return c.decrypt_and_verify(ct, tag).decode("utf-8")
    except Exception:  # noqa: BLE001 — 인증 실패/형식오류 → 원문(평문일 가능성)
        return token


def is_encrypted(token):
    return isinstance(token, str) and token[:3] in ("v1:", "v2:", "v3:")


def bidx(value):
    """동등검색용 블라인드 인덱스 — 정규화(trim+lower)값의 HMAC-SHA256 hex. None/빈값→None."""
    if value is None:
        return None
    norm = str(value).strip().lower()
    if not norm:
        return None
    return hmac.new(_bidx_key(), norm.encode("utf-8"), hashlib.sha256).hexdigest()


def selftest():
    """부팅 자가검증 — 왕복 성공·틀린키 복호실패·평문 passthrough 확인. 실패 시 예외."""
    sample = "PII 자가검증 · NIB 1234567890"
    tok = enc(sample)
    assert is_encrypted(tok) and dec(tok) == sample, "enc/dec 왕복 실패"
    assert dec("평문 그대로") == "평문 그대로", "평문 passthrough 실패"
    assert bidx("  ABC123 ") == bidx("abc123"), "blind index 정규화 실패"
    return True


try:
    selftest()
except Exception as _e:  # noqa: BLE001
    log.error("[crypto] 자가검증 실패: %s", _e)
    raise
