"""Mock SMS inbox selected by the bank's security phone number.

The separate receiver keeps its own short-lived browser session. Phone lookup
is deliberately a demo interaction, not proof of phone possession.
"""
from pathlib import Path
import sys, os, json, hashlib, secrets, time, asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'backend')]
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from cryptography.fernet import Fernet
from security.sms_mailbox import normalize_phone, phone_inbox

sessions = {}
attempts = {}
PORT = 8022
SERVER_MODE = False
SERVER_ORIGIN = None


def base():
    return Path(os.environ.get('BA_MOBILE_STATE_DIR', str(Path.home() / '.banking-agent/mobile_20261028'))).expanduser().resolve()


def allowed():
    return os.environ.get('BA_DEPLOYMENT_MODE') == 'demo' and os.environ.get('BA_SMS_PROVIDER') == 'mock'


def purge_expired():
    directory = base() / 'sms'
    if not directory.is_dir():
        return
    try:
        cipher = Fernet((directory / 'spool.key').read_bytes().strip())
    except OSError:
        return
    for target in directory.glob('*.sms'):
        try:
            cipher.decrypt(target.read_bytes(), ttl=300)
        except Exception:
            target.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(app):
    async def cleanup():
        while True:
            now = time.time()
            for digest, session in list(sessions.items()):
                if session['until'] <= now:
                    sessions.pop(digest, None)
            for source, history in list(attempts.items()):
                recent = [t for t in history if t > now - 600]
                if recent:
                    attempts[source] = recent
                else:
                    attempts.pop(source, None)
            if allowed():
                purge_expired()
            await asyncio.sleep(30)
    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware('http')
async def boundary(request, call_next):
    trusted_server = SERVER_MODE and request.scope.get('deployment_proxy_verified') is True
    local = not SERVER_MODE and request.client.host in {'127.0.0.1', '::1'} and request.headers.get('host') in {f'127.0.0.1:{PORT}', f'localhost:{PORT}', 'testserver'}
    if not allowed() or not (trusted_server or local):
        return JSONResponse({'error': 'receiver_unavailable'}, status_code=404)
    response = await call_next(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"})
    return response


def same_origin(request):
    origins = {SERVER_ORIGIN} if SERVER_MODE else {None, f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'}
    return request.headers.get('origin') in origins


def identity(request):
    digest = hashlib.sha256(request.cookies.get('demo_receiver', '').encode()).hexdigest()
    value = sessions.get(digest)
    if not value or value['until'] <= time.time():
        sessions.pop(digest, None)
        return None
    return value


@app.get('/', response_class=HTMLResponse)
def page():
    return Path(__file__).with_suffix('.html').read_text(encoding='utf-8')


@app.post('/login')
async def login(request: Request):
    if not same_origin(request):
        return JSONResponse({}, 403)
    now = time.time()
    source = request.client.host
    prior = [t for t in attempts.get(source, []) if t > now - 600]
    attempts[source] = prior
    if len(prior) >= 10:
        return JSONResponse({'error': 'rate_limited'}, 429)
    prior.append(now)
    try:
        payload = await request.json()
        phone = normalize_phone(payload['phone'])
    except (ValueError, TypeError, KeyError):
        return JSONResponse({'error': 'invalid_phone'}, 400)
    try:
        spool_key = (base() / 'sms/spool.key').read_bytes().strip()
        Fernet(spool_key)
        inbox = phone_inbox(phone, spool_key)
    except (OSError, ValueError):
        return JSONResponse({'error': 'receiver_unavailable'}, 503)
    # A valid number may enter before its first SMS is sent. There is no token
    # registry, account lookup, or receiver-credential enrollment step.
    old = hashlib.sha256(request.cookies.get('demo_receiver', '').encode()).hexdigest()
    sessions.pop(old, None)
    session = secrets.token_urlsafe(32)
    sessions[hashlib.sha256(session.encode()).hexdigest()] = {'inbox': inbox, 'until': now + 1800}
    response = JSONResponse({'ok': True})
    response.set_cookie('demo_receiver', session, secure=SERVER_MODE, httponly=True, samesite='strict', max_age=1800)
    return response


@app.post('/logout')
def logout(request: Request):
    if not same_origin(request):
        return JSONResponse({}, 403)
    sessions.pop(hashlib.sha256(request.cookies.get('demo_receiver', '').encode()).hexdigest(), None)
    response = JSONResponse({'ok': True})
    response.delete_cookie('demo_receiver', secure=SERVER_MODE, httponly=True, samesite='strict')
    return response


@app.get('/messages')
def messages(request: Request):
    row = identity(request)
    if not row:
        return JSONResponse({'error': 'receiver_unauthorized'}, 401)
    target = base() / 'sms' / (row['inbox'] + '.sms')
    if not target.is_file():
        return {'message': None}
    try:
        cipher = Fernet((base() / 'sms/spool.key').read_bytes().strip())
        value = json.loads(cipher.decrypt(target.read_bytes(), ttl=300))
        if datetime.fromisoformat(value['expires_at']) <= datetime.now(timezone.utc):
            target.unlink(missing_ok=True)
            return {'message': None}
    except Exception:
        target.unlink(missing_ok=True)
        return {'message': None}
    return {'message': {k: value[k] for k in ['phone', 'code', 'expires_at']}}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, choices=[8022, 8034], default=8022)
    args = parser.parse_args()
    PORT = args.port
    if base().is_relative_to(ROOT):
        raise ValueError('receiver_state_must_be_external')
    if not allowed():
        raise SystemExit('Set BA_DEPLOYMENT_MODE=demo and BA_SMS_PROVIDER=mock explicitly; production receiver forbidden.')
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False)
