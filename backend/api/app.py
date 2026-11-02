from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent.llm_client import LLMConfigurationError

from .presenter import present, present_state
from .read import get_read_only_service
from .runtime import runtime
from .schemas import (
    ApiProblem,
    CapabilitiesResponse,
    ChatRequest,
    ConfirmRequest,
    CreateSessionResponse,
    HealthResponse,
    ReadRequest,
    UiEnvelope,
)
from .v2_routes import router as v2_router
from .v2_schemas import ApiProblemV2
# Registers the payment-1.0 dialogue tool executor (dependency inversion: the
# dialogue gate must not import payments/service modules directly).
from . import payment_dialogue  # noqa: F401
from build_info import LOADED_BUILD

BACKEND_VERSION = "1.0.0-rc.1"

app = FastAPI(
    title="Banking Agent UI API",
    version="1.0.0-rc.1",
    description=(
        "Stable UI adapter over Banking Agent V0.9.2. "
        "Contract 1.1 (/api/v1) is frozen; contract 2.0 (/api/v2) adds the "
        "independent security protocol (server-side identity, simulated strong "
        "authentication, permission decisions and structured restrictions)."
    ),
)

# CORS 明确配置：默认只允许本机演示来源，不默认公开全部来源。
_ALLOWED_ORIGINS = list(runtime.security.config.cors_allow_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Session-Credential"],
)


from .secure import router as secure_router
from . import secure_chat  # Register NL routes before including the shared router.
from . import tool_foundation  # Additive structured tools; does not change chat routing.
from . import dialogue  # R2 fixed-engine dialogue routes.
from . import ui_integration  # Additive frontend state and bound confirmation.
from . import mobile  # Additive candidate payment authentication, feature gated.
from . import mobile_business
from billvision import routes as billvision_routes
from payments import routes as payment_routes  # noqa: F401  (A-line payment-1.0 routes; C owns public wiring)
from security.strong_auth import StrongAuthError
app.include_router(secure_router)
app.include_router(dialogue.router)
from api.voice import router as voice_router
app.include_router(voice_router)
from api.assistance import router as assistance_router
app.include_router(assistance_router)
billvision_routes.register_page(app)

@app.get('/internal/build-info', include_in_schema=False)
def internal_build_info():
    return JSONResponse(LOADED_BUILD, headers={'Cache-Control':'no-store'})

@app.exception_handler(billvision_routes.VisionError)
async def billvision_problem(request,exc):
    return JSONResponse(status_code=exc.status,content={'contract_version':'bill-vision-1.0','error':{'code':exc.code,'message':exc.message}},headers={'Cache-Control':'no-store'})

from payments.errors import PaymentError
@app.exception_handler(PaymentError)
async def mobile_business_problem(request,exc):
    return JSONResponse(status_code=exc.status,content={'code':exc.code,'message':exc.message},headers={'Cache-Control':'no-store'})


