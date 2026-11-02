"""Trusted projection only: canonical QueryData -> relational answers and UI cards.

No SQL, model JSON, owner IDs, document IDs, or client-side arithmetic here.
"""
from datetime import date,timedelta
from decimal import Decimal
from typing import Literal
from pydantic import Field
from agent.schemas import StrictModel


def money(n):
    from grounded_answer import money as render
    return '未知' if n is None else render(n)+'元'

def period(scope):
    p=scope['period']
    if not p:return '全部已记录日期'
    start=date.fromisoformat(p['start']);end=date.fromisoformat(p['end_exclusive'])
    if start.day==1 and end.day==1 and (end.year*12+end.month)-(start.year*12+start.month)==1:
        return f'{start.year}年{start.month}月'
    return p['start']+'至'+str(end-timedelta(days=1))

def subject(scope):
    name='、'.join(scope['merchants']) or '所选范围'
    if scope['categories']:name+='（'+'、'.join(scope['categories'])+'）'
    if scope['exclude_merchants']:name+='（不含'+'、'.join(scope['exclude_merchants'])+'）'
    if scope.get('card_tail'):name+='（卡尾号'+scope['card_tail']+'）'
    for v in scope['amounts']:
        name+='，单笔'+{'gt':'超过','gte':'不少于','lt':'低于','lte':'不超过','eq':'等于'}[v['operator']]+money(v['cents'])
    return name

def source(scope):return '银行已记录流水' if scope['source']=='bank' else '已确认上传账单'

def metric_label(p,metric):
    from data_query import KINDS,METRICS,selected_kind,Selection
    kinds=selected_kind(Selection.model_validate(p['selection']))
    kind='购买支出' if kinds==['purchase'] else '退款' if kinds==['refund'] else '所选交易金额'
    return kind if metric=='sum' else {'count':'交易笔数','average':'平均单笔','net':'购买额减退款额'}.get(metric,METRICS.get(metric,{}).get('label',metric))

def scope_label(cur):
    return period(cur['scope'])+' · '+subject(cur['scope'])

def comparison_rows(entry):
    p=entry['plan'];comp=entry['comparison'];rows=[]
    if not comp:return rows
    for metric,v in comp['metrics'].items():
        fmt=(lambda n:'未知' if n is None else str(int(Decimal(n)))+'笔') if v['unit']=='笔' else money
        delta=Decimal(v['delta']) if v['delta'] is not None else None
        rows.append(dict(label=metric_label(p,metric),current_label=scope_label(entry['current']),base_label=scope_label(comp['base']),
            current=fmt(v['current']),base=fmt(v['base']),delta='未知' if delta is None else fmt(abs(delta)),
            direction='未知' if delta is None else '多' if delta>0 else '少' if delta<0 else '相同',
            signed_delta=fmt(v['delta']),percent=v['change_percent']+'%' if v['change_percent'] is not None else None))
    return rows

class Value(StrictModel):
    label:str
    value:str
class Detail(StrictModel):
    day:str
    merchant:str
    amount:str
    kind:str
    direction:str
    card_tail:str|None=None
    source:str
class Ranked(StrictModel):
    rank:int
    name:str
    value:str
    values:list[Value]=Field(default_factory=list)
class Comparison(StrictModel):
    label:str
    current_label:str
    base_label:str
    current:str
    base:str
    delta:str
    signed_delta:str
    direction:Literal['多','少','相同','未知']
    percent:str|None=None
class QueryCard(StrictModel):
    kind:Literal['summary','details','ranking','comparison']
    title:str
    source:str
    values:list[Value]=Field(default_factory=list)
    rows:list[Detail|Ranked]=Field(default_factory=list)
    comparisons:list[Comparison]=Field(default_factory=list)
    sort:str|None=None
    empty:bool=False
    next_page:bool=False
    page:str|None=None
    warnings:list[str]=Field(default_factory=list)
class QueryDisplay(StrictModel):
    kind:Literal['query_result_v1']='query_result_v1'
    turn_id:str
    cards:list[QueryCard]
    coverage:str='仅基于已记录流水；历史查询快照不代表当前实时数据。'
    warnings:list[str]
    accounting:list[str]

