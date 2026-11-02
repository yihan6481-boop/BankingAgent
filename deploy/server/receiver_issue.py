"""Operator-only independent mock inbox credential; never a bank login or grant."""
from pathlib import Path
from datetime import datetime,timedelta,timezone
import argparse,sys,json,hashlib,hmac,secrets,os
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'backend')]
def main():
    from deploy.server.settings import external_file,read_json
    from psycopg.conninfo import make_conninfo
    import psycopg,re
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--username',required=True);p.add_argument('--output',required=True);p.add_argument('--bootstrap-purpose',choices=['enroll','reset']);a=p.parse_args()
    c=read_json(external_file(a.config))
    if not re.fullmatch('(?:banking_server_[a-z0-9_]+|pg02_test_[0-9a-f]{32})',c['database']):raise ValueError('server_or_isolated_test_database_required')
    out=Path(a.output).resolve()
    if out.is_relative_to(ROOT) or out.exists():raise ValueError('new_private_output_required')
    target=make_conninfo(host=c['host'],port=c['port'],dbname=c['database'],user='banking_migrator',passfile=str(external_file(c['passfile'])))
    with psycopg.connect(target) as db:
        row=db.execute('SELECT owner_uuid FROM pg02.credentials WHERE username=%s AND revoked=0',(a.username,)).fetchone()
    if not row:raise ValueError('existing_user_required')
    owner=str(row[0]);token=secrets.token_urlsafe(32);key=external_file(c['payment_key']).read_bytes().strip()
    registry=external_file(str(Path(c['mobile_state_dir'])/'receiver_tokens.json'))
    # One maintenance operator; receiver state is shared and atomically replaced.
    lock=registry.with_suffix('.lock')
    with lock.open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        if os.name=='nt':
            import msvcrt;msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl;fcntl.flock(f,fcntl.LOCK_EX)
        try:
            data=read_json(registry);data={k:v for k,v in data.items() if v['owner']!=owner}
            expires=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
            data[hashlib.sha256(token.encode()).hexdigest()]={'owner':owner,'inbox':hmac.new(key,('inbox:'+owner).encode(),hashlib.sha256).hexdigest(),'expires_at':expires}
            tmp=registry.with_suffix('.new');tmp.write_text(json.dumps(data),'utf8');os.replace(tmp,registry)
            out.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            material={'receiver_token':token,'expires_at':expires,'demo':True}
            if a.bootstrap_purpose:
                bootstrap=secrets.token_urlsafe(32)
                material.update(bootstrap=bootstrap,purpose=a.bootstrap_purpose)
                # FORCE RLS policy is explicitly granted to banking_runtime.
                # Keep it enabled; do not elevate the migrator or change policy.
                with psycopg.connect(make_conninfo(target,user='banking_runtime')) as db:
                    db.execute("SELECT set_config('banking.owner_id',%s,true)",(owner,))
                    db.execute('UPDATE mobile_security.bootstrap SET used_at=now() WHERE owner_uuid=%s AND used_at IS NULL',(owner,))
                    db.execute('INSERT INTO mobile_security.bootstrap(owner_uuid,token_hash,expires_at,purpose) VALUES(%s,%s,%s,%s)',(owner,hmac.new(key,('bootstrap:'+bootstrap).encode(),hashlib.sha256).hexdigest(),expires,a.bootstrap_purpose))
            descriptor=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w',encoding='utf8') as stream:json.dump(material,stream)
            os.chmod(out,0o600)
        finally:
            f.seek(0)
            if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f,fcntl.LOCK_UN)
    print('independent_mock_receiver_credential_written_privately')
if __name__=='__main__':
    try:main()
    except Exception as e:print('receiver_issue_refused:'+type(e).__name__,file=sys.stderr);raise SystemExit(2)
