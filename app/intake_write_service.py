"""DocumentAsset.fields의 유일한 저장 경계. flush/commit은 호출자가 소유한다.

OCR/LLM/주소 조회 전에 document_snapshot으로 잡은 base_fields와 new_fields를 전달한다.
잠금 뒤 최신값과 3-way 병합하며 같은 트랜잭션의 앞선 미커밋 결정도 읽는다.
"""
from copy import deepcopy

from sqlalchemy import inspect
from sqlalchemy.orm.exc import NoResultFound

from . import db as database, models
from .intake import aggregate_fields
from .write_version import (Snapshot, MAX_ATTEMPTS, compare_and_swap, same_version,
                            observe_merge, retry_exhausted)


# 양쪽이 바꾼 키의 충돌 정책. '*'와 표에 없는 예약 키는 새 추출값을 쓴다.
# 새 예약 키 누락은 tests/test_doc_write_service.py의 소스 검사로 검출한다.
FIELD_POLICIES = {
    "*": "replace",
    "_sources": "replace",
    "_field_meta": "replace",
    "_classification": "replace",
    "_rule_omissions": "replace",
    "_measurement_excluded": "replace",
    "_rejected_identifiers": "replace",
    "_missing_fields": "replace",
    "_empty": "replace",
    "_cert_not_valid": "replace",
    "_analysis": "replace",
    "_ai_budget": "replace",
    "_truncated": "replace",
    "_truncations": "replace",
    "_tail_screening": "replace",
    "_gpt_read_spans": "replace",
    "_original_not_stored": "replace",
    "_llm_error": "error",
    "_ocr_error": "error",
    "_verify": "verification",
    "_autofill": "decision",
    "_autofill.applied": "decision",
    "_autofill.changes": "decision",
    "_autofill.conflicts": "decision",
    "_autofill.product_pending": "decision",
    "_scope_pending": "decision",
    "_counts_as_evidence": "evidence",  # 원본 문서의 증빙 지정: 같은 원본의 재추출에서도 보존
    "_upload_id": "preserve",
}
_MISSING = object()
_DECISION_POLICIES = {"decision", "verification", "preserve", "evidence"}


def document_snapshot(doc):
    return Snapshot(doc.fields or {}, doc.row_version or 0)


def document_snapshots(db, case_id):
    return {did: Snapshot(fields or {}, version or 0) for did, fields, version in
            db.query(models.DocumentAsset.document_id, models.DocumentAsset.fields,
                     models.DocumentAsset.row_version).filter_by(case_id=case_id)}


def aggregate_document_fields(documents, *, lookup_result=None, lang="ko"):
    """저장된 문서만 집계한다. 잠금 중에는 주소의 외부 우편번호 조회를 하지 않는다.

    집계기의 조회 옵션이 없으므로 주소를 제외한 목록·식별값을 먼저 계산하고,
    주소는 같은 선택·완성 규칙의 순수 계산으로 복원한다. 문서 JSON은 바꾸지 않는다.
    """
    from . import intake

    rows, addresses = [], {"address": None, "factory_address": None}
    for doc in documents:
        fields = doc.fields or {}
        rows.append({"doc_type": doc.doc_type, "text_full": doc.text_full,
                     "fields": {key: value for key, value in fields.items()
                                if key not in ("address", "factory_address")}})
        if doc.doc_type in intake._APPLICANT_DOCS or doc.doc_type is None:
            if not addresses["address"] and doc.doc_type != "factory_registration":
                addresses["address"] = fields.get("address")
            if not addresses["factory_address"]:
                addresses["factory_address"] = (fields.get("factory_address") or
                    (fields.get("address") if doc.doc_type == "factory_registration" else None))
    agg = aggregate_fields(rows, lang=lang)
    for key, prefix in (("address", ""), ("factory_address", "factory_")):
        agg[key] = intake._complete_address(addresses[key], agg[prefix + "city"],
                                            agg[prefix + "province"], agg[prefix + "country"])
        # 잠금 전에 얻은 우편번호는 같은 주소에 대해서만 재사용한다.
        # 병합으로 주소가 바뀌었다면 과거 주소의 조회값을 옮기지 않는다.
        if (lookup_result and agg[key] and agg[key] == lookup_result.get(key)
                and not agg.get(prefix + "zip")):
            agg[prefix + "zip"] = lookup_result.get(prefix + "zip")
    return agg


