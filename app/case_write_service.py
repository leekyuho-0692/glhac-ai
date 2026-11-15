"""CaseApplication 프로필·식별 컬럼의 저장 경계. commit/flush는 호출자 소유.

외부 작업 전에 case_snapshot으로 base를 복사한다. 잠금 후 최신값과 base,
쓰기 의도를 병합한다. 즉시 명령은 base=None과 순수 changes 함수를 사용한다.
"""
from copy import deepcopy

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import NoResultFound

from . import db as database, models
from .write_version import (Snapshot, MAX_ATTEMPTS, compare_and_swap, same_version,
                            observe_merge, retry_exhausted)


CASE_COLUMNS = frozenset(("company_name", "nib", "responsible_person", "halal_supervisor",
                          "email", "phone", "address", "factory_reg_no", "factory_address", "due_date"))
# 같은 키에 수동 입력도 가능하다. 작성 주체를 분리하지 않는 2단계에서는 혼합 키의
# 동시 변경도 사람 결정처럼 보호한다. 순수 기계 근거만 새 값 우선이다.
PROFILE_POLICIES = {
    "*": "latest", "_registration_extraction": "replace",
    **dict.fromkeys(("city", "country", "zip", "office_phone", "business_type",
                    "establishment_date", "corporate_reg_no", "company_name_ko", "company_name_en",
                    "responsible_person_en", "total_employee", "product_aliases", "pending_handled",
                    "sector", "sectors", "halal_org", "email_consent", "pic_*", "cp_*"), "latest"),
}
_BASE_WRITES = "case_write_service.base_writes"
_MISSING = object()


def case_snapshot(case):
    return Snapshot({key: getattr(case, key) for key in CASE_COLUMNS | {"profile_ext"}},
                    case.row_version or 0)


def _advance_base(base, before, after):
    """실제로 서비스가 바꾼 경로만 전진시킨다. 보존한 외부 변경은 제외한다."""
    if before == after:
        return base
    if all(value is _MISSING or isinstance(value, dict) for value in (before, after)) and after is not _MISSING:
        result = dict(base) if isinstance(base, dict) else {}
        previous = {} if before is _MISSING else before
        for key in previous.keys() | after.keys():
            value = _advance_base(result.get(key, _MISSING), previous.get(key, _MISSING),
                                  after.get(key, _MISSING))
            if value is _MISSING:
                result.pop(key, None)
            else:
                result[key] = value
        return result
    return after if after is _MISSING else deepcopy(after)


def case_write_base(db, case_id, base):
    """같은 트랜잭션에서 앞서 반영한 값으로 읽기 스냅샷을 전진시킨다."""
    if base is None:
        return None
    result = deepcopy(base)
    version = getattr(base, "row_version", None)
    for _, before, after in db.info.get(_BASE_WRITES, {}).get(case_id, ()):
        result = _advance_base(result, before, after)
        # 다른 writer의 변경을 포함하는 스냅샷은 빠른 경로로 승격하지 않는다.
        if version == before.row_version:
            version = after.row_version
    return Snapshot(result, version)


@event.listens_for(Session, "after_transaction_end")
def _clear_base_writes(db, transaction):
    if transaction.parent is None:
        db.info.pop(_BASE_WRITES, None)


@event.listens_for(Session, "after_soft_rollback")
def _rollback_base_writes(db, previous):
    def retained(owner):
        while owner is not None and owner is not previous:
            owner = owner.parent
        return owner is None
    for case_id, writes in db.info.get(_BASE_WRITES, {}).items():
        writes[:] = [row for row in writes if retained(row[0])]


def _merge(base, latest, mine, *, prefer_latest=True):
    if latest == base:
        return mine
    if mine == base:
        return latest
    if not prefer_latest:
        return mine
    if latest is _MISSING or mine is _MISSING:
        return latest
    if all(value is _MISSING or isinstance(value, dict) for value in (base, latest, mine)):
        before, current, incoming = ({} if value is _MISSING else value for value in (base, latest, mine))
        result = {}
        for key in before.keys() | current.keys() | incoming.keys():
            value = _merge(*(row.get(key, _MISSING) for row in (before, current, incoming)),
                           prefer_latest=prefer_latest)
            if value is not _MISSING:
                result[key] = deepcopy(value)
        return result
    return latest if prefer_latest else mine


