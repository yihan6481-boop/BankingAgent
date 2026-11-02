"""SQLite snapshots, CAS and recoverable leases on the existing bank database."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from database import get_connection
from .models import DurableTask


@contextmanager
def db():
    connection = get_connection()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class TaskError(Exception):
    def __init__(self, code, message=None, status=409):
        self.code, self.message, self.status = code, message or code, status
        super().__init__(self.message)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


class TaskStore:
    def __init__(self, clock=None):
        self.clock = clock or now

    def _decode(self, connection, row, principal=None):
        if [r[0] for r in connection.execute('SELECT version FROM lifecycle_schema')] != [1]:
            raise TaskError('invalid_storage', '未知数据库任务格式版本')
        if row is None:
            raise TaskError('task_not_found', status=404)
        identity = connection.execute('SELECT * FROM lifecycle_identity WHERE task_id=?',
                                      (row['task_id'],)).fetchone()
        if identity is None or identity['owner_id'] != row['owner_id']:
            raise TaskError('invalid_storage')
        if principal and (identity['owner_id'] != principal.user_id or
                          identity['principal_id'] != principal.principal_id):
            raise TaskError('task_not_found', status=404)
        try:
            payload = json.loads(row['snapshot'])
            if digest(payload) != row['checksum']:
                raise ValueError('checksum')
            task = DurableTask.model_validate(payload)
            if (task.owner_id != identity['owner_id'] or
                task.principal_id != identity['principal_id'] or
                task.task.task_id != row['task_id'] or task.revision != row['revision'] or
                task.status != row['status'] or
                (task.scheduled_at.isoformat() if task.scheduled_at else None) != row['due_at']):
                raise ValueError('row/snapshot mismatch')
            from .service import validate_task
            validate_task(task)
            return task
        except (ValueError, TypeError, KeyError) as exc:
            raise TaskError('invalid_storage', '任务存储版本、结构或状态不合法') from exc

    def load(self, task_id, principal=None):
        with db() as connection:
            return self._decode(connection, connection.execute(
                'SELECT * FROM lifecycle_tasks WHERE task_id=?', (task_id,)).fetchone(), principal)

    def list(self, principal, *, limit=100):
        if not 1 <= limit <= 200: raise TaskError('invalid_limit')
        with db() as connection:
            rows = connection.execute('SELECT t.* FROM lifecycle_tasks t JOIN lifecycle_identity i '
                'ON i.task_id=t.task_id WHERE i.owner_id=? AND i.principal_id=? ORDER BY t.rowid DESC LIMIT ?',
                (principal.user_id, principal.principal_id,limit)).fetchall()
            return [self._decode(connection, row, principal) for row in rows]

    def save(self, task, *, token=None, create=False, rollback=None):
        task = DurableTask.model_validate(task.model_dump(mode='json'))
        from .service import validate_task
        validate_task(task)
        payload = task.model_dump(mode='json')
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if create:
                connection.execute('INSERT INTO lifecycle_identity VALUES(?,?,?)',
                    (task.task.task_id, task.owner_id, task.principal_id))
                connection.execute('INSERT INTO lifecycle_tasks '
                    '(task_id,owner_id,revision,status,due_at,snapshot,checksum) VALUES(?,?,?,?,?,?,?)',
                    (task.task.task_id, task.owner_id, task.revision, task.status,
                     task.scheduled_at.isoformat() if task.scheduled_at else None,
                     json.dumps(payload, ensure_ascii=False), digest(payload)))
            else:
                payload['revision'] += 1
                updated = connection.execute('UPDATE lifecycle_tasks SET revision=?,status=?,due_at=?, '
                    'snapshot=?,checksum=? WHERE task_id=? AND revision=? AND lease_token=? AND lease_until>?',
                    (payload['revision'], task.status,
                     task.scheduled_at.isoformat() if task.scheduled_at else None,
                     json.dumps(payload, ensure_ascii=False), digest(payload), task.task.task_id,
                     task.revision, token, self.clock().isoformat()))
                if updated.rowcount != 1:
                    raise TaskError('stale_revision', '任务已变化或执行租约已失效')
                task.revision += 1
            # Cross-process invalidation: persisted preview identities decide
            # whether old challenges are still usable, not in-memory cleanup.
            from .service import matches_preview
            from agent.schemas import TaskPreviewBinding
            for binding_row in connection.execute(
                    'SELECT * FROM authenticator_bindings WHERE task_id=?', (task.task.task_id,)).fetchall():
                binding = TaskPreviewBinding.model_validate_json(binding_row['binding_json'])
                if task.status not in {'active','waiting'} or not matches_preview(task,binding):
                    connection.execute("UPDATE security_challenges SET status='cancelled',cancelled_at=? "
                        "WHERE challenge_id=? AND status IN ('pending','verified')",
                        (self.clock().isoformat(),binding_row['challenge_id']))
                    connection.execute('UPDATE security_authorizations SET consumed=1 WHERE challenge_id=? AND consumed=0',
                                       (binding_row['challenge_id'],))
            from security.audit import AuditRecorder, AuditChain
            from types import SimpleNamespace
            from .service import active_scope
            scope = active_scope.get()
            principal = scope.session.principal if scope else SimpleNamespace(
                user_id=task.owner_id, principal_id=task.principal_id)
            if create or task.status in {'completed', 'cancelled'} or rollback:
                AuditRecorder().record(AuditChain(), 'plan_validation' if create else 'final_outcome', 'ok',
                    connection=connection, principal=principal,
                    session_id=scope.session.session_id if scope else None, task_id=task.task.task_id,
                    detail={'step_count': len(task.task.steps), 'task_status': task.status,
                            'revision': task.revision, **(rollback['detail'] if rollback else {})})
            if rollback:
                connection.execute('INSERT INTO lifecycle_rollbacks VALUES(?,?,?,?,?,?,?,?)',
                    (uuid4().hex,task.task.task_id,task.revision,rollback['target'],rollback['reason'],
                     json.dumps(rollback['affected']),json.dumps(rollback['history'],ensure_ascii=False),self.clock().isoformat()))
                connection.execute("UPDATE security_challenges SET status='cancelled',cancelled_at=? "
                    "WHERE status IN ('pending','verified') AND challenge_id IN "
                    '(SELECT challenge_id FROM authenticator_bindings WHERE task_id=?)',
                    (self.clock().isoformat(),task.task.task_id))
        return task

    def acquire(self, task_id, principal, revision, *, due=False):
        token = uuid4().hex
        with db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM lifecycle_tasks WHERE task_id=?', (task_id,)).fetchone()
            task = self._decode(connection, row, principal)
            if task.revision != revision:
                raise TaskError('stale_revision')
            current = self.clock()
            if due and (task.status not in {'scheduled','active'} or task.scheduled_at is None or task.scheduled_at > current):
                raise TaskError('not_due')
            updated = connection.execute('UPDATE lifecycle_tasks SET lease_token=?,lease_until=? '
                'WHERE task_id=? AND (lease_until IS NULL OR lease_until<=?)',
                (token, (current + timedelta(seconds=60)).isoformat(), task_id, current.isoformat()))
            if updated.rowcount != 1:
                raise TaskError('task_busy', '另一个请求正在处理该任务，请稍后重试')
        return task, token

    def release(self, task_id, token):
        with db() as connection:
            connection.execute('UPDATE lifecycle_tasks SET lease_token=NULL,lease_until=NULL '
                               'WHERE task_id=? AND lease_token=?', (task_id, token))

    def due(self):
        with db() as connection:
            return [(r['task_id'], r['revision']) for r in connection.execute(
                "SELECT task_id,revision FROM lifecycle_tasks WHERE status IN ('scheduled','active') AND due_at<=? "
                'AND (lease_until IS NULL OR lease_until<=?)',
                (self.clock().isoformat(), self.clock().isoformat()))]
