"""e2e 시나리오 공용 헬퍼.

1) '진짜' 업로드 파일 — 서버는 확장자와 내용(시그니처·구조)이 맞는지 검사한다
(main._validate_upload_type). 가짜 바이트(b"\\x89PNG dummy")는 415 CONTENT_MISMATCH 로 거절되므로
시나리오는 여기서 만든 최소 유효 파일을 올린다.
2) 오디터 배정 — 오디터는 배정받은 케이스만 연다(조직 분리 10/04 이후). 지적 종결(finding.update)처럼
   오디터 전용 동작을 보려면 운영자가 먼저 배정해야 한다."""
import base64
import io

import httpx
import fitz  # PyMuPDF — 앱 의존성
from PIL import Image


def png_b64(color=(200, 30, 30), size=(8, 8)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def pdf_b64(text="GL-HAC e2e document"):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    raw = doc.tobytes()
    doc.close()
    return base64.b64encode(raw).decode()


def _login(base, username, password="pw"):
    r = httpx.post(f"{base}/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["token"]}


def assign_auditor(base, case_id):
    """운영자(operator1)가 케이스에 데모 오디터를 배정하고 그 오디터의 인증 헤더를 돌려준다.
    같은 서버를 여러 시나리오가 공유하면 담당 한도(AUDITOR_OVERLOADED)에 걸릴 수 있어 후보를 돈다.
    전부 실패하면 None(호출부가 실패로 기록)."""
    users = httpx.get(f"{base}/admin/users", headers=_login(base, "admin", "admin")).json()
    ids = {u.get("username"): u.get("user_id") for u in users}
    operator = _login(base, "operator1")
    for name in ("auditor1", "auditor2", "auditor3"):
        if ids.get(name) and httpx.post(f"{base}/ops/cases/{case_id}/assign-auditor", headers=operator,
                                        json={"auditor_id": ids[name]}).status_code == 200:
            return _login(base, name)
    return None


def link_all_materials(base, headers, case_id):
    """케이스의 모든 제품-원재료를 잇는다. 발급 가드(PRODUCT_MATERIAL_LINKS_MISSING)는 제품마다 실제
    원재료 연결을 요구한다 — 범위 동결 전에(신청 제출 전) 불러야 한다. 연결 실패 건수를 돌려준다."""
    matrix = httpx.get(f"{base}/cases/{case_id}/matrix", headers=headers).json()
    fails = 0
    for product in matrix.get("products", []):
        for material in matrix.get("materials", []):
            r = httpx.post(f"{base}/cases/{case_id}/matrix/link", headers=headers, json={
                "product_id": product["product_id"], "material_id": material["material_id"], "linked": True})
            fails += r.status_code != 200
    return fails
