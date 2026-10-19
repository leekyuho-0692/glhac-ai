#!/usr/bin/env python3
"""requirements.txt 의 직접 의존을 requirements.lock 의 정확한 버전(==)으로 고정해 출력한다(CI 용).

- 주석 제거, 빈 줄 제외, `--skip 접두`(대소문자 무시)로 시작하는 패키지는 뺀다(예: paddle — 무거운 OCR).
- extras(uvicorn[standard])는 유지한다. lock 에 없는 항목은 원래 범위 선언을 그대로 둔다(CI 를 깨지 않기 위해).
사용: python scripts/pin_from_lock.py [--skip paddle] > ci-reqs.txt
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def req_name(line):
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
    return m.group(1) if m else None


def read_lines(path):
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = re.sub(r"\s*#.*$", "", raw).strip()
        if line:
            out.append(line)
    return out


def load_lock(path):
    pins = {}
    for line in read_lines(path):
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$", line)
        if m:
            pins[norm(m.group(1))] = m.group(2)
    return pins


def pin(requirements=None, lock=None, skip=()):
    pins = load_lock(lock or ROOT / "requirements.lock")
    out = []
    for line in read_lines(requirements or ROOT / "requirements.txt"):
        name = req_name(line)
        if not name or any(norm(name).startswith(norm(s)) for s in skip):
            continue
        ver = pins.get(norm(name))
        if ver is None:
            out.append(line)
            continue
        extras = re.search(r"\[[^\]]*\]", line)
        out.append(f"{name}{extras.group(0) if extras else ''}=={ver}")
    return out


if __name__ == "__main__":
    skips = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--skip" and i + 1 < len(sys.argv)]
    print("\n".join(pin(skip=skips)))
