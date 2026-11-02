"""Host-side Compose maintenance. No cloud setup, provisioning or seed command."""
from pathlib import Path
import argparse,subprocess,json,hashlib,re,os,shutil
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[2]
RETIRE_AUTH_SQL="BEGIN; UPDATE pg02.auth_sessions SET revoked=1 WHERE revoked=0; UPDATE assistance.seat_sessions SET online=false,expires_at=now(); UPDATE pg02.security_challenges SET status='expired' WHERE status IN ('pending','verified'); UPDATE mobile_security.bootstrap SET used_at=now() WHERE used_at IS NULL; COMMIT;"
def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
class Operations:
    def __init__(self,env):self.cmd=['docker','compose','--env-file',str(Path(env).resolve()),'-f',str(ROOT/'deploy/server/compose.yaml')]
    def run(self,args,*,data=None,stream=None):
        r=subprocess.run(self.cmd+args,input=data,stdin=stream,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if r.returncode:raise RuntimeError('compose_operation_failed_no_secret_details')
        return r.stdout
    def sql(self,db,q):
        raw=self.run(['exec','-T','database','psql','-X','-U','postgres','-d',db,'-qAt','-v','ON_ERROR_STOP=1'],data=("SET TIME ZONE 'Asia/Shanghai';\n"+q).encode())
        return raw.decode().strip()
    def rows(self,db,q):return [json.loads(x) for x in self.sql(db,'SELECT row_to_json(x) FROM ('+q+') x;').splitlines() if x]
    def stopped(self):
        active=set(self.run(['ps','--status','running','--services']).decode().split())
        if active & {'bank','memory','gateway','sms'}:raise RuntimeError('stop_all_application_writers_before_maintenance')
    def verify(self,db,path=None):
        expected=json.loads(path.read_text('utf8')) if path else None
        tables=self.rows(db,"SELECT n.nspname schema,c.relname name,pg_get_userbyid(c.relowner) owner,c.relrowsecurity rls,c.relforcerowsecurity force,c.relacl::text acl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname NOT IN ('information_schema') AND n.nspname !~ '^pg_' ORDER BY 1,2")
        def identifier(v):return '"'+v.replace('"','""')+'"'
        for t in tables:
            hashes=self.sql(db,'SELECT md5(to_jsonb(t)::text) FROM '+identifier(t['schema'])+'.'+identifier(t['name'])+' t ORDER BY 1;').splitlines()
            t.update(rows=len(hashes),digest=hashlib.sha256(''.join(hashes).encode()).hexdigest())
        if expected and tables!=expected['tables']:raise RuntimeError('restored_table_or_ownership_rls_acl_mismatch')
        # The restored archive retains owners, policies, FK/check/unique constraints;
        # compare their actual definitions too, not just command success.
        queries={
            'constraints':"SELECT n.nspname schema,c.relname table_name,x.conname,x.contype,x.convalidated,pg_get_constraintdef(x.oid) definition FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' ORDER BY 1,2,3",
            'policies':"SELECT schemaname,tablename,policyname,permissive,roles::text,cmd,qual,with_check FROM pg_policies ORDER BY 1,2,3",
            'indexes':"SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes WHERE schemaname NOT LIKE 'pg_%' ORDER BY 1,2,3",
            'schemas':"SELECT nspname,pg_get_userbyid(nspowner) owner,nspacl::text acl FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema' ORDER BY 1",
            'extensions':"SELECT extname,extversion FROM pg_extension ORDER BY extname",
        }
        queries['sequences']="SELECT schemaname,sequencename,sequenceowner,data_type,start_value,min_value,max_value,increment_by,cycle,cache_size,last_value FROM pg_sequences WHERE schemaname !~ '^pg_' ORDER BY 1,2"
        queries['functions']="SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) args,pg_get_userbyid(p.proowner) owner,p.proacl::text acl,p.prosecdef,p.proconfig::text,md5(p.prosrc) body_hash FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' ORDER BY 1,2,3"
        actual={'tables':tables}
        for key,q in queries.items():
            q=q.replace("NOT LIKE 'pg_%'","!~ '^pg_'")
            actual[key]=self.rows(db,q)
            if expected and actual[key]!=expected[key]:raise RuntimeError('restored_'+key+'_mismatch')
        return len(tables) if expected else actual

def outside(path):
    p=Path(path).expanduser().resolve()
    if p.is_relative_to(ROOT):raise RuntimeError('maintenance_material_must_be_outside_source')
    return p
def scoped(name,prefix):
    if not re.fullmatch(prefix+'[a-z0-9_]+',name):raise RuntimeError('server_database_name_required')
    return name
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--env-file',required=True)
    sub=p.add_subparsers(dest='action',required=True)
    restore=sub.add_parser('restore');restore.add_argument('--private-backup',required=True);restore.add_argument('--private-root',required=True);restore.add_argument('--bank-name',required=True);restore.add_argument('--memory-name',required=True);restore.add_argument('--deployment-id',required=True);restore.add_argument('--confirm-empty-cluster',action='store_true',required=True);restore.add_argument('--retire-transient-auth',action='store_true',required=True)
    backup=sub.add_parser('backup');backup.add_argument('--output',required=True);backup.add_argument('--bank-name',required=True);backup.add_argument('--memory-name',required=True);backup.add_argument('--private-root',required=True);backup.add_argument('--data-root',required=True)
    a=p.parse_args();ops=Operations(a.env_file);ops.stopped()
    bank=scoped(a.bank_name,'banking_server_');memory=scoped(a.memory_name,'r4_memory_server_')
    if a.action=='restore':
        source=outside(a.private_backup);private=outside(a.private_root)
        if int(ops.sql('postgres',"SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres','template0','template1');"))!=0:raise RuntimeError('restore_requires_empty_dedicated_cluster')
        if int(ops.sql('postgres',"SELECT count(*) FROM pg_roles WHERE rolname IN ('banking_runtime','banking_migrator','r4_hindsight_native');"))!=0:raise RuntimeError('restore_will_not_replace_existing_roles')
        manifest=json.loads((source/'SHA256_MANIFEST.private.json').read_text('utf8'))
        for name in ['business.dump','memory.dump','roles.private.sql','business-snapshot-oracle.private.json','memory-snapshot-oracle.private.json']:
            item=next(x for x in manifest if x['path']==name)
            if digest(source/name)!=item['sha256']:raise RuntimeError('private_backup_checksum_mismatch')
        ops.sql('postgres',(source/'roles.private.sql').read_text('utf8'))
        for name,owner,file in [(bank,'banking_migrator','business.dump'),(memory,'r4_hindsight_native','memory.dump')]:
            ops.sql('postgres',f'CREATE DATABASE "{name}" OWNER "{owner}" TEMPLATE template0 ENCODING \'UTF8\' LOCALE \'C.UTF-8\'; REVOKE ALL ON DATABASE "{name}" FROM PUBLIC;')
            if name==bank:ops.sql('postgres',f'GRANT CONNECT ON DATABASE "{name}" TO banking_runtime;')
            with (source/file).open('rb') as f:ops.run(['exec','-T','database','pg_restore','-U','postgres','--exit-on-error','--dbname='+name],stream=f)
        counts=[ops.verify(db,source/(label+'-snapshot-oracle.private.json')) for db,label in [(bank,'business'),(memory,'memory')]]
        # Only the newly named restored DB, after exact raw snapshot validation.
        # Long-lived account credentials, confirmed TOTP and payment factors stay.
        ops.sql(bank,RETIRE_AUTH_SQL)
        oid=lambda db:int(ops.sql(db,'SELECT oid FROM pg_database WHERE datname=current_database();'))
        identity={'deployment_id':a.deployment_id,'database':{'name':bank,'oid':oid(bank)},'memory_database':{'name':memory,'oid':oid(memory)},'schema_version':int(ops.sql(bank,'SELECT max(version) FROM ops.schema_migrations;')),'transient_auth_retired':True}
        for role in ['bank','memory']:
            dest=private/role/'restore-identity.json';dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():raise RuntimeError('restore_identity_already_exists')
            with dest.open('x',encoding='utf8') as f:json.dump(identity,f,indent=2)
            os.chmod(dest,0o600)
        print(json.dumps({'restore_verified':True,'tables':counts,'schema_version':identity['schema_version'],'writers_started':False}))
    else:
        output=outside(a.output);output.mkdir(mode=0o700,parents=True,exist_ok=False)
        start=datetime.now(timezone.utc).isoformat()
        for db,label in [(bank,'business'),(memory,'memory')]:
            (output/(label+'-snapshot-oracle.private.json')).write_text(json.dumps(ops.verify(db),indent=2),'utf8')
            data=ops.run(['exec','-T','database','pg_dump','-U','postgres','-Fc',db])
            (output/(label+'.dump')).write_bytes(data)
        # Quiesced writers make the sequential snapshots a cutover set; keep the
        # external monotonic fence and accumulated budget intact, never reset.
        shutil.copytree(outside(a.private_root),output/'private')
        data=outside(a.data_root)
        for name in ['shared','mobile']:shutil.copytree(data/name,output/name,ignore=shutil.ignore_patterns('*.sms','*.tmp','*.lock'))
        roles=ops.sql('postgres',"SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L;',rolname,rolpassword) FROM pg_authid WHERE rolname IN ('banking_runtime','banking_migrator','r4_hindsight_native') ORDER BY rolname;")
        (output/'roles.private.sql').write_text(roles+'\n','utf8')
        (output/'backup-consistency.private.json').write_text(json.dumps({'started_utc':start,'completed_utc':datetime.now(timezone.utc).isoformat(),'writers_stopped':True},indent=2),'utf8')
        manifest=[{'path':f.relative_to(output).as_posix(),'bytes':f.stat().st_size,'sha256':digest(f)} for f in output.rglob('*') if f.is_file()]
        (output/'SHA256_MANIFEST.private.json').write_text(json.dumps(manifest,indent=2),'utf8')
        print('private_backup_written; secrets omitted from output')
if __name__=='__main__':
    try:main()
    except Exception as e:
        print('maintenance_refused:'+str(e) if type(e) is RuntimeError else 'maintenance_refused:'+type(e).__name__,file=__import__('sys').stderr)
        raise SystemExit(2)