def projection(data,turn_id):
    from data_query import verify_output,METRICS,KINDS
    verify_output(data)
    cards=[];accounting=[];sources=set();warnings=[]
    for e in data['results']:
        p=e['plan'];c=e['current'];a=c['summary'];s=c['scope'];sources.add(s['source'])
        accounting.extend([c['accounting'],c['coverage']])
        kind='comparison' if e['comparison'] else 'details' if 'details' in p['metrics'] else 'ranking' if p['group_by'] else 'summary'
        values=[]
        for m in p['metrics']:
            if m not in METRICS:continue
            value=str(a['count'])+'笔' if m=='count' else money(a[METRICS[m]['field']])
            values.append(dict(label=metric_label(p,m),value=value))
        if kind=='summary' and 'count' not in p['metrics']:values.append(dict(label='交易笔数',value=str(a['count'])+'笔'))
        if p.get('refunds_separate'):values.append(dict(label='退款单列',value=money(a['refund_cents'])))
        cw=[]
        if c.get('as_of'):cw.append('数据截至'+c['as_of'])
        if c.get('date_tie'):cw.append('边界日期有并列记录，无法确定唯一最新或日内先后。')
        if c['net_label']:cw.append(c['net_label'])
        if a['excluded_count']:cw.append('非成功、非人民币或未知项目未计入金额。')
        if c['truncated']:cw.append('仅展示指定数量；合计按完整匹配集计算。')
        rows=[];sort=None
        if kind=='details':
            rows=[dict(day=(r.get('event_at') or (r['day']+'（仅日期）')).replace('T',' ')+((' · 演示规则时间') if r.get('time_source')=='demo_rule_v2' else '')+' · 已入账 · 交易编号'+r['record_id'],merchant=r['merchant'],amount=money(r['amount_cents']),kind=KINDS[r['kind']],
                direction='支出' if r['direction']=='expense' else '收入',card_tail=r.get('card_tail'),source=source(s)) for r in c['details']]
        elif kind=='ranking':
            primary=next(m for m in ['net','sum','average','count'] if m in p['metrics'])
            key={'amount':'net' if 'net' in p['metrics'] else 'sum','count':'count','average':'average','date':primary}[p['sort_by']]
            sort=('日期' if p['sort_by']=='date' else metric_label(p,key))+' · '+(('从晚到早' if p['order']=='desc' else '从早到晚') if p['sort_by']=='date' else ('从高到低' if p['order']=='desc' else '从低到高'))
            def group_value(g,m):return str(g['count'])+'笔' if m=='count' else money(g['amount_cents'] if m=='net' else g[METRICS[m]['field']])
            rows=[dict(rank=i+1,name=g['label'],value=group_value(g,key),values=[dict(label=metric_label(p,m),value=group_value(g,m)) for m in p['metrics'] if m in METRICS]) for i,g in enumerate(c['groups'])]
        if e['comparison']:
            sources.add(e['comparison']['base']['scope']['source'])
            if e['comparison']['status'] in {'missing_records','undefined_metric'}:cw.append('一侧缺少可比较记录，差额无法确认；不能把缺失当零。')
            if e['comparison']['status']=='different_sources_no_merge':cw.append(data['source_note'])
        temporal='最近记录 · ' if p['time_mode']=='recent_records' else '最新日期记录 · ' if p['time_mode']=='latest' else ''
        cards.append(dict(kind=kind,title=temporal+scope_label(c),source=source(s),values=values,rows=rows,
            comparisons=comparison_rows(e),sort=sort,empty=not a['count'],next_page=c.get('next_offset') is not None,
            page=f"本页 {len(c['details'])} 笔，起始位置 {c['page_offset']+1}" if kind=='details' else None,warnings=cw))
    if len(sources)>1:warnings.append(data['source_note'])
    return QueryDisplay(turn_id=turn_id,cards=cards,warnings=warnings,accounting=list(dict.fromkeys(accounting))).model_dump(mode='json')

