export interface VoiceCapabilities {configured:boolean;reason?:string;accepted_mime_types:string[];max_bytes:number;max_duration_ms:number}
export type VoiceHandle={readonly active:boolean;start:()=>void;stop:()=>void;cancel:()=>void}
type Callbacks={onRecording:(value:boolean)=>void;onNote:(text:string)=>void;onText:(text:string)=>void;onDone:()=>void;onActivity?:(active:boolean)=>void;onProcessing?:()=>void}
const base=()=>((import.meta.env?.VITE_API_BASE as string)||'/api').replace(/\/$/,'')
export const voiceMessage=(code:string)=>({voice_not_configured:'录音转写服务未配置，可使用可用的浏览器识别或继续打字。',voice_configuration_invalid:'录音转写服务配置无效，请联系维护人员；可继续打字。',voice_format_unsupported:'本机录音格式与转写服务不匹配，可继续打字。',voice_recording_invalid:'录音文件未正常生成，请重试或继续打字。',voice_audio_size_invalid:'录音为空或过大，请缩短录音后重试。',voice_duration_invalid:'录音时长无效，请重新录制。',voice_service_busy:'转写服务繁忙，请稍后重试或继续打字。',voice_service_unavailable:'语音识别服务不可用或连接中断，可继续打字。',voice_service_permission_failed:'转写服务凭据未被接受，与手机麦克风权限无关；可继续打字。',voice_recognition_timeout:'语音识别超时，本次未回填，可继续打字。',voice_no_text:'没有识别到文字，可重试或继续打字。',voice_service_invalid_response:'转写服务未返回有效文字，可继续打字。',voice_request_in_progress:'原录音仍在识别，请勿重复提交；可继续打字。',voice_request_conflict:'原录音身份不匹配，本次未采用，可继续打字。'} as Record<string,string>)[code]||'语音处理未完成，可重试或继续打字。'

export async function voiceCapabilities(sid:string,signal:AbortSignal):Promise<VoiceCapabilities>{
  const response=await fetch(`${base()}/secure/sessions/${encodeURIComponent(sid)}/voice/capabilities`,{cache:'no-store',headers:{Authorization:`Bearer ${sid}`},signal})
  if(!response.ok)throw Error('voice_service_unavailable')
  return response.json()
}

export function recordingMime(recorder:typeof MediaRecorder|undefined,accepted:string[]){
  if(!recorder||typeof recorder.isTypeSupported!=='function')return null
  return ['audio/webm;codecs=opus','audio/mp4','audio/webm','audio/ogg;codecs=opus','audio/wav'].find(m=>accepted.includes(m.split(';')[0])&&recorder.isTypeSupported(m))||null
}

export async function uploadRecording(sid:string,audio:Blob,duration:number,requestId:string,signal:AbortSignal){
  let response:Response
  try{response=await fetch(`${base()}/secure/sessions/${encodeURIComponent(sid)}/voice/transcriptions`,{method:'POST',cache:'no-store',signal,headers:{Authorization:`Bearer ${sid}`,'Content-Type':audio.type,'X-Voice-Request-ID':requestId,'X-Audio-Duration-Ms':String(Math.max(1,Math.round(duration)))},body:audio})}
  catch{throw Error(signal.aborted?'voice_recognition_timeout':'voice_service_unavailable')}
  let result:any
  try{result=await response.json()}catch{throw Error('voice_service_invalid_response')}
  if(!response.ok)throw Error(result.code||'voice_service_unavailable')
  if(typeof result.text!=='string'||!result.text.trim()||result.requires_user_send!==true)throw Error('voice_no_text')
  return result.text
}

