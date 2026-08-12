"""알림 채널 프로바이더 (Rizky #5 + §10.2 채널 확장).

실제 발송은 환경변수(크리덴셜)가 주입될 때만 활성화된다. 미주입 시 로그 스텁으로 동작하고
ok=False + reason("no_credentials")을 반환한다(워커는 이 경우 재시도하지 않음).
inapp(앱 내 알림 = DB 저장)은 항상 동작한다.

지원 연동:
- SMS → Twilio REST API (GLHAC_TWILIO_SID/TOKEN/SMS_FROM)
- WhatsApp → Twilio REST API (GLHAC_TWILIO_WA_FROM). 24시간 고객응대 창 밖에서는
  Meta 승인 템플릿만 허용 — GLHAC_TWILIO_WA_TEMPLATE_SID 설정 시 템플릿 경로로 발송.
- KakaoTalk 알림톡 → 대행사 HTTP API 연동(GLHAC_KAKAO_API_URL/API_KEY, 선택 SENDER_KEY·TEMPLATE_CODE)
- Email → SMTP (GLHAC_SMTP_HOST/PORT/USER/PASS/FROM)
- Webhook → HTTP POST + HMAC 서명 (case별/전역 URL, GLHAC_WEBHOOK_SECRET)

프로바이더 시그니처: fn(contacts: dict, text: str, notification) -> {channel, ok, reason?}
  contacts = {"phone","email","webhook"}
"""
import os
import logging

log = logging.getLogger("glhac.notify")


def _twilio_send(to, text, from_key, prefix="", content_sid=None, content_vars=None):
    sid = os.environ.get("GLHAC_TWILIO_SID")
    token = os.environ.get("GLHAC_TWILIO_TOKEN")
    frm = os.environ.get(from_key)
    if not (sid and token and frm):
        return None
    if not to:
        return {"ok": False, "reason": "no_contact"}
    import httpx
    try:
        data = {"To": prefix + to, "From": prefix + frm}
        if content_sid:
            # 승인 템플릿 발송 — 24시간 창 밖에서는 이 경로만 허용된다.
            data["ContentSid"] = content_sid
            if content_vars:
                import json as _json
                data["ContentVariables"] = _json.dumps(content_vars, ensure_ascii=False)
        else:
            data["Body"] = text
        r = httpx.post(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid,
            auth=(sid, token), timeout=10,
            data=data)
        if r.status_code in (200, 201):
            return {"ok": True, "sid": r.json().get("sid")}
        return {"ok": False, "reason": "twilio_%d" % r.status_code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "twilio_error:%s" % str(e)[:60]}


def _send_sms(contacts, text, notification=None):
    to = contacts.get("phone")
    res = _twilio_send(to, text, "GLHAC_TWILIO_SMS_FROM", prefix="")
    if res is None:
        log.info("[SMS stub · no Twilio creds] to=%s :: %s", to, text)
        return {"channel": "sms", "ok": False, "reason": "no_credentials"}
    res["channel"] = "sms"
    return res


def _send_whatsapp(contacts, text, notification=None):
    """WhatsApp 발송. 24시간 고객응대 창 밖에서는 Meta 승인 템플릿만 허용되므로,
    GLHAC_TWILIO_WA_TEMPLATE_SID가 설정되면 템플릿 경로로 보낸다(권장).
    미설정 시 자유 텍스트로 보내며, 이는 24시간 창 안에서만 성공한다."""
    to = contacts.get("phone")
    tpl = os.environ.get("GLHAC_TWILIO_WA_TEMPLATE_SID")
    cvars = None
    if tpl:
        title = getattr(notification, "title", None) or ""
        body = getattr(notification, "body", None) or text or ""
        # 기본 템플릿 변수 규약: {{1}}=제목, {{2}}=본문. 승인 템플릿의 변수 개수와 맞춰야 한다.
        cvars = {"1": str(title)[:200], "2": str(body)[:800]}
    res = _twilio_send(to, text, "GLHAC_TWILIO_WA_FROM", prefix="whatsapp:",
                       content_sid=tpl, content_vars=cvars)
    if res is None:
        log.info("[WhatsApp stub · no Twilio creds] to=%s :: %s", to, text)
        return {"channel": "whatsapp", "ok": False, "reason": "no_credentials"}
    res["channel"] = "whatsapp"
    if not tpl:
        res["note"] = "freeform_24h_window_only"
    return res


def _send_kakao(contacts, text, notification=None):
    """카카오 알림톡 — 비즈메시지 대행사(Solapi·Aligo·NHN 등) HTTP API 제네릭 연동(P3 실구현).
    GLHAC_KAKAO_API_URL + GLHAC_KAKAO_API_KEY 설정 시 실발송, 미설정 시 no_credentials 폴백
    (SMS/WhatsApp의 Twilio 크리덴셜 게이트와 동일 패턴). 선택: SENDER_KEY·TEMPLATE_CODE."""
    to = contacts.get("phone")
    url = os.environ.get("GLHAC_KAKAO_API_URL")
    key = os.environ.get("GLHAC_KAKAO_API_KEY")
    if not (url and key):
        log.info("[KakaoTalk stub · no credentials] to=%s :: %s", to, text)
        return {"channel": "kakao", "ok": False, "reason": "no_credentials"}
    if not to:
        return {"channel": "kakao", "ok": False, "reason": "no_contact"}
    try:
        import httpx
        r = httpx.post(url, timeout=10,
                       headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                       json={"to": to, "text": text,
                             "senderKey": os.environ.get("GLHAC_KAKAO_SENDER_KEY", ""),
                             "templateCode": os.environ.get("GLHAC_KAKAO_TEMPLATE_CODE", "")})
        ok = 200 <= r.status_code < 300
        if not ok:
            log.warning("[KakaoTalk 발송 실패] to=%s http=%s body=%s", to, r.status_code, r.text[:200])
        return {"channel": "kakao", "ok": ok, "reason": None if ok else ("http_%d" % r.status_code)}
    except Exception as e:  # noqa: BLE001
        log.warning("[KakaoTalk 발송 오류] to=%s err=%s", to, e)
        return {"channel": "kakao", "ok": False, "reason": "send_error"}


