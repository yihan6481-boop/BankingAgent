"""Unique server entry: --component bank|sms|gateway|memory; no initialization."""
from pathlib import Path
import argparse,json,os,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'backend'),str(ROOT/'tools')]
from deploy.server.settings import load,load_service,install,read_json,environment_file,ConfigurationError

def database_identity(c):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.conninfo import make_conninfo
    expected=read_json(c['files']['restore_identity'])
    for section in ['database']:
        d=c[section]
        with psycopg.connect(make_conninfo(host=d['host'],port=d['port'],dbname=d['name'],user=d['role'],passfile=d['passfile']),connect_timeout=5,autocommit=True,row_factory=dict_row) as db:
            r=db.execute('SELECT current_database() name,current_user role,(SELECT oid FROM pg_database WHERE datname=current_database()) oid,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            if any(r[k] for k in ['rolsuper','rolcreatedb','rolcreaterole','rolbypassrls']):raise ConfigurationError('restricted_runtime_role_required')
            if r['name']!=d['name'] or r['role']!=d['role'] or r['oid']!=expected[section]['oid']:raise ConfigurationError('restored_database_identity_changed')
            if section=='database':
                from storage.postgres import assert_runtime_role
                assert_runtime_role(db)
                if db.execute('SELECT max(version) v FROM ops.schema_migrations').fetchone()['v']!=expected['schema_version']:raise ConfigurationError('migration_version_mismatch')
                from storage.pg02 import readiness
                if not readiness().get('application_ready'):raise ConfigurationError('schema_not_ready')
                from storage.migrations import load_migrations
                history=db.execute('SELECT version,name,checksum FROM ops.schema_migrations ORDER BY version').fetchall()
                if [(r['version'],r['name'],r['checksum']) for r in history]!=[(m.version,m.name,m.checksum) for m in load_migrations()]:raise ConfigurationError('migration_source_checksum_mismatch')

def dependencies(c):
    import httpx
    from recall_bridge.config import read_client
    from recall_bridge.transport import Hindsight,MemoryUnavailable
    client=Hindsight(**read_client(c['files']['memory_client']))
    try:client.health()
    except MemoryUnavailable as exc:raise ConfigurationError(str(exc)) from None
    finally:client.close()
    with httpx.Client(timeout=8,trust_env=False) as h:
        if h.get(c['gateway_url']+'/health').status_code!=200:raise ConfigurationError('memory_gateway_unavailable')
    from dialogue.budget import Budget
    Budget().status()  # no reserve/settle, never changes ledger or its ceiling

def bank_app(c):
    from api.app import app
    from deploy.server.boundary import DeploymentBoundary
    from fastapi.responses import JSONResponse
    from build_info import LOADED_BUILD
    from security.totp import TOTPService
    from cryptography.fernet import Fernet
    Fernet(Path(c['files']['totp_key']).read_bytes().strip())
    Fernet(Path(c['files']['spool_key']).read_bytes().strip())
    app.add_middleware(DeploymentBoundary,origin=c['bank_origin'],proxies=c['trusted_proxy_addresses'])
    @app.get('/health/live',include_in_schema=False)
    def live():return {'status':'live'}
    @app.get('/health/deployment',include_in_schema=False)
    def version():return {'deployment_id':c['deployment_id'],'build':LOADED_BUILD['build'],'ui_sha256':LOADED_BUILD['ui_sha256']}
    @app.get('/health/ready',include_in_schema=False)
    def ready():
        try:database_identity(c);dependencies(c);return {'status':'ready','deployment_id':c['deployment_id']}
        except Exception:return JSONResponse({'status':'unavailable'},status_code=503)
    return app

def start(c,component):
    models=install(c) if component=='bank' else {}
    if component=='memory':
        import importlib.metadata
        if importlib.metadata.version('hindsight-api-slim')!='1.1.0':raise ConfigurationError('hindsight_version_mismatch')
        mem=environment_file(c['environment'])
        import psycopg
        with psycopg.connect(mem['HINDSIGHT_API_DATABASE_URL'],connect_timeout=5) as db:
            row=db.execute('SELECT (SELECT oid FROM pg_database WHERE datname=current_database()),rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            expected=read_json(c['restore_identity'])
            if row[0]!=expected['memory_database']['oid'] or any(row[1:]):raise ConfigurationError('memory_runtime_identity_invalid')
        for k in list(os.environ):
            if k.startswith(('BA','LLM_','VISION_','R4_','PG')):os.environ.pop(k,None)
        os.environ.update({k:v for k,v in mem.items() if k.startswith(('HINDSIGHT','HF_'))})
        os.environ.update(HINDSIGHT_API_HOST='0.0.0.0',HINDSIGHT_API_PORT='18889')
        os.chdir(c['state_dir']);sys.argv=[sys.argv[0]]
        from hindsight_api.main import main
        return main()
    import uvicorn
    if component=='bank':
        database_identity(c);dependencies(c);app=bank_app(c);port=c.get('port',8039)
    elif component=='sms':
        os.environ.update(BA_MOBILE_STATE_DIR=c['mobile_state_dir'],BA_DEPLOYMENT_MODE='demo',BA_SMS_PROVIDER='mock')
        import demo_sms_receiver as receiver
        receiver.SERVER_MODE=True;receiver.SERVER_ORIGIN=c['sms_origin']
        from deploy.server.boundary import DeploymentBoundary
        receiver.app.add_middleware(DeploymentBoundary,origin=c['sms_origin'],proxies=c['trusted_proxy_addresses'],receiver=True)
        app=receiver.app;port=8040
    else:
        models=environment_file(c['environment'])
        os.environ.update({k:v for k,v in models.items() if k.startswith('R4_')})
        os.environ.update(BA_LLM_BUDGET_CONFIG=c['budget_config'],BA_LLM_DISABLED='0')
        os.environ.pop('R4_PHASE_CONFIG',None)
        sys.path.insert(0,str(ROOT/'deploy/r4'))
        from model_gateway import app
        port=18761
    uvicorn.run(app,host='0.0.0.0',port=port,workers=1,proxy_headers=False,access_log=False,log_level='warning')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--component',choices=['bank','sms','gateway','memory'],default='bank');p.add_argument('--check-config',action='store_true');p.add_argument('--check-ready',action='store_true');a=p.parse_args()
    try:
        c=load(a.config) if a.component=='bank' else load_service(a.config,a.component)
        if a.check_config:print('server_configuration_valid');return 0
        if a.check_ready:install(c);database_identity(c);dependencies(c);print('server_dependencies_ready');return 0
        start(c,a.component);return 0
    except Exception as e:
        # Driver/provider errors may contain secrets; never interpolate them.
        code=str(e) if isinstance(e,ConfigurationError) else type(e).__name__
        print('server_start_refused:'+code,file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
