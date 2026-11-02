"""Explicit restricted migration; never imported by normal server startup."""
import argparse,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'backend')]
def main():
    from deploy.server.settings import external_file,read_json
    from psycopg.conninfo import make_conninfo
    from storage.config import PostgresConfig
    from storage.migrations import migrate
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    c=read_json(external_file(a.config))
    import re
    if not re.fullmatch('banking_server_[a-z0-9_]+',c['database']):raise ValueError('server_database_required')
    config=PostgresConfig(make_conninfo(host=c['host'],port=c['port'],dbname=c['database'],user='banking_migrator',passfile=str(external_file(c['passfile']))))
    statuses=migrate(config,apply=a.apply)
    print(json.dumps({'migration_count':len(statuses),'applied':all(s.applied for s in statuses),'explicit_apply':a.apply}))
if __name__=='__main__':
    try:main()
    except Exception as e:print('migration_refused:'+type(e).__name__,file=sys.stderr);raise SystemExit(2)
