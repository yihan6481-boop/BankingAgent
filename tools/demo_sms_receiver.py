"""Loopback-only demo receiver with independent, owner-scoped expiring access.

No ordinary Banking Agent login, bearer session or LLM tool can access inboxes.
Not mounted in the main application; never added to the public proxy allowlist.
"""
from pathlib import Path
import sys,os,json,hashlib,secrets,time,asyncio
from contextlib import asynccontextmanager,suppress
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'backend')]
from fastapi import FastAPI,Request
from fastapi.responses import HTMLResponse,JSONResponse
from cryptography.fernet import Fernet
def purge_expired():
    directory=base()/'sms'
    if not directory.is_dir():return
    try:cipher=Fernet((directory/'spool.key').read_bytes().strip())
    except OSError:return
    for target in directory.glob('*.sms'):
        try:cipher.decrypt(target.read_bytes(),ttl=300)
        except Exception:target.unlink(missing_ok=True)

@asynccontextmanager
async def lifespan(app):
    async def cleanup():
        while True:
            if allowed():purge_expired()
            await asyncio.sleep(30)
    task=asyncio.create_task(cleanup())
    try:yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):await task

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
sessions={};attempts={}
PORT=8022
SERVER_MODE=False
SERVER_ORIGIN=None
def base():return Path(os.environ.get('BA_MOBILE_STATE_DIR',str(Path.home()/'.banking-agent/mobile_20261028'))).expanduser().resolve()
def allowed():return os.environ.get('BA_DEPLOYMENT_MODE')=='demo' and os.environ.get('BA_SMS_PROVIDER')=='mock'
@app.middleware('http')
async def boundary(request,call_next):
    trusted_server=SERVER_MODE and request.scope.get('deployment_proxy_verified') is True
    local=not SERVER_MODE and request.client.host in {'127.0.0.1','::1'} and request.headers.get('host') in {f'127.0.0.1:{PORT}',f'localhost:{PORT}','testserver'}
    if not allowed() or not (trusted_server or local):
        return JSONResponse({'error':'receiver_unavailable'},status_code=404)
    response=await call_next(request);response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"})
    return response
def identity(request):
    value=sessions.get(hashlib.sha256(request.cookies.get('demo_receiver','').encode()).hexdigest())
    if not value or value['until']<time.time():return None
    tokens=json.loads((base()/'receiver_tokens.json').read_text('utf8'))
    row=tokens.get(value['token_hash'])
    return row if row and datetime.fromisoformat(row['expires_at'])>datetime.now(timezone.utc) else None
@app.get('/',response_class=HTMLResponse)
def page():
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>演示短信接收器</title><style>body{background:linear-gradient(125deg,#e1efff,#efebff);font:16px system-ui;color:#172139;margin:0;padding:32px 20px}main{max-width:440px;margin:auto;background:#fff;border-radius:24px;padding:25px}h1{font-size:23px}p{color:#718099;line-height:1.7}input,button{box-sizing:border-box;padding:13px;width:100%;border:1px solid #e3eaf6;border-radius:12px;margin:8px 0}button{background:#126aff;color:white;cursor:pointer}strong{font-size:38px;letter-spacing:9px;display:block;color:#125be6;margin:20px 0}#message{padding:15px;background:#f3f7ff;border-radius:14px}</style><main><h1>演示短信接收器｜模拟发送</h1><p>仅供受控本机演示。此窗口使用独立领取凭据，与银行登录态分离。</p><section id="login"><input id="credential" type="password" autocomplete="off" placeholder="独立领取凭据" aria-label="领取凭据"><button onclick="login()">进入本人收件箱</button></section><section id="message">尚未登录接收器</section><p id="status"></p><button onclick="logout()">退出接收器</button><p>Mock通道不证明真实手机号持有权。消息到期自动停止展示。</p></main><script>async function login(){let token=document.querySelector('#credential').value;document.querySelector('#credential').value='';const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});document.querySelector('#status').textContent=r.ok?'已进入本人接收器':'领取凭据无效或已限流';await refresh()}async function refresh(){const r=await fetch('/messages');const box=document.querySelector('#message');box.replaceChildren();if(!r.ok){box.textContent='请使用独立领取凭据登录';return}const v=await r.json();if(!v.message){box.textContent='尚无有效短信';return}let p=document.createElement('p');p.textContent='安全手机号 '+v.message.phone;let code=document.createElement('strong');code.textContent=v.message.code;let note=document.createElement('p');note.textContent='本次交易验证码，5分钟内有效。请在银行任务验证框输入。';box.append(p,code,note)}async function logout(){await fetch('/logout',{method:'POST'});await refresh()}setInterval(refresh,2000);refresh()</script></html>'''
    return html.replace('仅供受控本机演示。','仅供受控 HTTPS 演示。') if SERVER_MODE else html
@app.post('/login')
async def login(request:Request):
    origins={SERVER_ORIGIN} if SERVER_MODE else {None,f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'}
    if request.headers.get('origin') not in origins:return JSONResponse({},403)
    now=time.time();source=request.client.host;prior=[t for t in attempts.get(source,[]) if t>now-600];attempts[source]=prior
    if len(prior)>=10:return JSONResponse({'error':'rate_limited'},429)
    try:token=(await request.json())['token'];hashed=hashlib.sha256(token.encode()).hexdigest();tokens=json.loads((base()/'receiver_tokens.json').read_text('utf8'));row=tokens.get(hashed)
    except Exception:row=None
    if not row or datetime.fromisoformat(row['expires_at'])<=datetime.now(timezone.utc):prior.append(now);return JSONResponse({'error':'invalid_credential'},401)
    session=secrets.token_urlsafe(32);sessions[hashlib.sha256(session.encode()).hexdigest()]={'token_hash':hashed,'until':now+1800}
    response=JSONResponse({'ok':True});response.set_cookie('demo_receiver',session,secure=SERVER_MODE,httponly=True,samesite='strict',max_age=1800);return response
@app.post('/logout')
def logout(request:Request):
    sessions.pop(hashlib.sha256(request.cookies.get('demo_receiver','').encode()).hexdigest(),None)
    response=JSONResponse({'ok':True});response.delete_cookie('demo_receiver',secure=SERVER_MODE,httponly=True,samesite='strict');return response
@app.get('/messages')
def messages(request:Request):
    row=identity(request)
    if not row:return JSONResponse({'error':'receiver_unauthorized'},401)
    target=base()/'sms'/(row['inbox']+'.sms')
    if not target.is_file():return {'message':None}
    try:
        cipher=Fernet((base()/'sms/spool.key').read_bytes().strip());value=json.loads(cipher.decrypt(target.read_bytes(),ttl=300))
        if datetime.fromisoformat(value['expires_at'])<=datetime.now(timezone.utc):target.unlink(missing_ok=True);return {'message':None}
    except Exception:
        target.unlink(missing_ok=True)
        return {'message':None}
    return {'message':{k:value[k] for k in ['phone','code','expires_at']}}
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,choices=[8022,8034],default=8022);args=parser.parse_args()
    PORT=args.port
    if base().is_relative_to(ROOT):raise ValueError('receiver_state_must_be_external')
    if not allowed():raise SystemExit('Set BA_DEPLOYMENT_MODE=demo and BA_SMS_PROVIDER=mock explicitly; production receiver forbidden.')
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=PORT,access_log=False)