@app.middleware('http')
async def frozen_auth_boundary(request: Request, call_next):
    from security.payment_auth import enabled as payment_enabled
    if payment_enabled():
        path=request.url.path
        # Frozen legacy protocols cannot manufacture an alternative grant.
        # Reads/login/logout remain usable; all writes use bound secure tasks.
        if ((path.startswith('/api/v2/sessions/') and not (path.endswith('/read') or path.endswith('/state')))
                or (path.startswith('/api/tasks/') and request.method!='GET')
                or path=='/api/v1/chat'):
            return JSONResponse(status_code=409,content={'code':'payment_protocol_required','message':'本版本请使用绑定预览的支付验证与安全任务接口'})
    from storage.config import pg02_enabled as enabled
    if enabled():
        from storage.pg02 import verified_session, principal_scope
        from security.identity import IdentityError
        from storage.errors import StorageError
        path=request.url.path
        parts=path.split('/')
        sid=(parts[parts.index('sessions')+1] if 'sessions' in parts and
             len(parts)>parts.index('sessions')+1 else request.query_params.get('session_id'))
        analysis_read=(len(parts)==6 and parts[1:4]==['api','analysis','sessions'] and parts[5]=='read')
        if path.startswith('/api/v1') and path not in {'/api/v1/health','/api/v1/capabilities'} and not (path=='/api/v1/chat' and sid):
            return JSONResponse(status_code=409,content={'code':'invalid_request','message':'pg02_unsupported_business'})
        if sid:
            bearer=request.headers.get('authorization','')
            import secrets
            if not bearer.startswith('Bearer ') or not secrets.compare_digest(bearer[7:],sid):
                return JSONResponse(status_code=401,content={'code':'unauthorized','message':'unauthorized'})
            try:
                context,_=verified_session(sid,runtime.security.identity._now())
            except IdentityError as error:
                return JSONResponse(status_code=401,content={'code':'credential_expired','message':error.message})
            except StorageError:
                return JSONResponse(status_code=503,content={'code':'storage_unavailable','message':'数据库暂不可用，请稍后同步原引用核实。'},headers={'Cache-Control':'no-store'})
            with principal_scope(context):
                return await call_next(request)
    # Enforce protocol ownership before old presenters can expose any state.
    if request.url.path.startswith('/api/v1/sessions/'):
        sid = request.url.path.split('/')[4]
        try:
            runtime.legacy_session(sid)
        except HTTPException as exc:
            return _problem(exc.status_code, 'invalid_request', str(exc.detail))
    if not request.url.path.startswith('/api/secure/'):
        parts = request.url.path.split('/')
        if 'sessions' in parts:
            index = parts.index('sessions') + 1
            session = runtime.get_session(parts[index]) if len(parts) > index else None
            if session and session.pending_challenge and session.pending_challenge.method == 'totp':
                return JSONResponse(status_code=409, content={
                    'code': 'invalid_request' if request.url.path.startswith('/api/v') else 'unsupported_authentication_method',
                    'message': 'unsupported_authentication_method: 请使用 /api/secure 独立TOTP合同'})
    return await call_next(request)


@app.get('/internal/storage-status', include_in_schema=False)
def internal_storage_status():
    from storage.config import pg02_enabled as enabled
    if enabled():
        from storage.pg02 import readiness
        return readiness()
    return {'backend':'sqlite','application_ready':False,'pg02_transfer_ready':False,'pg03_bill_ready':False,'pg04_wealth_ready':False}


@app.exception_handler(StrongAuthError)
async def strong_auth_error(request: Request, exc: StrongAuthError):
    code = exc.code
    if request.url.path.startswith('/api/v'):
        code = 'invalid_request'
    return JSONResponse(status_code=409, content={'code':code, 'message':exc.message}, headers={'Cache-Control':'no-store'})


CAPABILITIES = [
    {"id": "bill", "name": "账单分析", "enabled": True},
    {"id": "card", "name": "银行卡管理", "enabled": True},
    {"id": "subscription", "name": "订阅与代扣", "enabled": True},
    {"id": "transfer", "name": "智能转账", "enabled": True},
    {"id": "wealth", "name": "模拟理财", "enabled": True},
    {"id": "workflow", "name": "跨场景任务", "enabled": True},
]


def _problem(status_code: int, code: str, message: str):
    return JSONResponse(
        status_code=status_code,
        content=ApiProblem(code=code, message=message).model_dump(mode="json"),
    )


def _v2_problem(status_code: int, code: str, message: str):
    return JSONResponse(
        status_code=status_code,
        content=ApiProblemV2(code=code, message=message).model_dump(mode="json"),
    )


