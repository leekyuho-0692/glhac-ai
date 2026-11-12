"""Read-only projection of submitted data. No parsing, screening or LLM calls.

Keep JSON filtering at this boundary: extension profiles and historic payloads
are schemaless and must never expose credentials or implementation metadata.
"""
import json
import re
from collections import defaultdict
from types import SimpleNamespace

from . import domain_dict, models


_PRIVATE = {"pending_handled", "source_docs", "normalized_name", "matched_uid", "authorization", "cookie",
            "content_b64", "text_full", "ocr_lines", "signature", "signature_ref"}


def public_key(key):
    return (isinstance(key, str) and not key.startswith("_") and key not in _PRIVATE
            and not re.search(r"password|passwd|token|secret|hash|credential|api.?key|private.?key", key, re.I))


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if public_key(k)}
    if isinstance(value, (list, tuple)):
        return [public(v) for v in value]
    return value


def pick(row, keys):
    return {key: getattr(row, key, None) for key in keys.split()}


def stamp(value):
    return value.isoformat() if value else None


def _changes(payload):
    """Only recorded before/after pairs; a missing side stays explicitly absent."""
    out = []
    if "from" in payload and "to" in payload:
        out.append({"field": "name", "before": payload["from"], "after": payload["to"]})
    before, after = payload.get("before"), payload.get("after")
    if isinstance(before, dict) or isinstance(after, dict):
        before = before if isinstance(before, dict) else {}
        after = {**(after if isinstance(after, dict) else {}), **(payload.get("changed") or {})}
        for key in sorted(before.keys() | after.keys()):
            if not public_key(key):
                continue
            pair = {"field": key}
            if key in before:
                pair["before"] = before[key]
            if key in after:
                pair["after"] = after[key]
            out.append(pair)
    for before, after in (payload.get("renames") or {}).items():
        out.append({"field": "name", "before": before, "after": after})
    for before in payload.get("dismiss") or []:
        out.append({"field": "name", "before": before, "after": None, "operation": "dismiss"})
    for item in payload.get("autofill_choices") or []:
        out.append({"field": item.get("field"), "before": item.get("before"), "after": item.get("value")})
    for kind in ("products", "materials"):
        for value in payload.get(kind) or []:
            out.append({"field": kind, "after": value})
    return public(out)


