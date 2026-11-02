"""Authenticated dictation only. Text requires a separate explicit chat send."""
import asyncio,re
from fastapi import APIRouter,Depends,Header,Request
from fastapi.responses import JSONResponse
from .dialogue import require_task_principal
from security.schemas import Principal
from voice_transcription import capabilities,requests,VoiceError,MAX_BYTES

router=APIRouter(prefix='/api/secure/sessions/{session_id}/voice',tags=['voice-dictation'])


@router.get('/capabilities')
def voice_capabilities(session_id:str,p:Principal=Depends(require_task_principal)):
    return capabilities()


@router.post('/transcriptions')
async def voice_transcription(session_id:str,request:Request,x_voice_request_id:str=Header(''),x_audio_duration_ms:int=Header(0),p:Principal=Depends(require_task_principal)):
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',x_voice_request_id):raise VoiceError('voice_request_invalid',422)
        if not capabilities()['configured']:raise VoiceError(capabilities()['reason'])
        async def read_audio():
            size=0;parts=[]
            async for part in request.stream():
                size+=len(part)
                if size>MAX_BYTES:raise VoiceError('voice_audio_size_invalid',413)
                parts.append(part)
            return b''.join(parts)
        try:audio=await asyncio.wait_for(read_audio(),timeout=15)
        except asyncio.TimeoutError:raise VoiceError('voice_recording_upload_timeout',408) from None
        # Session revocation/expiry is checked both before sending audio and
        # before publishing text, even while the provider request was pending.
        require_task_principal(session_id,'Bearer '+session_id)
        text=await asyncio.to_thread(requests.run,(p.user_id,p.principal_id),x_voice_request_id,audio,request.headers.get('content-type',''),x_audio_duration_ms)
        require_task_principal(session_id,'Bearer '+session_id)
        return dict(text=text,source='configured_transcription',requires_user_send=True)
    except VoiceError as exc:return JSONResponse(status_code=exc.status,content=dict(code=exc.code,message=exc.code))
