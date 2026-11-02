"""Additive authenticated tools, deliberately separate from /chat."""
from fastapi import Depends
from api.secure import router
from api.runtime import runtime
from lifecycle.routes import require_task_principal
from security.schemas import Principal
from tool_foundation import Call, run


@router.get('/tools/capabilities')
def capabilities(principal: Principal = Depends(require_task_principal)):
    from foundation.gateway import capabilities as describe
    return describe()


@router.post('/tools/call')
def call_tool(session_id: str, body: Call, principal: Principal = Depends(require_task_principal)):
    with runtime.session_lock(session_id):
        return run(body,principal,session_id)
