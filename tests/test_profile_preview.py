"""프로필 템플릿 미리보기 검증 — 업체(Form.1)·공장(Factory Audit 기업정보) 폼 미리보기 + PDF.
TestClient 기반(서버 불필요). 파일 고유 DB를 app import 전에 직접 대입해 격리.
실행: <venv>/bin/python tests/test_profile_preview.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# 파일 고유 DB로 격리 — setdefault 아님(다른 테스트 env 오염 방지).
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_preview_test.db"
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _all_values(form):
    return [r["value"] for s in form.get("sections", []) for r in s.get("rows", [])]


def test_profile_preview_and_pdf():
    with TestClient(app) as c:
        atok = _tok(c, "applicant1", "pw")     # org_demo
        adm = _tok(c, "admin", "admin")

        # 1) 케이스 생성 + 업체 프로필(저장 데이터) 세팅
        cid = c.post("/cases", json={"company_name": "Preview Co Ltd"}, headers=_h(atok)).json()["case_id"]
        pr = c.patch(f"/cases/{cid}/profile", json={
            "company_name": "Preview Co Ltd",
            "responsible_person": "Hong Gil-dong",
            "email": "biz@preview.co",
            "phone": "0212345678",
            "address": "Jl. Preview No.1, Jakarta",
            "factory_address": "Jl. Industri, Bekasi",
            "halal_supervisor": "Supervisor Kim",
            "profile_ext": {"city": "Jakarta", "country": "Indonesia", "zip": "12345",
                            "business_type": "Manufacturing", "office_phone": "0217654321",
                            "pic_name": "PIC Lee", "pic_title": "Manager",
                            "registration_type": "Manufacturing", "application_type": "Regular",
                            "registration_status": "New", "production_capacity": "1000/day"},
        }, headers=_h(atok))
        assert pr.status_code == 200, pr.text

        # 2) 업체 프로필 미리보기 — 200 + 예상 라벨/값 포함
        r = c.get(f"/cases/{cid}/docs/company-info/preview", headers=_h(atok))
        assert r.status_code == 200, r.text
        form = r.json()
        vals = _all_values(form)
        assert "Preview Co Ltd" in vals, "회사명이 미리보기 rows에 없음"
        assert "Hong Gil-dong" in vals and "Jakarta" in vals
        # 할랄감독자는 '정식 등록된 penyelia 우선, 없으면 신청서 입력값'이 제품 규칙이다.
        # penyelia는 org 단위라 같은 프로세스의 앞선 테스트가 org_demo에 남길 수 있다
        # → 입력값이 나온다고 못박으면 전체 실행에서만 깨진다. 규칙 그대로 기대값을 세운다.
        _p = c.get("/orgs/org_demo/penyelia", headers=_h(atok)).json()
        _items = _p.get("items", _p) if isinstance(_p, dict) else _p
        _expect_sup = _items[0]["name"] if _items else "Supervisor Kim"
        _sup_rows = [r["value"] for s in form["sections"] for r in s["rows"]
                     if r["label_en"] == "Halal Supervisor"]
        assert _sup_rows == [_expect_sup], (_sup_rows, _expect_sup)
        # 라벨(EN/KO 병기) 존재 확인
        labels = [(row["label_en"], row["label_ko"]) for s in form["sections"] for row in s["rows"]]
        assert ("Client Organization / Company Name", "고객 기관/회사 이름") in labels
        assert ("Office Phone", "사무실 전화번호") in labels
        assert form["header"]["bismillah"] and form["header"]["form_title"]

        # 3) 공장(Facility) 생성·저장 + 공장 프로필 미리보기
        fid = c.post(f"/cases/{cid}/factories", json={"name": "Preview Factory"}, headers=_h(atok)).json()["facility_id"]
        c.patch(f"/facilities/{fid}", json={"name": "Preview Factory", "reg_no": "FAC-REG-99",
                                            "address": "Factory Rd 7, Bekasi", "city": "Bekasi",
                                            "country": "Indonesia", "zip": "17530",
                                            "profile_ext": {"phone": "0219998888", "email": "fac@preview.co",
                                                            "pic_name": "Fac PIC", "pic_title": "Head"}},
                headers=_h(atok))
        r = c.get(f"/cases/{cid}/facilities/{fid}/docs/factory-profile/preview", headers=_h(atok))
        assert r.status_code == 200, r.text
        fform = r.json()
        fvals = _all_values(fform)
        assert "Preview Factory" in fvals and "FAC-REG-99" in fvals and "Bekasi" in fvals
        # 공장 폼도 같은 규칙(등록 penyelia 우선) — 업체 폼과 동일 기대값을 쓴다.
        assert _expect_sup in fvals, "할랄감독자가 공장 폼에 반영 안됨"

        # 4) 두 .pdf 라우트 — 200 + PDF 바이트
        for url in (f"/cases/{cid}/docs/company-info.pdf",
                    f"/cases/{cid}/facilities/{fid}/docs/factory-profile.pdf"):
            pr = c.get(url, headers=_h(atok))
            assert pr.status_code == 200, (url, pr.text[:200])
            assert pr.headers["content-type"].startswith("application/pdf"), (url, pr.headers)
            assert pr.content[:4] == b"%PDF", url

        # 5) org 격리 — 타 조직 사용자 403
        c.post("/admin/users", json={"username": "other_applicant", "password": "pw",
                                     "role": "applicant", "org_id": "org_other"}, headers=_h(adm))
        otok = _tok(c, "other_applicant", "pw")
        assert c.get(f"/cases/{cid}/docs/company-info/preview", headers=_h(otok)).status_code == 403
        assert c.get(f"/cases/{cid}/facilities/{fid}/docs/factory-profile/preview",
                     headers=_h(otok)).status_code == 403
        assert c.get(f"/cases/{cid}/docs/company-info.pdf", headers=_h(otok)).status_code == 403

    print("OK test_profile_preview_and_pdf")


if __name__ == "__main__":
    test_profile_preview_and_pdf()
    print("ALL PASS")
