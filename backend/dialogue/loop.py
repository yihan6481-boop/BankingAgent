"""Bounded LLM dialogue. Business state changes only through the tool bridge."""
from dataclasses import dataclass
import json
import time
import os
import math
from pydantic import ValidationError
from lifecycle.store import TaskError
from foundation.contracts import feedback, classify
from storage.pg02 import verified_session,principal_scope
from security.service import get_security_services
from .provider import DeepSeek,ModelError
from .budget import BudgetError
from .context import assemble,ContextError,MODEL_WINDOW_TURNS
from .projection import project,safe_text,user_text
from .store import DialogueStore
from .tools import catalog,execute
from .links import tool_scope
from . import answer_contract as answers


@dataclass(frozen=True)
class Limits:
    requests:int=8
    tools:int=12
    seconds:float=240
    corrections:int=2
    context_bytes:int=64000

    @classmethod
    def configured(cls):
        try:
            value=cls(requests=int(os.getenv('BA_R2_MAX_REQUESTS','8')),tools=int(os.getenv('BA_R2_MAX_TOOLS','12')),
                      seconds=float(os.getenv('BA_R2_MAX_SECONDS','240')),corrections=int(os.getenv('BA_R2_MAX_CORRECTIONS','2')),
                      context_bytes=int(os.getenv('BA_R2_CONTEXT_BYTES','64000')))
            if not (1<=value.requests<=8 and 1<=value.tools<=12 and math.isfinite(value.seconds) and 0<value.seconds<=240
                    and 0<=value.corrections<=2 and 24000<=value.context_bytes<=128000):raise ValueError()
            return value
        except ValueError:raise TaskError('invalid_r2_limits',status=503) from None


ERRORS={
 'model_disabled':'AI调用在服务端被禁用；请检查是否以experience模式启动。手动业务仍可使用。',
 'model_http_401':'模型服务商未接受现有凭据（HTTP 401）；需要核对API密钥。未自动重试。',
 'model_http_402':'模型服务商余额不足（HTTP 402）；需要在服务商账户充值。未重置本地账本。',
 'model_http_429':'模型服务商限流（HTTP 429）；请稍后重试。本地额度和服务商限流分别统计。',
 'answer_contract_invalid':'回复结构连续未通过校验，本轮未完成自然答复。已执行工具的结果与记忆操作状态仍保留，请按原引用核实；不会重放保存。',
 'answer_correction_service_failure':'答复纠正阶段发生服务异常，未完成回答。原校验失败与异常类型已分别记录；已核实工具结果保留，未重新执行业务。',
 'answer_evidence_unavailable':'本轮答复证据暂时无法核实，未发布未经核实的内容；请读取原轮次或任务状态，未重新执行业务。',
 'memory_changed_during_answer':'长期资料在本轮生成期间发生更正、撤回或删除，已停止展示可能过期的回答。银行工具结果仍保留，请按当前状态继续核实。',
 'paid_budget_exhausted':'本次模型预算不足以预留下一请求，已停止付费调用。已保存的工具结果和任务仍保留，请核对当前状态；系统不会切换账本或自动重试。',
 'budget_configuration_invalid':'模型预算配置无效，未发出付费请求。请检查服务端预算配置；已保存业务状态不受影响。',
 'budget_configuration_mismatch':'模型预算批次与账本不匹配，已拒绝付费调用。请检查服务端配置；没有重置账本。',
 'generation_cancelled':'已停止等待。已完成的操作不会因此撤销，请查看下方真实状态。',
 'loop_budget_exhausted':'本轮处理已达到限制。已保存的草稿和工具结果仍保留，请查看当前目标状态。',
 'required_context_exceeds_budget':'当前消息和必要业务状态超出输入预算，未丢弃条件继续操作。请分开处理目标或缩小查询范围。',
 'no_progress':'本轮出现重复且没有进展的调用，已停止。已保存的目标和真实结果仍保留。',
 'parameter_corrections_exhausted':'工具参数连续校验失败，已停止本轮。已保存的字段仍保留，请查看当前目标。',
 'semantic_review_unavailable':'答复核对服务暂时不可用，本轮自然答复未完成。已取得的有效业务结果仍保留；请核实原轮次，不必重新办理。',
 'semantic_review_protocol_invalid':'答复核对服务返回了无法解析的格式，本轮自然答复未完成。已取得的有效结果仍保留。',
 'semantic_review_tool_call_denied':'答复核对服务返回了不允许的工具调用，本轮自然答复未完成。未执行该调用。',
 'semantic_review_budget_exhausted':'本轮答复核对次数已用完，未发布未经核对的说明。已取得的有效结果仍保留。',
 'answer_semantically_unverified':'这次生成的答复未通过事实与语义核对，已停止发布。已取得的有效业务结果仍保留，可以核实原轮次。',
 'model_refused':'模型服务拒绝生成本轮答复。已保存的业务结果仍保留。',
 'model_timeout':'模型回复等待超时，本轮答复未完成。已保存的业务结果仍保留，请核实原轮次。',
 'model_protocol_invalid':'模型服务返回的回答协议无法解析，本轮答复未完成。已保存的业务结果仍保留。',
 'model_stream_incomplete':'模型回复连接未完整结束，本轮答复未完成。已保存的结果仍保留，请核实原轮次。',
 'model_stream_timeout':'模型回复接收超时，本轮答复未完成。已保存的结果仍保留，请核实原轮次。',
}
ERRORS.update(intent_review_context_exceeded='本次计划核对所需信息过多，尚未执行该计划。可以明确当前目标后再试。',
    intent_review_unavailable='计划核对服务暂时不可用，本次计划未执行，请稍后再试。',
    intent_review_protocol_invalid='计划核对服务没有返回有效结果，本次计划未执行。',
    intent_review_budget_exhausted='本轮已预留答复收尾，本次额外计划未执行。',
    thinking_history_expired='旧工具会话协议已到期，请按原引用核实；不会重放业务。')
