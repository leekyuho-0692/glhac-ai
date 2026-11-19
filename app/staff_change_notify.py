"""Capture staff edits in their transaction, then queue quiet batches or send deletion mail."""
from collections import defaultdict
from datetime import date, datetime
import hashlib
from html import escape
import json
import logging
import os
import re
import threading

from sqlalchemy import event, inspect, select, true
from sqlalchemy.orm import Session, load_only

from . import db as database, models, mail_delivery, mail_log
from .change_context import current_actor
from .staff_change_text import render


log = logging.getLogger("glhac.staff_changes")
EVENT = "staff.changes"
# Customer-owned input/evidence only. Workflow, assignments, audit and payment tables are excluded.
TRACKED_MODELS = (
    models.CaseApplication, models.DocumentAsset, models.Material, models.Product,
    models.ProductMaterial, models.MaterialEvidenceLink, models.MaterialMeasurement,
    models.CertificateScopeItem, models.SjphEvidence, models.Facility, models.Vehicle,
    models.VehicleCleaning, models.PenyeliaHalal, models.Org, models.User,
)
_BY_TABLE = {model.__tablename__: model for model in TRACKED_MODELS}
_OMIT = frozenset({
    "password_hash", "token_version", "content_b64", "text_full", "text_excerpt", "ocr_lines",
    "translations", "row_version", "created_at", "updated_at", "last_login_at", "uploaded_at",
    "uploaded_by", "uploader_role", "file_hash", "normalized_name", "normalized", "confidence",
    "screen_result", "screen_status", "screen_severity", "matched_uid", "v1_risk", "verdict",
    "created_by", "recorded_by", "verified_by", "verified_at", "pending_handled",
    "tenant",  # branding/billing/tenant settings are internal, even without a leading underscore
})
_SECRET = re.compile(r"password|passwd|secret|token|credential|api.?key|private.?key", re.I)
_MISSING = object()


def now():
    return datetime.utcnow()


def _public(key):
    return not str(key).startswith("_") and key not in _OMIT and not _SECRET.search(str(key))


def _columns(model):
    if model is models.Org:
        return {"name", "address", "profile_ext"}
    return {c.key for c in model.__table__.columns if not c.primary_key and _public(c.key)}


def _read_columns(model):
    return _columns(model) | {inspect(model).primary_key[0].key} | (
        {"org_id", "consultant_id"} if model is models.Org else set())


def _rows(db, model, condition):
    table = model.__table__
    return [dict(row) for row in db.connection().execute(
        select(*(table.c[key] for key in sorted(_read_columns(model)))).where(
            condition if condition is not None else true())).mappings()]


def _stored(db, obj):
    state = inspect(obj)
    if not state.identity:
        return None
    pk = state.mapper.primary_key[0]
    rows = _rows(db, type(obj), pk == state.identity[0])
    return rows[0] if rows else None