def answer_facts(data,*,cards_available=False):
    """Whole relations stay atomic. Core conclusions cannot be delegated to cards."""
    from grounded_answer import Fact
    from data_query import METRICS
    facts=[];sources={e['current']['scope']['source'] for e in data['results']}
    if len(sources)>1:facts.append(data['source_note'])
    for e in data['results']:
        p=e['plan'];c=e['current'];a=c['summary'];s=c['scope'];who=subject(s);when=period(s);src=source(s)
        prefix=when+'，'+who
        basis='（'+src+'）'
        if not a['count'] and 'net' not in p['metrics']:
            label=metric_label(p,'sum')
            kind='购买' if label=='购买支出' else label
            facts.append(Fact(prefix+'未查到已记录的'+kind+'匹配项；这不能证明没有实际消费。'+basis))
        else:
            bits=[]
            for m in p['metrics']:
                if m in METRICS:
                    bits.append(metric_label(p,m)+(str(a['count'])+'笔' if m=='count' else money(a[METRICS[m]['field']])))
            if 'sum' in p['metrics'] and 'count' not in p['metrics']:bits.append('共'+str(a['count'])+'笔')
            if bits and not e['comparison']:
                joined='，'.join(bits)
                natural=when+who+'的已记录'+joined+'。'+basis
                if set(p['metrics'])<= {'sum','count'} and 'sum' in p['metrics']:
                    natural='你在'+when+'的'+who+'有'+str(a['count'])+'笔已记录的'+metric_label(p,'sum')+'，共'+money(a['amount_cents'])+'。'+basis
                facts.append(Fact(prefix+'的已记录'+joined+'。'+basis,
                    compact=natural,
                    explanation='按'+src+'核算，'+prefix+'：'+joined+'。'))
            if 'details' in p['metrics']:
                if p['time_mode']!='latest' or len(c['details'])>3:
                    facts.append(Fact(prefix+f"查到{len(c['details'])}笔本页明细"+('，逐笔记录见下方卡片。' if cards_available else '。')+basis))
                # Reviews without attached displays must retain actual rows.
                if not cards_available or p['time_mode']=='latest' and len(c['details'])<=3:
                    from data_query import KINDS
                    for r in c['details']:
                        tail='，卡尾号'+r['card_tail'] if r.get('card_tail') else ''
                        facts.append(Fact(f"{src}：{r['day']}，{r['merchant']}，{KINDS[r['kind']]}{money(r['amount_cents'])}{tail}。"))
            if p['group_by']:
                primary=next(m for m in ['net','sum','average','count'] if m in p['metrics'])
                key={'amount':'net' if 'net' in p['metrics'] else 'sum','count':'count','average':'average','date':primary}[p['sort_by']]
                groups=c['groups'];top=groups[:1] if cards_available else groups
                parts=[g['label']+'：'+(str(g['count'])+'笔' if key=='count' else money(g['amount_cents'] if key=='net' else g[METRICS[key]['field']])) for g in top]
                facts.append(Fact(prefix+'按'+('日期' if p['sort_by']=='date' else metric_label(p,key))+('降序' if p['order']=='desc' else '升序')+f'展示{len(groups)}项'+('，首项为'+'；'.join(parts) if parts else '')+'。'+basis))
        if p.get('refunds_separate'):
            facts.append(Fact(prefix+'退款单列'+money(a['refund_cents'])+'。'+basis))
        if e['comparison']:
            for r in comparison_rows(e):
                rel='差额无法确认：一侧缺少记录或来源不同。' if r['direction']=='未知' else '两者相同。' if r['direction']=='相同' else '前者比后者'+r['direction']+r['delta']+'。'
                rate='变化率'+r['percent']+'。' if r['percent'] is not None else '基数为零或数据不足时不计算变化率。'
                sentence=r['current_label']+'的'+r['label']+r['current']+'；'+r['base_label']+'为'+r['base']+'，'+rel+rate+basis
                facts.append(Fact(sentence))
        if c.get('date_tie'):facts.append('这些记录在边界日期并列，无法确定唯一最新或日内先后。')
        if c.get('next_offset') is not None:facts.append('本页仅展示部分明细，可继续查看下一页；合计按完整匹配集计算。')
        if c['net_label']:facts.append('购买和退款按各自发生日期计算；'+c['net_label'])
        for window in [c,e['comparison']['base'] if e['comparison'] else None]:
            if window and window.get('as_of') and window['scope']['period'] and window['scope']['period']['end_exclusive']>window['as_of']:
                notice='数据截至'+window['as_of']+'，不包含未来尚未发生的交易。'
                if notice not in facts:facts.append(notice)
    if any(e['comparison'] and e['comparison']['base']['scope']['source']!=e['current']['scope']['source'] for e in data['results']):facts.append(data['source_note'])
    if not cards_available:facts.append('仅为已记录项目，无法保证完整消费覆盖。')
    return facts
