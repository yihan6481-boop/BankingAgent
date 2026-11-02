"""One bounded memory worker, using R3's owner-discovery pattern and PG lock.

No bank tools, bearer credentials, or per-job threads. A new process can recover
the exact persisted source/native operation identities after a crash.
"""
import logging
import threading
from storage.pg02 import store as pg_store,VerifiedContext,principal_scope
from security.service import get_security_services
from .service import Service,configured

LOCK=1346842734


def process_once():
    if not configured():return 0
    processed=0
    with pg_store()._pool.connection() as c:
        if not c.execute('SELECT pg_try_advisory_lock(%s) AS acquired',(LOCK,)).fetchone()['acquired']:return 0
        try:
            owners=c.execute('SELECT * FROM recall.due_owners()').fetchall()
            for owner in owners:
                with principal_scope(VerifiedContext(owner['owner_uuid'],owner['principal_id'],owner['user_id'],'r4-internal-worker',None)):
                    p=get_security_services().identity.get_principal(owner['principal_id'])
                    if p is None or p.user_id!=owner['user_id']:continue
                    service=Service()
                    try:processed+=bool(service.advance(p))
                    finally:service.close()
        finally:c.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
    return processed


class Worker:
    def __init__(self):self.stop_event=threading.Event();self.thread=None
    def start(self):
        if not configured():return
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        def run():
            while not self.stop_event.is_set():
                try:process_once()
                except Exception as e:
                    # Recovery errors never include task bodies or SQL/DSNs.
                    logging.getLogger('r4.worker').warning('memory_worker_unavailable:%s',type(e).__name__)
                self.stop_event.wait(2)
        self.thread=threading.Thread(target=run,name='r4-memory-worker',daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=1)


worker=Worker()
