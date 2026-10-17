"""Read-only production readiness check. Never print credentials or personal data."""
import json
import os
import shlex
import sqlite3
import subprocess
from pathlib import Path


def environment():
    values = {}
    source = Path('/opt/glhac/.env')
    if source.exists():
        for line in source.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            if line.startswith('export '):
                line = line[7:]
            key, value = line.split('=', 1)
            try:
                parts = shlex.split(value, comments=True)
                values[key.strip()] = parts[0] if parts else ''
            except ValueError:
                continue
    try:
        pid = subprocess.check_output(['systemctl', 'show', 'glhac-app', '-p', 'MainPID', '--value'], universal_newlines=True).strip()
        for item in Path('/proc/' + pid + '/environ').read_bytes().split(b'\0'):
            if b'=' in item:
                key, value = item.split(b'=', 1)
                values[key.decode()] = value.decode()
    except (OSError, subprocess.SubprocessError):
        pass
    return values


def check():
    env = environment()
    result = {'smtp': {k: bool(env.get(k)) for k in
        ('GLHAC_SMTP_HOST', 'GLHAC_SMTP_USER', 'GLHAC_SMTP_PASS', 'GLHAC_SMTP_FROM')},
        'dev_mode': env.get('GLHAC_DEV') == '1'}
    path = env.get('GLHAC_DB_URL', 'sqlite:////var/lib/glhac/glhac_v3.db')
    if not path.startswith('sqlite:///'):
        result['database_check'] = 'non_sqlite_requires_application_session'
        return result
    path = Path(path[len('sqlite:///'):])
    if not path.is_absolute():
        path = Path('/opt/glhac') / path
    if not path.exists():
        result['database_check'] = 'database_not_found'
        return result
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        result['roles'] = dict(db.execute('SELECT role, COUNT(*) FROM app_user GROUP BY role'))
        result['auditors'] = dict(db.execute("SELECT CASE WHEN username IN ('auditor1','auditor2','auditor3') AND org_id='org_demo' THEN 'sample' ELSE 'real' END, COUNT(*) FROM app_user WHERE role='auditor' GROUP BY 1"))
        result['cases'] = db.execute('SELECT COUNT(*) FROM case_application').fetchone()[0]
        result['cases_without_real_client_account'] = db.execute("SELECT COUNT(*) FROM case_application c WHERE NOT EXISTS (SELECT 1 FROM app_user u WHERE u.org_id=c.org_id AND u.role IN ('applicant','penyelia_halal') AND u.username NOT IN ('applicant1','penyelia1'))").fetchone()[0]
        result['cases_using_demo_org'] = db.execute("SELECT COUNT(*) FROM case_application WHERE org_id='org_demo'").fetchone()[0]
        result['pending_staff'] = dict(db.execute("SELECT requested_role, COUNT(*) FROM staff_signup_request WHERE status='pending' GROUP BY requested_role"))
    except sqlite3.Error as error:
        result['database_check'] = type(error).__name__
    finally:
        db.close()
    return result


if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False))
