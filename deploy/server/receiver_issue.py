"""Operator-only payment-security bootstrap; SMS inboxes use phone numbers."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import argparse, sys, json, hmac, hashlib, secrets, os, re

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'backend')]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config')
    p.add_argument('--username')
    p.add_argument('--output')
    p.add_argument('--bootstrap-purpose', choices=['enroll', 'reset'])
    a = p.parse_args()
    if not a.bootstrap_purpose:
        print('SMS receiver: enter the security phone number; no receiver credential is issued or required.')
        return
    if not all([a.config, a.username, a.output]):
        p.error('--config, --username and --output are required for a security bootstrap')

    from deploy.server.settings import external_file, read_json
    from psycopg.conninfo import make_conninfo
    import psycopg
    c = read_json(external_file(a.config))
    if not re.fullmatch('(?:banking_server_[a-z0-9_]+|pg02_test_[0-9a-f]{32})', c['database']):
        raise ValueError('server_or_isolated_test_database_required')
    out = Path(a.output).resolve()
    if out.is_relative_to(ROOT) or out.exists():
        raise ValueError('new_private_output_required')
    target = make_conninfo(host=c['host'], port=c['port'], dbname=c['database'], user='banking_migrator', passfile=str(external_file(c['passfile'])))
    with psycopg.connect(target) as db:
        row = db.execute('SELECT owner_uuid FROM pg02.credentials WHERE username=%s AND revoked=0', (a.username,)).fetchone()
    if not row:
        raise ValueError('existing_user_required')
    owner = str(row[0])
    key = external_file(c['payment_key']).read_bytes().strip()
    bootstrap = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    # Keep enrollment/reset separate from phone-based SMS viewing. An operator
    # must still explicitly request a bootstrap; normal viewing writes no DB.
    with psycopg.connect(make_conninfo(target, user='banking_runtime')) as db:
        db.execute("SELECT set_config('banking.owner_id',%s,true)", (owner,))
        db.execute('UPDATE mobile_security.bootstrap SET used_at=now() WHERE owner_uuid=%s AND used_at IS NULL', (owner,))
        db.execute('INSERT INTO mobile_security.bootstrap(owner_uuid,token_hash,expires_at,purpose) VALUES(%s,%s,%s,%s)', (owner, hmac.new(key, ('bootstrap:' + bootstrap).encode(), hashlib.sha256).hexdigest(), expires, a.bootstrap_purpose))
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf8') as stream:
        json.dump({'bootstrap': bootstrap, 'expires_at': expires, 'purpose': a.bootstrap_purpose, 'demo': True}, stream)
    os.chmod(out, 0o600)
    print('payment_security_bootstrap_written_privately')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('receiver_issue_refused:' + type(e).__name__, file=sys.stderr)
        raise SystemExit(2)
