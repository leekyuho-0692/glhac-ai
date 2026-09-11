"""인증·RBAC/ABAC — 설계 B.4 / 24.1.3. stdlib HMAC 토큰(외부 의존 없음).

보안 Phase A:
- 비밀번호: PBKDF2-HMAC-SHA256(per-user salt, algo-tagged). 레거시 sha256 로그인 시 자동 재해시.
- 시크릿: 기본값이면 dev 모드에서만 허용(프로덕션 부팅 실패).
- 데모 계정: GLHAC_DEV=1 일 때만 시드(프로덕션 우회). 프로덕션은 GLHAC_ADMIN_USER/PASSWORD로 부트스트랩.
- 로그인 레이트리밋(인메모리 슬라이딩 윈도).
"""
import os
import time
import json
import hmac
import base64
import hashlib
import secrets
from fastapi import Depends, HTTPException, Header
from . import models
from .db import get_db

_DEFAULT_SECRET = "dev-secret-change-me"
SECRET = os.environ.get("GLHAC_SECRET", _DEFAULT_SECRET).encode()

# 토큰 수명 — access는 짧게, refresh는 길게(§9.1). 취소는 token_version 증가로.
ACCESS_TTL = int(os.environ.get("GLHAC_ACCESS_TTL", "1800"))          # 30분
REFRESH_TTL = int(os.environ.get("GLHAC_REFRESH_TTL", str(14 * 86400)))  # 14일


def dev_mode() -> bool:
    """개발 모드 — 데모 계정 시드 + 기본 시크릿 허용."""
    return os.environ.get("GLHAC_DEV") == "1"


def secret_is_default() -> bool:
    return SECRET == _DEFAULT_SECRET.encode()


def enforce_secret():
    """프로덕션(비-dev)에서 기본 시크릿이면 부팅 실패 — 토큰 위조 방지."""
    if secret_is_default() and not dev_mode():
        raise RuntimeError(
            "GLHAC_SECRET 미설정(기본값). 프로덕션 부팅 차단. "
            "GLHAC_SECRET에 강한 랜덤값을 설정하거나 개발 시 GLHAC_DEV=1."
        )


# 데모/시드 계정 — 프로덕션에서는 시드하지 않음(GLHAC_DEV=1 전용).
DEFAULT_USERS = [
    ("admin", "admin", "admin", "*"),
    ("consultant1", "pw", "consultant", "org_demo"),
    ("applicant1", "pw", "applicant", "org_demo"),
    ("penyelia1", "pw", "penyelia_halal", "org_demo"),
    ("pendamping1", "pw", "pendamping_pph", "org_demo"),
    ("auditor1", "pw", "auditor", "org_demo"),
    ("auditor2", "pw", "auditor", "org_demo"),     # P3: 배정 카드 데모용 복수 오디터
    ("auditor3", "pw", "auditor", "org_demo"),
    ("fatwa1", "pw", "fatwa_liaison", "org_demo"),
    ("operator1", "pw", "operator", "org_demo"),   # v3: 최고 업무운영자(최종승인자)
]

# P3: 오디터 프로필 시드(전문분야·언어·캐파) — 종전엔 운영자가 ✎로 직접 입력해야 '미설정'을 벗어났다.
# username → profile payload (main._seed_auditor_profiles가 WorkflowEvent sentinel로 적재).
DEFAULT_AUDITOR_PROFILES = {
    "auditor1": {"specialty": ["식품"], "languages": ["ID", "EN"], "capacity": 8},
    "auditor2": {"specialty": ["화장품", "식품"], "languages": ["ID"], "capacity": 6},
    "auditor3": {"specialty": ["식품", "의약"], "languages": ["ID", "EN", "AR"], "capacity": 8},
}

# ---- 비밀번호 해시: PBKDF2-HMAC-SHA256 (stdlib, 신규 의존성 없음) ----
_PBKDF2_ROUNDS = 200_000


def hash_pw(pw, salt=None):
    """새 해시 포맷: pbkdf2_sha256$rounds$salt_hex$hash_hex (per-user 랜덤 salt)."""
    salt_hex = salt if salt else secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt_hex}${dk.hex()}"


def _hash_legacy(pw, salt="glhac"):
    return hashlib.sha256((salt + (pw or "")).encode()).hexdigest()