_V2_HTTP_MAPPING = {
    "session_not_found": (404, "session_not_found"),
    "unauthorized": (401, "unauthorized"),
    "credential_expired": (401, "credential_expired"),
    "session_principal_mismatch": (409, "session_principal_mismatch"),
    "no_pending_interaction": (409, "no_pending_interaction"),
    "challenge_not_found": (404, "challenge_not_found"),
    "validation_failed": (422, "validation_failed"),
}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    code = str(exc.detail)
    if request.url.path.startswith('/api/secure/'):
        from .secure import Problem
        return JSONResponse(status_code=exc.status_code, content=Problem(code=code,message=code).model_dump(mode='json'))
    if request.url.path.startswith('/api/analysis/'):
        from .analysis import Problem
        return JSONResponse(status_code=exc.status_code, content=Problem(code=code,message=code).model_dump(mode='json'))
    if request.url.path.startswith('/api/tasks/'):
        return JSONResponse(status_code=exc.status_code, content={'code':code, 'message':code})
    if request.url.path.startswith("/api/v2") or request.url.path.startswith(
        "/api/v2/"
    ):
        status_code, v2_code = _V2_HTTP_MAPPING.get(code, (exc.status_code, "invalid_request"))
        return _v2_problem(status_code, v2_code, code)
    mapping = {
        "session_not_found": ("session_not_found", "会话不存在或后端已重启"),
        "no_pending_interaction": ("no_pending_interaction", "当前没有等待确认或取消的操作"),
    }
    stable_code, message = mapping.get(code, ("invalid_request", code))
    return _problem(exc.status_code, stable_code, message)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, _exc: RequestValidationError):
    parts = request.url.path.split('/')
    if len(parts) >= 6 and parts[1:3] == ['api', 'secure'] and parts[3] == 'sessions' and parts[5] == 'bills':
        # Never serialize Pydantic input/ctx/msg: any may contain private input.
        fields = []
        from billvision.models import Candidate
        safe_names = {'body','query','path','revision','digest','row_ids','text','rows','platform','row_id','value','included','reviewed','duplicate_resolution','document_id','start','end_exclusive','merchant',*Candidate.model_fields}
        for item in _exc.errors()[:30]:
            loc = [str(p) if isinstance(p, int) else p if p in safe_names else 'field' for p in item['loc']]
            field = loc[-1] if loc else 'body'
            suggestion = {'text': '请刷新页面后使用确认保存；确认文本须使用固定协议常量。',
                          'revision': '请重新打开当前批次，使用最新整数版本。',
                          'digest': '请重新读取最新批次摘要后确认。',
                          'row_ids': '请至少选择一条当前可见明细。',
                          'amount_cents': '金额请输入大于零且最多两位小数的元数。',
                          'date': '请填写含年份的有效日期。'}.get(field, '请核对该行字段、必填项及选项后重试。')
            fields.append({'path': '.'.join(loc), 'type': item['type'], 'suggestion': suggestion})
        return JSONResponse(status_code=422, content={'contract_version':'bill-vision-1.0', 'error':{
            'code':'billvision_invalid_request', 'message':'账单请求字段不合法：'+'；'.join(x['path']+'：'+x['suggestion'] for x in fields), 'fields':fields}}, headers={'Cache-Control':'no-store'})
    if request.url.path.startswith('/api/secure/'):
        return JSONResponse(status_code=422, content={'code':'invalid_request','message':'请求不符合独立安全合同'})
    if request.url.path.startswith('/api/analysis/'):
        from .analysis import Problem
        return JSONResponse(status_code=422, content=Problem(code='invalid_analysis_arguments',message='分析请求不符合严格Schema').model_dump(mode='json'))
    if request.url.path.startswith('/api/tasks/'):
        return JSONResponse(status_code=422, content={
            'code':'invalid_request', 'message':'请求字段不符合严格任务管理 Schema'})
    if request.url.path.startswith("/api/v2"):
        return _v2_problem(422, "invalid_request", "请求字段不符合 UI Contract 2.0")
    return _problem(422, "invalid_request", "请求字段不符合 UI Contract 1.1")


@app.get("/api/v1/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        backend_version=BACKEND_VERSION,
        contract_version="1.1",
        llm_configured=runtime.llm_configured,
    )


@app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
def capabilities():
    return CapabilitiesResponse(capabilities=CAPABILITIES)


@app.post("/api/v1/sessions", response_model=CreateSessionResponse)
def create_session():
    return CreateSessionResponse(session_id=runtime.create_session())


@app.post("/api/v1/chat", response_model=UiEnvelope | secure_chat.SecureChatResponse, responses={404: {"model": ApiProblem}, 503: {"model": ApiProblem}})
def chat(body: ChatRequest, request: Request):
    # Additive authenticated adapter. Legacy requests keep their frozen envelope;
    # the V2 credential path uses exactly the secure protocol and authorization.
    sid=request.query_params.get('session_id')
    if sid:
        if body.session_id not in {None,sid}:raise HTTPException(401,'unauthorized')
        from .v2_routes import require_principal
        from lifecycle.routes import require_task_principal
        from security.totp import independent_protocol
        principal=require_principal(sid,request.headers.get('authorization'))
        require_task_principal(sid,principal)
        token=independent_protocol.set(True)
        try:
            mid=request.headers.get('x-client-message-id')
            if mid is not None:
                import re
                if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',mid):raise HTTPException(422,'invalid_client_message_id')
            value=secure_chat.chat(sid,secure_chat.SecureChatRequest(message=body.message,intent_mode=body.intent_mode,client_message_id=mid),principal)
            if isinstance(value,JSONResponse):return value
            return JSONResponse(value.model_dump(mode='json'),headers={'Cache-Control':'no-store'})
        finally:independent_protocol.reset(token)
    try:
        session_id, _session = runtime.get_or_create_session(body.session_id)
        reply, session = runtime.handle(session_id, body.message)
        return present(reply, session_id, session)
    except KeyError:
        raise HTTPException(status_code=404, detail="session_not_found")
    except LLMConfigurationError:
        return _problem(503, "service_unavailable", "LLM 未配置或暂不可用")


