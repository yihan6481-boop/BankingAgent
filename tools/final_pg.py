"""Explicit local Demo management. Normal launcher never calls init/seed."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT/'tools'))

def local_config(database='banking_demo',role='runtime'):
    """Local PostgreSQL 17 service on 127.0.0.1:5432 (not the retired Docker PG).

    Credentials live only in the external passfile, never in the project or ZIP.
    """
    from psycopg.conninfo import make_conninfo
    from storage.config import PostgresConfig
    directory=Path.home()/'.banking-agent/pg17'
    # Reuse established passfile; no generation/rewrite during startup.
    passfile=directory/'pgpass.conf'
    if not passfile.is_file(): raise RuntimeError('existing_pg_passfile_missing')
    return PostgresConfig(make_conninfo(host='127.0.0.1',port=5432,dbname=database,
        user={'admin':'postgres','migration':'banking_migrator','runtime':'banking_runtime'}[role],
        passfile=str(passfile)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','seed','validate','ready','reset-owned-initial'])
    parser.add_argument('--confirm-database')
    args=parser.parse_args()
    from final_pg_demo import manage
    if args.command=='reset-owned-initial':
        if args.confirm_database!='banking_demo':parser.error('explicit --confirm-database banking_demo required')
        from final_pg_demo import reset_owned_initial
        reset_owned_initial()
        return manage('init')
    return manage(args.command)

if __name__=='__main__': raise SystemExit(main())
