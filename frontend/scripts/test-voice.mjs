import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { test } from 'node:test';
const source=fs.readFileSync(new URL('../src/components/voiceSession.ts',import.meta.url),'utf8');
function fixture(){
 const timers=new Map();let seq=0,rec;const texts=[],notes=[],states=[];let done=0;
 const scope={exports:{},setTimeout(fn,ms){const id=++seq;timers.set(id,{fn,ms});return id},clearTimeout(id){timers.delete(id)}};
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,scope);
 const handle=scope.exports.createVoiceCapture({makeRecognition(){rec={start(){},stop(){this.stopped=true},abort(){this.aborted=true}};return rec},onRecording:v=>states.push(v),onNote:v=>notes.push(v),onText:v=>texts.push(v),onDone:()=>done++});
 const result=(parts)=>rec.onresult({results:parts.map(([text,final])=>Object.assign([{transcript:text}],{isFinal:final}))});
 const expire=()=>{const [id,t]=[...timers].at(-1);timers.delete(id);t.fn()};
 return {handle,get rec(){return rec},texts,notes,states,timers,result,expire,get done(){return done}};
}
test('service start does not claim audio capture',()=>{const f=fixture();f.handle.start();f.rec.onstart();assert.ok(!f.states.includes(true));assert.match(f.notes.at(-1),/等待音频/);f.handle.cancel()});
test('audio event starts capture indicator',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.rec.onaudiostart();assert.equal(f.states.at(-1),true);f.handle.cancel()});
test('Safari provisional-only result survives normal end as a reviewable draft',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.result([['查询本月支出',false]]);f.handle.stop();f.rec.onend();assert.deepEqual(f.texts,['查询本月支出']);assert.match(f.notes.at(-1),/临时/);assert.equal(f.done,1)});
test('final results replace provisional text without duplicates',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.result([['查本月',false]]);f.result([['查询本月支出',true]]);f.rec.onend();assert.deepEqual(f.texts,['查询本月支出']);assert.equal(f.timers.size,0)});
test('ordered final and provisional segments are both retained',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.result([['查询',true],['本月支出',false]]);f.rec.onend();assert.deepEqual(f.texts,['查询本月支出'])});
test('cancel never publishes provisional or late text',()=>{const f=fixture();f.handle.start();f.result([['不能发送',false]]);f.handle.cancel();f.result([['迟到结果',true]]);f.rec.onend();assert.deepEqual(f.texts,[]);assert.equal(f.done,1);assert.equal(f.timers.size,0)});
test('stop during permission startup waits for start then stops',()=>{const f=fixture();f.handle.start();f.handle.stop();assert.equal(f.rec.stopped,undefined);f.rec.onstart();assert.equal(f.rec.stopped,true);f.rec.onend();assert.deepEqual(f.texts,[])});
test('no audio triggers a bounded diagnostic timeout',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.expire();assert.equal(f.handle.active,false);assert.match(f.notes.at(-1),/未收到音频/);assert.equal(f.rec.aborted,true)});
test('slow stop retains available text as unconfirmed draft',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.result([['临时文字',false]]);f.handle.stop();assert.equal([...f.timers.values()][0].ms,10000);f.expire();assert.deepEqual(f.texts,['临时文字']);assert.match(f.notes.at(-1),/服务未正常结束/)});
test('service denial does not claim successful capture',()=>{const f=fixture();f.handle.start();f.rec.onerror({error:'service-not-allowed'});assert.deepEqual(f.texts,[]);assert.match(f.notes.at(-1),/不允许/);assert.equal(f.handle.active,false)});
test('network failure is reported without silent retry',()=>{const f=fixture();f.handle.start();f.rec.onstart();f.rec.onerror({error:'network'});assert.match(f.notes.at(-1),/连接失败/);assert.equal(f.done,1);assert.equal(f.timers.size,0)});
test('natural result then late end cannot publish twice',()=>{const f=fixture();f.handle.start();f.result([['账单',true]]);f.rec.onend();f.rec.onend();assert.deepEqual(f.texts,['账单']);assert.equal(f.done,1)});
