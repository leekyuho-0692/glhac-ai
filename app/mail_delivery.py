"""SMTP delivery with explicit failures and optional inline QR image."""
import os
import smtplib
import ssl
from email.message import EmailMessage

from fastapi import HTTPException


def settings():
    host = os.getenv("GLHAC_SMTP_HOST")
    sender = os.getenv("GLHAC_SMTP_FROM") or os.getenv("GLHAC_SMTP_USER")
    if not host or not sender:
        raise HTTPException(503, {"code": "EMAIL_UNAVAILABLE", "message": "메일 발송 설정을 확인해 주세요."})
    return host, sender


def send(recipient, subject, text, markup, png=None):
    host, sender = settings()
    message = EmailMessage()
    message["Subject"] = "[GL-HAC] " + subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text)
    message.add_alternative(markup, subtype="html")
    if png:
        message.get_payload()[-1].add_related(
            png, maintype="image", subtype="png", cid="<sales-qr>",
            filename="glhac-sales-qr.png", disposition="inline")
    try:
        port = int(os.getenv("GLHAC_SMTP_PORT", "587"))
        context = ssl.create_default_context()
        connection = (smtplib.SMTP_SSL(host, port, timeout=10, context=context)
                      if port == 465 else smtplib.SMTP(host, port, timeout=10))
        with connection as smtp:
            if port != 465:
                smtp.starttls(context=context)
            user, password = os.getenv("GLHAC_SMTP_USER"), os.getenv("GLHAC_SMTP_PASS")
            if user and password:
                smtp.login(user, password)
            if smtp.send_message(message):
                raise RuntimeError("Recipient rejected")
    except Exception:
        raise HTTPException(503, {"code": "EMAIL_SEND_FAILED", "message": "메일 발송에 실패했습니다. 잠시 후 다시 시도해 주세요."}) from None