def build(db, case, pending, problem_reasons, explain_reason):
    cid = case.case_id
    org = db.get(models.Org, case.org_id)
    px, ox = public(case.profile_ext or {}), public(org.profile_ext or {}) if org else {}
    company = pick(case, "company_name nib responsible_person halal_supervisor address phone email "
                   "factory_reg_no factory_address due_date scheme logistics_scope pathway risk_category "
                   "is_msme annual_revenue outlet_count")
    company.update(org_id=case.org_id, org_name=org.name if org else None,
                   org_address=org.address if org else None, company_email=ox.get("company_email"),
                   profile_ext=px, org_profile_ext=ox)
    contacts = {prefix: {key: px.get(prefix + "_" + key) for key in ("name", "title", "phone", "email")}
                for prefix in ("pic", "cp")}
    consent = {"notify_consent": case.notify_consent, "email_consent": px.get("email_consent"),
               "org_email_consent": ox.get("email_consent")}

    events = (db.query(models.WorkflowEvent).filter_by(case_id=cid)
              .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())
    audits = (db.query(models.AuditLog).filter_by(case_id=cid)
              .filter(models.AuditLog.action.in_(("extraction.apply", "material.add", "material.update",
                                                 "material.delete", "product.add", "product.update", "profile.autofill")))
              .order_by(models.AuditLog.created_at, models.AuditLog.id).all())
    frozen = next(((e.payload or {}).get("snapshot") for e in reversed(events) if e.action == "scope.freeze"), None)
    frozen_facilities = {f["facility_id"]: f for f in (frozen or {}).get("facilities", [])}
    facilities = []
    for f in (db.query(models.Facility).filter(models.Facility.facility_id.in_(case.facility_ids or []),
                                              models.Facility.org_id == case.org_id)
              .order_by(models.Facility.created_at, models.Facility.facility_id)):
        item = pick(f, "facility_id name address reg_no city country zip")
        item.update(profile_ext=public(f.profile_ext or {}), scope_frozen=bool(case.scope_frozen),
                    in_frozen_snapshot=f.facility_id in frozen_facilities,
                    frozen_snapshot=public(frozen_facilities.get(f.facility_id)))
        facilities.append(item)

    # Select only visible document columns and a boolean for the binary content.
    doc_cols = "document_id filename doc_type fields review_status created_at uploader_role uploaded_by material_id product_id confidence text_excerpt text_full".split()
    D = models.DocumentAsset
    rows = db.query(*(getattr(D, k) for k in doc_cols), D.content_b64.isnot(None).label("has_file")).filter_by(case_id=cid).order_by(D.created_at, D.document_id).all()
    # Older generic uploads kept attribution only in the audit resource reference.
    uploaders = {a.resource_id: a for a in db.query(models.AuditLog)
                 .join(D, D.document_id == models.AuditLog.resource_id)
                 .filter(D.case_id == cid, models.AuditLog.action == "document.upload")
                 .order_by(models.AuditLog.created_at)}
    measurements = db.query(models.MaterialMeasurement).filter_by(case_id=cid).all()
    measures = defaultdict(list)
    for measurement in measurements:
        measures[measurement.document_id].append(measurement)
    documents, autofill_history = [], []
    for row in rows:
        d = SimpleNamespace(**dict(row._mapping))
        d.content_b64 = d.has_file
        upload = uploaders.get(d.document_id)
        if upload:
            d.uploader_role = d.uploader_role or upload.actor_role
            d.uploaded_by = d.uploaded_by or upload.actor_id
        raw = d.fields or {}
        fields = public(raw)
        verify = raw.get("_verify") or {}
        reasons = public(problem_reasons(d, measures[d.document_id]))
        for reason in reasons:
            reason["explanations"] = {lang: explain_reason(reason, lang) for lang in ("ko", "en", "id")}
        documents.append({**pick(d, "document_id filename doc_type review_status uploader_role uploaded_by material_id product_id confidence has_file"),
                          "uploaded_at": stamp(d.created_at), "fields": fields,
                          "sources": {k: public(v) for k, v in (raw.get("_sources") or {}).items() if k in fields},
                          "verify": public({k: verify[k] for k in ("verdict", "field_issues", "summary", "at") if k in verify}),
                          "truncated": bool(raw.get("_truncated") or raw.get("_truncations")),
                          "truncations": public(raw.get("_truncations") or ([raw["_truncated"]] if raw.get("_truncated") else [])),
                          "problem_reasons": reasons})
        meta = raw.get("_autofill") or {}
        changes = list((meta.get("changes") or {}).values()) + list(meta.get("conflicts") or [])
        for change in changes:
            if change.get("status") not in ("applied", "confirmed", "reverted") or not public_key(change.get("field", "")):
                continue
            confirmed = change.get("status") == "confirmed"
            entry = {"source": "autofill", "action": "autofill." + change["status"],
                     "actor_id": change.get("confirmed_by") if confirmed else None,
                     "actor_role": None, "at": change.get("confirmed_at") if confirmed else meta.get("at"),
                     "document_id": d.document_id, "filename": d.filename,
                     "field": change.get("field"), "scope": change.get("scope"),
                     "changes": [{"field": change.get("field")} ]}
            pair = entry["changes"][0]
            old_key = "value" if confirmed and "value" in change else "before" if "before" in change else "current"
            new_key = "selected" if confirmed else "value"
            if change["status"] == "reverted":
                old_key, new_key = "value", "before"
            if old_key in change:
                pair["before"] = public(change[old_key])
            if new_key in change:
                pair["after"] = public(change[new_key])
            autofill_history.append(entry)

    by_doc = {d["document_id"]: d for d in documents}
    evidence = defaultdict(dict)
    for d in documents:
        if d["material_id"]:
            evidence[d["material_id"]][d["document_id"]] = {k: d[k] for k in ("document_id", "filename", "doc_type", "review_status", "has_file")}
    for link in db.query(models.MaterialEvidenceLink).filter_by(case_id=cid):
        d = by_doc.get(link.document_id)
        if d:
            evidence[link.material_id][link.document_id] = {**{k: d[k] for k in ("document_id", "filename", "doc_type", "review_status", "has_file")},
                                                          **pick(link, "link_type link_reason match_basis verified_by verified_at")}
    declarations = {(e.payload or {}).get("material_id"): (e.payload or {}).get("declared_cert_no")
                    for e in events if e.action == "material.certificate"}
    materials = []
    for m in db.query(models.Material).filter_by(case_id=cid).order_by(models.Material.material_id):
        materials.append({**pick(m, "material_id name e_number mat_type source supplier manufacturer origin note "
                                 "screen_result screen_status screen_severity cert cert_no evidence_provided"),
                          "declared_cert_no": declarations.get(m.material_id),
                          "evidence": list(evidence[m.material_id].values())})
    links = defaultdict(set)
    for link in db.query(models.ProductMaterial).filter_by(case_id=cid):
        links[link.product_id].add(link.material_id)
    products = [{**pick(p, "product_id name category description registration_type status"),
                 "material_count": len(links[p.product_id])}
                for p in db.query(models.Product).filter_by(case_id=cid).order_by(models.Product.product_id)]

    history = []
    for e in events:
        if not (e.action.startswith(("extraction.", "material.", "product.", "profile.", "case.scale.", "documents.review"))):
            continue
        payload = public(e.payload or {})
        history.append({"source": "event", "action": e.action, "actor_id": e.actor_id,
                        "actor_role": e.actor_type, "at": stamp(e.created_at),
                        "details": payload, "changes": _changes(payload)})
    for row in audits:
        payload = public(row.meta or {})
        history.append({"source": "audit", "action": row.action, "actor_id": row.actor_id,
                        "actor_role": row.actor_role, "at": stamp(row.created_at),
                        "details": payload, "changes": _changes(payload)})
    history.extend(autofill_history)
    actor_ids = {h["actor_id"] for h in history if h["actor_id"]}
    actors = {u.user_id: u for u in db.query(models.User.user_id, models.User.display_name, models.User.username, models.User.role)
              .filter(models.User.user_id.in_(actor_ids))} if actor_ids else {}
    for h in history:
        actor = actors.get(h["actor_id"])
        h["actor_name"] = actor.display_name or actor.username if actor else None
        if actor and not h["actor_role"]:
            h["actor_role"] = actor.role
    history.sort(key=lambda h: h["at"] or "", reverse=True)

    submitted = next((e for e in reversed(events) if e.action == "application.submit"), None)
    formal_event = next((e for e in reversed(events) if e.action in (
        "formal_application.submitted", "formal_application.accepted", "formal_application.returned")), None)
    g = (db.query(models.GeneratedDocument).filter_by(case_id=cid, doc_type="formal_application")
         .order_by(models.GeneratedDocument.version.desc(), models.GeneratedDocument.created_at.desc()).first())
    formal = None
    if g:
        try:
            content = json.loads(g.content or "{}")
        except (ValueError, TypeError):
            content = {}
        if not isinstance(content, dict):
            content = {}
        formal = {"status": formal_event.action.rsplit(".", 1)[-1] if formal_event else g.status,
                  "fields": public(content.get("fields") or {}), "pledge": content.get("pledge"),
                  "signed": bool(content.get("signature")), "submitted_at": content.get("submitted_at"),
                  "submitted_by": content.get("submitted_by"), "submitted_role": content.get("submitted_role")}
    labels, value_labels = {}, {}
    for lang in ("ko", "en", "id"):
        known = domain_dict.code_labels("EXTRACT_FIELD", "extract_field", lang)
        # Unknown keys are humanized and reported by the view, not mistaken for translations.
        labels[lang] = known
        value_labels[lang] = {**domain_dict.code_labels("ENUM", "enum_code", lang),
                             **domain_dict.doc_labels(lang),
                             **domain_dict.code_labels("SOURCE", "screen_source", lang),
                             **domain_dict.code_labels("MATERIAL", "material_type", lang)}
    return {"case_id": cid, "company": company, "contacts": contacts, "consent": consent,
            "facilities": facilities, "products": products, "materials": materials, "documents": documents,
            "pending": public(pending), "history": history,
            "application": {"status": case.status, "draft_state": case.draft_state,
                            "submitted_at": stamp(submitted.created_at) if submitted else None,
                            "formal_application": formal},
            "summary": {"problem_document_count": sum(bool(d["problem_reasons"]) or d["review_status"] in ("rejected", "rework") for d in documents),
                        "pending_count": pending["count"], "conflict_count": pending["conflict_count"]},
            "labels": labels, "value_labels": value_labels}