def _send_email(contacts, text, notification=None):
    """SMTP 발송(§10.2). GLHAC_SMTP_HOST 미설정 시 no_credentials."""
    to = contacts.get("email")
    host = os.environ.get("GLHAC_SMTP_HOST")
    port = int(os.environ.get("GLHAC_SMTP_PORT", "587"))
    user = os.environ.get("GLHAC_SMTP_USER")
    pw = os.environ.get("GLHAC_SMTP_PASS")
    frm = os.environ.get("GLHAC_SMTP_FROM") or user
    if not (host and frm):
        log.info("[Email stub · no SMTP creds] to=%s :: %s", to, text)
        return {"channel": "email", "ok": False, "reason": "no_credentials"}
    if not to:
        return {"channel": "email", "ok": False, "reason": "no_contact"}
    import smtplib
    from email.mime.text import MIMEText
    try:
        subj = (notification.title if notification and notification.title else text)[:120]
        msg = MIMEText(text, _charset="utf-8")
        msg["Subject"] = "[GL-HAC] " + subj
        msg["From"] = frm
        msg["To"] = to
        s = smtplib.SMTP(host, port, timeout=10)
        try:
            s.starttls()
        except Exception:  # noqa: BLE001
            pass
        if user and pw:
            s.login(user, pw)
        s.sendmail(frm, [to], msg.as_string())
        s.quit()
        return {"channel": "email", "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"channel": "email", "ok": False, "reason": "smtp_error:%s" % str(e)[:60]}


def _send_webhook(contacts, text, notification=None):
    """Webhook HTTP POST + HMAC 서명(§10.2). URL 없으면 no_contact."""
    url = contacts.get("webhook")
    if not url:
        return {"channel": "webhook", "ok": False, "reason": "no_contact"}
    import json
    import hmac
    import hashlib
    import httpx
    payload = json.dumps({
        "event_type": getattr(notification, "event_type", None),
        "title": getattr(notification, "title", None),
        "body": getattr(notification, "body", None),
        "case_id": getattr(notification, "case_id", None),
        "role": getattr(notification, "role", None),
    }, ensure_ascii=False)
    secret = os.environ.get("GLHAC_WEBHOOK_SECRET", "").encode()
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest() if secret else ""
    try:
        r = httpx.post(url, content=payload.encode("utf-8"), timeout=10,
                       headers={"Content-Type": "application/json", "X-GLHAC-Signature": sig})
        if 200 <= r.status_code < 300:
            return {"channel": "webhook", "ok": True}
        return {"channel": "webhook", "ok": False, "reason": "webhook_%d" % r.status_code}
    except Exception as e:  # noqa: BLE001
        return {"channel": "webhook", "ok": False, "reason": "webhook_error:%s" % str(e)[:60]}


def channel_status():
    """채널별 설정·구현 상태를 반환한다(관리자 가시성).

    크리덴셜 '값'은 절대 노출하지 않고 '존재 여부(bool)'만 계산한다.
      - inapp    : 항상 connected(DB 저장)
      - sms/whatsapp : Twilio 크리덴셜 완비 시 connected, 아니면 unset
      - kakao    : 대행사 HTTP API 실구현(P3) — URL+KEY 완비 시 connected, 아니면 unset
    """
    e = os.environ.get
    twilio_core = bool(e("GLHAC_TWILIO_SID") and e("GLHAC_TWILIO_TOKEN"))
    sms_ok = bool(twilio_core and e("GLHAC_TWILIO_SMS_FROM"))
    wa_ok = bool(twilio_core and e("GLHAC_TWILIO_WA_FROM"))
    wa_tpl = bool(e("GLHAC_TWILIO_WA_TEMPLATE_SID"))
    kakao_ok = bool(e("GLHAC_KAKAO_API_URL") and e("GLHAC_KAKAO_API_KEY"))
    return {
        "inapp": {"configured": True, "implemented": True, "status": "connected"},
        "sms": {"configured": sms_ok, "implemented": True,
                "status": "connected" if sms_ok else "unset"},
        "whatsapp": {"configured": wa_ok, "implemented": True,
                     "template_configured": wa_tpl,
                     "status": ("connected" if wa_tpl else "connected_freeform_only")
                               if wa_ok else "unset"},
        "kakao": {"configured": kakao_ok, "implemented": True,
                  "status": "connected" if kakao_ok else "unset"},
    }


PROVIDERS = {"sms": _send_sms, "kakao": _send_kakao, "whatsapp": _send_whatsapp,
             "email": _send_email, "webhook": _send_webhook}


def dispatch(notification, contacts=None, channels=None):
    """channels 순회 발송. contacts={phone,email,webhook}. inapp은 DB 저장으로 항상 성공."""
    contacts = contacts or {}
    results = []
    text = (notification.title or "") + (": " + notification.body if notification.body else "")
    for ch in (channels if channels is not None else (notification.channels or ["inapp"])):
        if ch == "inapp":
            results.append({"channel": "inapp", "ok": True})
            continue
        fn = PROVIDERS.get(ch)
        if fn:
            results.append(fn(contacts, text, notification))
    return results
