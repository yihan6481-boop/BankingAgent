"""Separate model explanations from authoritative operation acknowledgements.

The language model selects segments/evidence. The server renders memory state;
it never scans Chinese prose to guess whether a completion claim is correct.
"""
import json
from typing import Literal
from pydantic import Field
from agent.schemas import StrictModel
from lifecycle.store import TaskError
from .context_guard import valid
from .transport import MemoryUnavailable


class Part(StrictModel):
    kind:Literal['answer','memory']
    text:str=Field(max_length=16000)
    call_ids:list[str]=Field(default_factory=list,max_length=12)
    sources:dict[str,int]=Field(default_factory=dict)

class Answer(StrictModel):
    parts:list[Part]=Field(min_length=1,max_length=12)


CONTRACT='''[服务器分段回答契约]
本轮使用长期记忆或发生记忆操作。最终content必须为JSON对象 {"parts":[{"kind":"memory","text":"","call_ids":["实际记忆调用ID"],"sources":{}},{"kind":"answer","text":"其他目标的自然回答，保留财务口径","call_ids":["实际银行调用ID"],"sources":{}}]}。仅查询时省略memory段。只输出JSON，不加说明或Markdown围栏。
记忆保存/更正/删除的状态只能放kind=memory段；text不负责状态，服务器按当前权威操作渲染，禁止把记忆完成声称写进answer段。其他目标分段回答，不能省略已完成的独立目标。
个人背景解释段必须在sources中列出实际使用的rm引用及整数版本；不使用记忆的银行解释sources留空。call_ids仅引用本轮已执行调用。不得虚构引用或金额。仍可先调用合法工具，最终才提交此JSON。
发生记忆写操作后，纯记忆段（包括失败、无法处理、没有保存等否定状态）统一由服务器按操作状态及有效来源原文显示。独立银行结果仍用answer段和实际银行call_ids解释；不要把记忆状态叙述混入银行或个人背景解释。自动整理已启用时，普通自述还会在本轮结束后进入后台，不得声称仅留在当前对话。
'''


def required(calls,observed=None):
    from .dialogue import WRITE_TOOLS
    return bool(observed) or any(c['name'] in WRITE_TOOLS for c in calls)


def receipt(states):
    lines=[]
    for state in states:
        phase=state['operation_state'];effect=state['effect_state'];availability=state['availability']
        ref=state['operation_ref']
        if phase=='succeeded' and effect=='committed':
            text='本次记忆操作已完成。' if availability=='available' else '本次记忆操作已完成；该资料当前不可用于召回。'
        elif phase in {'failed','cancelled'}:text='记忆操作'+('已明确失败' if phase=='failed' else '已停止继续执行')+'；请核对副作用和原操作记录。'
        else:text='记忆操作正在处理或核实，尚不能作为已完成的保存、更正或删除使用。'
        # Internal reference/phase/effect stay in the structured operation DTO.
        # Saving background never creates a deliverable reminder notification.
        phase_label={'queued':'等待处理','running':'正在执行','reconciling':'正在核实','failed':'失败','succeeded':'已完成','cancelled':'已取消'}.get(phase,'未核实')
        effect_label={'none':'尚未发生','partial':'部分发生','unknown':'结果未知','committed':'已提交'}.get(effect,'未核实')
        available_label={'pending':'尚未可用','blocked':'暂不可用','available':'可用'}.get(availability,'未核实')
        lines.append(text+' 执行状态：'+phase_label+'；资料变更：'+effect_label+'；当前可用性：'+available_label+'。')
    return '\n'.join(lines)


def states(p,calls):
    from .dialogue import WRITE_TOOLS
    from .service import Service
    refs=[]
    for call in calls:
        if call['name'] not in WRITE_TOOLS|{'memory_operation'}:continue
        data=(call.get('result') or {}).get('data') or {}
        if data.get('operation_ref'):refs.append(data['operation_ref'])
        refs.extend(x['operation_ref'] for x in data.get('operations',[]))
    service=Service()
    try:return [service.state(p,ref) for ref in dict.fromkeys(refs)]
    finally:service.close()


