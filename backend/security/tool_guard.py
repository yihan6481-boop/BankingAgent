"""Synchronous infrastructure protection; no language or authorization decisions."""
from contextvars import ContextVar

from foundation.contracts import feedback
from storage.errors import CommitUnknown, StorageError, Code
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

_active = ContextVar('tool_protection_dependencies', default=frozenset())
_INFRA_CODES = frozenset({'storage_unavailable', 'database_error', 'storage_error',
                         'service_unavailable', 'payment_service_unavailable',
                         'connection_failed', 'connection_timeout'})


def dependency_for(tool_name):
    if tool_name.startswith(('memory_', 'recall_', 'profile_')):
        return 'recall_service'
    if tool_name.startswith('payment_'):
        return 'payment_storage'
    return 'bank_storage'


def breaker_operation(dependency, mode, error=None):
    from security.service import get_security_services
    from storage.pg02 import store
    services = get_security_services()
    cfg = services.config
    with store()._pool.connection() as connection:
        return connection.execute('SELECT pg02.tool_breaker(%s,%s,%s,%s,%s,%s,%s) AS value',
            (dependency, mode, services.restrictions._now(), cfg.breaker_failure_threshold,
             cfg.breaker_window_seconds, cfg.breaker_open_seconds, error)).fetchone()['value']


def outcome(result):
    if not isinstance(result, dict):
        return 'business_rejected', None
    code = str(result.get('code') or result.get('error_code') or '')
    if result.get('status') == 'commit_unknown' or code in {
            'commit_unknown', 'commit_outcome_unknown', 'preparation_recovery_required',
            'payment_preparation_recovery_required'}:
        return 'outcome_unknown', code
    if code in _INFRA_CODES:
        return 'infrastructure_failed', code
    if result.get('success') is True or result.get('status') in {'completed', 'read_ok'}:
        return 'completed', code
    return 'business_rejected', code


def guarded_tool_call(tool_name, operation, *, read_only):
    dependency = dependency_for(tool_name)
    if dependency in _active.get():
        return operation()
    state = breaker_operation(dependency, 'read' if read_only else 'write')
    if not state['allowed'] and not read_only and state['state']=='half_open' and dependency in {'bank_storage','payment_storage'}:
        # Claim an atomic READ probe, never run the business write as a probe.
        probe=breaker_operation(dependency,'read')
        if probe['allowed']:
            try:
                from storage.pg02 import store,current_context
                with store()._pool.connection() as c,c.transaction():
                    c.execute('SET TRANSACTION READ ONLY')
                    c.execute("SELECT set_config('banking.owner_id',%s,true)",(str(current_context().owner_uuid),))
                    c.execute('SELECT 1 FROM '+('pg02.cards' if dependency=='bank_storage' else 'payments.payment_documents')+' LIMIT 1').fetchone()
                breaker_operation(dependency,'success')
                state=breaker_operation(dependency,'write')
            except Exception:
                _record(dependency,'failure','recovery_probe_failed')
    if not state['allowed']:
        return feedback('conflict', 'tool_circuit_open',
                        data={'retry_after_seconds': state['retry_after_seconds']},
                        next_actions=['memory_list' if dependency=='recall_service' else 'cards_balance' if dependency=='bank_storage' else 'payment_document_list'])
    token = _active.set(_active.get() | {dependency})
    try:
        try:
            result = operation()
        except Exception as error:
            unknown = isinstance(error, CommitUnknown) or getattr(error, 'pg02_commit_unknown', False)
            code = str(getattr(error, 'code', ''))
            infra = isinstance(error, (ConnectionError, TimeoutError, PoolTimeout)) or (
                isinstance(error, OperationalError) and
                (error.sqlstate is None or str(error.sqlstate).startswith('08'))) or (
                isinstance(error, StorageError) and error.code == Code.UNAVAILABLE)
            # Unknown commit retains its exception/reference and is never retried.
            _record(dependency, 'failure' if infra and not unknown else 'neutral', code)
            raise
        category, code = outcome(result)
        _record(dependency, {'completed': 'success', 'infrastructure_failed': 'failure'}.get(category, 'neutral'), code)
        return result
    finally:
        _active.reset(token)


def _record(dependency, mode, code):
    try:
        breaker_operation(dependency, mode, code)
    except Exception:
        # Never obscure a completed write or its unknown-outcome reference
        # because a subsequent protection-state write failed. No automatic retry.
        import logging
        logging.getLogger(__name__).warning('tool_protection_record_unavailable')


def guarded_service_call(tool_name, operation, *, read_only):
    """Legacy service-result adapter; dialogue uses guarded_tool_call directly."""
    result = guarded_tool_call(tool_name, operation, read_only=read_only)
    if isinstance(result, dict) and result.get('code') == 'tool_circuit_open':
        return {'success': False, 'error_code': 'tool_circuit_open',
                'message': '依赖暂时不可用，请稍后核实原引用再继续', 'data': result['data']}
    return result