def _extracted_values(fields):
    """확인 표시와 진단을 제외한 검증 대상 값. 거절된 식별자도 검산 입력이다."""
    return {key: value for key, value in fields.items()
            if not key.startswith("_") or key == "_rejected_identifiers"}


def verification_inputs(doc_type, filename, text, fields, company_name):
    """검증의 전체 입력 스냅샷. _sources 등 검증에 쓰지 않는 표시는 제외한다."""
    # verify_extraction은 현재 추출값에 없는 미확인 자동반영값도 검산한다.
    # 같은 추출값의 사람 확인은 입력 변경이 아니다. 확인자/상태 전체를 비교하면
    # 다른 필드의 유효한 실패 판정과 회수까지 막으므로 추가 검산값만 복사한다.
    autofill_values = {}
    for change in ((fields.get("_autofill") or {}).get("changes") or {}).values():
        scope, key = change.get("scope"), change.get("field")
        if change.get("status") != "applied" or scope == "facility_link":
            continue
        if scope in ("facility", "facility_profile"):
            key = {"reg_no": "factory_reg_no", "address": "factory_address", "city": "factory_city",
                   "country": "factory_country", "zip": "factory_zip", "phone": "factory_phone"}.get(key, key)
        elif scope == "profile":
            key = {"office_phone": "phone", "total_employee": "employee_count"}.get(key, key)
        value = change.get("value")
        if key and value != fields.get(key, _MISSING):
            values = autofill_values.setdefault(key, [])
            if value not in values:
                values.append(value)
    return deepcopy({"doc_type": doc_type, "filename": filename, "text": text or "",
                     "fields": _extracted_values(fields), "company_name": company_name,
                     "autofill_values": autofill_values,
                     "ai_budget": fields.get("_ai_budget")})


def _decision_value(record):
    return record.get("selected", record.get("value"))


def reanalysis_document_fields(base_fields, extracted_fields):
    """원본의 결정·회수 이력을 유지하려는 재분석의 쓰기 의도를 만든다.

    최신 DB가 아닌 시작 스냅샷만 사용한다. 검증 무효화는 병합 전에 처리한다.
    일반 저장의 누락 키는 삭제이므로 이 보존 의도는 호출부에서 명시한다.
    """
    return deepcopy({**{key: value for key, value in base_fields.items()
                        if FIELD_POLICIES.get(key) in _DECISION_POLICIES},
                     **extracted_fields})


def _merge_value(base, latest, mine, *, prefer_latest):
    if latest == base:
        return mine
    if mine == base or prefer_latest:
        return latest
    return mine


def _replacement_autofill_fields(base, latest, mine):
    """다른 값에 대한 확인을 새 적용값의 확인으로 옮기지 않는다."""
    replaced = set()
    for key, candidate in (mine.get("changes") or {}).items():
        current = (latest.get("changes") or {}).get(key) or {}
        original = (base.get("changes") or {}).get(key) or {}
        if not all(isinstance(value, dict) for value in (candidate, current, original)):
            continue
        applied = mine.get("applied") or {}
        value_changed = (_decision_value(candidate) != _decision_value(original) or
                         key in applied and applied[key] != (base.get("applied") or {}).get(key, _MISSING))
        if (candidate.get("status") == "applied" and current.get("status") == "confirmed"
                and all(candidate.get(part) == current.get(part)
                        for part in ("scope", "field", "facility_id"))
                and _decision_value(candidate) != _decision_value(current)
                and value_changed):
            replaced.add(key)
    return replaced


