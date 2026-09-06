"""OCR 워커 프로세스 — 무거운 모델을 웹 프로세스 밖에 둔다.

왜 분리하나(실측):
    앱 유휴            31 MB
    OCR(인니어) 정착  2,369 MB
    OCR(+한국어) 피크 7,780 MB
웹 프로세스 안에서 돌리면 이 메모리가 계속 상주한다. 8GB 서버에서 한국어를 켜면
그대로 터진다. 워커로 빼면 웹은 30MB만 유지하고, 워커는 일이 끊기면 스스로 죽어
메모리를 반납한다.

왜 '일감마다 새 프로세스'가 아닌가:
    모델 로딩이 매번 10초다. 서류 16개짜리 인테이크면 160초가 그냥 늘어난다.
    그래서 상주하되 유휴 시간이 지나면 종료한다 — 묶음 작업 중에는 로딩 1회.

규약: stdin 으로 JSON 한 줄을 받고 stdout 으로 JSON 한 줄을 돌려준다.
    {"op":"ocr","path":"...","langs":["id"]}  →  {"ok":true,"text":"...","langs_used":[...]}
    {"op":"ping"}                             →  {"ok":true}
표준출력은 결과 전용이다. PaddleOCR 이 진행 로그를 stdout 에 찍으므로, 워커는
그것을 stderr 로 돌려 결과 줄이 오염되지 않게 한다.
"""
import contextlib
import json
import os
import sys


def _run():
    # PaddleOCR/Paddle 이 stdout 에 찍는 로그가 결과 JSON 과 섞이면 부모가 파싱에 실패한다.
    # 모델 로딩 로그는 전부 stderr 로 보낸다.
    with contextlib.redirect_stdout(sys.stderr):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app import ai_local

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:  # noqa: BLE001
            _emit({"ok": False, "error": "BAD_REQUEST"})
            continue
        op = req.get("op")
        if op == "ping":
            _emit({"ok": True})
            continue
        if op == "shutdown":
            _emit({"ok": True})
            return
        if op not in ("ocr", "ocr_lines"):
            _emit({"ok": False, "error": "UNKNOWN_OP"})
            continue
        try:
            with contextlib.redirect_stdout(sys.stderr):
                if op == "ocr_lines":
                    res = ai_local._ocr_image_inproc(req.get("path"),
                                                     req.get("lang") or "korean")
                else:
                    res = ai_local._ocr_text_multi_inproc(req.get("path"),
                                                          req.get("langs") or [])
            _emit(res)
        except Exception as e:  # noqa: BLE001
            _emit({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    _run()
