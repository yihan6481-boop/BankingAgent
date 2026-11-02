"""Durable, bounded admission for image-model requests.

The gate is deliberately independent of the chat runtime budget ledger:

* a reservation row is committed *before* any provider HTTP request;
* a failed, timed-out or crashed call is still counted against the owner's
  business-day budget (only its concurrency slot is reclaimed by lease expiry);
* capacity is enforced through a short per-owner advisory lock + row count, so
  it holds across processes and pool connections, not just inside one process;
* exhaustion returns a typed error and never falls through to the provider.

It never authorizes a business action: the existing preview, confirmation,
TOTP and ledger kernel remains the only payment authorization path.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo('Asia/Shanghai')


class VisionGateError(Exception):
    def __init__(self, code, message, status=503):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def provider_settings():
    enabled=os.getenv('BA_VISION_ENABLED')
    if enabled!='1' and (enabled=='0' or os.getenv('BA091_LLM_ACCESS','disabled')!='configured'):
        raise VisionGateError('vision_not_configured','识图服务未启用',503)
    values=tuple(os.getenv('VISION_'+key) or os.getenv('LLM_'+key,'') for key in ['API_KEY','BASE_URL','MODEL'])
    if not all(values) or not values[1].startswith('https://'):
        raise VisionGateError('vision_not_configured','缺少识图服务凭据、地址或图像模型配置',503)
    return values[0],values[1].rstrip('/'),values[2]


@contextmanager
def provider_usage(payload):
    """Optional existing shared runtime ledger; PG owner/day gate still applies."""
    from dialogue.budget import Budget,BudgetError
    meter={};budget=None;ref=None;error=None;start=time.monotonic()
    if os.getenv('BA_VISION_USE_RUNTIME_BUDGET')=='1':
        try:budget=Budget();ref=budget.reserve(payload)
        except BudgetError as exc:raise VisionGateError(str(exc),'本地付费调用额度不足，未发起识图请求；不是服务商余额判断',503) from None
    try:yield meter
    except Exception as exc:
        error=getattr(exc,'code',type(exc).__name__)
        if isinstance(error,int):error='provider_http_'+str(error)
        raise
    finally:
        if ref:budget.settle(ref,meter.get('response'),time.monotonic()-start,error)


def provider_rejection(status):
    return {401:('vision_provider_authentication_failed','识图服务商未接受现有凭据（HTTP 401）'),
            402:('vision_provider_balance_insufficient','识图服务商余额不足（HTTP 402），需要在服务商账户充值'),
            429:('vision_provider_rate_limited','识图服务商限流（HTTP 429），请稍后重试')}.get(status,
            ('vision_provider_rejected','识图服务商拒绝图像请求（HTTP %d）；请核对图像模型/接口配置'%status))


def capture_response(response):
    """Operator-enabled private diagnostics: content/usage, never key/image input."""
    folder=os.getenv('BA_VISION_DIAGNOSTIC_DIR')
    if not folder:return
    from pathlib import Path
    import json
    path=Path(folder).resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('vision_diagnostics_must_be_external')
    path.mkdir(parents=True,exist_ok=True)
    safe=dict(model=response.get('model'),usage=response.get('usage'),content=(response.get('choices') or [{}])[0].get('message',{}).get('content'))
    with (path/(uuid4().hex+'.json')).open('x',encoding='utf8') as stream:json.dump(safe,stream,ensure_ascii=False)


def _int_env(name, default, minimum, maximum):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def limits():
    return dict(
        daily_limit=_int_env('PAYMENT_VISION_DAILY_LIMIT', 200, 1, 100000),
        owner_concurrency=_int_env('PAYMENT_VISION_CONCURRENCY', 2, 1, 32),
        lease_seconds=_int_env('PAYMENT_VISION_LEASE_SECONDS', 180, 30, 3600),
        wait_seconds=_int_env('PAYMENT_VISION_WAIT_SECONDS', 3, 0, 30),
    )


def enabled():
    from storage.config import pg02_enabled
    return pg02_enabled()


def _has_verified_context():
    """A request-scoped owner context is mandatory for the durable gate.

    Direct provider construction outside a verified request (legacy unit tools
    and component probes) has no owner to charge; those callers keep the old
    explicit acceptance ledger and never reach the HTTP service path.
    """
    try:
        from storage.pg02 import current_context
        current_context()
        return True
    except Exception:
        return False


@contextmanager
def _gate_transaction():
    """Short owner-scoped transaction, independent from application UoWs."""
    from storage.pg02 import current_context, store
    context = current_context()
    with store()._pool.connection() as connection, connection.transaction():
        connection.execute("SELECT set_config('banking.owner_id',%s,true)", (str(context.owner_uuid),))
        yield connection, context


def _day(now=None):
    return (now or datetime.now(BUSINESS_TIMEZONE)).astimezone(BUSINESS_TIMEZONE).date()


def reserve(kind, reference, *, wait_seconds=None):
    """Reserve one vision call or raise a typed admission error.

    Returns a token to pass to :func:`finish` in a ``finally`` block. When the
    process backend is not PostgreSQL the legacy optional acceptance ledger
    (owned by ``billvision.provider``) still applies; there is no durable gate
    to reserve against, so this returns ``None``.
    """
    if not enabled() or not _has_verified_context():
        return None
    if kind not in {'bill_screenshot', 'payment_document'}:
        raise VisionGateError('vision_gate_invalid_kind', '识图调用类型不合法', 422)
    if not isinstance(reference, str) or not 1 <= len(reference) <= 128:
        raise VisionGateError('vision_gate_invalid_reference', '识图引用不合法', 422)
    config = limits()
    wait_seconds = config['wait_seconds'] if wait_seconds is None else max(0, min(int(wait_seconds), 30))
    day = _day()
    deadline = time.monotonic() + wait_seconds
    while True:
        with _gate_transaction() as (connection, context):
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                               ('vision-gate:' + str(context.owner_uuid),))
            # Reclaim capacity from crashed processes, but keep the spent budget.
            connection.execute("""UPDATE payments.vision_calls SET state='expired',finished_at=now()
                WHERE owner_uuid=%s AND state='reserved'
                AND reserved_at < now() - make_interval(secs => %s)""",
                (context.owner_uuid, config['lease_seconds']))
            spent = connection.execute("""SELECT count(*) AS n FROM payments.vision_calls
                WHERE owner_uuid=%s AND business_day=%s""", (context.owner_uuid, day)).fetchone()['n']
            if spent >= config['daily_limit']:
                raise VisionGateError('vision_budget_exhausted',
                    '今日识图真实调用预算已用完；未发起新的模型请求，可先核实已有单据/任务。', 503)
            owner_inflight = connection.execute("""SELECT count(*) AS n FROM payments.vision_calls
                WHERE owner_uuid=%s AND state='reserved'""", (context.owner_uuid,)).fetchone()['n']
            if owner_inflight < config['owner_concurrency']:
                call_id = 'vc_' + uuid4().hex
                connection.execute("""INSERT INTO payments.vision_calls
                    (call_id,kind,reference,business_day) VALUES(%s,%s,%s,%s)""",
                    (call_id, kind, reference, day))
                return dict(call_id=call_id, kind=kind, reference=reference, day=day.isoformat())
        if time.monotonic() >= deadline:
            raise VisionGateError('vision_capacity_exhausted',
                '识图并发已满，等待超时；未发起新的模型请求，请稍后重试。', 503)
        time.sleep(0.1)


def finish(token, *, succeeded, error_code=None):
    """Close a reservation. Absence of a token (non-PG backend) is a no-op."""
    if token is None or not enabled() or not _has_verified_context():
        return
    with _gate_transaction() as (connection, context):
        connection.execute("""UPDATE payments.vision_calls SET state=%s,finished_at=now(),error_code=%s
            WHERE owner_uuid=%s AND call_id=%s AND state='reserved'""",
            ('succeeded' if succeeded else 'failed', error_code, context.owner_uuid, token['call_id']))


def snapshot(principal=None):
    """Read-only diagnostics for the current owner; never starts a call."""
    if not enabled():
        return dict(enabled=False, limits=limits())
    if not _has_verified_context():
        return dict(enabled=False, limits=limits(), reason='no_verified_owner_context')
    from storage.pg02 import current_context, read_snapshot
    current_context()
    day = _day()
    with read_snapshot() as connection:
        rows = connection.execute("""SELECT state,count(*) AS n FROM payments.vision_calls
            WHERE business_day=%s GROUP BY state""", (day,)).fetchall()
    return dict(enabled=True, business_day=day.isoformat(), limits=limits(),
                calls={row['state']: row['n'] for row in rows})
