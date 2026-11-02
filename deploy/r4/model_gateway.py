"""Authenticated, metered provider exit for Hindsight. No request bodies in logs.

Run on the Windows host to share the existing Windows ledger lock with chat.
Only this process receives the provider credential; no bodies in diagnostics.
"""
import asyncio
import hmac
import os
import time
from pathlib import Path
import sys

locations=[Path(__file__).parent/'code/dialogue']
if len(Path(__file__).resolve().parents)>2:locations.append(Path(__file__).resolve().parents[2]/'backend/dialogue')
for location in locations:
    if (location/'budget.py').is_file():sys.path.insert(0,str(location))

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from budget import Budget, BudgetError

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
slots = asyncio.Semaphore(1)


@app.get('/health')
def health():
    budget=Budget()
    return {'status': 'ready', 'provider': 'deepseek', 'concurrency': 1,'budget_profile':budget.profile,'budget_batch':budget.batch_id,
        'cumulative_requests':budget.max_requests,'cumulative_tokens':budget.max_tokens}


@app.post('/v1/chat/completions')
async def complete(request: Request):
    expected = os.environ.get('R4_GATEWAY_TOKEN', '')
    auth = request.headers.get('authorization', '')
    if not expected or not hmac.compare_digest(auth, 'Bearer ' + expected):
        raise HTTPException(401, 'unauthorized')
    raw = await request.body()
    if len(raw) > 160_000:
        raise HTTPException(413, 'request_too_large')
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, 'invalid_json') from None
    if (not isinstance(payload, dict) or payload.get('model') != os.environ['R4_TARGET_MODEL']
            or payload.get('stream') or not isinstance(payload.get('messages'), list)):
        raise HTTPException(400, 'unsupported_model_request')
    # Keep thinking/high throughout retain and reflect. No implicit weaker model.
    payload['thinking'] = {'type': 'enabled'}
    payload['reasoning_effort'] = 'high'
    maximum = payload.get('max_tokens', payload.get('max_completion_tokens', 8192))
    if type(maximum) is not int or maximum <= 0 or maximum > 32768:
        raise HTTPException(400, 'unsupported_token_reservation')
    payload['max_tokens'] = maximum
    payload.pop('max_completion_tokens', None)
    # Separate phase ledger protects later smoke reserves; overall ledger is the
    # shared R4 total. A failed second reservation leaves conservative phase debt.
    overall = Budget()
    config = os.environ.get('R4_PHASE_CONFIG')
    phase = Budget(path=None) if overall.profile=='acceptance' and config else None
    import json
    if phase:
        phase_cfg = json.loads(Path(config).read_text('utf8'))
        phase.path = Path(phase_cfg['ledger'])
        phase.max_requests = phase_cfg['max_requests']
        phase.max_tokens = phase_cfg['max_tokens']
        phase.batch_id = phase_cfg['batch_id']
    async with slots:
        try:
            phase_ref = phase.reserve(payload) if phase else None
            overall_ref = overall.reserve(payload)
        except BudgetError as exc:
            return JSONResponse(status_code=429, content={'error': {'code': str(exc), 'message': 'R4 budget refused'}})
        start = time.monotonic()
        data = None
        error = None
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=10)) as client:
                result = await client.post(os.environ.get('R4_PROVIDER_BASE_URL','https://api.deepseek.com').rstrip('/')+'/chat/completions',
                    headers={'Authorization': 'Bearer ' + os.environ['R4_PROVIDER_KEY']}, json=payload)
            if result.status_code != 200:
                error = 'provider_http_' + str(result.status_code)
                return JSONResponse(status_code=502, content={'error': {'code': error}})
            if len(result.content) > 1_000_000:
                error = 'provider_response_too_large'
                raise HTTPException(502, error)
            data = result.json()
            return data
        except (httpx.HTTPError, ValueError):
            error = 'provider_network_unknown'
            raise HTTPException(502, error) from None
        finally:
            elapsed = time.monotonic() - start
            overall.settle(overall_ref, data, elapsed, error)
            if phase:phase.settle(phase_ref, data, elapsed, error)
