type Callbacks={makeRecognition:()=>any;onRecording:(value:boolean)=>void;onNote:(text:string)=>void;onText:(text:string)=>void;onDone:()=>void;onActivity?:(active:boolean)=>void}
export function createVoiceCapture(c:Callbacks){
  let r:any,active=false,stopping=false,transcript='',timer:ReturnType<typeof setTimeout>|undefined
  const clear=()=>{if(timer!==undefined)clearTimeout(timer);timer=undefined}
  const finish=(note:string,text=false)=>{
    if(!active)return
    active=false;clear();c.onRecording(false);c.onActivity?.(false)
    const value=transcript;c.onDone()
    if(text&&value)c.onText(value)
    c.onNote(text&&value?'语音已转为文字，请核对后发送。':note)
  }
  const abort=()=>{try{r?.abort()}catch{/* local cleanup is already complete */}}
  const fail=(note:string)=>{finish(note);abort()}
  const wait=(ms:number,fn:()=>void)=>{clear();timer=setTimeout(()=>{if(active)fn()},ms)}
  const stop=()=>{
    if(!active||stopping)return
    stopping=true;c.onRecording(false)
    wait(4000,()=>fail('识别结束等待超时，本次文字未采用；原草稿已保留，可重试或继续打字。'))
    try{r.stop()}catch{fail('录音尚未开始或未正常结束，原草稿已保留，可重试。')}
  }
  return {
    get active(){return active},
    start(){
      if(active)return
      active=true;c.onActivity?.(true)
      try{
        r=c.makeRecognition();r.lang='zh-CN';r.interimResults=true;r.continuous=false
        r.onstart=()=>{if(!active){abort();return}if(stopping){try{r.stop()}catch{fail('录音未正常结束，可重试。')}return}
          c.onRecording(true);c.onNote('正在录音，松开结束，上滑取消。');wait(20000,stop)}
        r.onresult=(event:any)=>{if(active)transcript=Array.from(event.results).filter((v:any)=>v.isFinal!==false).map((v:any)=>v[0].transcript).join('')}
        r.onerror=(event:any)=>{if(!active)return;fail(({'not-allowed':'麦克风权限未允许，请检查微信或浏览器权限，或继续打字。','service-not-allowed':'当前环境不允许此识别服务，可继续打字。','audio-capture':'未找到可用麦克风，请检查设备。','no-speech':'没有识别到声音，可重试或继续打字。',network:'语音识别服务连接失败，可继续打字。',aborted:'录音已取消。'} as Record<string,string>)[event.error]||'语音识别未完成，原草稿已保留，可重试。')}
        r.onend=()=>finish('本次没有识别到最终文字，可重试或继续打字。',true)
        c.onNote('正在请求麦克风，请保持按住…');wait(12000,()=>fail('麦克风或识别服务启动等待超时，可重试或继续打字。'));r.start()
      }catch{fail('当前环境未能启动语音识别，原草稿已保留，可重试或继续打字。')}
    },
    stop,
    cancel(){fail('录音已取消。')},
  }
}
