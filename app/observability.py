"""관측성(§11.3) — 요청 ID·구조화 JSON 접근로그·인프로세스 메트릭(Prometheus 노출).

stdlib만 사용(추가 의존성 없음). 메트릭은 프로세스 내 카운터/지연 합계로 집계하고
GET /metrics 에서 Prometheus 텍스트 포맷으로 노출한다(고카디널리티 방지 위해 라우트 템플릿 기준).
"""
import json
import time
import logging
import threading
from collections import defaultdict

alog = logging.getLogger("glhac.access")

_lock = threading.Lock()
_counters = defaultdict(float)          # (name, labeltuple) -> value
_dur_sum = defaultdict(float)           # (route, method) -> sum seconds
_dur_count = defaultdict(float)         # (route, method) -> count
_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_dur_bucket = defaultdict(lambda: defaultdict(float))  # (route,method) -> {le: count}


def _lk(labels):
    return tuple(sorted((labels or {}).items()))


def inc(name, labels=None, value=1.0):
    """카운터 증가(비즈니스/HTTP 메트릭 공용)."""
    with _lock:
        _counters[(name, _lk(labels))] += value


def observe_request(route, method, status, seconds):
    with _lock:
        _counters[("glhac_http_requests_total",
                   _lk({"route": route, "method": method, "status": str(status)}))] += 1
        if status >= 500:
            _counters[("glhac_http_errors_total", ())] += 1
        if status in (401, 403):
            _counters[("glhac_auth_failures_total", ())] += 1
        key = (route, method)
        _dur_sum[key] += seconds
        _dur_count[key] += 1
        b = _dur_bucket[key]
        for le in _BUCKETS:
            if seconds <= le:
                b[le] += 1
        b["+Inf"] = b.get("+Inf", 0) + 1


def _fmt_labels(lt):
    return "{%s}" % ",".join('%s="%s"' % (k, str(v).replace('"', "'")) for k, v in lt) if lt else ""


def render_prometheus():
    lines = []
    with _lock:
        # 카운터
        by_name = defaultdict(list)
        for (name, lt), v in _counters.items():
            by_name[name].append((lt, v))
        for name, items in sorted(by_name.items()):
            lines.append("# TYPE %s counter" % name)
            for lt, v in items:
                lines.append("%s%s %g" % (name, _fmt_labels(lt), v))
        # 지연 히스토그램(라우트×메서드)
        lines.append("# TYPE glhac_http_request_duration_seconds histogram")
        for (route, method), cnt in _dur_count.items():
            base = _fmt_labels((("method", method), ("route", route)))[:-1]  # drop closing }
            for le in list(_BUCKETS) + ["+Inf"]:
                cval = _dur_bucket[(route, method)].get(le, 0)
                lines.append('glhac_http_request_duration_seconds_bucket%s,le="%s"} %g'
                             % (base, le, cval))
            lines.append("glhac_http_request_duration_seconds_sum%s %g"
                         % (_fmt_labels((("method", method), ("route", route))),
                            _dur_sum[(route, method)]))
            lines.append("glhac_http_request_duration_seconds_count%s %g"
                         % (_fmt_labels((("method", method), ("route", route))), cnt))
    return "\n".join(lines) + "\n"


def snapshot():
    """JSON 요약(디버그/대시보드용)."""
    with _lock:
        out = {}
        for (name, lt), v in _counters.items():
            out.setdefault(name, []).append({"labels": dict(lt), "value": v})
        return out


def access_log(request_id, method, path, status, dur_ms, role=None):
    alog.info(json.dumps({
        "request_id": request_id, "method": method, "path": path,
        "status": status, "dur_ms": round(dur_ms, 1), "role": role,
    }, ensure_ascii=False))
