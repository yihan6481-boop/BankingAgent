"""Function calling with bounded nonstreaming or buffered provider SSE.

Provider deltas (including opaque reasoning/tool arguments) never reach the UI.
"""
from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import json
import os
import time
from threading import RLock
import httpx
from .budget import Budget


class ModelError(RuntimeError): pass


class ThinkingProtocolMemory:
    """Opaque provider fields only. No durable storage, logging or summarization."""
    def __init__(self,max_bytes=64*1024*1024):
        self.entries=OrderedDict();self.bytes=0;self.max_bytes=max_bytes;self.lock=RLock()

    @staticmethod
    def digest(message):
        public={k:v for k,v in message.items() if k!='reasoning_content'}
        return hashlib.sha256(json.dumps(public,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def remember(self,key,message,reasoning):
        if not isinstance(reasoning,str):raise ModelError('thinking_reasoning_missing')
        size=len(reasoning.encode())
        if size>self.max_bytes:raise ModelError('thinking_protocol_memory_exceeded')
        with self.lock:
            old=self.entries.pop(key,None)
            if old:self.bytes-=old[2]
            self.entries[key]=(self.digest(message),reasoning,size);self.bytes+=size
            while self.bytes>self.max_bytes:
                _,entry=self.entries.popitem(last=False);self.bytes-=entry[2]

    def attach(self,key,message,*,server_answer=False):
        with self.lock:
            entry=self.entries.get(key)
            if entry is None:
                if server_answer:return dict(message,reasoning_content='')
                raise ModelError('thinking_history_unavailable')
            if entry[0]!=self.digest(message):raise ModelError('thinking_history_mismatch')
            self.entries.move_to_end(key)
            return dict(message,reasoning_content=entry[1])


_thinking_protocol_memory=ThinkingProtocolMemory()


@dataclass
class ModelReply:
    message:dict
    usage:dict
    model:str


def parse_response(data):
    import re
    from .projection import safe_text
    try:
        choice=data['choices'][0];m=choice['message'];finish=choice['finish_reason']
        if m.get('refusal'):raise ModelError('model_refused')
        if m.get('role')!='assistant' or finish not in {'stop','tool_calls'}:raise ValueError()
        calls=m.get('tool_calls') or []
        if not isinstance(calls,list) or len(calls)>12:raise ValueError()
        ids=[];safe=[]
        for call in calls:
            if call.get('type')!='function' or not isinstance(call.get('id'),str) or not 1<=len(call['id'])<=200:raise ValueError()
            f=call['function']
            if not isinstance(f.get('name'),str) or not isinstance(f.get('arguments'),str) or len(f['arguments'].encode())>16000:raise ValueError()
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',call['id']) or safe_text(call['id'])!=call['id']:raise ValueError()
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',f['name']):raise ValueError()
            ids.append(call['id']);safe.append({'id':call['id'],'type':'function','function':{'name':f['name'],'arguments':f['arguments']}})
        if len(set(ids))!=len(ids):raise ValueError()
        content=m.get('content') or ''
        if not isinstance(content,str) or len(content.encode())>24000 or '<think>' in content:raise ValueError()
        message=dict(role='assistant',content=content)
        if safe:message['tool_calls']=safe
        # Opaque provider field: memory-only, never parsed as instructions.
        if isinstance(m.get('reasoning_content'),str):message['reasoning_content']=m['reasoning_content']
        return ModelReply(message,data.get('usage') or {},str(data.get('model','unknown')))
    except (KeyError,IndexError,TypeError,ValueError):raise ModelError('model_protocol_invalid') from None


class DeepSeek:
    def __init__(self,budget=None):
        self.budget=budget or Budget();self.conversation=None;self.streaming=False

    def bind_conversation(self,cid):self.conversation=cid

    def bind_turn(self,turn_id,generation):self.turn_generation=(turn_id,generation)

    def validate_pending(self,turn_id,slot,message):
        from .protocol import attach
        attach(self.conversation,turn_id,slot,message)

    def retain_protocol(self,turn_id,slot,public_message,model_message):
        from .protocol import retain
        generation=getattr(self,'turn_generation',(None,None))[1]
        retain(self.conversation,turn_id,slot,public_message,model_message.get('reasoning_content'),generation=generation)

    def protocol_messages(self,messages,selected_history,turn_id,exchanges):
        # The existing assembler already chose complete public turns. Attach
        # provider-only fields AFTER selection, without changing those choices.
        from .protocol import attach
        result=list(messages);cursor=1
        for turn in selected_history:
            cursor+=1  # user message
            for index,message in enumerate(turn['exchanges']):
                if result[cursor]!=message:raise ModelError('thinking_history_mismatch')
                if message['role']=='assistant':result[cursor]=attach(self.conversation,turn['id'],index,message)
                cursor+=1
            if turn['answer']:
                from .context import answer_message
                message=answer_message(turn)
                if result[cursor]!=message:raise ModelError('thinking_history_mismatch')
                if message['role']=='assistant':result[cursor]=attach(self.conversation,turn['id'],'answer',message)
                cursor+=1
        if exchanges:
            start=len(result)-len(exchanges)
            for index,message in enumerate(exchanges):
                if result[start+index]!=message:raise ModelError('thinking_history_mismatch')
                if message['role']=='assistant':result[start+index]=attach(self.conversation,turn_id,index,message)
        return result

    def history_view(self,turn):
        """Expired old transport becomes a labelled server history, never fake CoT."""
        from .protocol import attach
        try:
            for index,m in enumerate(turn['exchanges']):
                if m['role']=='assistant':attach(self.conversation,turn['id'],index,m)
            from .context import answer_message
            if turn.get('answer') and answer_message(turn)['role']=='assistant':attach(self.conversation,turn['id'],'answer',answer_message(turn))
            return turn
        except ModelError as exc:
            if str(exc) not in {'thinking_history_expired','thinking_history_unavailable'}:raise
            from .projection import safe_text
            refs=[dict(turn_id=turn['id'],call_id=c['id'],tool=c['function']['name']) for m in turn['exchanges'] for c in m.get('tool_calls',[])]
            answer=safe_text(turn.get('answer',''))+'\n历史工具协议已到期或缺失，未重放。可按原turn/call回读：'+json.dumps(refs,ensure_ascii=False)
            return dict(turn,exchanges=[],answer=answer,usage=dict(turn.get('usage') or {},r4_server_ack=True),protocol_history_degraded=str(exc))

    def complete(self,messages,tools,timeout):
        if os.environ.get('BA_LLM_DISABLED')=='1' or os.environ.get('BA091_LLM_ACCESS')=='disabled':raise ModelError('model_disabled')
        key=os.getenv('LLM_API_KEY');base=os.getenv('LLM_BASE_URL','').rstrip('/');model=os.getenv('LLM_MODEL')
        if not key or not model or base not in {'https://api.deepseek.com','https://api.deepseek.com/v1'}:raise ModelError('model_configuration_invalid')
        if any(m.get('role')=='assistant' and not isinstance(m.get('reasoning_content'),str) for m in messages):raise ModelError('thinking_history_unavailable')
        review=bool(messages and messages[0].get('role')=='system' and messages[0].get('content','').startswith(('banking-intent-review-1','你是最终答复语义核对器')))
        payload=dict(model=model,messages=messages,tools=tools,tool_choice='auto',stream=False,max_tokens=1536 if review else 16384,
                     thinking={'type':'disabled' if review else 'enabled'})
        if not review:payload['reasoning_effort']='high'
        streaming=self.streaming and not review
        if streaming:payload.update(stream=True,stream_options={'include_usage':True})
        reservation=self.budget.reserve(payload);start=time.monotonic();data=None;error=None
        try:
            request_timeout=min(120,max(.1,timeout))
            with httpx.Client(timeout=httpx.Timeout(request_timeout,connect=min(10,request_timeout)),trust_env=False) as client:
                if streaming:
                    # Assemble only in private memory; settle the same existing
                    # paid reservation, even if the stream fails partway.
                    data={'model':model,'choices':[]}
                    with client.stream('POST',base+'/chat/completions',headers={'Authorization':'Bearer '+key},json=payload) as response:
                        if response.status_code!=200:raise ModelError('model_http_'+str(response.status_code))
                        message={'role':'assistant','content':'','reasoning_content':''};calls={};finish=None;size=0;done=False
                        for line in response.iter_lines():
                            if time.monotonic()-start>request_timeout:raise ModelError('model_stream_timeout')
                            size+=len(line.encode())
                            if size>1_000_000:raise ModelError('model_response_too_large')
                            if not line.startswith('data:'):continue
                            raw=line[5:].strip()
                            if raw=='[DONE]':done=True;break
                            chunk=json.loads(raw)
                            if chunk.get('error'):raise ModelError('model_stream_failure')
                            if chunk.get('usage'):data['usage']=chunk['usage']
                            data['model']=chunk.get('model') or data['model']
                            for choice in chunk.get('choices',[]):
                                if choice.get('index',0)!=0:raise ModelError('model_protocol_invalid')
                                delta=choice.get('delta') or {}
                                if delta.get('role') not in {None,'assistant'} or delta.get('refusal'):raise ModelError('model_refused')
                                for field in ('content','reasoning_content'):
                                    if delta.get(field) is not None:
                                        if not isinstance(delta[field],str):raise ModelError('model_protocol_invalid')
                                        message[field]+=delta[field]
                                for part in delta.get('tool_calls') or []:
                                    index=part.get('index')
                                    if not isinstance(index,int) or not 0<=index<12:raise ModelError('model_protocol_invalid')
                                    call=calls.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                                    if part.get('type') not in {None,'function'}:raise ModelError('model_protocol_invalid')
                                    if part.get('id'):call['id']+=part['id']
                                    for field in ('name','arguments'):
                                        if (part.get('function') or {}).get(field):call['function'][field]+=part['function'][field]
                                if choice.get('finish_reason') is not None:finish=choice['finish_reason']
                        if not done or finish not in {'stop','tool_calls'}:raise ModelError('model_stream_incomplete')
                        if calls:message['tool_calls']=[calls[i] for i in sorted(calls)]
                        data['choices']=[{'message':message,'finish_reason':finish}]
                else:
                    response=client.post(base+'/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
                    if response.status_code!=200:raise ModelError('model_http_'+str(response.status_code))
                    if len(response.content)>1_000_000:raise ModelError('model_response_too_large')
                    data=response.json()
            reply=parse_response(data)
            if not review and not isinstance(reply.message.get('reasoning_content'),str):raise ModelError('thinking_reasoning_missing')
            return reply
        except httpx.TimeoutException:
            error='model_timeout';raise ModelError(error) from None
        except httpx.HTTPError:
            error='model_network_failure';raise ModelError(error) from None
        except (ValueError,TypeError,KeyError):
            error='model_protocol_invalid';raise ModelError(error) from None
        except ModelError as exc:error=str(exc);raise
        finally:self.budget.settle(reservation,data,time.monotonic()-start,error)
