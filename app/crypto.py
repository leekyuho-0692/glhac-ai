"""PII·문서 필드 암호화 (Track A) — AES-256-GCM 봉투암호화.

설계 원칙
  - 토큰 형식: ``<ver>:<urlsafe_b64(nonce[12] || tag[16] || ciphertext)>`` — 키버전 접두로 회전 대비.
  - 평문 혼재 안전: 접두가 알려진 키버전이 아니거나 복호 실패 시 원문을 그대로 반환.
    → 컬럼별·행별 점진 마이그레이션 중에도 읽기가 깨지지 않음(EncryptedType의 안전판).
  - 키 소스: 환경변수 ``GLHAC_ENC_KEY``(base64 44자 또는 hex 64자 = 32바이트).
    미설정 시 앱 SECRET에서 HKDF 파생(데모 폴백) — 경고 로그. 프로덕션은 전용 키 권장.
  - 키 로딩은 **지연**(첫 사용 시) — models→crypto→auth 순환임포트 회피.
  - 블라인드 인덱스: 암호화 컬럼의 동등검색용 HMAC(정규화값). 현재 PII 컬럼은 SQL 동등검색이
    없어 미사용이나, 향후 필요 시 위해 제공.

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
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

log = logging.getLogger("glhac.crypto")

ACTIVE_VER = "v1"          # 신규 암호화가 붙이는 키버전
_NONCE = 12
_TAG = 16
_KEYS = None               # 지연 로딩 캐시

# 배포 게이트 — 기본 off. 코드는 배포하되 GLHAC_ENCRYPTION=1 설정 전까지 암호화 미적용.
# off일 때 EncryptedType는 평문 Text와 동일(쓰기 그대로, 읽기 passthrough).
ENCRYPT_COLUMNS = os.environ.get("GLHAC_ENCRYPTION", "0") == "1"


def _parse_key(raw):
    """base64(44자) 또는 hex(64자) 문자열 → 32바이트 키. 임의 길이는 HKDF 정규화."""
    raw = (raw or "").strip()
    try:
        k = bytes.fromhex(raw) if len(raw) == 64 else base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        k = raw.encode("utf-8")
    if len(k) != 32:
        k = HKDF(k, 32, b"", SHA256, context=b"glhac-enc-normalize")
    return k


def _load_keys():
    """{keyver: 32바이트키}. 명시키 우선, 없으면 SECRET 파생(폴백). 회전용 구버전키 병행."""
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
    for env, ver in [("GLHAC_ENC_KEY_V2", "v2"), ("GLHAC_ENC_KEY_V3", "v3")]:
        if os.environ.get(env):
            keys[ver] = _parse_key(os.environ[env])
    return keys


def _keys():
    global _KEYS
    if _KEYS is None:
        _KEYS = _load_keys()
    return _KEYS


def _bidx_key():
    return HKDF(_keys()[ACTIVE_VER], 32, b"", SHA256, context=b"glhac-bidx-v1")


def enc(plaintext):
    """평문 → ``ver:token``. None은 None 유지."""
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    key = _keys()[ACTIVE_VER]
    nonce = get_random_bytes(_NONCE)
    c = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = c.encrypt_and_digest(plaintext.encode("utf-8"))
    return "%s:%s" % (ACTIVE_VER, base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii"))


def dec(token):
    """``ver:token`` → 평문. 암호문이 아니거나 복호 실패 시 원문 반환(혼재 안전)."""
    if token is None or not isinstance(token, str) or ":" not in token:
        return token
    ver, _, blob = token.partition(":")
    key = _keys().get(ver)
    if key is None:
        return token
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        nonce, tag, ct = raw[:_NONCE], raw[_NONCE:_NONCE + _TAG], raw[_NONCE + _TAG:]
        c = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return c.decrypt_and_verify(ct, tag).decode("utf-8")
    except Exception:  # noqa: BLE001
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


class EncryptedType(TypeDecorator):
    """투명 암복호 컬럼 — 쓰기 시 enc(), 읽기 시 dec(). 저장은 Text(암호문 base64).

    기존 평문 행은 dec()의 passthrough로 그대로 읽히므로 점진 백필이 안전하다."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # 배포 게이트 off면 평문 그대로 저장(암호화 미적용). on이면 enc().
        return enc(value) if ENCRYPT_COLUMNS else value

    def process_result_value(self, value, dialect):
        # 항상 dec — off여도 평문은 passthrough라 안전. on 전환 후 기존 평문+신규 암호문 모두 정상.
        return dec(value)


def selftest():
    """자가검증 — 왕복·평문 passthrough·blind index 정규화. 실패 시 예외. main 부팅에서 호출."""
    sample = "PII 자가검증 · NIB 1234567890"
    tok = enc(sample)
    assert is_encrypted(tok) and dec(tok) == sample, "enc/dec 왕복 실패"
    assert dec("평문 그대로") == "평문 그대로", "평문 passthrough 실패"
    assert bidx("  ABC123 ") == bidx("abc123"), "blind index 정규화 실패"
    return True
