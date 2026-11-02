"""External monotonic invalidation floor, never fact storage or a restore input.

Write before PG commit: a crash/rollback can conservatively block memory, but
cannot resurrect it. Losing or restoring this file is NOT an approved recovery
procedure. Bank operations remain available through existing degraded handling.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from lifecycle.store import TaskError


def initialize(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf8') as stream:
        json.dump({'format':1,'banks':{}},stream)
        stream.flush();os.fsync(stream.fileno())


@contextmanager
def ledger():
    configured=os.environ.get('BA_R4_RECOVERY_FENCE')
    if not configured:
        yield None
        return
    path=Path(configured).resolve()
    try:
        if not path.is_file():raise ValueError()
        with path.with_suffix('.lock').open('a+b') as lock:
            lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
            if os.name=='nt':
                import msvcrt
                deadline=time.monotonic()+5
                while True:
                    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1);break
                    except OSError:
                        if time.monotonic()>=deadline:raise
                        time.sleep(.02)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            try:
                data=json.loads(path.read_text('utf8'))
                if data.get('format')!=1 or not isinstance(data['banks'],dict):raise ValueError()
                before=json.dumps(data,sort_keys=True)
                yield data
                if json.dumps(data,sort_keys=True)!=before:
                    tmp=path.with_suffix('.new')
                    with tmp.open('w',encoding='utf8') as stream:
                        json.dump(data,stream,sort_keys=True)
                        stream.flush();os.fsync(stream.fileno())
                    os.replace(tmp,path)
            finally:
                lock.seek(0)
                if os.name=='nt':msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
    except (OSError,ValueError,KeyError,TypeError):
        raise TaskError('memory_recovery_fence_blocked',status=503) from None


def snapshot(c,space):
    return dict(config=space['config_version'],
                sources={r['id']:r['generation'] for r in c.execute('SELECT id,generation FROM recall.sources')},
                exclusions=sorted([r['origin_ref'],r['origin_version']] for r in c.execute('SELECT origin_ref,origin_version FROM recall.exclusions')))


def check(c,space,*,created=False):
    with ledger() as data:
        if data is None:return
        bank=space['bank_ref'];old=data['banks'].get(bank)
        now=snapshot(c,space)
        if old is None:
            if not created:raise TaskError('memory_recovery_fence_blocked',status=503)
            data['banks'][bank]=now
            return
        if (now['config']<old['config'] or any(now['sources'].get(k,0)<v for k,v in old['sources'].items())
                or any(e not in now['exclusions'] for e in old['exclusions'])):
            raise TaskError('memory_recovery_fence_blocked',status=503)


def advance(c):
    if not os.environ.get('BA_R4_RECOVERY_FENCE'):return
    space=c.execute('SELECT * FROM recall.spaces').fetchone()
    check(c,space)
    now=snapshot(c,space)
    with ledger() as data:
        old=data['banks'][space['bank_ref']]
        # Another worker can only raise the floor. Never lower an external floor.
        if now['config']<old['config'] or any(now['sources'].get(k,0)<v for k,v in old['sources'].items()):
            raise TaskError('memory_recovery_fence_blocked',status=503)
        data['banks'][space['bank_ref']]=now