def render(p,text,calls,observed):
    if text.startswith('```json\n') and text.endswith('\n```'):text=text[8:-4]
    answer=Answer.model_validate_json(text)
    actual={c['call_id']:c for c in calls};parts=[];memory_seen=False
    from .dialogue import WRITE_TOOLS
    current=states(p,calls)
    has_write=any(c['name'] in WRITE_TOOLS for c in calls)
    from .context_guard import retrieved_references
    supplied,profile_refs=retrieved_references(calls)
    supplied={ref:version for ref,version in supplied.items() if valid(p,{ref:version},profile=ref in profile_refs)}
    bank_calls=[call for call in calls if not call['name'].startswith('memory_')]
    for part in answer.parts:
        if any(ref not in actual for ref in part.call_ids):raise ValueError('answer_call_reference_denied')
        if part.kind=='answer':
            # Empty/partial model-declared references are not proof that prose
            # is independent. Bind successful actual memory reads ourselves.
            # Mixed prose can be quarantined together; independent bank tool
            # results stay in their canonical cards and history records.
            separate_memory=any(other is not part and set(other.sources)&set(supplied) for other in answer.parts)
            explicit_bank=bool(part.call_ids) and all(not actual[ref]['name'].startswith('memory_') for ref in part.call_ids)
            if not (explicit_bank and separate_memory and not part.sources):
                part.sources={**supplied,**part.sources}
            if not has_write and not bank_calls:
                part.sources={**{ref:version for ref,version in observed.items() if valid(p,{ref:version})},**part.sources}
        if part.kind=='memory' and not current and not has_write:
            # A read-only mixed answer can mislabel its factual memory segment
            # as an operation segment. Never silently discard that user goal,
            # and never render its prose as an invented write acknowledgement.
            if any(observed.get(ref)!=version for ref,version in part.sources.items()):raise ValueError('answer_source_reference_denied')
            if part.text and part.sources:
                from .service import Service
                from .context_guard import references
                profile=any(call['name']=='memory_profile' and (call['call_id'] in part.call_ids or set(references(call.get('result')))&set(part.sources)) for call in calls)
                service=Service()
                try:
                    for ref,version in part.sources.items():
                        if not valid(p,{ref:version},profile=profile):continue
                        try:source=service.read(p,ref)
                        except (MemoryUnavailable,TaskError):
                            parts.append(dict(text='该项记忆暂时无法核实；独立银行结果以本轮查询为准。',sources={},profile=False))
                            continue
                        if source.get('availability')=='available' and source['version']==version:
                            parts.append(dict(text='当前有效资料：'+source['text'],sources={ref:version},profile=profile))
                finally:service.close()
            elif part.text:raise ValueError('read_answer_segment_required')
            continue
        is_memory=part.kind=='memory' or any(actual[ref]['name'] in WRITE_TOOLS for ref in part.call_ids)
        if is_memory:
            if not current and not has_write:continue
            if not memory_seen:
                parts.append(dict(text=receipt(current) or '本次记忆工具请求未形成可确认完成的操作，请查看实际工具状态。',sources={},operation_refs=[s['operation_ref'] for s in current]))
                memory_seen=True
        else:
            if any(observed.get(ref)!=version for ref,version in part.sources.items()):raise ValueError('answer_source_reference_denied')
            from .context_guard import references
            profile=any(call['name']=='memory_profile' and (call['call_id'] in part.call_ids or set(references(call.get('result')))&set(part.sources)) for call in calls)
            if valid(p,part.sources,profile=profile):
                bank_calls=[c for c in calls if not c['name'].startswith('memory_')]
                independent_bank=any(not actual[ref]['name'].startswith('memory_') for ref in part.call_ids) or (not part.call_ids and bool(bank_calls))
                if has_write and not independent_bank:
                    # A model can label a status denial as an answer and attach
                    # an unrelated valid source. Provenance alone does not make
                    # that denial true. Render only the cited canonical sources
                    # for memory-only explanations in a mutation turn.
                    from .service import Service
                    service=Service()
                    try:
                        for ref,version in part.sources.items():
                            try:source=service.read(p,ref)
                            except (MemoryUnavailable,TaskError):
                                parts.append(dict(text='该项记忆暂时无法核实；独立银行结果以本轮查询为准。',sources={},profile=False))
                                continue
                            if source.get('availability')=='available' and source['version']==version:
                                parts.append(dict(text='当前有效资料：'+source['text'],sources={ref:version},profile=profile))
                    finally:service.close()
                else:parts.append(dict(text=part.text,sources=part.sources,profile=profile))
    if not memory_seen and any(c['name'] in WRITE_TOOLS for c in calls):parts.append(dict(text=receipt(current) or '请查看本轮记忆工具的权威状态；未确认完成。',sources={},operation_refs=[s['operation_ref'] for s in current]))
    if not parts:
        if not current and not has_write and all(part.kind=='memory' for part in answer.parts):raise ValueError('read_answer_segment_required')
        raise TaskError('memory_changed_during_answer')
    for part in parts:part['provenance_checked']=True
    return '\n\n'.join(part['text'] for part in parts),parts,current
