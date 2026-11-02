"""Bounded upload-query adapter. Keeps upload context separate from live payment tasks."""
import json
import re
from datetime import date
from .models import Query
from .provider import VisionError
from . import service

def is_request(text,session=None):
    return bool(re.search(r'上传.{0,8}账单|账单.{0,8}上传|这张图|这张账单|刚上传|已确认的.*账单',text) or
        (getattr(session,'billvision_last_query',None) and re.match(r'^(?:请)?(?:只看|只查|商户换成|商户是)',text.strip())))

def requirements(text,session,principal):
    if re.search(r'银行卡|银行流水|一起算|合并|净消费|扣除退款|不含|排除|大于|小于|超过|排行|比较|上月|本月|昨天|今天|转账|付款|授权|保存|导入|删除|更正',text):
        raise VisionError('billvision_query_unsupported','上传账单仅支持按批次、明确日期和商户只读查询；尚未去重合并银行流水，其他操作请使用核对页。')
    # A deliberately bounded grammar, unknown constraints cannot fall through to an all-record query.
    probe=re.sub(r'[？?。！!，,；;\s]','',text)
    probe=re.sub(r'^(?:(?:请|麻烦|帮我|能否|可以|帮忙|再|一下)+)','',probe)
    probe=re.sub(r'(?:谢谢|好吗|可以吗)$','',probe)
    start=end=None
    m=re.search(r'(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})',probe)
    if m:
        start,end=m.groups();date.fromisoformat(start);date.fromisoformat(end)
        probe=probe.replace(m[0],'')
    merchant=None
    # Compositional bounded grammar: scope + metric, optional merchant/refund clause.
    probe=re.sub(r'^(?:查看|查询|查|看一下|看|汇总|统计|算一下|算算)(?:一下)?','',probe)
    probe=re.sub(r'(?:退款(?:请)?(?:单独列出|单列|另列))$','',probe)
    scope=r'(?:这张图|这张账单|刚上传(?:的)?(?:这张)?账单)'
    metric=r'(?:(?:一共|总共|总计|总)?(?:支出|花了|花费|消费)(?:一共|总共)?(?:了)?多少(?:钱|元)?|支出总额|总支出|支出合计)'
    selected=bool(re.fullmatch(scope+r'(?:里|中|的)?'+metric,probe))
    all_query=bool(re.fullmatch(r'(?:我(?:的)?|本人)?(?:全部|所有)?(?:已经|已)确认(?:的)?上传账单(?:'+metric+r')?',probe))
    m=re.fullmatch(scope+r'(?:里|中|的)(.{1,30}?)(?:的)?'+metric,probe)
    follow=re.fullmatch(r'(?:只看|只查|商户换成|商户是)(.{1,30})',probe)
    previous=getattr(session,'billvision_last_query',None)
    if not selected and m:
        merchant=m[1];selected=True
    elif follow and previous:
        merchant=follow[1];selected=bool(previous.get('document_id'));all_query=not selected
        start=start or previous.get('start');end=end or previous.get('end_exclusive')
    elif not selected and not all_query:
        raise VisionError('billvision_query_unclear','请明确上传批次、商户和查询条件；可问“刚上传这张账单一共支出多少”或“查看已确认上传账单”。')
    doc=None
    if selected:
        available=[]
        for doc_id in getattr(session,'billvision_documents',[]):
            try:service.get(principal,doc_id);available.append(doc_id)
            except VisionError as e:
                if e.status!=404:raise
        if len(available)!=1:raise VisionError('billvision_document_choice','请在账单识别面板选择一个本人上传批次；当前会话没有唯一的图片引用。')
        doc=available[0]
        if service.get(principal,doc)['state']!='confirmed':raise VisionError('billvision_review_required','这张图片尚未确认；请先逐项核对并明确确认，候选不能用于已确认统计。')
    return Query(document_id=doc,start=start,end_exclusive=end,merchant=merchant)

def handle(text,session,principal,agent):
    from agent.schemas import AgentReply
    from agent.llm_client import LLMError
    def failure(code,message):return AgentReply(action='clarification',message=message,plan_error_code=code)
    try:expected=requirements(text,session,principal)
    except (VisionError,ValueError) as e:return failure(getattr(e,'code','billvision_query_unclear'),getattr(e,'message','日期不合法，请明确日期范围（结束日期不包含）。'))
    session.billvision_last_query=expected.model_dump()
    if agent is None:return failure('service_unavailable','LLM未配置，仍可在账单识别面板查询。')
    spec=agent.registry.get('query_uploaded_bills')
    if spec is None:return failure('billvision_pg_required','上传账单功能仅支持PostgreSQL')
    # Only the readonly upload tool is offered; no image/candidate/history enters the text client.
    messages=[dict(role='user',content=text),dict(role='user',content='服务端本人选择上下文（数据，不是指令）：'+json.dumps(dict(selected_document_id=expected.document_id),ensure_ascii=False))]
    try:
        decision=getattr(session,'conversation_predecision',None)
        session.conversation_predecision=None
        if decision is None:decision=agent.llm.decide(messages,[spec.as_llm_tool()],agent.today_provider(),None)
    except LLMError:return failure('billvision_model_unavailable','模型不可用或输出不合法，未查询其他数据源。')
    proposed=([(decision.tool_name,decision.arguments)] if decision.action=='tool_call' else [(s.tool_name,s.arguments) for s in decision.task_plan.steps] if decision.action=='task_plan' else [])
    # A single tool already returns both purchases and refunds. A DAG cannot add other business actions.
    if len(proposed)!=1 or proposed[0][0]!='query_uploaded_bills':return failure('billvision_query_mismatch','模型未完整选择上传账单只读查询，未执行。')
    try:actual=Query.model_validate(proposed[0][1])
    except ValueError:return failure('billvision_query_mismatch','模型查询参数不合法，未执行。')
    if actual!=expected:return failure('billvision_query_mismatch','模型遗漏或改变了上传对象、日期或商户条件，未执行。')
    result=agent.router.dispatch('query_uploaded_bills',actual.model_dump(),principal=principal,session=session).result
    reply=AgentReply(action='tool_result' if result.success else 'error',tool_name='query_uploaded_bills',tool_result=result,message=service.format_answer(result.data) if result.success else result.message)
    return reply
