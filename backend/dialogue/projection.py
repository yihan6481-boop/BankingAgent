"""Model projection is an allowlist. Confirmation material belongs only to UI."""
import json
import re
from security.redaction import redact

# Only these reviewed business fields cross the model boundary. Nested free text
# never acquires instruction authority. Unknown future internal keys fail closed.
KEYS=set('''protocol status code data effects next_actions retry allowed prerequisite
reference revision kind fields missing_fields field_errors field conflicts_with candidates
resolved_recipient ready validation_scope draft prepared task task_created preparation_outcome
verification_reference source produced_at request result query_reference query_revision
cards recipients queried_at historical session_reference focus items unfinished truncated limit
server_time business_timezone scope associated_at execution_outcome context_association_status
card_id bank_name card_type masked_card_number balance daily_limit per_tx_limit daily_used daily_remaining limit_business_date
recipient_id name masked_account_number trusted from_card_id recipient_label recipient_name amount remark
task_id mode steps step_id tool_name purpose arguments depends_on current_step_id error_code
success message verification_status completed_at created_at expires_at reason scheduled_at notes
time_mode selection comparison start end_exclusive period card_tail merchants exclude_merchants
categories amounts operator cents direction kinds statuses refunds_separate metrics group_by sort_by
order offset plans entries current summary groups details recorded_count matched_count excluded_count
purchase_cents refund_cents amount_cents count average_cents net_cents currency label record_id day
merchant category document_id event_at time_source time_precision as_of page_offset next_offset coverage accounting net_label
date_tie base change_cents change_percent unit delta change metric_id metric unit_label
value result_reference changed error field_errors format_version title preview_summary
results plan version result_id source_note
dialogue_id observed_at authority_scope association source_turn source_call source_tool
authorized_queries turn_id call_id
pagination actual_coverage net_consumption
product_id product_name risk_level risk_name updated_at min_purchase reference_annual_rate
duration_days notice holding_id principal purchased_at subscription_id cycle next_charge_date
card_last_four active_count monthly_cost annual_cost subscriptions daily_limit_minor
annual_cost_cents cost_basis monthly_rounding complete unknown_cycle_count retry_after_seconds
month year transaction_count total_spending card_specified percentage transaction_id date
overseas_enabled online_payment_enabled error_code verification_status condition condition_status
source_step_id field_path target_step_id constraints condition_result condition_error_code
tool operator value_type timezone totals income expense net_cash_flow income_count expense_count
merchants categories range coverage first_recorded last_recorded recorded_months note evidence
evidence_truncated ledger_sha256 completeness changes percentage_reason months explanation
products suitability risk_match purchase_allowed product profile checks expected_return assumptions
clues triggers rule_id rule_version actual threshold transaction_ids applicability evaluated
available_cents principal_cents min_purchase_cents card_balance eligible warnings
user_risk_level product_risk_level risk_matches product_active risk_fact purchase_conditions
purchase_assessment authorization reason_code state minimum card_status liquidity early_redemption fees
applicable_count inapplicable_count matched_transaction_count transaction triggers
transaction_type classification_source refund_of_transaction_id refund_count unlinked_refund_count
unclassified_cents unclassified_count classification merchant_key aliases omitted_evidence
documents has_more document preparation preparations preparation_id client_request_id
state digest blocking ready total_amount_cents selected_item_ids selected_payee_index
verification user_association document_authenticity payee_ownership payment_status
external_channels links link link_id stale resolution_id candidate_id masked_account
beneficiary_kind supports replayed repeated_upload user_notes user_confirmed_own_bill
obligation obligation_id item_ids task_revision draft_reference draft_revision contract_version
selector_kind selected_candidate_id matches compared unknown_fields reconciliation_id
recovery historical_evidence stale_evidence_ignored task_status released required
resolution selector limitations preview preview_id permission_level required_authentication
payment_target
'''.split())


