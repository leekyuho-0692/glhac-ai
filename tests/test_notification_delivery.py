"""Existing notification APIs must retain distinct skipped and failed results."""
import pytest
from fastapi.testclient import TestClient

from app import main, models, notify
from app.db import SessionLocal


@pytest.fixture(scope='module')
def api():
    with TestClient(main.app) as client:
        headers = {}
        for username in ('admin', 'auditor1'):
            response = client.post('/auth/login', json={
                'username': username, 'password': 'admin' if username == 'admin' else 'pw'})
            assert response.status_code == 200, response.text
            headers[username] = {'Authorization': 'Bearer ' + response.json()['token']}
        yield client, headers


@pytest.mark.parametrize('lang', ['ko', 'en', 'id'])
@pytest.mark.parametrize('viewer', ['admin', 'auditor1'])
@pytest.mark.parametrize('scenario,expected', [
    ('partial_skips', 'partial'), ('partial_failure', 'partial'),
    ('all_skipped', 'skipped'), ('sent', 'sent'),
])
def test_delivery_api_preserves_status_reason_and_attempts(api, monkeypatch, lang, viewer, scenario, expected):
    client, headers = api
    channels = ['inapp', 'email', 'sms']
    if scenario == 'all_skipped':
        channels.remove('inapp')
    if scenario == 'partial_failure':
        channels.append('whatsapp')
    results = {channel: {'channel': channel, 'ok': True} for channel in channels}
    if scenario != 'sent':
        results.update({
            'email': {'channel': 'email', 'ok': False, 'reason': 'no_credentials'},
            'sms': {'channel': 'sms', 'ok': False, 'reason': 'no_contact'},
        })
    if scenario == 'partial_failure':
        results['whatsapp'] = {'channel': 'whatsapp', 'ok': False, 'reason': 'twilio_error:timeout'}
    monkeypatch.setattr(notify, 'dispatch', lambda n, contacts=None, channels=None:
                        [results[channel] for channel in channels])
    with SessionLocal() as db:
        db.query(models.Notification).delete(synchronize_session=False)
        auditor = db.query(models.User).filter_by(username='auditor1').one()
        case = models.CaseApplication(org_id=auditor.org_id, company_name='Delivery API regression',
                                      notify_consent=True)
        db.add(case)
        db.flush()
        notification = models.Notification(case_id=case.case_id, org_id=auditor.org_id,
            role='auditor', event_type='delivery_regression', title='Delivery regression',
            channels=channels, payload={'auditor_id': auditor.user_id}, status='unsent')
        db.add(notification)
        db.commit()
        nid = notification.notification_id
        main.drain_notifications(db)
    endpoint = '/admin/notifications' if viewer == 'admin' else '/notifications'
    response = client.get(endpoint, params={'lang': lang}, headers=headers[viewer])
    assert response.status_code == 200, response.text
    data = response.json()
    if viewer == 'admin':
        assert data['counts'][expected] == 1
        rows = data['items']
    else:
        rows = data
    row = next(item for item in rows if item['id'] == nid)
    assert row['status'] == expected
    assert row['channels'] == channels
    assert row['retry_pending'] is (scenario == 'partial_failure')
    for channel, result in results.items():
        status = ('sent' if result['ok'] else 'failed' if channel == 'whatsapp' else 'skipped')
        actual = row['channel_status'][channel]
        assert actual['status'] == status
        assert actual['reason'] == result.get('reason')
        assert actual['attempts'] == (0 if status == 'skipped' else 1)
        assert actual['retryable'] is (status == 'failed')


@pytest.mark.parametrize('lang', ['ko', 'en', 'id'])
@pytest.mark.parametrize('last_error,expected', [
    ('case_closed', 'case_closed'),
    ('no_credentials', 'no_credentials'),
    ('no_contact', 'no_contact'),
    ('not_implemented', 'not_implemented'),
    ('consent_skipped', 'consent_skipped'),
    ('no_result', 'no_result'),
    ('smtp_tls_failed', 'smtp_tls_failed'),
    ('send_error', 'send_error'),
    ('smtp_error:535 Authentication failed for private@example.invalid', 'error'),
    ('email:smtp_error:private SMTP host', 'error'),
    ('email:no_credentials;sms:no_contact', 'error'),
    ('case_closed:<img src=x onerror=alert(1)>', 'error'),
    ('case_closed\nprivate provider detail', 'error'),
    ('CASE_CLOSED', 'error'),
    ('unknown provider failure with private details', 'error'),
    ('', None),
    (None, None),
])
def test_notifications_expose_only_safe_delivery_reason(api, lang, last_error, expected):
    client, headers = api
    with SessionLocal() as db:
        db.query(models.Notification).delete(synchronize_session=False)
        auditor = db.query(models.User).filter_by(username='auditor1').one()
        notification = models.Notification(
            org_id=auditor.org_id, role='auditor', event_type='delivery_regression',
            title='Delivery regression', channels=['email'], channel_status={},
            payload={'auditor_id': auditor.user_id}, retry_pending=False,
            status='failed' if last_error else 'sent', last_error=last_error)
        db.add(notification)
        db.commit()
        nid = notification.notification_id

    response = client.get('/notifications', params={'lang': lang}, headers=headers['auditor1'])
    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item['id'] == nid)
    assert row['delivery_reason'] == expected
    assert row['channel_status'] == {}
    assert 'last_error' not in row
    if expected == 'error':
        assert last_error not in response.text

    history = client.get('/admin/notifications', params={'lang': lang}, headers=headers['admin'])
    assert history.status_code == 200, history.text
    history_row = next(item for item in history.json()['items'] if item['id'] == nid)
    assert history_row['last_error'] == last_error
    assert 'delivery_reason' not in history_row
    with SessionLocal() as db:
        assert db.get(models.Notification, nid).last_error == last_error