ERRORS.update(answer_contract_invalid='这次答复格式未能整理好，可重新组织答复。已核实结果保留。',
    answer_semantically_unverified='这次答复未通过核对，可基于本轮结果重新组织。',
    semantic_review_unavailable='答复核对服务暂时不可用，可稍后重新组织答复。已核实结果保留。')


MEMORY_WRITES=frozenset({'memory_save','memory_save_business','memory_replace','memory_delete','memory_exclude_turn'})


class DialogueLoop:
    def __init__(self,provider=None,limits=None):
        self.provider=provider or DeepSeek();self.limits=limits or Limits.configured();self.store=DialogueStore()

    def retain_protocol(self,tid,slot,public_message,model_message):
        retain=getattr(self.provider,'retain_protocol',None)
        if callable(retain):retain(tid,slot,public_message,model_message)

    def run(self,p,sid,cid,client_id,text,*,resume_turn=None,answer_request_id=None,authorization_revision=None):
        from execution.sessions import session_lock
        from security.support import assert_new_automation
        with session_lock(sid):
            assert_new_automation(sid)
        original_limits=self.limits
        try:return self._run(p,sid,cid,client_id,text,resume_turn=resume_turn,answer_request_id=answer_request_id,authorization_revision=authorization_revision)
        finally:self.limits=original_limits

    def _run(self,p,sid,cid,client_id,text,*,resume_turn=None,answer_request_id=None,authorization_revision=None):
        text=user_text(text)
        answer_only=answer_request_id is not None
        turn,fresh=(self.store.begin_answer_retry(p,cid,resume_turn,answer_request_id,self.limits.seconds+10,**({'authorization_revision':authorization_revision} if authorization_revision is not None else {})) if answer_only else self.store.resume(p,cid,resume_turn,self.limits.seconds+10) if resume_turn else
                    self.store.begin(p,cid,client_id,text,self.limits.seconds+10))
        if not fresh:return turn
        if answer_only:
            from dataclasses import replace
            self.limits=replace(self.limits,requests=2,corrections=0)
        text=turn['user_text'];tid=turn['id'];start=time.monotonic()
        exchanges=[] if answer_only else list(turn['exchanges']);seen=set();corrections=0;answer_correction=answer_only;answer_failures=0;facts=[]
        current_text=text;current_history=[];review_history_input=[];last_candidate=None;stop_reason=None
        usage=dict(requests=0,tool_calls=0,prompt_tokens=0,completion_tokens=0,models=[])
        usage.update(turn.get('usage') or {})
        def complete(messages,current_tools,timeout,stage):
            began=time.monotonic();kind=None
            try:return self.provider.complete(messages,current_tools,timeout)
            except Exception as exc:kind=type(exc).__name__;raise
            finally:usage.setdefault('model_stages',[]).append(dict(stage=stage,seconds=round(time.monotonic()-began,3),exception_kind=kind))
        saved_calls=self.store.calls(tid);calls=len(saved_calls)
        corrections=sum((c.get('result') or {}).get('status')=='invalid_parameters' for c in saved_calls)
        # Recovery keeps the same no-progress boundary. A newly generated call
        # ID cannot replay a mutation already consumed in this original turn.
        refresh_reads={'task_read','task_verify','draft_read','draft_validate','context_snapshot','draft_list','task_list',
                       'payment_document_list','payment_document_read','payment_resolution_read','payment_preparation_read'}
        for c in saved_calls:
            if c['name'] not in refresh_reads:
                seen.add(json.dumps([c['name'],c['arguments'],None],sort_keys=True,ensure_ascii=False))
        bind=getattr(self.provider,'bind_conversation',None)
        if callable(bind):bind(cid)
        bind_turn=getattr(self.provider,'bind_turn',None)
        if callable(bind_turn):bind_turn(tid,turn.get('usage',{}).get('lease_generation'))
        tools=catalog()
        memory_epoch=usage.get('r4_memory_epoch')
        memory_context=({'availability':'unknown','items':[],'reason':'recovered_turn_use_explicit_memory_tool',
            'epoch':usage.get('r4_memory_epoch')} if usage.get('r4_prefetch_used') else None)
        if answer_only:memory_context=dict(availability='unknown',items=[],reason='answer_only_existing_evidence')
        current_text=text;current_history=[]
        def check():
            if time.monotonic()-start>=self.limits.seconds:raise TaskError('loop_budget_exhausted')
            if not self.store.active(tid):raise TaskError('generation_cancelled')
            context,_=verified_session(sid,get_security_services().now_provider())
            if context.principal_id!=p.principal_id or context.user_id!=p.user_id:raise TaskError('session_principal_mismatch')
            return context

        def dispatch(assistant,invalid_ids,recovering=False):
            nonlocal calls,corrections,stop_reason
            results=[];stops=[]
            saved={c['call_id']:c for c in self.store.calls(tid)}
            memory_names={f['function']['name'] for f in assistant['tool_calls'] if f['function']['name'].startswith('memory_')}
            def model_result(value,call_id):
                from .projection import bounded_result
                bounded=bounded_result(value)
                if bounded.get('code')=='result_exceeds_context':
                    bounded['data'].update(turn_id=tid,call_id=call_id)
                    bounded['next_actions']=['call_result_read']
                return json.dumps(bounded,ensure_ascii=False)
            for function in assistant['tool_calls']:
                context=check();call_id=function['id'];f=function['function'];name=f['name']
                args=json.loads(f['arguments'])
                prior=saved.get(call_id)
                if prior:
                    if prior['name']!=name or prior['arguments']!=args:raise ModelError('thinking_history_mismatch')
                    recovered=None
                    if prior['status']!='completed' and name in {'memory_save','memory_replace','memory_delete'}:
                        from recall_bridge.dialogue import reconcile_original
                        with principal_scope(context):recovered=reconcile_original(p,tid,name,args)
                        if recovered:self.store.finish_call(tid,call_id,project(recovered))
                    result=recovered or (prior['result'] if prior['status']=='completed' and prior['result'] is not None else feedback(
                        'commit_unknown','tool_outcome_unknown',data={'verification_reference':prior.get('preparation_reference')},
                        next_actions=['task_verify','draft_read','verify original call; do not replay']))
                    results.append({'role':'tool','tool_call_id':call_id,'content':model_result(result,call_id)})
                    continue
                from recall_bridge.context_guard import progress
                with principal_scope(context):memory_progress=progress(p,name,args)
                fingerprint=json.dumps([name,args,memory_progress if name.startswith('memory_') else usage.get('context',{}).get('state_revision_digest') if name in {'task_read','task_verify','draft_read','draft_validate','context_snapshot','draft_list','task_list','payment_document_list','payment_document_read','payment_resolution_read','payment_preparation_read'} else None],sort_keys=True,ensure_ascii=False)
                if stop_reason or fingerprint in seen:
                    stop_reason='no_progress'
                    result=feedback('conflict','no_progress');stops.append(True)
                    usage.setdefault('stopped_calls',[]).append(dict(call_id=call_id,name=name,reason=stop_reason))
                else:
                    seen.add(fingerprint);self.store.progress(tid,'正在查询或准备业务')
                    self.store.start_call(tid,call_id,name,args)
                    try:
                        if name in MEMORY_WRITES and memory_context is not None and memory_context.get('reason')=='memory_metadata_unavailable':
                            # A hidden tool can still be requested by a model; enforce at dispatch too.
                            usage['r4_hold_automatic']=True
                            result=feedback('service_failure','memory_state_unverified_for_this_turn',
                                next_actions=['read_original_operation_if_any'])
                        elif name.startswith('memory_') and {'memory_save','memory_exclude_turn'}<=memory_names:
                            usage['r4_hold_automatic']=True
                            from recall_bridge.state import Store
                            with principal_scope(context):Store().exclude(p,tid)
                            result=feedback('ambiguous','conflicting_memory_scope_clarification_required')
                        elif name.startswith('memory_') and usage.get('memory_calls',0)>=4:
                            result=feedback('business_condition','memory_turn_limit_reached',next_actions=['answer_from_known_operation_status_no_more_memory_calls'])
                        elif call_id in invalid_ids:result=feedback('invalid_parameters','invalid_json_arguments')
                        else:
                            if name.startswith('memory_'):usage['memory_calls']=usage.get('memory_calls',0)+1
                            from recall_bridge.limits import interaction
                            with principal_scope(context),tool_scope(cid,tid,call_id),interaction(usage):
                                from . import intent_review
                                verdict=None;blocked=False;review_error=None
                                if intent_review.required(name):
                                    from .tools import DESCRIPTIONS
                                    if name in DESCRIPTIONS:DESCRIPTIONS[name][0].model_validate(args)
                                    from .literal_binding import contradiction
                                    literal_error=contradiction(current_text,name,args,p)
                                    if literal_error:raise TaskError(literal_error)
                                    try:
                                        if usage['requests']>=self.limits.requests-2:raise ValueError('intent_review_budget_exhausted')
                                        review_messages=intent_review.payload(p,sid,cid,current_text,review_history_input,name,args)
                                        usage['requests']+=1;self.store.request_started(tid,usage)
                                        reviewed=complete(review_messages,[],max(.1,min(30,self.limits.seconds-(time.monotonic()-start))),'intent_review')
                                        usage['models'].append(reviewed.model)
                                        for key in ['prompt_tokens','completion_tokens']:usage[key]+=reviewed.usage.get(key,0)
                                        check();verdict=intent_review.interpret(reviewed)
                                        from assistance.risk import recent_context
                                        if not set(verdict.risk_source_turns)<={r['source_turn'] for r in recent_context(sid)}:raise ValueError('risk_source_not_available')
                                    except ValidationError:blocked=True;review_error='intent_review_protocol_invalid'
                                    except (ModelError,BudgetError) as exc:
                                        blocked=True;review_error='intent_review_unavailable';usage['intent_review_dependency_code']=str(exc)
                                    except ValueError as exc:
                                        blocked=True;review_error='intent_review_context_exceeded' if str(exc)=='semantic_context_exceeds_budget' else 'intent_review_budget_exhausted' if str(exc)=='intent_review_budget_exhausted' else 'intent_review_protocol_invalid'
                                    if verdict is None or verdict.decision!='allow':blocked=True
                                    usage.setdefault('intent_reviews',[]).append(dict(call_id=call_id,decision=verdict.decision if verdict else 'unavailable',code=review_error or (verdict.code if verdict else 'semantic_intent_uncertain')))
                                if blocked:result=intent_review.rejection(verdict,error=review_error)
                                else:
                                    result=execute(name,args,p,sid)
                                    if verdict:
                                        from assistance.risk import bind_review
                                        bind_review(p,sid,cid,tid,name,args,result,verdict)
                    except ValidationError as exc:
                        result=feedback('invalid_parameters','schema_invalid',data={'field_errors':[{'field':'.'.join(map(str,e['loc'])),'code':e['type']} for e in exc.errors()]})
                    except TaskError as exc:result=feedback(classify(exc.code),exc.code)
                    except ValueError as exc:
                        result=feedback('invalid_parameters',getattr(exc,'code','invalid_plan'))
                    except Exception:
                        result=feedback('commit_unknown','tool_outcome_unknown',next_actions=['draft_read','task_verify'])
                    result=project(result);self.store.finish_call(tid,call_id,result)
                calls+=1;usage['tool_calls']=calls
                if result['status']=='invalid_parameters':corrections+=1
                results.append({'role':'tool','tool_call_id':call_id,'content':model_result(result,call_id)})
            exchanges.extend([assistant,*results]);self.store.exchange(tid,exchanges)
            # Stop dispatching new calls, retain protocol pairing, and use the
            # existing two-request answer/review reservation. Never retry tools.
            if any(stops):usage['termination_reason']='no_progress'
            if corrections>self.limits.corrections:raise TaskError('parameter_corrections_exhausted')
        def finish_answer(answer,selected=None,parts=None,*,error=None):
            nonlocal facts
            from recall_bridge.context_guard import valid
            facts=[f for f in facts if f.required] if selected is None else [next(f for f in facts if f.reference==ref) for ref in selected]
            if any(not valid(p,f.sources,profile=f.profile) for f in facts) or any(not valid(p,part.get('sources',{}),profile=part.get('profile',False)) for part in (parts or [])):
                kept=[f for f in facts if valid(p,f.sources,profile=f.profile)]
                if not any(f.reference.startswith('bf_') for f in kept):raise TaskError('memory_changed_during_answer')
                facts=kept;answer=answers.fallback(facts)+'\n长期资料发生变化，相关部分无法核实；独立银行结果已保留。'
                parts=[dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in facts]
                parts.append(dict(text='长期资料发生变化，相关部分无法核实；独立银行结果已保留。',sources={},profile=False,provenance_checked=True))
            usage.update(answer_contract='dialogue-facts-1',r4_server_ack=True,
                answer_references=[f.reference for f in facts],
                r4_dependencies={**{ref:version for f in facts for ref,version in f.sources.items()},**{ref:version for part in (parts or []) for ref,version in part.get('sources',{}).items()}},
                r4_profile_used=any(f.profile for f in facts) or any(part.get('profile',False) for part in (parts or [])))
            if parts or facts:usage['r4_answer_parts']=parts or [dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in facts]
            else:usage.pop('r4_answer_parts',None)
            if memory_epoch is not None:
                from .store import transaction
                from recall_bridge.state import Store
                with transaction() as conn:
                    Store().space(p,conn,True)
                    if any(not valid(p,f.sources,conn,profile=f.profile) for f in facts) or any(not valid(p,part.get('sources',{}),conn,profile=part.get('profile',False)) for part in (parts or [])):raise TaskError('memory_changed_during_answer')
                    self.store.finish(tid,'failed' if error else 'completed',answer,error,usage=usage)
            else:self.store.finish(tid,'failed' if error else 'completed',answer,error,usage=usage)
        try:
            if turn.get('pending_exchange') and not answer_only:
                pending=turn['pending_exchange'];assistant=pending['assistant']
                # Validate the exact encrypted protocol before dispatching even an
                # unstarted call. Known/unknown calls are consumed, never replayed.
                validate=getattr(self.provider,'validate_pending',None)
                if callable(validate):validate(tid,len(exchanges),assistant)
                dispatch(assistant,pending['invalid_ids'],True)
            history=self.store.recent(p,cid,limit=MODEL_WINDOW_TURNS)
            if answer_only:history=self.store.history(p,cid,limit=MODEL_WINDOW_TURNS,before=turn['number']+1)[0]
            for old in history:
                if old['id']!=tid:old['recovery_calls']=[c for c in self.store.calls(old['id']) if c['status'] in {'started','unknown'} or not old['exchanges']]
            memory_epoch=usage.get('r4_memory_epoch');memory_phase_reset=usage.get('r4_context_reset',False)
            for _ in range(max(0,self.limits.requests-usage['requests'])):
                if usage['requests']>=self.limits.requests:raise TaskError('loop_budget_exhausted')
                check();self.store.progress(tid,'正在理解与组织回复')
                from recall_bridge.dialogue import safe_before_model,continuation,after_turn
                from recall_bridge.limits import interaction
                usage['failure_phase']='memory_context'
                with interaction(usage):current_history,new_memory,epoch=safe_before_model(p,history,text,recall_context=memory_context)
                memory_unavailable=new_memory is not None and new_memory.get('reason')=='memory_metadata_unavailable'
                if memory_unavailable:
                    memory_phase_reset=True
                    usage['r4_hold_automatic']=True
                from recall_bridge.context_guard import valid,references
                if not valid(p,usage.get('r4_observed',{})):memory_phase_reset=True
                usage.setdefault('r4_observed',{}).update(references(new_memory))
                for saved in self.store.calls(tid):
                    if saved['name'].startswith('memory_'):usage['r4_observed'].update(references(saved.get('result')))
                memory_phase_reset=memory_phase_reset or not valid(p,usage.get('r4_observed',{}))
                if memory_phase_reset:
                    usage['r4_observed']={ref:version for ref,version in usage.get('r4_observed',{}).items() if valid(p,{ref:version})}
                if memory_phase_reset:usage['r4_context_reset']=True
                if new_memory is not None:usage['r4_prefetch_used']=True
                memory_context=new_memory;memory_epoch=epoch
                if epoch is not None:usage['r4_memory_epoch']=epoch
                request_exchanges=[] if memory_phase_reset else exchanges
                fresh_results=continuation(p,tid,memory_unavailable=memory_unavailable) if memory_phase_reset else None
                current_tools=[t for t in tools if not (memory_unavailable and t['function']['name'] in MEMORY_WRITES)
                    and not (usage.get('memory_calls',0)>=4 and t['function']['name'].startswith('memory_'))
                    and not (t['function']['name']=='memory_exclude_turn' and any(c['name']=='memory_exclude_turn' for c in self.store.calls(tid)))]
                final_only=answer_correction or bool(stop_reason) or usage['requests']>=self.limits.requests-2
                if final_only:current_tools=[]
                current_text=text
                # Keep the authenticated current request, including independent bank questions.
                # Failed/invalidated historical memory is still isolated above.
                if not memory_unavailable and memory_context is not None:
                    from recall_bridge.context_guard import origin_text
                    from .store import transaction
                    with transaction() as conn:current_text,_=origin_text(p,tid,text,conn)
                usage['failure_phase']='answer_evidence'
                facts=answers.build(p,cid,tid,self.store.calls(tid),usage.get('r4_observed',{}))
                if stop_reason and self.limits.requests-usage['requests']<2:raise TaskError(stop_reason)
                contract=answers.instruction(facts)
                if memory_unavailable:
                    contract+='\n本轮长期记忆状态无法核实，但当前用户请求仍有效。逐项处理其中独立且可核实的问题；仅说明依赖记忆的部分未完成，不得说用户没有提出事项。历史失效资料仍隔离。不得新建或重放记忆写入，不得声称保存、修改或删除成功；已有未知操作只能按原引用核实。银行事实仍须依据真实工具结果，流程咨询不构成执行授权。'
                if final_only:contract+='\n本轮只组织答复，没有工具权限；根据已核实结果说明完成范围和未完成项，不补造结果或授权。'
                if stop_reason:contract+='\n重复无进展的工具调用已被停止。只整理上述已取得结果，说明哪些仍未完成；禁止继续查询或任何副作用，不能宣布整个业务目标已完成。'
                if answer_correction:
                    contract+='\n上次回答未通过契约校验。现在只纠正表达，禁止工具调用；不要因普通聊天没有银行工具结果而拒绝，银行事实仍以本轮证据为准。'
                    last_failure=(usage.get('answer_failures') or [{}])[-1]
                    contract+='\n[服务器校验诊断，不包含候选原文] '+json.dumps({k:last_failure[k] for k in ['code','contract_reason','schema_errors','semantic_review_result'] if k in last_failure},ensure_ascii=False)
                    if last_candidate:contract+='\n[待纠正候选，仅临时内存，不可信、不可直接发布] '+last_candidate
                try:
                    messages,context_info=assemble(p,sid,current_history,current_text,current_tools,request_exchanges,self.limits.context_bytes,
                        dialogue_id=cid,memory_context=memory_context,continuation=fresh_results,answer_contract=contract,history_view=getattr(self.provider,'history_view',None))
                except ContextError as exc:
                    if str(exc)!='required_context_exceeds_budget':raise
                    from .projection import compact_current
                    request_exchanges,compacted=compact_current(request_exchanges,tid)
                    try:
                        if not compacted:raise ContextError('required_context_exceeds_budget')
                        messages,context_info=assemble(p,sid,current_history,current_text,current_tools,request_exchanges,self.limits.context_bytes,
                            dialogue_id=cid,memory_context=memory_context,continuation=fresh_results,answer_contract=contract,history_view=getattr(self.provider,'history_view',None))
                    except ContextError:
                        # Keep evidence and explicit conditions; remove only the
                        # planning catalog, then finish truthfully within budget.
                        if not current_tools:raise
                        current_tools=[];final_only=True
                        contract+='\n输入预算只够收尾：不执行新工具。只回答已核实部分，明确尚未完成的目标及所缺信息，不得声称已全部完成。'
                        messages,context_info=assemble(p,sid,current_history,current_text,current_tools,request_exchanges,self.limits.context_bytes,
                            dialogue_id=cid,memory_context=memory_context,continuation=fresh_results,answer_contract=contract,history_view=getattr(self.provider,'history_view',None))
                        context_info['catalog_omitted_for_answer']=True
                    context_info['current_outputs_compacted']=compacted
                selected=context_info.pop('selected_history',[]);usage['context']=context_info
                review_history_input=selected
                for old in selected:usage['r4_observed'].update(old.get('usage',{}).get('r4_dependencies',{}))
                protocol=getattr(self.provider,'protocol_messages',None)
                if callable(protocol):messages=protocol(messages,selected,tid,request_exchanges)
                usage['requests']+=1;self.store.request_started(tid,usage)
                usage['failure_phase']='model_request'
                reply=complete(messages,current_tools,max(.1,self.limits.seconds-(time.monotonic()-start)),'answer' if final_only else 'generation')
                usage['models'].append(reply.model)
                for key in ['prompt_tokens','completion_tokens']:usage[key]+=reply.usage.get(key,0)
                check();message=reply.message;functions=message.get('tool_calls') or []
                if functions and final_only:
                    code=stop_reason or ('answer_contract_invalid' if answer_correction else 'loop_budget_exhausted')
                    kept=answers.unique_facts([f for f in facts if f.required]);notice=ERRORS[code]
                    parts=[dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in kept]+[dict(text=notice,sources={},profile=False,provenance_checked=True)]
                    usage['answer_fallback']='classified_failure_with_verified_evidence'
                    finish_answer('\n\n'.join([f.compact for f in kept]+[notice]),None,parts,error=code);break
                if functions and not valid(p,references(memory_context)):raise TaskError('memory_changed_during_answer')
                if not functions:
                    candidate=message.get('content') or ''
                    selected=[];parts=[]
                    def verify_prose(answer):
                        from .prose import review_messages,approved
                        if usage.get('semantic_checks',0)>=2 or usage['requests']>=self.limits.requests:
                            raise ValueError('semantic_review_budget_exhausted')
                        check()
                        usage['semantic_checks']=usage.get('semantic_checks',0)+1
                        usage['requests']+=1
                        # Durable reservation also bounds resumed generations.
                        self.store.request_started(tid,usage)
                        try:
                            review=complete(review_messages(answer,facts,current_text,
                                profile_allowed=bool(memory_context and memory_context.get('profile_purpose')),dialogue_history=review_history_input),
                                [],max(.1,min(30,self.limits.seconds-(time.monotonic()-start))),'final_review')
                            usage['models'].append(review.model)
                            for key in ['prompt_tokens','completion_tokens']:usage[key]+=review.usage.get(key,0)
                            check()
                            verdict=approved(review)
                            usage['semantic_review_result']=dict(allow=verdict.allow,reason=verdict.reason)
                        except (ModelError,BudgetError) as exc:
                            usage['semantic_review']='unavailable'
                            usage['semantic_review_error']=str(exc)
                            raise ValueError('semantic_review_unavailable') from None
                        except ValueError as exc:
                            usage['semantic_review']='rejected' if str(exc)=='answer_semantically_unverified' else 'invalid_protocol'
                            usage['semantic_review_error']=str(exc)
                            if str(exc)=='answer_semantically_unverified':usage['semantic_review_result']=dict(allow=False,reason=getattr(exc,'reason','unspecified'))
                            raise
                        except TaskError:
                            raise
                        except Exception as exc:
                            # Reviewer transport/protocol failures never lose
                            # already verified business results or retry tools.
                            usage['semantic_review']='unavailable'
                            usage['semantic_review_exception_kind']=type(exc).__name__
                            raise ValueError('semantic_review_unavailable') from None
                        usage['semantic_review']='approved'
                    usage['failure_phase']='answer_render'
                    try:answer=answers.render(candidate,facts,selected=selected,parts=parts,request_text=text,verify_prose=verify_prose)
                    except (ValueError,ValidationError) as exc:
                        last_candidate=safe_text(candidate) if len(candidate)<=4000 else None
                        answer_failures+=1;usage['answer_rejections']=answer_failures
                        failure=str(exc) if str(exc) in ERRORS else 'answer_contract_invalid'
                        usage['answer_failure_code']=failure
                        reasons={'fact_reference_denied','fact_coverage_invalid','evidence_prose_required','empty_answer','prose_invalid','json_output_envelope_invalid','json_output_not_object','duplicate_json_key','json_output_not_text'}
                        usage['answer_contract_reason']='schema_invalid' if isinstance(exc,ValidationError) else 'json_decode_invalid' if isinstance(exc,json.JSONDecodeError) else str(exc) if str(exc) in reasons else failure
                        shape='json_fenced' if candidate.lstrip().lower().startswith('```json') else 'json_object' if candidate.lstrip().startswith('{') else 'plain_text' if candidate.strip() else 'empty'
                        diagnostic=dict(code=failure,contract_reason=usage['answer_contract_reason'],semantic_review=usage.get('semantic_review'),semantic_review_result=usage.get('semantic_review_result'),candidate_format=shape,candidate_sha256=__import__('hashlib').sha256(candidate.encode()).hexdigest(),candidate_content_saved=False)
                        if isinstance(exc,ValidationError):
                            allowed={'paragraphs','facts','conversation','prose','layout','facts_style','reference','form'}
                            diagnostic['schema_errors']=[dict(field='.'.join(str(v) if isinstance(v,int) or v in allowed else '<extra-field>' for v in e['loc']),code=e['type']) for e in exc.errors()]
                        usage.setdefault('answer_failures',[]).append(diagnostic)
                        if failure not in {'semantic_review_unavailable','semantic_review_budget_exhausted'} and answer_failures<=min(1,self.limits.corrections) and usage['requests']<self.limits.requests and usage.get('semantic_checks',0)<2:
                            answer_correction=True
                            continue
                        kept=answers.unique_facts([f for f in facts if f.required])
                        notice=ERRORS[failure]
                        if failure=='semantic_review_unavailable':
                            cause=usage.get('semantic_review_error')
                            reasons={'paid_budget_exhausted':'本地付费预算不足以执行答复核对','model_timeout':'答复核对服务等待超时','model_stream_timeout':'答复核对服务接收超时','model_http_401':'答复核对服务未接受现有模型凭据','model_http_402':'答复核对的模型服务商返回余额不足','model_http_429':'答复核对的模型服务商正在限流','model_network_failure':'答复核对服务连接中断','model_protocol_invalid':'答复核对服务的模型响应协议无法解析','model_disabled':'服务端已禁用答复核对所需的模型调用'}
                            if cause in reasons:notice=reasons[cause]+'，本轮自然答复未完成。已取得的有效业务结果仍保留，不会自动重新办理。'
                        answer='\n\n'.join([f.compact for f in kept]+[notice])
                        parts=[dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in kept]+[dict(text=notice,sources={},profile=False,provenance_checked=True)]
                        usage['answer_fallback']='classified_failure_with_verified_evidence'
                        finish_answer(answer,None,parts,error=failure)
                        break
                    if stop_reason:
                        notice=ERRORS[stop_reason]
                        answer+='\n\n'+notice
                        parts.append(dict(text=notice,sources={},profile=False,provenance_checked=True))
                        usage['answer_fallback']='classified_failure_with_verified_evidence'
                    finish_answer(answer,selected,parts,error=stop_reason)
                    break
                if calls+len(functions)>self.limits.tools:raise TaskError('loop_budget_exhausted')
                existing_ids={c['call_id'] for c in self.store.calls(tid)}
                if any(f['id'] in existing_ids for f in functions):raise ModelError('duplicate_call_id')
                assistant={'role':'assistant','content':'','tool_calls':[]};invalid_ids=[]
                for function in functions:
                    call_id=function['id'];f=function['function']
                    try:
                        args=json.loads(f['arguments'])
                        if not isinstance(args,dict):raise ValueError()
                        from .projection import model_arguments
                        if model_arguments(f['name'],args)!=args:raise ValueError()
                        safe_args=json.dumps(args,ensure_ascii=False)
                    except (ValueError,TypeError):safe_args='{}';invalid_ids.append(call_id)
                    assistant['tool_calls'].append({'id':call_id,'type':'function','function':{'name':f['name'],'arguments':safe_args}})
                # Durable provider data and the complete call envelope precede
                # the first business operation. Public exchanges remain paired.
                self.retain_protocol(tid,len(exchanges),assistant,message)
                self.store.stage(tid,assistant,invalid_ids,usage)
                usage['failure_phase']='tool_dispatch'
                dispatch(assistant,invalid_ids)
            else:raise TaskError('loop_budget_exhausted')
        except (TaskError,ModelError,BudgetError,ContextError) as exc:
            code=exc.code if isinstance(exc,TaskError) else str(exc)
            answer=ERRORS.get(code,'模型暂时未能完成回复。已发生的真实工具结果已保存，请查看目标和任务状态；系统未切换旧聊天。')
            if code not in {'generation_cancelled','turn_lease_expired','memory_changed_during_answer'}:
                try:
                    facts=answers.build(p,cid,tid,self.store.calls(tid),usage.get('r4_observed',{}))
                    kept=answers.unique_facts([f for f in facts if f.required])
                    parts=[dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in kept]+[dict(text=answer,sources={},profile=False,provenance_checked=True)]
                    usage['answer_fallback']='classified_failure_with_verified_evidence'
                    finish_answer('\n\n'.join([f.compact for f in kept]+[answer]),None,parts,error=code)
                except Exception as secondary:
                    usage['fallback_exception_kind']=type(secondary).__name__
                    self.store.finish(tid,'failed',answer,code,usage)
            else:self.store.finish(tid,'cancelled' if code=='generation_cancelled' else 'failed',answer,code,usage)
        except Exception as exc:
            # Type + phase only: exceptions/tracebacks may include provider data
            # or credentials. Preserve a safe diagnostic without serializing it.
            import logging
            usage['failure_kind']=type(exc).__name__
            logging.getLogger('dialogue.loop').warning('dialogue_service_failure kind=%s correction=%s',type(exc).__name__,answer_correction)
            if answer_correction:
                try:
                    usage['answer_fallback']='classified_failure_with_verified_evidence'
                    kept=answers.unique_facts([f for f in facts if f.required]);notice=ERRORS['answer_correction_service_failure']
                    parts=[dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in kept]+[dict(text=notice,sources={},profile=False,provenance_checked=True)]
                    finish_answer('\n\n'.join([f.compact for f in kept]+[notice]),None,parts,error='answer_correction_service_failure')
                except Exception as secondary:
                    usage['failure_secondary_kind']=type(secondary).__name__
                    self.store.finish(tid,'failed',ERRORS['answer_evidence_unavailable'],'answer_evidence_unavailable',usage)
            else:
                code=stop_reason or 'dialogue_service_failure'
                notice=ERRORS[stop_reason]+' 本轮自然答复整理也未完成，已有结果保留。' if stop_reason else '本轮自然答复服务中断。已取得的查询结果保留；结果未知的操作仍需核实原引用，不会自动重新办理。'
                try:
                    recovered=[]
                    for call in self.store.calls(tid):
                        try:recovered.extend(answers.build(p,cid,tid,[call],usage.get('r4_observed',{})))
                        except Exception as evidence_error:usage.setdefault('evidence_failures',[]).append(dict(call_id=call['call_id'],exception_kind=type(evidence_error).__name__))
                    facts=answers.unique_facts(recovered)
                    kept=[f for f in facts if f.required]
                    parts=[dict(text=f.compact,sources=f.sources,profile=f.profile,provenance_checked=True) for f in kept]+[dict(text=notice,sources={},profile=False,provenance_checked=True)]
                    usage['answer_fallback']='classified_failure_with_verified_evidence'
                    finish_answer('\n\n'.join([f.compact for f in kept]+[notice]),None,parts,error=code)
                except Exception as secondary:
                    usage['fallback_exception_kind']=type(secondary).__name__
                    self.store.finish(tid,'failed',notice,code,usage)
        else:
            if not answer_only and 'after_turn' in locals() and not usage.get('r4_hold_automatic'):
                try:
                    from security.support import status as support_status
                    if not support_status(sid)['automation_paused']:after_turn(p,tid,text)
                except Exception:
                    import logging
                    logging.getLogger('r4.dialogue').warning('automatic_memory_enqueue_unavailable')
        return self.store.history(p,cid,limit=1,before=turn['number']+1)[0][0]