def project(value):
    if isinstance(value,dict) and value.get('kind')=='server_calendar':
        from .calendar_view import CalendarView
        try:return CalendarView.model_validate(value).model_dump()
        except ValueError:return dict(kind='invalid_calendar',status='unavailable')
    if isinstance(value,dict) and value.get('kind')=='dialogue_source':
        from .source_view import project as source_projection
        try:return source_projection(value)
        except ValueError:return dict(kind='dialogue_source',entries=[],scope='untrusted original user statement; not bank evidence or authorization')
    if isinstance(value,dict) and value.get('kind')=='advisory_risk':
        from assistance.risk import SemanticView,LABELS
        try:
            checked=SemanticView.model_validate(value).model_dump()
            if any(x['label']!=LABELS[x['kind']] for x in checked['clues']):raise ValueError()
            return redact(checked)
        except ValueError:return {'kind':'advisory_risk','state':'unavailable','clues':[]}
    # Narrow typed service state, not a global exception for arbitrary keys.
    if isinstance(value,dict) and value.get('kind')=='human_service':
        from security.support import SupportView
        try:return SupportView.model_validate(value).model_dump()
        except Exception:return {'kind':'human_service','state':'idle','human_connected':False,'automation_paused':False,'case_id':None}
    if isinstance(value,dict) and value.get('memory_contract')=='r4-memory-1':
        from recall_bridge.projection import project as memory_projection
        return memory_projection(value)
    if isinstance(value,dict):
        # Dynamic category labels are data only after the exact schema passes.
        if set(value)=={'month','card_id','total_spending','categories'} and isinstance(value['categories'],dict):
            from read_models import Categories
            checked=Categories.model_validate(value)
            value=dict(value,categories=[dict(name=k,**v.model_dump()) for k,v in checked.categories.items()])
        # A linked draft is a provenance record, not the current task arguments.
        # Keep the task link and revision, and force live task reads for values.
        if value.get('kind')=='transfer' and value.get('status')=='linked' and isinstance(value.get('fields'),dict) and value['fields'].get('task_id'):
            value=dict(value,fields={'task_id':value['fields']['task_id']},historical=True,
                       scope='关联记录；当前业务字段以实时任务为准',resolved_recipient=None,candidates=[],missing_fields=[],field_errors=[])
        if isinstance(value.get('draft'),dict) and value['draft'].get('status')=='linked':
            value=dict(value,resolved_recipient=None,candidates=[],missing_fields=[],field_errors=[],
                       validation_scope='已关联任务；读取原task_id获取当前业务字段')
        from .identifier_projection import restore_metadata
        return restore_metadata(value,{k:project(v) for k,v in value.items() if k in KEYS and (k!='authorization' or v=='not_granted')})
    if isinstance(value,(list,tuple)): return [project(v) for v in value]
    if isinstance(value,str): return safe_text(value)
    return value


def safe_text(value):
    return redact(value)


def model_arguments(name,value):
    """Narrow UUID slot exception; authorization still happens in the service.

    A same-named key in a remark, unknown tool or invalid argument shape never
    gets this exception. All other strings keep the shared sensitive scan.
    """
    cleaned=redact(value)
    if not isinstance(value,dict):return cleaned
    names={'task_read','task_verify','task_modify','task_pause','task_cancel','task_resume',
           'task_schedule','task_takeover','task_return','task_note','task_rollback','context_focus','memory_save_business'}
    from .identifier_projection import uuid4_text
    if name not in names or not uuid4_text(value.get('reference')):return cleaned
    from .tools import DESCRIPTIONS
    from recall_bridge.dialogue import DESCRIPTIONS as memory
    try:(DESCRIPTIONS|memory)[name][0].model_validate(value)
    except ValueError:return cleaned
    return dict(cleaned,reference=value['reference'])


def user_text(value):
    # Digit count does not identify a secret: quantities and YYYYMM are valid
    # chat. Labelled credentials remain redacted; chat never authorizes payment.
    return safe_text(value)


def bounded_result(value,max_bytes=18000):
    result=project(value)
    if len(json.dumps(result,ensure_ascii=False).encode())<=max_bytes:return result
    # Large immutable query results have a retrievable reference. Never shrink
    # query scope/aggregates or silently return a partial detail set as complete.
    if isinstance(value.get('data'),dict) and value['data'].get('kind')=='result':
        d=result['data']; f=d['fields']; r=f.get('result',{})
        for entry in r.get('results',[]):
            entry.get('current',{})['details']=[]
            entry.get('current',{})['groups']=[]
        result['data']['truncated']=True
        result['data']['message']='大结果仅提供汇总；通过 result_read 按引用分页读取，明细未完整呈现。'
        if len(json.dumps(result,ensure_ascii=False).encode())<=max_bytes:return result
    return dict(status='business_condition',code='result_exceeds_context',data={
        'reference':value.get('data',{}).get('reference') if isinstance(value.get('data'),dict) else None,
        'message':'结果超出模型输入预算；请缩小明细分页，业务调用结果已保存。'},effects=value.get('effects',[]))


def compact_current(exchanges,turn_id):
    """Request-only pointers for oversized tool bodies; full PG/UI evidence stays.

    Assistant envelopes and call pairing never change, including opaque thinking
    digests. The fact contract still comes from the full original stored result.
    """
    from copy import deepcopy
    compacted=deepcopy(exchanges);count=0
    for message in compacted:
        if message.get('role')!='tool' or len(message.get('content','').encode())<=2200:continue
        result=json.loads(message['content'])
        pointer={k:result[k] for k in ['protocol','status','code','effects','retry','next_actions'] if k in result}
        pointer['data']=dict(turn_id=turn_id,call_id=message['tool_call_id'],historical=True,
            scope='tool body omitted from model context only; full result retained in original call; use verified facts and owned original-reference reads')
        message['content']=json.dumps(pointer,ensure_ascii=False);count+=1
    return compacted,count
