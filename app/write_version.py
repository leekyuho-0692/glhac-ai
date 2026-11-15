"""두 쓰기 서비스의 스냅샷과 원자적 compare-and-swap. 트랜잭션은 호출자 소유.

ORM version_id_col은 쓰지 않는다. 보호 대상 외 컬럼의 flush는 기존대로 두고,
두 서비스의 명시적 UPDATE에만 버전 비교/증가와 3-way 재시도를 적용한다.
"""
from copy import deepcopy
import logging

from fastapi import HTTPException
from sqlalchemy import inspect, update
from sqlalchemy.orm.attributes import set_committed_value


log = logging.getLogger("glhac.write_version")
MAX_ATTEMPTS = 3


class Snapshot(dict):
    """버전은 dict 키가 아니므로 JSON/API 응답이나 저장 데이터에 섞이지 않는다."""

    def __init__(self, values, row_version=None):
        super().__init__(deepcopy(values))
        self.row_version = row_version


def same_version(base, latest, version):
    # 버전을 모르는 기존 호출은 항상 병합한다. 버전 없는 우회/구버전 writer의
    # 값 변경도 감지하여 기존 사람 결정 보호 정책을 유지한다.
    return (getattr(base, "row_version", None) == (version or 0)
            and base == latest)


def observe_merge(model, source, base, latest, attempt):
    log.info("optimistic_merge model=%s source=%s base_version=%s latest_version=%s attempt=%s",
             model, source, getattr(base, "row_version", None), latest or 0, attempt)


def compare_and_swap(db, row, version, values):
    """JSON/식별값과 버전을 한 UPDATE로 저장하고 ORM의 중복 UPDATE를 막는다."""
    model = type(row)
    state = inspect(row)
    pk = state.mapper.primary_key[0]
    statement = (update(model).where(pk == state.identity[0], model.row_version == version)
                 .values(**values, row_version=(version or 0) + 1)
                 .execution_options(synchronize_session=False, write_service_row=row))
    previous = {key: getattr(row, key) for key in values}
    # do_orm_execute의 기존 신원/시설 가드도 ORM history로 변경 의도를 읽는다.
    # 전체 세션 flush 없이 이 행만 검증하고, CAS 실패 시 이 임시 의도는 복구한다.
    for key, value in values.items():
        setattr(row, key, value)
    try:
        affected = db.execute(statement).rowcount
    except Exception:
        for key, value in previous.items():
            set_committed_value(row, key, value)
        raise
    if affected != 1:
        for key, value in previous.items():
            set_committed_value(row, key, value)
        log.info("optimistic_retry model=%s latest_version=%s", model.__name__, version or 0)
        return False
    for key, value in {**values, "row_version": (version or 0) + 1}.items():
        # dirty 등록은 SAVEPOINT rollback의 만료 대상에도 포함되게 한다.
        setattr(row, key, value)
        set_committed_value(row, key, value)
    return True


def retry_exhausted(db):
    db.rollback()
    raise HTTPException(409, {"code": "CONCURRENT_CHANGE_RETRY"})
