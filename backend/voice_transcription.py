"""Replaceable OpenAI-compatible audio transcription transport, disabled by default.

Audio stays in bounded memory, never in bank facts/history or a business tool.
No provider fallback, simulation, automatic retry, TLS bypass or secret logging.
"""
from dataclasses import dataclass
import os,threading,time,hashlib
from urllib.parse import urlsplit
import httpx

MAX_BYTES=4*1024*1024
MAX_MS=30000
MIMES={'audio/webm':'.webm','audio/mp4':'.mp4','audio/ogg':'.ogg','audio/wav':'.wav','audio/mpeg':'.mp3'}


class VoiceError(ValueError):
    def __init__(self,code,status=503):self.code=code;self.status=status;super().__init__(code)


@dataclass(frozen=True)
class Configuration:
    endpoint:str
    model:str
    mimes:tuple[str,...]


def configuration():
    if os.getenv('BA_ASR_ENABLED')!='1':raise VoiceError('voice_not_configured')
    endpoint=os.getenv('BA_ASR_ENDPOINT','').strip();model=os.getenv('BA_ASR_MODEL','').strip()
    if not endpoint or not model:raise VoiceError('voice_not_configured')
    try:
        url=urlsplit(endpoint)
        local=url.hostname in {'localhost','127.0.0.1','::1'} and os.getenv('BA_ASR_ALLOW_LOOPBACK_HTTP')=='1'
        if not url.hostname or url.username or url.password or url.query or url.fragment or not url.path or (url.scheme!='https' and not (url.scheme=='http' and local)):raise ValueError()
        if any(ord(c)<32 for c in endpoint+model) or len(model)>120:raise ValueError()
    except ValueError:raise VoiceError('voice_configuration_invalid') from None
    mimes=tuple(dict.fromkeys(x.strip() for x in os.getenv('BA_ASR_MIME_TYPES','audio/webm,audio/mp4,audio/ogg,audio/wav,audio/mpeg').split(',')))
    if not mimes or any(x not in MIMES for x in mimes):raise VoiceError('voice_configuration_invalid')
    return Configuration(endpoint,model,mimes)


def capabilities():
    try:c=configuration();return dict(configured=True,reason=None,accepted_mime_types=list(c.mimes),max_bytes=MAX_BYTES,max_duration_ms=MAX_MS)
    except VoiceError as exc:return dict(configured=False,reason=exc.code,accepted_mime_types=[],max_bytes=MAX_BYTES,max_duration_ms=MAX_MS)


def validate_audio(audio,mime,duration_ms,config):
    mime=mime.split(';',1)[0].strip().lower()
    if mime not in config.mimes:raise VoiceError('voice_format_unsupported',415)
    if not 0<len(audio)<=MAX_BYTES:raise VoiceError('voice_audio_size_invalid',413)
    if not 0<duration_ms<=MAX_MS+1000:raise VoiceError('voice_duration_invalid',422)
    magic={'audio/webm':audio.startswith(b'\x1aE\xdf\xa3'),'audio/mp4':len(audio)>12 and audio[4:8]==b'ftyp','audio/ogg':audio.startswith(b'OggS'),'audio/wav':audio.startswith(b'RIFF') and audio[8:12]==b'WAVE','audio/mpeg':audio.startswith(b'ID3') or len(audio)>1 and audio[0]==255 and audio[1]&224==224}
    if not magic[mime]:raise VoiceError('voice_recording_invalid',422)
    return mime


def transcribe(audio,mime,config):
    headers={};key=os.getenv('BA_ASR_API_KEY','')
    if key:headers['Authorization']='Bearer '+key
    try:
        with httpx.Client(timeout=httpx.Timeout(25,connect=5),verify=True,trust_env=False,follow_redirects=False) as client:
            # A bounded streamed response prevents a provider returning a huge
            # body from exhausting application memory. No redirects carry keys.
            with client.stream('POST',config.endpoint,headers=headers,data=dict(model=config.model,language='zh',response_format='json'),files={'file':('voice'+MIMES[mime],audio,mime)}) as response:
                if response.status_code in {401,403}:raise VoiceError('voice_service_permission_failed')
                if response.status_code==429:raise VoiceError('voice_service_busy',429)
                if response.status_code!=200:raise VoiceError('voice_service_unavailable')
                parts=[];size=0
                for part in response.iter_bytes():
                    size+=len(part)
                    if size>64000:raise VoiceError('voice_service_invalid_response')
                    parts.append(part)
        import json
        value=json.loads(b''.join(parts));text=value.get('text') if isinstance(value,dict) else None
        if not isinstance(text,str) or len(text)>4000:raise VoiceError('voice_service_invalid_response')
        if not text.strip():raise VoiceError('voice_no_text',422)
        return text.strip()
    except httpx.TimeoutException:raise VoiceError('voice_recognition_timeout',504) from None
    except httpx.HTTPError:raise VoiceError('voice_service_unavailable') from None
    except (UnicodeError,ValueError) as exc:
        if isinstance(exc,VoiceError):raise
        raise VoiceError('voice_service_invalid_response') from None


class Requests:
    """Bounded process-local dedup/rate guard; never replays a lost request.

    Provider idempotency across process restarts is not promised; clients do not
    automatically retry transcription. This changes no existing paid ledger.
    """
    def __init__(self):self.lock=threading.Lock();self.rows={};self.recent={}
    def run(self,owner,request_id,audio,mime,duration_ms):
        config=configuration();mime=validate_audio(audio,mime,duration_ms,config)
        digest=hashlib.sha256(audio+mime.encode()).hexdigest();key=(owner,request_id);now=time.monotonic()
        with self.lock:
            self.rows={k:v for k,v in self.rows.items() if now-v['at']<300}
            prior=self.rows.get(key)
            if prior:
                if prior['digest']!=digest:raise VoiceError('voice_request_conflict',409)
                if 'text' in prior:return prior['text']
                raise VoiceError(prior.get('error','voice_request_in_progress'),409)
            self.recent={k:[t for t in v if now-t<60] for k,v in self.recent.items() if any(now-t<60 for t in v)}
            recent=self.recent.setdefault(owner,[])
            if len(recent)>=3 or any(k[0]==owner and v.get('running') for k,v in self.rows.items()) or len(self.rows)>=512:raise VoiceError('voice_service_busy',429)
            recent.append(now);self.rows[key]=dict(at=now,digest=digest,running=True)
        try:
            text=transcribe(audio,mime,config)
            with self.lock:self.rows[key].update(text=text,running=False)
            return text
        except Exception as exc:
            code=exc.code if isinstance(exc,VoiceError) else 'voice_service_unavailable'
            with self.lock:self.rows[key].update(error=code,running=False)
            if isinstance(exc,VoiceError):raise
            raise VoiceError(code) from None


requests=Requests()
