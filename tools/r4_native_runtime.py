"""Native-only service supervisor. No Docker, DDL, SQLite or port takeover."""
from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=Path.home()/'.banking-agent/r4_native_01'


def load_deployment():
    value=json.loads((PRIVATE/'deployment.json').read_text('utf8'))
    backend='windows-native' if os.name=='nt' else 'linux-native'
    if value.get('backend')!=backend or value.get('enabled') is not True or value.get('database')!='r4_memory_native' or value.get('api_port')!=18889:
        raise ValueError('native_memory_deployment_not_ready')
    return value


def listener(port):
    with socket.socket() as s:return s.connect_ex(('127.0.0.1',port))==0


def owner(port,script):
    # Only public process metadata is returned. Credentials never appear in
    # command lines; they are read from the protected external env file.
    if os.name=='nt':
        command="$c=Get-NetTCPConnection -State Listen -LocalPort "+str(port)+" -ErrorAction Stop | Select-Object -First 1; $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$c.OwningProcess); @{pid=$p.ProcessId;command=$p.CommandLine;path=$p.ExecutablePath}|ConvertTo-Json -Compress"
        r=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,encoding='utf8',errors='replace')
        if r.returncode:raise ValueError('native_process_owner_unavailable')
        value=json.loads(r.stdout)
        expected=str((ROOT/'tools'/script).resolve()).replace('\\','/').lower()
        matches=expected in value['command'].replace('\\','/').lower()
    else:
        inodes=set()
        for name in ['tcp','tcp6']:
            table=Path('/proc/net')/name
            if not table.is_file():continue
            for line in table.read_text().splitlines()[1:]:
                fields=line.split()
                if fields[3]=='0A' and int(fields[1].split(':')[1],16)==port:inodes.add(fields[9])
        value=None
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():continue
            try:
                if any(fd.readlink().as_posix() in {'socket:['+i+']' for i in inodes} for fd in (process/'fd').iterdir()):
                    value=dict(pid=int(process.name),command=(process/'cmdline').read_bytes().replace(b'\0',b' ').decode('utf8'));break
            except (FileNotFoundError,PermissionError):continue
        if value is None:raise ValueError('native_process_owner_unavailable')
        matches=str((ROOT/'tools'/script).resolve()) in value['command']
    if not matches:raise ValueError('native_port_owned_by_unexpected_process')
    return value['pid']


@contextmanager
def locked():
    path=PRIVATE/'startup.lock'
    with path.open('a+b') as stream:
        stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
        if os.name=='nt':import msvcrt
        else:import fcntl
        deadline=time.monotonic()+90
        while True:
            try:
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>deadline:raise ValueError('native_start_lock_timeout')
                time.sleep(.1)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def spawn(script,python,args):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=PRIVATE/(Path(script).stem+'_'+stamp+'.log')
    env={k:v for k,v in os.environ.items() if not k.startswith(('BA_','BA091_','LLM_','R4_'))}
    env['NO_PROXY']='127.0.0.1,localhost,::1'
    with path.open('w',encoding='utf8') as log:
        proc=subprocess.Popen([str(python),'-u','-X','utf8',str(ROOT/'tools'/script),*args],cwd=PRIVATE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return proc


def ensure(*,start):
    deployment=load_deployment()
    from psycopg.conninfo import conninfo_to_dict
    from storage.config import PostgresConfig
    from storage.postgres import connect
    environment=dict(line.split('=',1) for line in Path(deployment['environment']).read_text('utf8').splitlines() if '=' in line)
    target=conninfo_to_dict(environment['HINDSIGHT_API_DATABASE_URL'])
    if target.get('host')!='127.0.0.1' or target.get('port')!='5432' or target.get('dbname')!='r4_memory_native' or target.get('user')!='r4_hindsight_native':raise ValueError('native_database_target_invalid')
    with connect(PostgresConfig(environment['HINDSIGHT_API_DATABASE_URL'])) as c:
        db=c.execute("SELECT current_database() db,current_user role,(SELECT oid FROM pg_database WHERE datname=current_database()) oid,current_setting('server_version') version").fetchone()
        if db['oid']!=deployment['database_oid']:raise ValueError('native_restored_database_requires_review')
    gateway_script='r4_model_gateway.py'
    gateway_args=['--phase','runtime']
    cfg=json.loads(Path(deployment['client']).read_text('utf8'))
    if cfg.get('url')!='http://127.0.0.1:18889':raise ValueError('native_client_target_invalid')
    with locked():
        for port,script,python,args in [(18761,gateway_script,sys.executable,gateway_args),
                (18889,'r4_native_service.py',deployment['python'],['--environment',deployment['environment']])]:
            proc=None
            if not listener(port):
                if not start:raise ValueError('native_services_not_running')
                proc=spawn(script,python,args)
                deadline=time.monotonic()+65
                while not listener(port):
                    if proc.poll() is not None:raise ValueError('native_service_start_failed')
                    if time.monotonic()>deadline:raise ValueError('native_service_start_timeout')
                    time.sleep(.2)
            owner(port,script)
        with httpx.Client(trust_env=False,timeout=5) as client:
            health=client.get('http://127.0.0.1:18761/health').json()
            if health.get('budget_profile')!='runtime':
                raise ValueError('native_gateway_budget_profile_mismatch')
            if (any(k not in health for k in ['cumulative_requests','cumulative_tokens']) or
                    health['cumulative_requests'] is not None or health['cumulative_tokens'] is not None):
                raise ValueError('native_gateway_runtime_budget_stale')
            response=client.get(cfg['url']+'/v1/default/banks',headers={'Authorization':'Bearer '+cfg['token']})
            if response.status_code!=200:raise ValueError('native_service_authentication_unavailable')
            if client.get(cfg['url']+'/v1/default/banks').status_code!=401:raise ValueError('native_service_authentication_not_enforced')
        instance=json.loads((PRIVATE/'service_instance.json').read_text('utf8'))
        if instance['pid']!=owner(18889,'r4_native_service.py') or instance['database_oid']!=db['oid']:
            raise ValueError('native_service_database_identity_mismatch')
    return dict(database=db['db'],pg_version=db['version'],api_port=18889,gateway_profile=health['budget_profile'],database_oid=db['oid'],pid=instance['pid'])
