import {JSDOM} from 'jsdom';import {build} from 'esbuild';import assert from 'node:assert/strict';
const bundle=await build({stdin:{contents:`import React,{useState} from 'react';import{createRoot}from'react-dom/client';import{ChatPanel}from'./src/components/AssistantChatPanel';window.sends=0;function App(){const[d,setD]=useState('原草稿：');return <ChatPanel messages={[]} loading={false} draft={d} setDraft={setD} onSend={async()=>{window.sends++}} mode="analysis" setMode={()=>{}} sid="test-session"/>}window.root=createRoot(document.getElementById('root'));window.root.render(<App/>);`,resolveDir:process.cwd(),loader:'tsx'},bundle:true,write:false,format:'iife',jsx:'automatic',define:{'import.meta.env':'{}','process.env.NODE_ENV':'"production"'}});
const sleep=()=>new Promise(r=>setTimeout(r,40));
async function setup(configured){
 const dom=new JSDOM('<div id="root"></div>',{url:'https://test.invalid/ui/',runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window;
 Object.defineProperty(w,'isSecureContext',{value:true});w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});w.ResizeObserver=class{observe(){}disconnect(){}};w.HTMLElement.prototype.scrollTo=function(){};
 w.fetch=async()=>({ok:true,json:async()=>({configured,reason:configured?null:'voice_not_configured',accepted_mime_types:configured?['audio/webm']:[],max_bytes:4194304,max_duration_ms:30000})});
 w.webkitSpeechRecognition=class{constructor(){w.rec=this}start(){queueMicrotask(()=>this.onstart?.())}stop(){this.onend?.()}abort(){this.onend?.()}};
 w.eval(bundle.outputFiles[0].text);await sleep();await sleep();
 const button=name=>[...w.document.querySelectorAll('button')].find(x=>x.getAttribute('aria-label')===name||x.textContent===name);
 const click=async name=>{const b=button(name);assert.ok(b,'button '+name);b.dispatchEvent(new w.MouseEvent('click',{bubbles:true}));await sleep()};
 return {w,click,button,close(){w.root.unmount();dom.window.close()}};
}
let count=0;
{
 const f=await setup(false);await f.click('切换语音输入');await f.click('开始语音输入');assert.match(f.w.document.body.textContent,/等待音频/);assert.equal(f.w.document.querySelector('.compose-row.recording'),null);
 f.button('结束语音输入').dispatchEvent(new f.w.Event('pointercancel',{bubbles:true}));
 f.w.rec.onresult({results:[Object.assign([{transcript:'查询本月支出'}],{isFinal:false})]});await sleep();await f.click('结束语音输入');
 assert.equal(f.w.document.querySelector('input[aria-label="输入需求"]').value,'原草稿：查询本月支出');assert.equal(f.w.sends,0);count++;
 await f.click('切换语音输入');await f.click('开始语音输入');f.w.rec.onresult({results:[Object.assign([{transcript:'丢弃'}],{isFinal:false})]});await sleep();await f.click('取消本次语音');
 assert.equal(f.w.document.querySelector('input[aria-label="输入需求"]').value,'原草稿：查询本月支出');count++;f.close();
}
{
 const f=await setup(true);let getMedia=0,stops=0,uploads=0;
 Object.defineProperty(f.w.navigator,'mediaDevices',{value:{getUserMedia(){getMedia++;return Promise.resolve({getTracks:()=>[{stop(){stops++}}]})}}});
 f.w.MediaRecorder=class{static isTypeSupported(){return true}constructor(){this.mimeType='audio/webm';this.state='inactive'}start(){this.state='recording';queueMicrotask(()=>this.onstart?.())}stop(){this.state='inactive';this.ondataavailable?.({data:new f.w.Blob(['mock-audio'],{type:'audio/webm'})});this.onstop?.()}};
 f.w.fetch=async url=>{assert.match(url,/transcriptions$/);uploads++;return{ok:true,json:async()=>({text:'录音转写测试',requires_user_send:true})}};
 await f.click('切换语音输入');await f.click('开始语音输入');assert.equal(getMedia,1);assert.match(f.w.document.body.textContent,/正在录音/);await f.click('结束语音输入');await sleep();
 assert.equal(f.w.document.querySelector('input[aria-label="输入需求"]').value,'原草稿：录音转写测试');assert.equal(uploads,1);assert.ok(stops>0);assert.equal(f.w.sends,0);count++;f.close();
}
{
 const f=await setup(false);f.w.webkitSpeechRecognition=undefined;f.w.MediaRecorder=class{};Object.defineProperty(f.w.navigator,'mediaDevices',{value:{getUserMedia(){throw Error('must not record')}}});
 await f.click('切换语音输入');await f.click('开始语音输入');assert.match(f.w.document.body.textContent,/未配置/);assert.equal(f.w.sends,0);count++;f.close();
}
console.log('React DOM voice scenarios passed:',count,'(mock recognition/recorder/provider; no real microphone)');
