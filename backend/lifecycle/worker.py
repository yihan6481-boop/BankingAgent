"""One-shot due processing. Run periodically yourself; no implicit daemon."""
from __future__ import annotations

import argparse
import json
from uuid import uuid4

from execution.context import ExecutionContext
from database import initialize_database
from security.service import get_security_services
from .models import RevisionRequest
from .service import transition
from .store import TaskStore, TaskError, now


def process_due(*, clock=now):
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.pg02 import store as pg_store,principal_scope,VerifiedContext,_context
        if _context.get() is None:
            outcomes=[]
            with pg_store()._pool.connection() as c:
                owners=c.execute('SELECT * FROM pg02.due_owners(%s)',(clock().isoformat(),)).fetchall()
            for owner in owners:
                with principal_scope(VerifiedContext(owner['owner_uuid'],owner['principal_id'],owner['user_id'],owner['credential_id'],None)):
                    outcomes.extend(process_due(clock=clock))
            return outcomes
    store = TaskStore(clock)
    outcomes = []
    for task_id, revision in store.due():
        try:
            record = store.load(task_id)
            principal = get_security_services().identity.get_principal(record.principal_id)
            if principal is None or principal.user_id != record.owner_id:
                raise TaskError('invalid_storage')
            # Internal read-only processing identity; this is not a bearer credential.
            session = ExecutionContext(principal=principal, principal_id=principal.principal_id,
                user_id=principal.user_id, session_id='due_' + uuid4().hex, protocol='v2')
            updated = transition(session, task_id, RevisionRequest(revision=revision),
                                 'resume', clock=clock, due=True)
            outcomes.append({'task_id': task_id, 'status': updated.status})
        except TaskError as exc:
            outcomes.append({'task_id': task_id, 'error': exc.code})
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    import sys,os
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
    from run_integrated import load_environment,configure_postgres,ensure_external_key
    load_environment()
    os.environ['BA091_AUTH_MODE']='secure'
    configure_postgres();ensure_external_key()
    initialize_database()
    outcomes = process_due()
    print(json.dumps(outcomes, ensure_ascii=False))
    return int(any('error' in o and o['error'] not in {'task_busy','stale_revision','not_due'} for o in outcomes))


if __name__ == '__main__':
    raise SystemExit(main())
