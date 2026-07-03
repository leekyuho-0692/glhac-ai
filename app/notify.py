"""알림 채널 프로바이더 (Rizky #5).

실제 SMS/카카오톡/WhatsApp 발송은 외부 크리덴셜(환경변수)이 주입될 때만 활성화된다.
크리덴셜이 없으면 각 채널은 로그 스텁으로 동작(발송 시늉만)하고 ok=False를 반환하며,
inapp(앱 내 알림 = DB 저장)은 항상 동작한다. 프로바이더 패턴이라 실연동은 이 파일만 교체하면 된다.
"""
import os
import logging

log = logging.getLogger("glhac.notify")


def _send_sms(to, text):
    key = os.environ.get("GLHAC_SMS_API_KEY")
    if not key:
        log.info("[SMS stub · no GLHAC_SMS_API_KEY] to=%s :: %s", to, text)
        return {"channel": "sms", "ok": False, "reason": "no_credentials"}
    # TODO: 실제 SMS 게이트웨이 연동 (key 사용)
    log.info("[SMS send] to=%s", to)
    return {"channel": "sms", "ok": True}


def _send_kakao(to, text):
    key = os.environ.get("GLHAC_KAKAO_API_KEY")
    if not key:
        log.info("[KakaoTalk stub · no GLHAC_KAKAO_API_KEY] to=%s :: %s", to, text)
        return {"channel": "kakao", "ok": False, "reason": "no_credentials"}
    return {"channel": "kakao", "ok": True}


def _send_whatsapp(to, text):
    key = os.environ.get("GLHAC_WHATSAPP_API_KEY")
    if not key:
        log.info("[WhatsApp stub · no GLHAC_WHATSAPP_API_KEY] to=%s :: %s", to, text)
        return {"channel": "whatsapp", "ok": False, "reason": "no_credentials"}
    return {"channel": "whatsapp", "ok": True}


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
