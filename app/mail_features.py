"""Account recovery and consultant sharing, using existing auth and QR helpers."""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from . import auth, mail_delivery, models, schemas
from .db import get_db


class EmailRequest(BaseModel):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}", value):
            raise ValueError("올바른 이메일 주소를 입력해 주세요")
        return value


class ResetConfirm(BaseModel):
    token: str = Field(min_length=1, max_length=2048)
    new_password: str = Field(max_length=128)

    @field_validator("new_password")
    @classmethod
    def valid_password(cls, value):
        return schemas._password(value)


_hits = {}
_lock = threading.Lock()


def _client_ip(request):
    # 운영은 nginx 뒤라 request.client.host 가 전부 127.0.0.1 이다 — 그대로 쓰면 'IP 당' 제한이
    # 전체 사용자 합계 제한이 되어 20번이면 모두의 재설정이 막힌다. main._extract_real_ip 와 같은
    # 규칙(nginx 가 끝에 붙이는 XFF 마지막 값)을 쓴다(main 을 import 하면 순환이라 여기 둔다).
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    last = xff.split(",")[-1].strip() if xff else ""
    return last or (request.client.host if request.client else "unknown")


def rate_limit(key, maximum, window=3600):
    now = time.time()
    with _lock:
        for old in list(_hits):
            if not _hits[old] or now - _hits[old][-1] >= window:
                del _hits[old]
        hits = [t for t in _hits.get(key, []) if now - t < window]
        if len(hits) >= maximum or (key not in _hits and len(_hits) >= 10000):
            raise HTTPException(429, {"code": "EMAIL_RATE_LIMIT", "message": "잠시 후 다시 시도해 주세요."})
        _hits[key] = hits + [now]


def _signature(payload):
    return hmac.new(auth.SECRET, ("password-reset:" + payload).encode(), hashlib.sha256).hexdigest()


def make_reset_token(user):
    data = {"uid": user.user_id, "tv": user.token_version or 0,
            "ph": hashlib.sha256(user.password_hash.encode()).hexdigest(),
            "exp": int(time.time()) + 1800, "nonce": secrets.token_urlsafe(16)}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return payload + "." + _signature(payload)


def read_reset_token(token):
    try:
        payload, signature = token.split(".")
        if not hmac.compare_digest(signature, _signature(payload)):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["exp"] <= time.time():
            raise ValueError()
        return data
    except Exception:
        raise HTTPException(400, {"code": "INVALID_RESET_TOKEN", "message": "링크가 만료되었거나 이미 사용되었습니다."}) from None


def register_routes(app, invite_factory, qr_renderer, home_url):
    @app.post("/auth/password-reset/request")
    def request_reset(body: EmailRequest, request: Request, db=Depends(get_db)):
        rate_limit(("reset-ip", _client_ip(request)), 20)
        rate_limit(("reset-email", hashlib.sha256(body.email.lower().encode()).hexdigest()), 3)
        mail_delivery.settings()
        user = db.query(models.User).filter(func.lower(models.User.username) == body.email.lower()).first()
        if user:
            base = os.getenv("GLHAC_APP_URL", "https://glhac.co.kr").rstrip("/")
            url = base + "/ui/v4/#reset=" + quote(make_reset_token(user), safe="")
            mail_delivery.send(body.email, "비밀번호 재설정",
                "30분 안에 아래 링크에서 새 비밀번호를 설정해 주세요.\n" + url,
                '<p>30분 안에 새 비밀번호를 설정해 주세요. 링크는 한 번만 사용할 수 있습니다.</p><p><a href="'
                + html.escape(url, quote=True) + '">비밀번호 재설정</a></p><p>요청하지 않았다면 이 메일을 무시해 주세요.</p>')
        return {"ok": True, "message": "등록된 이메일이면 재설정 링크를 발송했습니다. 메일함과 스팸함을 확인해 주세요."}

    @app.post("/auth/password-reset/confirm")
    def confirm_reset(body: ResetConfirm, request: Request, db=Depends(get_db)):
        rate_limit(("confirm-ip", _client_ip(request)), 30)
        data = read_reset_token(body.token)
        user = db.get(models.User, data.get("uid"))
        if (not user or (user.token_version or 0) != data.get("tv") or
                not hmac.compare_digest(hashlib.sha256(user.password_hash.encode()).hexdigest(), data.get("ph", ""))):
            raise HTTPException(400, {"code": "INVALID_RESET_TOKEN", "message": "링크가 만료되었거나 이미 사용되었습니다."})
        if auth.verify_pw(body.new_password, user.password_hash):
            raise HTTPException(422, {"code": "SAME_PASSWORD", "message": "다른 비밀번호를 입력해 주세요."})
        # Compare-and-swap prevents simultaneous reuse across workers.
        changed = db.query(models.User).filter(models.User.user_id == user.user_id,
                    models.User.password_hash == user.password_hash,
                    func.coalesce(models.User.token_version, 0) == data["tv"]).update(
                    {models.User.password_hash: auth.hash_pw(body.new_password),
                     models.User.token_version: data["tv"] + 1}, synchronize_session=False)
        if changed != 1:
            db.rollback()
            raise HTTPException(400, {"code": "INVALID_RESET_TOKEN"})
        db.commit()
        return {"ok": True}

    @app.post("/consultant/qr/email")
    def email_qr(body: EmailRequest, user=Depends(auth.require_roles("consultant")), db=Depends(get_db)):
        rate_limit(("qr-user", user["uid"]), 20)
        mail_delivery.settings()
        invite = invite_factory(db, user["uid"])
        db.commit()
        url = "%s/?ref=%s" % (home_url.rstrip("/"), invite.code)
        image = qr_renderer(url, "png").body
        name = user["username"]
        safe_url = html.escape(url, quote=True)
        mail_delivery.send(body.email, "할랄 인증 상담 · 영업 QR 및 가입 링크",
            "%s 컨설턴트가 보낸 할랄 인증 가입 안내입니다.\nQR 이미지와 아래 가입 링크를 이용해 주세요.\n%s" % (name, url),
            '<p>' + html.escape(name) + ' 컨설턴트의 할랄 인증 가입 안내입니다.</p><p><img src="cid:sales-qr" alt="컨설턴트 가입 QR" width="240"></p><p><a href="'
            + safe_url + '">가입 페이지 열기</a></p><p>가입 링크 주소: <a href="' + safe_url + '">' + safe_url + '</a></p>', image)
        return {"ok": True}