def _merge_autofill(base, latest, mine, *, entries=False, replacements=()):
    if latest == base:
        return mine
    if mine == base:
        return latest
    if not all(value is _MISSING or isinstance(value, dict) for value in (base, latest, mine)):
        return latest
    before, current, incoming = ({} if value is _MISSING else value for value in (base, latest, mine))
    replaced = replacements if entries else _replacement_autofill_fields(before, current, incoming)
    merged = {}
    for key in before.keys() | current.keys() | incoming.keys():
        values = tuple(fields.get(key, _MISSING) for fields in (before, current, incoming))
        if not entries and key in ("changes", "applied"):
            value = _merge_autofill(*values, entries=True, replacements=replaced)
        else:
            value = _merge_value(*values, prefer_latest=not (entries and key in replaced))
        if value is not _MISSING:
            merged[key] = deepcopy(value)
    return _MISSING if not merged and (mine is _MISSING or latest is _MISSING) else merged


def normalize_document_fields(new_fields, *, base_fields=None, current_fields=None, analysis=None, text=None,
                              decision_keys=(),
                              clear_verification=False, inputs_changed=False,
                              prune_product_pending=False, merge=True):
    """쓰기 의도 정규화 → 3-way 병합. 누락과 None은 구분하고 입력을 바꾸지 않는다."""
    if current_fields is not None and base_fields is None:
        raise ValueError("A base_fields snapshot is required for existing documents")
    base = base_fields or {}
    current = current_fields or {}
    incoming = deepcopy(dict(new_fields or {}))
    inputs_changed = inputs_changed or (base_fields is not None and
                                       _extracted_values(base) != _extracted_values(incoming))
    error_keys = [key for key, policy in FIELD_POLICIES.items() if policy == "error"]
    errors = {key: incoming[key] for key in error_keys if key in incoming}
    if analysis is not None:
        for source in (analysis.get("fields") or {}, getattr(text, "parse_fields", {})):
            errors.update({key: source[key] for key in error_keys if key in source})
        for key in error_keys:
            if analysis.get(key[1:]) is not None:
                errors[key] = analysis[key[1:]]
    succeeded = (analysis is not None and bool((text or "").strip()) and not errors
                 and not incoming.get("_empty") and not (incoming.get("_analysis") or {}).get("partial"))
    for path in decision_keys:
        if FIELD_POLICIES.get(path) not in _DECISION_POLICIES:
            raise ValueError(f"Not a document decision key: {path}")
    # base의 검증만 무효화한다. 병합 뒤 지우면 대기 중 커밋된 검증까지 잃는다.
    if "_verify" not in decision_keys and (inputs_changed or (clear_verification and succeeded)):
        incoming.pop("_verify", None)
    if prune_product_pending:
        from .intake import _prune_product_pending
        incoming = _prune_product_pending(incoming)
    if not merge:
        # 버전이 같아도 오류 수명·검증 무효화 등 쓰기 의도 정규화는 필요하다.
        for key in error_keys:
            value = errors.get(key, _MISSING if succeeded else current.get(key, _MISSING))
            if value is _MISSING:
                incoming.pop(key, None)
            else:
                incoming[key] = deepcopy(value)
        return incoming
    fields = {}
    for key in base.keys() | current.keys() | incoming.keys() | errors.keys():
        policy = FIELD_POLICIES.get(key, FIELD_POLICIES["*"])
        values = tuple(source.get(key, _MISSING) for source in (base, current, incoming))
        if policy == "error":
            value = errors.get(key, _MISSING if succeeded else current.get(key, _MISSING))
        elif key == "_autofill":
            value = _merge_autofill(*values)
        else:
            value = _merge_value(*values, prefer_latest=policy in _DECISION_POLICIES)
        if value is not _MISSING:
            fields[key] = deepcopy(value)
    return fields