def verify_pw(pw, stored) -> bool:
    """상수시간 비교. pbkdf2 우선, 레거시 sha256 fallback."""
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt_hex, hexhash = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt_hex), int(rounds))
            return hmac.compare_digest(dk.hex(), hexhash)
        except Exception:  # noqa: BLE001
            return False
    return hmac.compare_digest(_hash_legacy(pw), stored)


def needs_rehash(stored) -> bool:
    """레거시(비-pbkdf2) 해시면 로그인 성공 시 재해시 필요."""
    return not (stored or "").startswith("pbkdf2_sha256$")


# ---- 로그인 레이트리밋 (인메모리 슬라이딩 윈도; 단일 프로세스 가정) ----
_LOGIN_ATTEMPTS = {}   # key -> [timestamps]
_RL_WINDOW = 300       # 5분
_RL_MAX = 10           # 윈도당 최대 실패


def rate_limited(key) -> bool:
    now = time.time()
    arr = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _RL_WINDOW]
    _LOGIN_ATTEMPTS[key] = arr
    return len(arr) >= _RL_MAX


def record_attempt(key):
    _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def clear_attempts(key):
    _LOGIN_ATTEMPTS.pop(key, None)


def make_token(user, typ="access", ttl=None):
    if ttl is None:
        ttl = ACCESS_TTL if typ == "access" else REFRESH_TTL
    payload = {"uid": user.user_id, "username": user.username, "role": user.role,
               "org_id": user.org_id, "typ": typ, "tv": user.token_version or 0,
               "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def make_tokens(user):
    """로그인/발급용 — access + refresh 쌍."""
    return {"token": make_token(user, "access"), "refresh_token": make_token(user, "refresh")}


def verify_token(token):
    try:
        raw, sig = token.split(".")
        if not hmac.compare_digest(hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32], sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:  # noqa: BLE001
        return None


def get_current_user(authorization: str = Header(None), db=Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, {"code": "NO_AUTH"})
    payload = verify_token(authorization[7:])
    if not payload:
        raise HTTPException(401, {"code": "INVALID_TOKEN"})
    if payload.get("typ") == "refresh":   # refresh 토큰은 API 접근용으로 못 씀
        raise HTTPException(401, {"code": "REFRESH_NOT_ALLOWED"})
    # 토큰 취소 확인 — token_version 불일치 시 무효(로그아웃/강제철회 반영)
    u = db.get(models.User, payload.get("uid"))
    if not u or payload.get("tv", 0) != (u.token_version or 0):
        raise HTTPException(401, {"code": "TOKEN_REVOKED"})
    return payload  # {uid, username, role, org_id, typ, tv}


def optional_user(authorization: str = Header(None), db=Depends(get_db)):
    """로그인했으면 사용자를, 아니면 None. 401 을 던지지 않는다.

    한 엔드포인트를 직원(토큰)과 비회원(다른 수단)이 함께 쓰는 곳에 필요하다 —
    게시판 답글이 그렇다. get_current_user 를 쓰면 비회원이 무조건 막힌다."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return get_current_user(authorization, db)
    except HTTPException:
        return None                     # 토큰이 낡았어도 비회원 경로는 열어 둔다


def require_roles(*roles):
    """RBAC 게이트 — admin은 항상 통과(B.4.2)."""
    def dep(user=Depends(get_current_user)):
        if user["role"] != "admin" and user["role"] not in roles:
            raise HTTPException(403, {"code": "NOT_AUTHORIZED", "need": list(roles), "have": user["role"]})
        return user
    return dep


def check_org(user, case):
    """ABAC org 격리 — 타 조직 케이스 접근 차단."""
    if user["role"] != "admin" and case.org_id != user["org_id"]:
        raise HTTPException(403, {"code": "ORG_FORBIDDEN", "case_org": case.org_id, "user_org": user["org_id"]})


def seed_users(db):
    """데모 계정은 GLHAC_DEV=1 에서만. 프로덕션은 GLHAC_ADMIN_USER/PASSWORD로 admin 1개 부트스트랩."""
    if db.query(models.User).count() != 0:
        return
    if dev_mode():
        for u, pw, role, org in DEFAULT_USERS:
            db.add(models.User(username=u, password_hash=hash_pw(pw), role=role, org_id=org))
        db.commit()
        return
    admin_u = os.environ.get("GLHAC_ADMIN_USER")
    admin_p = os.environ.get("GLHAC_ADMIN_PASSWORD")
    if admin_u and admin_p:
        db.add(models.User(username=admin_u, password_hash=hash_pw(admin_p), role="admin", org_id="*"))
        db.commit()