export function createRecordedCapture(c:Callbacks&{capabilities:VoiceCapabilities;mime:string;getMedia?:()=>Promise<MediaStream>;makeRecorder?:(stream:MediaStream,mime:string)=>MediaRecorder;transcribe:(audio:Blob,duration:number,signal:AbortSignal)=>Promise<string>}):VoiceHandle{
  let active=false,stopping=false,stream:MediaStream|undefined,recorder:MediaRecorder|undefined,started=0,chunks:Blob[]=[],size=0
  let timer:ReturnType<typeof setTimeout>|undefined;const abort=new AbortController()
  const clear=()=>{if(timer!==undefined)clearTimeout(timer);timer=undefined}
  const release=()=>{stream?.getTracks().forEach(t=>t.stop());stream=undefined}
  const finish=(note:string,text?:string)=>{
    if(!active)return;active=false;clear();abort.abort();release()
    try{if(recorder?.state==='recording')recorder.stop()}catch{/* track cleanup has already run */}
    chunks=[];c.onRecording(false);c.onActivity?.(false);c.onDone();if(text)c.onText(text);c.onNote(note)
  }
  const wait=(ms:number,fn:()=>void)=>{clear();timer=setTimeout(()=>{if(active)fn()},ms)}
  const stop=()=>{
    if(!active||stopping)return;stopping=true;c.onRecording(false)
    if(!recorder){finish('录音尚未开始，已取消本次请求，可继续打字。');return}
    c.onNote('正在结束录音…');wait(4000,()=>finish('录音结束超时，已释放麦克风，可继续打字。'))
    try{recorder.stop()}catch{finish('录音未能正常结束，可继续打字。')}
  }
  return {get active(){return active},start(){
    if(active)return;active=true;c.onActivity?.(true);c.onNote('正在请求麦克风，请保持按住…')
    wait(12000,()=>finish('麦克风授权等待超时，尚未开始识别，可继续打字。'))
    void Promise.resolve().then(()=>c.getMedia?c.getMedia():navigator.mediaDevices.getUserMedia({audio:true})).then(value=>{
      if(!active){value.getTracks().forEach(t=>t.stop());return}stream=value
      try{
        recorder=c.makeRecorder?c.makeRecorder(value,c.mime):new MediaRecorder(value,{mimeType:c.mime})
        const actual=recorder.mimeType||c.mime
        if(!c.capabilities.accepted_mime_types.includes(actual.split(';')[0])){finish(voiceMessage('voice_format_unsupported'));return}
        recorder.ondataavailable=event=>{if(!active||!event.data.size)return;size+=event.data.size;if(size>c.capabilities.max_bytes){finish(voiceMessage('voice_audio_size_invalid'));return}chunks.push(event.data)}
        recorder.onerror=()=>finish('录音失败，已释放麦克风，可继续打字。')
        recorder.onstart=()=>{if(!active||stopping)return;c.onRecording(true);c.onNote('正在录音，松开后提交转写，上滑取消。');wait(c.capabilities.max_duration_ms,stop)}
        recorder.onstop=()=>{
          release();if(!active)return;clear();c.onRecording(false)
          if(!chunks.length){finish(voiceMessage('voice_no_text'));return}
          const blob=new Blob(chunks,{type:actual});chunks=[];c.onProcessing?.();c.onNote('正在识别，可继续编辑文字草稿…')
          wait(30000,()=>finish(voiceMessage('voice_recognition_timeout')))
          void c.transcribe(blob,Math.max(1,performance.now()-started),abort.signal).then(text=>{if(active)finish('语音已转为文字，请核对后发送。',text)}).catch(e=>{if(active)finish(voiceMessage(e instanceof Error?e.message:'voice_service_unavailable'))})
        }
        started=performance.now();recorder.start(250)
      }catch{finish('当前环境录音启动失败，已释放麦克风，可继续打字。')}
    }).catch(e=>{if(active)finish(e?.name==='NotAllowedError'?'麦克风权限被拒绝，请检查站点或微信权限，也可继续打字。':e?.name==='NotFoundError'?'未找到可用麦克风，可继续打字。':'录音设备不可用或被占用，可继续打字。')})
  },stop,cancel(){finish('录音已取消，原文字草稿保留。')}}
}