@app.post("/api/v1/sessions/{session_id}/confirm", response_model=UiEnvelope, responses={404: {"model": ApiProblem}, 409: {"model": ApiProblem}, 503: {"model": ApiProblem}})
def confirm(session_id: str, body: ConfirmRequest):
    session = runtime.legacy_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.pending_action is None:
        raise HTTPException(status_code=409, detail="no_pending_interaction")
    try:
        reply, session = runtime.handle(session_id, body.text)
        return present(reply, session_id, session)
    except LLMConfigurationError:
        return _problem(503, "service_unavailable", "LLM 未配置或暂不可用")


@app.post("/api/v1/sessions/{session_id}/cancel", response_model=UiEnvelope, responses={404: {"model": ApiProblem}, 409: {"model": ApiProblem}, 503: {"model": ApiProblem}})
def cancel(session_id: str):
    session = runtime.legacy_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.pending_action is None and session.pending_clarification is None:
        raise HTTPException(status_code=409, detail="no_pending_interaction")
    try:
        reply, session = runtime.handle(session_id, "取消")
        envelope = present(reply, session_id, session)
        if envelope.status not in {"failed", "expired"}:
            envelope = envelope.model_copy(update={"status": "cancelled"})
        return envelope
    except LLMConfigurationError:
        return _problem(503, "service_unavailable", "LLM 未配置或暂不可用")


@app.get("/api/v1/sessions/{session_id}/state", response_model=UiEnvelope, responses={404: {"model": ApiProblem}})
def state(session_id: str):
    session = runtime.legacy_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return present_state(session_id, session, runtime.get_last_reply(session_id))


@app.post("/api/v1/sessions/{session_id}/read", response_model=UiEnvelope, responses={404: {"model": ApiProblem}})
def read(session_id: str, body: ReadRequest):
    """确定性只读查询：UI 指定白名单内的只读工具，不经过 LLM，不需要 API Key。"""
    session = runtime.legacy_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    # 无登录的 V1 Demo 绑定隔离的 legacy demo principal（owner scope 0）。
    principal = runtime.session_principal(session_id)
    return get_read_only_service().run(session_id, body, principal=principal)


app.include_router(v2_router)
from .analysis import router as analysis_router, public_router as analysis_public_router
app.include_router(analysis_router)
app.include_router(analysis_public_router)
from lifecycle.routes import router as task_router
from lifecycle.store import TaskError
app.include_router(task_router)
from recall_bridge.routes import router as recall_router
app.include_router(recall_router)


@app.on_event('startup')
def start_recall_worker():
    from recall_bridge.worker import worker
    worker.start()


@app.on_event('shutdown')
def stop_recall_worker():
    from recall_bridge.worker import worker
    worker.stop()


@app.exception_handler(TaskError)
async def task_error_handler(request: Request, exc: TaskError):
    return JSONResponse(status_code=exc.status, content={
        'code': exc.code, 'message': exc.message,**({'message_quota':exc.quota} if hasattr(exc,'quota') else {})})


@app.get("/", include_in_schema=False)
def root():
    return {
        "backend_version": BACKEND_VERSION,
        "contracts": {
            "v1": {"contract_version": "1.1", "base": "/api/v1"},
            "v2": {"contract_version": "2.0", "base": "/api/v2"},
        },
        "docs": "/docs",
    }


# Built frontend is served on the same origin without Node at runtime.
# Keep the historical root JSON and every existing API path unchanged.
from pathlib import Path
from fastapi.staticfiles import StaticFiles
_ui_dist = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
if (_ui_dist / 'index.html').is_file():
    app.mount('/ui', StaticFiles(directory=str(_ui_dist), html=True), name='integrated-ui')
