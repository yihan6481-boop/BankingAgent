"""Run the built local UI and backend together; no Node needed at runtime."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import socket

ROOT = Path(__file__).resolve().parent.parent


def load_environment():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                if key and key.replace('_', '').isalnum():
                    os.environ.setdefault(key, value.strip().strip('\"\''))
    os.environ.setdefault('BA091_AUTH_MODE', 'secure')
    os.environ.setdefault('BA091_LLM_ACCESS', 'disabled')
    os.environ.setdefault('BA_LLM_BUDGET_CONFIG',str(ROOT/'deploy/llm_runtime_budget.json'))
    # Explicit deployment marker is created only after verified migration.
    # No service is started and no DB is migrated during bank startup.
    marker=Path.home()/'.banking-agent/r4_trial_01/backend.json'
    try:
        import json
        cfg=json.loads(marker.read_text('utf8'))
        if cfg.get('enabled') is True:
            os.environ.setdefault('BA_R4_ENABLED','1')
            os.environ.setdefault('BA_R4_SERVICE_CONFIG',str(marker.parent/'client.json'))
            os.environ.setdefault('BA_R4_RECOVERY_FENCE',str(marker.parent/'recovery_fence.json'))
    except (OSError,ValueError):pass


def ensure_external_key():
    """First-run local key only; never replace an existing/configured key."""
    if os.environ.get('BA091_AUTH_MODE') != 'secure':
        return
    from cryptography.fernet import Fernet
    configured = os.environ.get('BA091_TOTP_MASTER_KEY_FILE', '').strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_relative_to(ROOT):
            raise ValueError('TOTP key must be outside the project. Set BA091_TOTP_MASTER_KEY_FILE.')
        Fernet(path.read_bytes().strip())
        return
    base = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.local' / 'share'))
    directory = (base / 'BankingAgent' / 'local-security').resolve()
    if directory.is_relative_to(ROOT):
        raise ValueError('Default key directory must be outside the project.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'totp-master.key'
    if not path.exists():
        # Exclusive creation avoids replacing the key during concurrent starts.
        try:
            with path.open('xb') as stream:
                if os.name != 'nt':
                    os.chmod(path, 0o600)
                stream.write(Fernet.generate_key())
        except FileExistsError:
            pass
    Fernet(path.read_bytes().strip())
    os.environ['BA091_TOTP_MASTER_KEY_FILE'] = str(path)


def configure_postgres():
    """Reuse external local credentials and key; startup never runs DDL/seed."""
    sys.path.insert(0,str(ROOT/'backend'))
    from final_pg import local_config
    from storage.pg02 import readiness
    os.environ['BA_STORAGE_BACKEND']='postgres'
    if not os.environ.get('BA_PG_DSN'):
        os.environ['BA_PG_DSN']=local_config().dsn
    from psycopg.conninfo import conninfo_to_dict
    target=conninfo_to_dict(os.environ['BA_PG_DSN'])
    # The formal launcher is pinned to the local PostgreSQL 17 service on
    # 127.0.0.1:5432; it never silently falls back to SQLite or the old Docker PG.
    if str(target.get('host','')) not in {'127.0.0.1','localhost','::1'} or str(target.get('port','5432'))!='5432':
        raise ValueError('unexpected_database_target')
    if not os.environ.get('BA091_TOTP_MASTER_KEY_FILE'):
        existing=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'BankingAgent091Secrets/totp_master.key'
        if not existing.is_file(): raise ValueError('existing_totp_master_key_missing')
        os.environ['BA091_TOTP_MASTER_KEY_FILE']=str(existing)
    value=readiness()
    if not value['application_ready']: raise ValueError('application_schema_not_ready')
    if not value.get('r2_dialogue_ready'): raise ValueError('r2_dialogue_migration_required')
    from storage.postgres import connect
    from storage.config import PostgresConfig
    with connect(PostgresConfig.from_env()) as c:
        row=c.execute("SELECT current_database() AS database,current_user AS role,"
            "inet_server_addr()::text AS address,inet_server_port() AS port,"
            "current_setting('server_version') AS version,"
            "(SELECT max(version) FROM ops.schema_migrations) AS schema_version").fetchone()
        if row['schema_version']<17:raise ValueError('daily_message_quota_migration_required')
    print('Storage: PostgreSQL '+(row['version'].split()[0])+' local / '+row['address']+':'+str(row['port'])
          +' / database='+row['database']+' / role='+row['role']
          +' / schema_version='+str(row['schema_version'])+' / application_ready=true')
    return value


def configure_memory(*,start=False):
    """Explicit status/degradation; memory service failure never resets bank data."""
    if os.environ.get('BA_R4_ENABLED')!='1':
        print('R4: enabled=false / memory=disabled / reason=deployment_not_enabled')
        return
    import json
    import subprocess
    import httpx
    try:
        from storage.config import PostgresConfig
        from storage.postgres import connect
        with connect(PostgresConfig.from_env()) as c:
            row=c.execute('SELECT current_database() db,(SELECT oid FROM pg_database WHERE datname=current_database()) oid,'
                          '(SELECT max(version) FROM ops.schema_migrations) schema').fetchone()
        if row['schema']<16:raise ValueError('r4_schema_0016_required')
        marker=Path.home()/'.banking-agent/r4_trial_01/backend.json'
        if row['db']=='banking_demo':
            deployment=json.loads(marker.read_text('utf8'))
            if deployment.get('database_oid')!=row['oid']:raise ValueError('restored_database_requires_memory_recovery_review')
            fence=Path(os.environ.get('BA_R4_RECOVERY_FENCE',''))
            if not fence.is_file():raise ValueError('memory_recovery_fence_missing')
        from r4_native_runtime import ensure
        native=ensure(start=start)
        cfg=json.loads(Path(os.environ['BA_R4_SERVICE_CONFIG']).read_text('utf8'))
        response=httpx.get(cfg['url']+'/v1/default/banks',headers={'Authorization':'Bearer '+cfg['token']},timeout=5,trust_env=False)
        if response.status_code!=200:raise ValueError('memory_service_authentication_unavailable')
        gateway=httpx.get('http://127.0.0.1:18761/health',timeout=3,trust_env=False).json()
        print('R4: enabled=true / schema_version='+str(row['schema'])+' / memory=healthy / memory_pg='+native['database']+' / gateway='+str(gateway.get('budget_profile'))+' / recovery_fence=required / business_pg=Windows-local / hindsight=Windows-native')
    except Exception as error:
        allowed={'r4_schema_0016_required','restored_database_requires_memory_recovery_review','memory_recovery_fence_missing','memory_services_start_failed','memory_service_authentication_unavailable'}
        import re
        reason=str(error) if str(error) in allowed or re.fullmatch(r'native_[a-z0-9_]+',str(error)) else type(error).__name__
        # Startup without a verified native target blocks the memory worker;
        # banking remains available. A subsequent healthy restart can reconcile.
        os.environ['BA_R4_ENABLED']='0'
        print('R4: enabled='+str(os.environ.get('BA_R4_ENABLED')=='1').lower()+' / memory=unavailable / reason='+reason+' / bank_available=true')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--backend', choices=['postgres','pg02'],default=os.environ.get('BA_STORAGE_BACKEND','postgres'))
    parser.add_argument('--check',action='store_true',help='Readiness only; never run admin DDL or seed')
    args = parser.parse_args()
    for module in ('fastapi', 'uvicorn', 'httpx', 'psycopg', 'pyotp', 'cryptography'):
        if importlib.util.find_spec(module) is None:
            print('Missing dependency. Run: python -m pip install -r requirements.txt')
            return 2
    if not (ROOT / 'frontend' / 'dist' / 'index.html').is_file():
        print('Frontend build missing. Run in frontend: npm ci then npm run build')
        return 2
    load_environment()
    sys.path.insert(0,str(ROOT/'backend'))
    from build_info import current_build
    build=current_build()
    if not args.check:
        with socket.socket() as probe:
            occupied=probe.connect_ex((args.host,args.port))==0
        if occupied:
            import httpx
            try:running=httpx.get(f'http://{args.host}:{args.port}/internal/build-info',timeout=2).json()
            except Exception:running={}
            if running.get('project_id')==build['project_id']:
                if running.get('build')==build['build']:
                    try:
                        status=httpx.get(f'http://{args.host}:{args.port}/internal/storage-status',timeout=5).json()
                        if args.backend in {'postgres','pg02'} and not status.get('application_ready'):
                            print('Startup stopped: running_storage_not_ready. 已有进程数据库未就绪，请检查原PG容器和凭据；不要init/reset。')
                            return 2
                    except Exception:
                        print('Startup stopped: running_readiness_unavailable. 已有服务就绪检查失败，未宣称启动成功。')
                        return 2
                    try:
                        configure_postgres()
                        ensure_external_key()
                        configure_memory(start=True)
                    except Exception:
                        print('Startup stopped: running_native_readiness_unavailable')
                        return 2
                    print(f'Already running: http://{args.host}:{args.port}/ui/ / build='+build['build'][:16])
                    print('同项目相同版本已运行，复用以上地址。')
                    return 0
                print('Startup stopped: project_version_stale. 同项目旧版仍在运行，请先停止原启动窗口再启动；未终止进程。')
            else:print(f'Startup stopped: port_in_use. {args.host}:{args.port} 被其他或无法识别的进程占用，未终止它；请检查该端口进程。')
            return 2
    os.environ['BA_STORAGE_BACKEND']=args.backend
    try:
        from dialogue.budget import Budget
        budget=Budget()
        configure_postgres()
        ensure_external_key()
    except Exception as error:
        from storage.errors import StorageError
        safe_code = error.code.value if isinstance(error,StorageError) else str(error) if str(error) in {'existing_pg_passfile_missing','existing_totp_master_key_missing','application_schema_not_ready','unexpected_database_target','r2_dialogue_migration_required'} else type(error).__name__
        print('Startup stopped: '+safe_code+'. 请检查本机 PostgreSQL 17 服务（127.0.0.1:5432）、外部 passfile（~/.banking-agent/pg17/pgpass.conf）及 readiness；未回退 SQLite 或旧 Docker PG。首次在本机建立角色/库时运行 tools/pg17_local_bootstrap.ps1，现有库不要 init/reset/seed。')
        return 2
    if budget.profile=='runtime' and budget.max_requests is None and budget.max_tokens is None:
        print('Model budget: 每账号每日200条用户消息 / Asia/Shanghai 北京时间00:00重置 / 付费请求与token预算未设上限（显式配置） / 单次资源限制保留 / ledger='+str(budget.path))
    elif budget.profile=='runtime':
        print('Model budget: 每日消息额度层=每账号200条/Asia/Shanghai；付费调用预算层=requests='+str(budget.max_requests)
              +' / tokens='+str(budget.max_tokens)+'（共享累计上限，不是每日/金额预算，也不构成付费授权；'
              '先预留后调用，耗尽拒绝；不重置旧账本）/ ledger='+str(budget.path))
    else:
        print('Model budget: profile='+budget.profile+' / requests='+str(budget.max_requests)+' / tokens='+str(budget.max_tokens)+' / ledger='+str(budget.path))
    configure_memory(start=not args.check)
    if args.check: return 0
    os.chdir(ROOT / 'backend')
    sys.path.insert(0, str(ROOT / 'backend'))
    print('Banking Agent backend + UI')
    print('Build: '+build['build'][:16])
    print(f'Open: http://{args.host}:{args.port}/ui/')
    print(f'API docs: http://{args.host}:{args.port}/docs')
    print('Authentication: ' + os.environ['BA091_AUTH_MODE'])
    print('LLM access: ' + os.environ['BA091_LLM_ACCESS'])
    print('Ctrl+C to stop. No business task is resumed automatically.')
    import uvicorn
    uvicorn.run('api.app:app', host=args.host, port=args.port, log_level='warning', access_log=False,
                proxy_headers=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