def merge_profile(base, latest, mine):
    if not all(value is None or isinstance(value, dict) for value in (base, latest, mine)):
        raise ValueError("Case profile must be a dict or None")
    if mine is None:
        return deepcopy(_merge(base, latest, mine))
    result = {}
    before, current = base or {}, latest or {}
    for key in before.keys() | current.keys() | mine.keys():
        value = _merge(*(row.get(key, _MISSING) for row in (before, current, mine)),
                       prefer_latest=PROFILE_POLICIES.get(key, PROFILE_POLICIES["*"]) == "latest")
        if value is not _MISSING:
            result[key] = deepcopy(value)
    return result


def refresh_case(db, case_or_id):
    """잠금 뒤 populate_existing. 서비스 쓰기는 이미 같은 트랜잭션에 저장돼 있다."""
    with db.no_autoflush:
        if isinstance(case_or_id, str):
            database.lock_cases(db, [case_or_id])
            case = db.get(models.CaseApplication, case_or_id)
        else:
            case = case_or_id
        if case is None:
            return None
        state = inspect(case)
        if state.detached:
            case = db.get(models.CaseApplication, case.case_id)
            if case is None:
                return None
            state = inspect(case)
        if state.session is not None and state.session is not db:
            raise ValueError("Case belongs to a different session")
        if not state.persistent:
            return case
        case_id = case.case_id
        database.lock_cases(db, [case_id])
        local = {attr.key: deepcopy(attr.value) for attr in state.attrs
                 if attr.key not in CASE_COLUMNS | {"profile_ext", "row_version"} and attr.history.has_changes()}
        # lock_cases가 이미 행 잠금을 확보했다. 계보 종결의 재귀에서도
        # 확보한 잠금으로 최신값만 읽고 FOR UPDATE를 다시 요청하지 않는다.
        case = db.get(models.CaseApplication, case_id, populate_existing=True)
        if case is None:
            return None
        for key, value in local.items():
            setattr(case, key, value)
        return case


def write_case(db, case_or_id, *, base, changes, source):
    if not source:
        raise ValueError("Case write source is required")
    if base is None and not callable(changes):
        raise ValueError("A base snapshot is required for precomputed case values")
    original = deepcopy(base)
    incoming = changes if callable(changes) else deepcopy(changes)
    with db.no_autoflush:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            case = refresh_case(db, case_or_id)
            if case is None:
                raise NoResultFound("Case not found")
            before = case_write_base(db, case.case_id, original)
            latest = case_snapshot(case)
            version = case.row_version
            if callable(incoming):
                before = latest if before is None else before
                mine = incoming(deepcopy(latest))
            else:
                mine = incoming
            if mine.keys() - (CASE_COLUMNS | {"profile_ext"}):
                raise ValueError("Unsupported case write column")
            fast = same_version(before, latest, version)
            if not fast:
                observe_merge("CaseApplication", source, before, version, attempt)
            values = {}
            for key, value in mine.items():
                if fast and key == "profile_ext" and not all(
                        item is None or isinstance(item, dict)
                        for item in (before.get(key), latest.get(key), value)):
                    raise ValueError("Case profile must be a dict or None")
                if not fast:
                    merge = merge_profile if key == "profile_ext" else _merge
                    value = merge(before.get(key), latest.get(key), value)
                values[key] = deepcopy(value)
            if inspect(case).persistent:
                if not compare_and_swap(db, case, version, values):
                    continue
            else:
                for key, value in values.items():
                    setattr(case, key, value)
                case.row_version = (version or 0) + 1
            after = case_snapshot(case)
            if case.case_id:
                owner = db.get_nested_transaction() or db.get_transaction()
                db.info.setdefault(_BASE_WRITES, {}).setdefault(case.case_id, []).append((owner, latest, after))
            return case
    retry_exhausted(db)


def new_case(db, *, source, **values):
    changes = {key: values.pop(key) for key in tuple(values) if key in CASE_COLUMNS | {"profile_ext"}}
    case = models.CaseApplication(**values)
    return write_case(db, case, base={}, changes=changes, source=source)
