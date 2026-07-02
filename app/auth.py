"""인증·RBAC/ABAC — 설계 B.4 / 24.1.3. stdlib HMAC 토큰(외부 의존 없음)."""
import os
import time
import json
import hmac
import base64
import hashlib
from fastapi import Depends, HTTPException, Header
from . import models

SECRET = os.environ.get("GLHAC_SECRET", "dev-secret-change-me").encode()

DEFAULT_USERS = [
    ("admin", "admin", "admin", "*"),
    ("consultant1", "pw", "consultant", "org_demo"),
    ("applicant1", "pw", "applicant", "org_demo"),
    ("penyelia1", "pw", "penyelia_halal", "org_demo"),
    ("pendamping1", "pw", "pendamping_pph", "org_demo"),
    ("auditor1", "pw", "auditor", "org_demo"),
    ("fatwa1", "pw", "fatwa_liaison", "org_demo"),
    ("operator1", "pw", "operator", "org_demo"),   # v3: 최고 업무운영자(최종승인자)
]


def hash_pw(pw, salt="glhac"):
    return hashlib.sha256((salt + pw).encode()).hexdigest()


def make_token(user, ttl=86400):
    payload = {"uid": user.user_id, "username": user.username, "role": user.role,
               "org_id": user.org_id, "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


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


def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, {"code": "NO_AUTH"})
    payload = verify_token(authorization[7:])
    if not payload:
        raise HTTPException(401, {"code": "INVALID_TOKEN"})
    return payload  # {uid, username, role, org_id}


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
    if db.query(models.User).count() == 0:
        for u, pw, role, org in DEFAULT_USERS:
            db.add(models.User(username=u, password_hash=hash_pw(pw), role=role, org_id=org))
        db.commit()