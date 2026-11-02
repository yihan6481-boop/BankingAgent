type Callbacks={makeRecognition:()=>any;onRecording:(value:boolean)=>void;onNote:(text:string)=>void;onText:(text:string)=>void;onDone:()=>void;onActivity?:(active:boolean)=>void;onProcessing?:()=>void}

// onstart means that the recognition service started, NOT that audio was captured.
// Dictation only fills a draft. Even provisional results need a separate user send.
export function createVoiceCapture(c:Callbacks){
  let r:any,active=false,stopping=false,started=false,audioStarted=false,transcript='',provisional=false
  let timer:ReturnType<typeof setTimeout>|undefined
  const clear=()=>{if(timer!==undefined)clearTimeout(timer);timer=undefined}
  const abort=()=>{try{r?.abort()}catch{/* Already stopped. */}}
  const finish=(note:string,publish=false)=>{
    if(!active)return
    active=false;clear();c.onRecording(false);c.onActivity?.(false);c.onDone()
    if(publish&&transcript)c.onText(transcript)
    c.onNote(note)
  }
  const fail=(note:string)=>{finish(note);abort()}
  const wait=(ms:number,fn:()=>void)=>{clear();timer=setTimeout(()=>{if(active)fn()},ms)}
  const resultNote=()=>provisional?'已保留临时识别文字，请逐字核对后再发送。':'语音已转为文字，请核对后发送。'
  const stop=()=>{
    if(!active||stopping)return
    stopping=true;c.onRecording(false);c.onProcessing?.();c.onNote('正在结束识别，请稍候…')
    // Some Safari versions deliver the last result only after stop(), with a delay.
    wait(10000,()=>{finish(transcript?'已保留当前识别文字，服务未正常结束，请核对后再发送。':'识别服务未返回文字。可尝试录音后转写或手机键盘听写。',!!transcript);abort()})
    if(started){try{r.stop()}catch{fail('识别服务未能正常结束，请重试。')}}
  }
  const audio=()=>{
    if(!active||stopping)return
    if(!audioStarted){audioStarted=true;c.onRecording(true);wait(30000,stop)}
  }
  return {
    get active(){return active},
    start(){
      if(active)return
      active=true;c.onActivity?.(true)
      try{
        r=c.makeRecognition();r.lang='zh-CN';r.interimResults=true;r.continuous=false
        r.onstart=()=>{
          if(!active){abort();return}started=true
          if(stopping){try{r.stop()}catch{fail('识别服务未能正常结束，请重试。')}return}
          c.onNote('识别服务已启动，正在等待音频…')
          wait(12000,()=>fail('识别服务已启动，但未收到音频或文字。请检查麦克风，或改用录音后转写。'))
        }
        r.onaudiostart=()=>{audio();if(active&&!stopping)c.onNote('已开始采集音频，请说话；点击结束。')}
        r.onspeechstart=()=>{audio();if(active&&!stopping)c.onNote('检测到语音，正在识别；点击结束。')}
        r.onaudioend=()=>{if(active){c.onRecording(false);if(!stopping)c.onNote('音频采集已结束，正在等待识别结果…')}}
        r.onresult=(event:any)=>{
          if(!active)return
          const results=Array.from(event.results||[]) as any[]
          transcript=results.map(v=>typeof v?.[0]?.transcript==='string'?v[0].transcript:'').join('').trim()
          provisional=results.some(v=>v.isFinal===false)
          if(transcript){audio();c.onNote('识别文字：'+transcript.slice(0,120)+(provisional?'（临时结果）':''))}
        }
        r.onerror=(event:any)=>{
          if(!active)return
          fail(({'not-allowed':'麦克风权限未允许，请检查浏览器的站点权限。','service-not-allowed':'当前浏览器不允许此识别服务。可切换录音后转写，或使用手机键盘听写。','audio-capture':'未找到可用麦克风，或麦克风被其他应用占用。','no-speech':'识别服务没有检测到语音，请检查麦克风或尝试录音后转写。',network:'浏览器语音识别服务连接失败，可切换录音后转写或手机键盘听写。',aborted:'语音识别已取消。'} as Record<string,string>)[event.error]||'语音识别未完成，原草稿已保留。')
        }
        r.onend=()=>finish(transcript?resultNote():(audioStarted?'采集已结束，但识别服务没有返回文字。可尝试录音后转写或手机键盘听写。':'本次识别未取得音频或文字，请检查麦克风和识别服务。'),!!transcript)
        c.onNote('正在请求麦克风与识别服务，请允许权限后说话…')
        wait(12000,()=>fail('麦克风或识别服务启动超时。可重试或使用手机键盘听写。'))
        r.start()
      }catch{fail('当前浏览器未能启动语音识别，原草稿已保留。')}
    },
    stop,
    cancel(){fail('录音已取消，原草稿已保留。')},
  }
}
