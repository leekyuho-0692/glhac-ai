"""운영 v4 UX 점검에서 재현한 목록/준비도 오류의 회귀 테스트."""
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app, _calc_readiness
from app.db import SessionLocal
from app import models
from app.auth import hash_pw


def login(client, name, password='pw'):
    r=client.post('/auth/login',json={'username':name,'password':password})
    assert r.status_code==200, r.text
    return {'Authorization':'Bearer '+r.json()['token']}


def test_external_pph_inbox_and_reassignment():
    with TestClient(app) as client:
        cons=login(client,'consultant1')
        first=login(client,'pendamping1')
        with SessionLocal() as db:
            db.add(models.User(username='ux-pph-next',password_hash=hash_pw('pw'),role='pendamping_pph',org_id='ux-external'))
            db.commit()
        second=login(client,'ux-pph-next')
        cid=client.post('/cases',headers=cons,json={'company_name':'UX 외부기업'}).json()['case_id']
        assert client.get(f'/cases/{cid}',headers=first).status_code==403
        assigned=client.post(f'/cases/{cid}/pendamping/assign',headers=cons,json={'pendamping_id':'pendamping1'})
        assert assigned.status_code==200
        assert cid in {x['case_id'] for x in client.get('/pendamping/assignments',headers=first).json()['items']}
        assert client.get(f'/cases/{cid}',headers=first).status_code==200
        # UUID 사전순이 실제 배정 시각과 반대여도 새 담당자가 현재 배정이어야 한다.
        with SessionLocal() as db:
            row=db.get(models.PendampingAssignment,assigned.json()['assignment_id'])
            row.assignment_id='z-older';row.assigned_at=datetime.utcnow()-timedelta(days=1)
            db.commit()
        r=client.post(f'/cases/{cid}/pendamping/assign',headers=cons,json={'pendamping_id':'ux-pph-next'})
        assert r.status_code==200
        assert cid not in {x['case_id'] for x in client.get('/pendamping/assignments',headers=first).json()['items']}
        assert client.get(f'/cases/{cid}',headers=first).status_code==403
        assert client.post(f'/cases/{cid}/pendamping/verify',headers=first,json={'decision':'rework'}).status_code==403
        assert cid in {x['case_id'] for x in client.get('/pendamping/assignments',headers=second).json()['items']}
        assert client.get(f'/cases/{cid}',headers=second).status_code==200
        assert client.post(f'/cases/{cid}/pendamping/verify',headers=second,json={'decision':'rework'}).status_code==200
        admin=login(client,'admin','admin')
        assert cid in {x['case_id'] for x in client.get('/pendamping/assignments',headers=admin).json()['items']}


def test_readiness_distinguishes_missing_unreviewed_and_completed():
    with TestClient(app) as client:
        h=login(client,'applicant1')
        cid=client.post('/cases',headers=h,json={'company_name':'UX 준비도'}).json()['case_id']
        empty=client.get(f'/cases/{cid}/readiness',headers=h).json()
        assert empty['breakdown']['materials'] is None
        assert empty['breakdown']['audit'] is None
        assert empty['evaluation_complete'] is False
        assert empty['readiness']==0
        with SessionLocal() as db:
            material=models.Material(case_id=cid,name='미검토 원재료')
            plan=models.AuditPlan(case_id=cid,status='scheduled')
            db.add_all([material,plan]);db.commit()
            assert _calc_readiness(db,cid)['evaluation_complete'] is False
            material.screen_result='PASS';plan.status='completed';db.commit()
            assessed=_calc_readiness(db,cid)
            assert assessed['breakdown']['materials']==100
            assert assessed['breakdown']['audit']==100
            assert assessed['evaluation_complete'] is True
            db.add(models.AuditFinding(case_id=cid,status='open'))
            db.commit()
            assert _calc_readiness(db,cid)['breakdown']['audit']==80