def write_document_fields(db, doc_or_id, *, base_fields, new_fields, source, analysis=None, text=None,
                          decision_keys=(),
                          clear_verification=False, prune_product_pending=False,
                          next_doc_type=None, next_text=None):
    """케이스 잠금 → 최신 조회 → 버전 비교/정규화 → 조건부 UPDATE.

    new_fields는 dict 또는 최신 fields를 받아 dict를 만드는 순수 함수이다.
    함수 안에서 외부 호출하거나 ORM 객체를 수정하지 않는다. 이 서비스는
    autoflush도 막으므로 호출자의 여러 확인 명령을 중간 상태로 저장하지 않는다.
    next_doc_type/next_text는 호출 직후 저장할 유형·본문도 유효성 비교에 포함한다.
    text는 오류 정규화용 파서 결과이므로 실제로 저장할 본문과 구분한다.
    base_fields=None은 잠금 후 최신값으로 즉시 계산하는 순수 함수에만 허용한다.
    """
    if not source:
        raise ValueError("Document write source is required")
    if base_fields is None and not callable(new_fields):
        raise ValueError("A base_fields snapshot is required for precomputed fields")
    base = deepcopy(base_fields)
    # 호출자가 전달한 dict가 identity map의 JSON과 같은 객체여도 재조회에 안전하다.
    incoming = new_fields if callable(new_fields) else deepcopy(new_fields)
    with db.no_autoflush:
        doc = db.get(models.DocumentAsset, doc_or_id) if isinstance(doc_or_id, str) else doc_or_id
        if doc is None:
            raise NoResultFound("Document not found")
        state = inspect(doc)
        if state.detached:
            doc = db.get(models.DocumentAsset, doc.document_id)
            if doc is None:
                raise NoResultFound("Document not found")
            state = inspect(doc)
        if state.persistent and state.session is not db:
            raise ValueError("Document belongs to a different session")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            state = inspect(doc)
            if state.persistent:
                document_id, case_id = doc.document_id, doc.case_id
                database.lock_cases(db, [case_id])
                # 보호 대상 외 명시적 로컬 변경만 유지한다. 서비스 쓰기는 이미
                # DB에 있으므로 staged JSON으로 최신 조회를 가리지 않는다.
                local = {attr.key: deepcopy(attr.value) for attr in state.attrs
                         if attr.key not in ("fields", "row_version") and attr.history.has_changes()}
                doc = db.get(models.DocumentAsset, document_id,
                             populate_existing=True, with_for_update=True)
                if doc is None:
                    raise NoResultFound("Document not found")
                current = document_snapshot(doc)
                previous_input = (doc.doc_type, doc.filename, doc.text_full or "")
                for key, value in local.items():
                    setattr(doc, key, value)
            else:
                current = document_snapshot(doc)
                previous_input = (doc.doc_type, doc.filename, doc.text_full or "")
            version = doc.row_version
            before = current if base is None else base
            mine = incoming(deepcopy(current)) if callable(incoming) else incoming
            fast = same_version(before, current, version)
            if not fast:
                observe_merge("DocumentAsset", source, before, version, attempt)
            inputs_changed = previous_input != (
                next_doc_type if next_doc_type is not None else doc.doc_type,
                doc.filename, next_text if next_text is not None else (doc.text_full or ""))
            fields = normalize_document_fields(
                mine, base_fields=before, current_fields=current, analysis=analysis, text=text,
                decision_keys=decision_keys, merge=not fast,
                clear_verification=clear_verification, inputs_changed=inputs_changed,
                prune_product_pending=prune_product_pending)
            if state.persistent:
                if not compare_and_swap(db, doc, version, {"fields": fields}):
                    continue
            else:
                doc.fields = fields
                doc.row_version = (version or 0) + 1
            return fields
    retry_exhausted(db)
