"""Hindsight's native interpreter entry; no bank credentials or embedded PG."""
import argparse
from datetime import datetime,timezone
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--environment',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    path=args.environment.resolve()
    if path.is_relative_to(root):raise ValueError('native_secrets_must_be_external')
    for key in list(os.environ):
        if key.startswith(('BA_','BA091_','LLM_','R4_')):os.environ.pop(key,None)
    for line in path.read_text('utf8').splitlines():
        if '=' in line:
            k,v=line.split('=',1)
            if k.startswith(('HINDSIGHT','HF_')):os.environ[k]=v
    os.environ['NO_PROXY']='127.0.0.1,localhost,::1'
    url=os.environ['HINDSIGHT_API_DATABASE_URL']
    target=urlparse(url)
    if (target.hostname!='127.0.0.1' or target.port!=5432 or target.path!='/r4_memory_native'
        or target.username!='r4_hindsight_native' or os.environ.get('HINDSIGHT_API_HOST')!='127.0.0.1'
        or os.environ.get('HINDSIGHT_API_PORT')!='18889'):
        raise ValueError('native_memory_target_invalid')
    if os.environ.get('HINDSIGHT_API_LLM_BASE_URL')!='http://127.0.0.1:18761/v1':
        raise ValueError('native_gateway_target_invalid')
    if os.environ.get('HINDSIGHT_API_TENANT_EXTENSION')!='hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension' or not os.environ.get('HINDSIGHT_API_TENANT_API_KEY'):
        raise ValueError('native_service_authentication_required')
    if importlib.metadata.version('hindsight-api-slim')!='1.1.0':raise ValueError('native_version_mismatch')
    import psycopg2
    with psycopg2.connect(url) as c:
        with c.cursor() as cur:
            cur.execute("SELECT current_database(),current_user,(SELECT oid FROM pg_database WHERE datname=current_database()),current_setting('server_version'),(SELECT count(*) FROM memory_units)")
            db,role,oid,version,units=cur.fetchone()
            cur.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user')
            if any(cur.fetchone()) or not units:raise ValueError('restricted_migrated_database_required')
            cur.execute("SELECT has_database_privilege(current_user,'banking_demo','CONNECT')")
            if cur.fetchone()[0]:raise ValueError('memory_role_bank_access_denied')
    record=dict(pid=os.getpid(),database=db,role=role,database_oid=oid,pg_version=version,api_port=18889,hindsight='1.1.0',started_at=datetime.now(timezone.utc).isoformat())
    (path.parent/'service_instance.json').write_text(json.dumps(record,indent=2),'utf8')
    os.chdir(path.parent)
    sys.argv=[sys.argv[0]]
    from hindsight_api.main import main as api_main
    api_main()


if __name__=='__main__':
    try:main()
    except Exception as e:
        print('Native service failed: '+type(e).__name__,flush=True)
        raise SystemExit(2)
