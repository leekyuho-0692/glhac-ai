"""알림 채널 프로바이더 (Rizky #5).

실제 발송은 환경변수(크리덴셜)가 주입될 때만 활성화된다. 미주입 시 로그 스텁으로 동작하고
ok=False + reason("no_credentials")을 반환한다(워커는 이 경우 재시도하지 않음).
inapp(앱 내 알림 = DB 저장)은 항상 동작한다. 실연동은 이 파일만 교체하면 된다.

지원 연동:
- SMS / WhatsApp → Twilio REST API (GLHAC_TWILIO_SID / GLHAC_TWILIO_TOKEN / *_FROM)
- KakaoTalk 알림톡 → 발송대행사 스텁(한국 스태프용, GLHAC_KAKAO_API_KEY)
"""
import os
import logging

log = logging.getLogger("glhac.notify")


def _twilio_send(to, text, from_key, prefix=""):
    """Twilio REST API 발송. 크리덴셜/수신번호 없으면 no_credentials/no_contact."""
    sid = os.environ.get("GLHAC_TWILIO_SID")
    token = os.environ.get("GLHAC_TWILIO_TOKEN")
    frm = os.environ.get(from_key)
    if not (sid and token and frm):
        return None  # no_credentials — 상위에서 처리
    if not to:
        return {"ok": False, "reason": "no_contact"}
    import httpx
    try:
        r = httpx.post(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid,
            auth=(sid, token), timeout=10,
            data={"To": prefix + to, "From": prefix + frm, "Body": text})
        if r.status_code in (200, 201):
            return {"ok": True, "sid": r.json().get("sid")}
        return {"ok": False, "reason": "twilio_%d" % r.status_code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "twilio_error:%s" % str(e)[:60]}


def _send_sms(to, text):
    res = _twilio_send(to, text, "GLHAC_TWILIO_SMS_FROM", prefix="")
    if res is None:
        log.info("[SMS stub · no Twilio creds] to=%s :: %s", to, text)
        return {"channel": "sms", "ok": False, "reason": "no_credentials"}
    res["channel"] = "sms"
    return res


def _send_whatsapp(to, text):
    res = _twilio_send(to, text, "GLHAC_TWILIO_WA_FROM", prefix="whatsapp:")
    if res is None:
        log.info("[WhatsApp stub · no Twilio creds] to=%s :: %s", to, text)
        return {"channel": "whatsapp", "ok": False, "reason": "no_credentials"}
    res["channel"] = "whatsapp"
    return res


def _send_kakao(to, text):
    key = os.environ.get("GLHAC_KAKAO_API_KEY")
    if not key:
        log.info("[KakaoTalk 알림톡 stub · no GLHAC_KAKAO_API_KEY] to=%s :: %s", to, text)
        return {"channel": "kakao", "ok": False, "reason": "no_credentials"}
    # TODO: 발송대행사(Solapi/NHN 등) 알림톡 템플릿 발송
    return {"channel": "kakao", "ok": True}


PROVIDERS = {"sms": _send_sms, "kakao": _send_kakao, "whatsapp": _send_whatsapp}


def dispatch(notification, contact=None):
    """알림의 channels를 순회하며 발송. inapp은 DB 저장으로 이미 처리됨(항상 성공)."""
    results = []
    text = (notification.title or "") + (": " + notification.body if notification.body else "")
    for ch in (notification.channels or ["inapp"]):
        if ch == "inapp":
            results.append({"channel": "inapp", "ok": True})
            continue
        fn = PROVIDERS.get(ch)
        if fn:
            results.append(fn(contact, text))
    return results