def _clean(value):
    if value is _MISSING:
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if _public(k)}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _bounded(value):
    value = _clean(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value[:200] if isinstance(value, str) else value


def _digest(value):
    data = [value is not _MISSING, _clean(value)]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _diff(before, after, key, changes, hashes):
    if ((isinstance(before, dict) and (isinstance(after, dict) or after is _MISSING or after is None))
            or (isinstance(after, dict) and (before is _MISSING or before is None))):
        # Dictionary replacement/addition/removal is represented as individual public leaf keys.
        left, right = before if isinstance(before, dict) else {}, after if isinstance(after, dict) else {}
        for child in sorted(left.keys() | right.keys()):
            if _public(child):
                _diff(left.get(child, _MISSING), right.get(child, _MISSING),
                      key + "." + child, changes, hashes)
    elif _digest(before) != _digest(after):
        changes[key] = {"before": _bounded(before), "after": _bounded(after)}
        hashes[key] = {"before": _digest(before), "after": _digest(after)}


def _safe_name(name):
    name = str(name or "").strip()
    # Never fall back to an email address, phone number or an embedded email in the actor identity.
    if "@" in name or re.search(r"\d[\d ()+.-]{6,}\d", name):
        return ""
    return name[:200]


def _scope(db, model, row):
    case_id = row.get("case_id")
    org_id = row.get("org_id")
    if model is models.VehicleCleaning:
        vehicle = db.get(models.Vehicle, row.get("vehicle_id"))
        if vehicle:
            case_id, org_id = vehicle.case_id, org_id or vehicle.org_id
    if case_id:
        case = db.get(models.CaseApplication, case_id)
        org_id = case.org_id if case else org_id
    return org_id, case_id


def _eligible(actor, model, row, org_id):
    if not actor or not actor.get("uid") or not actor.get("role") or not org_id:
        return False
    if actor["role"] in ("system", "ai", "anonymous"):
        return False
    if actor["role"] == "applicant" and actor.get("org_id") == org_id:
        return False
    return model is not models.User or row.get("role") == "applicant"


def _label(db, model, row):
    if model is models.User:
        return _safe_name(row.get("display_name")) or "#" + row["user_id"]
    if model is models.CaseApplication:
        return _bounded((row.get("company_name") or "") + " (#" + row["case_id"] + ")")
    names = []
    for key, parent, field in (("product_id", models.Product, "name"),
                               ("material_id", models.Material, "name"),
                               ("document_id", models.DocumentAsset, "filename"),
                               ("vehicle_id", models.Vehicle, "plate_no")):
        if key in row and not model.__table__.c[key].primary_key and row[key]:
            value = db.connection().execute(select(getattr(parent, field)).where(
                inspect(parent).primary_key[0] == row[key])).scalar_one_or_none()
            if value:
                names.append(str(value))
    own = next((str(row[key]) for key in ("name", "filename", "plate_no", "scope_value", "param_key", "item_key")
                if row.get(key)), None)
    return _bounded(own or " / ".join(names) or str(row[inspect(model).primary_key[0].key]))


def _new_record(db, model, before, after=None):
    actor = current_actor()
    org_id, case_id = _scope(db, model, before)
    if not _eligible(actor, model, before, org_id):
        return None
    changes, hashes = {}, {}
    if after is not None:
        for key in sorted(_columns(model)):
            _diff(before.get(key), after.get(key), key, changes, hashes)
        if not changes:
            return None
    record = models.ChangeRecord(
        id=models.uid(), org_id=org_id, case_id=case_id, actor_id=actor["uid"], actor_role=actor["role"],
        actor_name=_safe_name(actor.get("display_name")), resource_type=model.__tablename__,
        resource_id=str(before[inspect(model).primary_key[0].key]), label=_label(db, model, before),
        op="update" if after is not None else "delete", changes=changes, value_hashes=hashes,
        created_at=now(), batch_key="case:" + case_id if case_id else "org:" + org_id)
    db.add(record)
    return record


def _language(org, case=None):
    for profile in ((case.profile_ext if case else None), (org.profile_ext if org else None)):
        for key in ("lang", "language", "locale"):
            lang = str((profile or {}).get(key) or "").lower().replace("_", "-").split("-")[0]
            if lang in ("ko", "en", "id"):
                return lang
    return "ko"


def _email(user, profile=None):
    return next((str(v).strip() for v in (user.contact_email, user.username,
                                         profile.email if profile else None) if v and "@" in v), "")


def recipients(db, org_id, case_id=None):
    org = db.get(models.Org, org_id)
    case = db.get(models.CaseApplication, case_id) if case_id else None
    users = db.query(models.User).filter_by(org_id=org_id, role="applicant").order_by(models.User.user_id).all()
    fallback = not users
    if fallback and org and org.consultant_id:
        consultant = db.get(models.User, org.consultant_id)
        if consultant:
            users = [consultant]
    if not users:
        log.warning("staff changes: no recipient org=%s case=%s", org_id, case_id)
    result = []
    for user in users:
        profile = db.get(models.ConsultantProfile, user.user_id) if fallback else None
        recipient_org = db.get(models.Org, user.org_id) if fallback else org
        result.append({"user_id": user.user_id, "role": user.role, "email": _email(user, profile),
                       "fallback": fallback, "lang": _language(recipient_org, None if fallback else case)})
    return result


def notification_contacts(db, notification):
    uid = (notification.payload or {}).get("recipient_user_id")
    user = db.get(models.User, uid) if uid else None
    if not user:
        return {}, True
    fallback = bool((notification.payload or {}).get("params", {}).get("fallback"))
    profile = db.get(models.ConsultantProfile, uid) if fallback else None
    # These are operational account/data notices, independent of marketing/channel opt-in.
    return {"email": _email(user, profile)}, True


def _owner(db):
    return db.get_nested_transaction() or db.get_transaction()


def _record_delete(db, model, before):
    key = (model.__tablename__, before[inspect(model).primary_key[0].key])
    seen = db.info.setdefault("staff_deleted", {})
    if key in seen:
        return
    record = _new_record(db, model, before)
    if record is None:
        return
    seen[key] = _owner(db)
    # Snapshot recipients while their accounts, case and organization still exist.
    if model in (models.CaseApplication, models.Org, models.User):
        org = db.get(models.Org, record.org_id)
        immediate_key = (record.org_id, record.case_id)
        db.info.setdefault("staff_immediate", {}).setdefault(immediate_key, {
            "org_id": record.org_id, "case_id": record.case_id, "owner": _owner(db),
            "company": (org.name if org else None) or before.get("company_name") or record.org_id,
            "recipients": recipients(db, record.org_id, record.case_id)})
    # SQL FK cascades do not put child rows in Session.deleted. Capture them before the parent.
    pk = inspect(model).primary_key[0]
    for child in TRACKED_MODELS:
        for fk in child.__table__.foreign_keys:
            if fk.column is pk or (fk.column.table is pk.table and fk.column.name == pk.name):
                if fk.ondelete not in ("CASCADE", "SET NULL"):
                    continue
                for row in _rows(db, child, fk.parent == before[pk.key]):
                    if fk.ondelete == "CASCADE":
                        _record_delete(db, child, row)
                    else:
                        _new_record(db, child, row, {**row, fk.parent.key: None})


def record_deletions(db, rows):
    """Call before Query.delete/raw SQL; parent calls also cover FK-deleted evidence/links."""
    if not current_actor():
        return
    with db.no_autoflush:
        for obj in rows:
            if type(obj) in TRACKED_MODELS:
                before = _stored(db, obj)
                if before:
                    _record_delete(db, type(obj), before)


def record_bulk_deletions(db, query):
    model = query.column_descriptions[0].get("entity")
    if current_actor() and model in TRACKED_MODELS:
        with db.no_autoflush:
            record_deletions(db, query.options(load_only(*(getattr(model, key) for key in _read_columns(model)))).all())


@event.listens_for(Session, "before_flush")
def _capture(db, _flush_context, _instances):
    if not current_actor():
        return
    with db.no_autoflush:
        for obj in list(db.dirty):
            model = type(obj)
            if model not in TRACKED_MODELS or obj in db.deleted:
                continue
            state = inspect(obj)
            changed = {key for key in _columns(model) if state.attrs[key].history.has_changes()}
            if not changed:
                continue
            before = _stored(db, obj)
            if before is None:
                continue
            # DB fallback is necessary for expired attributes and flag_modified/in-place JSON.
            after = dict(before)
            for key in changed:
                history = state.attrs[key].history
                if history.deleted:
                    before[key] = history.deleted[0]
                after[key] = getattr(obj, key)
            _new_record(db, model, before, after)
        record_deletions(db, list(db.deleted))


@event.listens_for(Session, "before_commit")
def _before_commit(db):
    if not db.in_nested_transaction() and (current_actor() or db.info.get("staff_immediate")):
        # Finalize once, after all flushes/deletes and successful savepoints, before the root commit.
        db.flush()
        with db.no_autoflush:
            try:
                _prepare_immediate(db)
            except Exception:
                log.exception("staff changes: _prepare_immediate failed; deferring notifications")
                db.info.pop("staff_immediate", None)
                db.info.pop("staff_outbox", None)


@event.listens_for(Session, "do_orm_execute", retval=True)
def _capture_bulk_updates(state):
    if not state.is_update or not current_actor():
        return
    model = _BY_TABLE.get(getattr(getattr(state.statement, "table", None), "name", None))
    if model is None:
        return
    db = state.session
    # Includes case/document compare-and-swap writes, which intentionally bypass ORM flush.
    with db.no_autoflush:
        if db.get_bind().dialect.name == "sqlite":
            database.lock_sqlite_writer(db)
        before = _rows(db, model, state.statement.whereclause)
        result = state.invoke_statement()
        if before and result.rowcount:
            pk = inspect(model).primary_key[0]
            after = {row[pk.key]: row for row in _rows(db, model, pk.in_([row[pk.key] for row in before]))}
            for row in before:
                if row[pk.key] in after:
                    _new_record(db, model, row, after[row[pk.key]])
        return result


def merge_records(records):
    """First before → last after per resource/field; deletion replaces earlier updates."""
    groups = {}
    for record in sorted(records, key=lambda r: (r.created_at, r.id)):
        key = (record.resource_type, record.resource_id)
        row = groups.setdefault(key, {"resource_type": record.resource_type, "resource_id": record.resource_id,
            "label": record.label, "op": record.op, "changes": {}, "hashes": {}, "actors": []})
        actor = {"role": record.actor_role, "name": record.actor_name, "at": record.created_at.isoformat(timespec="seconds")}
        if actor not in row["actors"]:
            row["actors"].append(actor)
        if record.op == "delete":
            row.update(op="delete", label=record.label, changes={}, hashes={})
        elif row["op"] != "delete":
            for field, change in (record.changes or {}).items():
                hashes = (record.value_hashes or {}).get(field) or {k: _digest(v) for k, v in change.items()}
                if field not in row["changes"]:
                    row["changes"][field], row["hashes"][field] = dict(change), dict(hashes)
                else:
                    row["changes"][field]["after"] = change["after"]
                    row["hashes"][field]["after"] = hashes["after"]
    result = []
    for row in groups.values():
        row["changes"] = {key: change for key, change in row["changes"].items()
                          if row["hashes"][key]["before"] != row["hashes"][key]["after"]}
        row.pop("hashes")
        if row["op"] == "delete" or row["changes"]:
            result.append(row)
    return result


def _prepare_immediate(db):
    pending = db.info.pop("staff_immediate", {})
    if not pending:
        return
    outbox = list(db.info.get("staff_outbox", []))
    to_mark = {}
    for (org_id, case_id), snapshot in pending.items():
        query = db.query(models.ChangeRecord).filter_by(org_id=org_id, notified_at=None)
        if case_id:
            query = query.filter_by(case_id=case_id)
        records = query.all() + [r for r in db.new if isinstance(r, models.ChangeRecord)
            and r.org_id == org_id and (not case_id or r.case_id == case_id) and r.notified_at is None]
        records = list({r.id: r for r in records}.values())
        entries = merge_records(records)
        to_mark.update((record.id, record) for record in records)
        if entries:
            outbox.append({**snapshot, "entries": entries})
    # Finish every snapshot and timestamp calculation before marking any record.
    timestamp = now()
    try:
        for record in to_mark.values():
            record.notified_at = timestamp
        db.info["staff_outbox"] = outbox
    except Exception:
        for record in to_mark.values():
            record.notified_at = None
        raise


@event.listens_for(Session, "after_commit")
def _send_after_commit(db):
    if db.in_nested_transaction():
        return
    outbox = db.info.pop("staff_outbox", [])
    if not outbox:
        return
    try:
        # Pass plain snapshots and the engine, never the request Session/transaction, to the worker.
        snapshots = [{key: value for key, value in row.items() if key != "owner"} for row in outbox]
        bind = db.get_bind()
        if os.environ.get("GLHAC_STAFF_MAIL_SYNC") == "1":
            _send_immediate(snapshots, bind)
        else:
            threading.Thread(target=_send_immediate, args=(snapshots, bind), daemon=True).start()
    except Exception:
        log.exception("staff changes: deletion email dispatch failed")


def _send_immediate(snapshots, bind):
    try:
        for snapshot in snapshots:
            for recipient in snapshot["recipients"]:
                address = recipient["email"]
                if not address:
                    log.warning("staff changes: no email org=%s user=%s", snapshot["org_id"], recipient["user_id"])
                    continue
                title, body = render({**snapshot, "fallback": recipient["fallback"]}, recipient["lang"])
                error = None
                try:
                    mail_delivery.send(address, title, body, "<pre>" + escape(body) + "</pre>")
                except Exception as exc:
                    error = type(exc).__name__
                    log.exception("staff changes: deletion email failed org=%s", snapshot["org_id"])
                try:
                    with Session(bind=bind) as log_db:
                        mail_log.add(log_db, "notification", address, "[GL-HAC] " + title, body, error is None,
                                     error, case_id=snapshot["case_id"], user_id=recipient["user_id"])
                        log_db.commit()
                except Exception:
                    log.exception("staff changes: deletion mail log failed")
    except Exception:
        log.exception("staff changes: deletion email worker failed")


@event.listens_for(Session, "after_transaction_end")
def _cleanup(db, transaction):
    if transaction.parent is None:
        for key in ("staff_deleted", "staff_immediate", "staff_outbox"):
            db.info.pop(key, None)


@event.listens_for(Session, "after_soft_rollback")
def _rollback_savepoint(db, previous):
    def retained(owner):
        while owner is not None:
            if owner is previous:
                return False
            owner = owner.parent
        return True
    db.info["staff_deleted"] = {k: owner for k, owner in db.info.get("staff_deleted", {}).items() if retained(owner)}
    db.info["staff_immediate"] = {k: v for k, v in db.info.get("staff_immediate", {}).items() if retained(v["owner"])}
    db.info["staff_outbox"] = [v for v in db.info.get("staff_outbox", []) if retained(v["owner"])]


def queue_staff_change_notifications(db):
    """Atomically mark records and enqueue notifications after the last change's quiet period."""
    from .main import _notify
    queued = 0
    try:
        # SQLite: reserve the writer before reading. Concurrent workers cannot queue the same batch.
        database.lock_chain_tail(db, "staff-change-notify")
        db.flush()
        groups = defaultdict(list)
        for record in db.query(models.ChangeRecord).filter_by(notified_at=None).order_by(
                models.ChangeRecord.created_at, models.ChangeRecord.id):
            groups[record.batch_key].append(record)
        delay = max(0, float(os.environ.get("GLHAC_CHANGE_NOTIFY_DELAY_S", "300")))
        timestamp = now()
        for records in groups.values():
            if (timestamp - max(row.created_at for row in records)).total_seconds() < delay:
                continue
            entries = merge_records(records)
            first = records[0]
            org = db.get(models.Org, first.org_id)
            case = db.get(models.CaseApplication, first.case_id) if first.case_id else None
            company = (org.name if org else None) or (case.company_name if case else None) or first.org_id
            if entries:
                for recipient in recipients(db, first.org_id, first.case_id):
                    params = {"company": company, "entries": entries, "fallback": recipient["fallback"]}
                    title, body = render(params, recipient["lang"])
                    note = _notify(db, case, EVENT, title=title, body=body,
                                   channels=["inapp", "email"], role=recipient["role"])
                    note.org_id = first.org_id
                    note.payload = {"key": EVENT, "params": params, "lang": recipient["lang"],
                                    "recipient_user_id": recipient["user_id"]}
                    queued += 1
            for record in records:
                record.notified_at = timestamp
        db.commit()
    except Exception:
        db.rollback()
        raise
    return queued
