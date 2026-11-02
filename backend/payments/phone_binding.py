"""Versioned mobile phone resolution; no payment execution or inferred intent."""
from datetime import timedelta
from uuid import uuid4
from .service import transaction,Jsonb,now,sha256_json,recipient_fingerprint
from .errors import PaymentError


def current(connection,phone):
    normalized='+86'+phone
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(core.require_owner()::text||':'||%s,0))",(normalized,))
    contacts=connection.execute("SELECT contact_id,recipient_id,revision FROM payments.contacts WHERE phone IN (%s,%s) AND status='active' ORDER BY contact_id FOR SHARE",(phone,normalized)).fetchall()
    legacy=connection.execute('SELECT recipient_id FROM mobile_security.recipient_phones WHERE phone=%s ORDER BY recipient_id FOR SHARE',(phone,)).fetchall()
    ids=sorted({r['recipient_id'] for r in [*contacts,*legacy]})
    # Runtime has SELECT only on the canonical directory. Do not widen table
    # rights for a row lock; the original kernel rereads recipient state and
    # binds the human-readable preview and execution authorization to it.
    rows=connection.execute("SELECT * FROM pg02.recipients WHERE recipient_id=ANY(%s) AND status='active' ORDER BY recipient_id",(ids,)).fetchall()
    candidates=[dict(recipient_id=r['recipient_id'],name=r['name'],bank_name=r['bank_name'],masked_account='**** '+r['account_number'][-4:],fingerprint=recipient_fingerprint(r)) for r in rows]
    return candidates,sha256_json([list(contacts),list(legacy),candidates])


def lookup(principal,sid,phone,sequence):
    with transaction(principal,True) as c:
        candidates,fingerprint=current(c,phone)
        ref='rr_'+uuid4().hex
        selector=dict(kind='phone',mobile_binding=1,session_hash=sha256_json(sid),request_sequence=sequence,mapping_fingerprint=fingerprint)
        status='resolved' if len(candidates)==1 else 'ambiguous' if candidates else 'not_found'
        c.execute('''INSERT INTO payments.recipient_resolutions
          (resolution_id,client_request_id,selector_kind,selector,selector_fingerprint,status,candidates,expires_at)
          VALUES(%s,%s,'phone',%s,%s,%s,%s,%s)''',
          (ref,'mobile-'+uuid4().hex,Jsonb(selector),sha256_json([sid,phone,sequence,'phone']),status,Jsonb(candidates),now()+timedelta(minutes=10)))
        return dict(status=status,candidates=[{k:v for k,v in r.items() if k!='fingerprint'} for r in candidates],selection_required=True,
                    resolution_id=ref,revision=1,request_sequence=sequence)


def prepare(principal,sid,binding,plan,operation):
    with transaction(principal,True) as c:
        row=c.execute('SELECT * FROM payments.recipient_resolutions WHERE resolution_id=%s FOR UPDATE',(binding.resolution_id,)).fetchone()
        if not row:raise PaymentError('resolution_not_found','未找到本人手机号解析，请重新查找',404)
        selector=row['selector']
        if (binding.session_id!=sid or selector.get('session_hash')!=sha256_json(sid)
            or selector.get('mobile_binding')!=1 or row['revision']!=binding.revision
            or selector.get('request_sequence')!=binding.request_sequence
            or row['selector_fingerprint']!=sha256_json([sid,binding.phone,binding.request_sequence,'phone'])
            or row['expires_at']<=now()):
            raise PaymentError('phone_resolution_stale','手机号解析或会话已变化，请重新查找并核对',409)
        candidates,fingerprint=current(c,binding.phone)
        if fingerprint!=selector.get('mapping_fingerprint'):
            raise PaymentError('phone_resolution_stale','手机号映射或收款账户已变化，请重新解析',409)
        if len(plan.steps)!=1 or plan.steps[0].tool_name!='transfer_money':
            raise PaymentError('phone_plan_invalid','手机号入口仅准备单笔转账预览',422)
        arguments=plan.steps[0].arguments
        if arguments.get('recipient_name') or arguments.get('recipient_id') not in {r['recipient_id'] for r in candidates}:
            raise PaymentError('phone_recipient_mismatch','收款对象与本次手机号解析不一致',409)
        # This transaction is the mapping validation point. Finish it before
        # entering the lifecycle kernel, which opens its own identity-locked
        # transactions. Holding our identity lock across that call self-blocks.
        # The plan already contains the selected immutable recipient ID: later
        # phone edits cannot redirect it. The kernel rereads that recipient and
        # binds its displayed identity/amount to confirmation and execution.
    return operation()
