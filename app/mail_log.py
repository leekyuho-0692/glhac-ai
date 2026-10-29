"""메일 발송 기록(mail_log) — 관리자 화면 '메일 발송 기록'의 데이터.

발송 경로마다 같은 DB 세션에 남긴다(SQLite 는 쓰기 1개라 별도 세션이면 잠금 충돌이 난다).
  · 알림 메일: main.drain_notifications — email 채널 결과마다 1행(재시도도 1행씩)
  · 임시 비밀번호·영업 QR: mail_features.send_logged
기록이 실패해도 발송 자체를 막지 않는다 — 호출한 쪽 트랜잭션을 깨지 않도록 savepoint 로 감싼다.
"""
import logging

from . import models

log = logging.getLogger("glhac.mail_log")

KIND_LABEL = {"notification": "알림", "password_reset": "임시 비밀번호", "consultant_qr": "영업 QR"}


def add(db, kind, recipient, subject, body, ok, error=None, **refs):
    try:
        with db.begin_nested():
            db.add(models.MailLog(kind=kind, recipient=(recipient or "")[:254],
                                  subject=(subject or "")[:300], body=body or "",
                                  status="sent" if ok else "failed",
                                  error=str(error)[:300] if error else None,
                                  **{k: v for k, v in refs.items() if v is not None}))
    except Exception as e:  # noqa: BLE001
        log.warning("[mail_log] 기록 실패: %s", type(e).__name__)


def row(m, with_body=False):
    d = {"log_id": m.log_id, "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
         "kind": m.kind, "kind_label": KIND_LABEL.get(m.kind, m.kind), "recipient": m.recipient,
         "subject": m.subject, "status": m.status, "error": m.error, "case_id": m.case_id,
         "notification_id": m.notification_id, "user_id": m.user_id, "actor_id": m.actor_id}
    if with_body:
        d["body"] = m.body
    return d
