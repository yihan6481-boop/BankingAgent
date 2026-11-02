"""Wealth adaptation on the existing PG02 identity/UoW, no alternate API."""
import json
import sqlite3

from security.args import wealth_purchase_args, wealth_redemption_args
from security.schemas import compute_action_digest
from security.write_context import (assert_write_authorization, authorization_failure,
    assert_permission_not_escalated, mark_verified, owner_scope, write_audit)
from . import pg02


def arguments(kind, product, card, holding, amount, key):
    return (wealth_purchase_args(product,card,amount,key) if kind=='BUY'
            else wealth_redemption_args(holding,amount,key))


def action(kind):
    return 'buy_wealth_product' if kind=='BUY' else 'redeem_wealth_product'


def same(row, kind, args):
    return row['order_type']==kind and arguments(kind,row['product_id'],row['card_id'],row['holding_id'],row['amount_cents'],row['idempotency_key'])==args


def canonical(connection, row):
    """Immutable order + typed ledger + authorization + receipt; no current balances."""
    from wealth_service import _completed_order_data
    receipt=connection.execute('SELECT * FROM lifecycle_receipts WHERE business_key=?',(row['idempotency_key'],)).fetchone()
    ledger=connection.execute('SELECT * FROM transactions WHERE source_wealth_order_id=?',(row['order_id'],)).fetchall()
    auth=connection.execute('SELECT * FROM security_authorizations WHERE authorization_id=?',(row['authorization_id'],)).fetchone()
    expected=arguments(row['order_type'],row['product_id'],row['card_id'],row['holding_id'],row['amount_cents'],row['idempotency_key'])
    if not receipt or len(ledger)!=1 or not auth: raise sqlite3.IntegrityError('wealth_evidence_missing')
    result=json.loads(receipt['result']); t=ledger[0]
    # Product display name/card mask in the receipt are execution snapshots too.
    data=dict(result.get('data') or {})
    from execution.result_models import WealthOrderCompletedData
    typed=dict(data);typed.pop('write_verification',None)
    WealthOrderCompletedData.model_validate(typed)
    from agent.output_guard import contains_sensitive_result
    if contains_sensitive_result(result,tool_name=action(row['order_type'])):
        raise sqlite3.IntegrityError('wealth_sensitive_receipt')
    expected_data=mark_verified(_completed_order_data(row,False))
    for key in ('product_name','masked_card_number'): expected_data[key]=data.get(key)
    if (not result.get('success') or data!=expected_data or receipt['action']!=action(row['order_type'])
        or receipt['owner_id']!=row['owner_id'] or receipt['execution_digest']!=compute_action_digest(action(row['order_type']),expected)
        or auth['action_digest']!=receipt['execution_digest'] or auth['action']!=receipt['action']
        or auth['consumed']!=1 or not auth['consumed_at'] or auth['session_id']!=row['session_id']
        or auth['principal_id']!=row['principal_id'] or t['card_id']!=row['card_id'] or t['amount_cents']!=row['amount_cents']
        or t['date']!=row['business_date'] or t['classification_source']!='wealth_service'
        or t['transaction_type']!=('wealth_buy' if row['order_type']=='BUY' else 'wealth_redeem')
        or t['direction']!=('expense' if row['order_type']=='BUY' else 'income')):
        raise sqlite3.IntegrityError('wealth_evidence_mismatch')
    if auth['challenge_id']:
        challenge=connection.execute('SELECT status,action_digest FROM security_challenges WHERE challenge_id=?',(auth['challenge_id'],)).fetchone()
        if not challenge or challenge['status']!='consumed' or challenge['action_digest']!=auth['action_digest']:
            raise sqlite3.IntegrityError('wealth_challenge_mismatch')
    return result


def unknown(key):
    from lifecycle.service import active_scope
    from wealth_service import _failure
    scope=active_scope.get()
    if scope and scope.record:
        for op in scope.record.operations.values():
            if op.key==key: op.state='unknown'
        scope.record.status='manual'
        scope.record.reason='提交状态未知，请使用原请求键核查回执'
    return _failure('commit_unknown','无法核实提交状态，已停止自动执行；请使用原请求键查询。')


def recover(kind, args, key, replay=False):
    from wealth_service import _completed_order, _failure
    connection=None
    try:
        connection=pg02.get_connection()
        connection.execute('BEGIN IMMEDIATE')  # synchronize with original owner transaction
        row=_completed_order(connection,key)
        if row is None:
            return _failure('commit_not_recorded','经原请求键同步核实未找到订单，资金未提交；请重新读取任务状态。')
        if not same(row,kind,args): return _failure('idempotency_conflict','请求键已绑定其他操作或参数')
        result=canonical(connection,row)
        from lifecycle.service import active_scope
        if replay and active_scope.get() is None: result['data']['idempotent_replay']=True
        return result
    except Exception:
        return unknown(key)
    finally:
        if connection: connection.close()


def execute(kind, *, product_id=None, card_id=None, holding_id=None, amount_cents, idempotency_key,
            principal, security_context, now_provider=None, id_provider=None):
    from wealth_service import (_purchase_state,_redemption_state,_completed_order,_completed_order_data,
        _failure,_new_order_id)
    from transfer_service import _normalize_now,_now_datetime
    from utils import make_result
    timestamp,day=_normalize_now(now_provider); now=_now_datetime(now_provider)
    if not timestamp or now is None: return _failure('invalid_time','理财交易时间无效')
    args=arguments(kind,product_id,card_id,holding_id,amount_cents,idempotency_key)
    connection=None
    try:
        connection=pg02.get_connection()
        connection.execute('BEGIN IMMEDIATE')
        connection.assert_identity(security_context,now)
        if principal!=security_context.principal: return authorization_failure()
        # Locking owner first serializes with transfers AND protects absent holdings.
        prior=_completed_order(connection,idempotency_key)
        if prior:
            if not same(prior,kind,args): return _failure('idempotency_conflict','请求键已绑定其他操作或参数')
            connection.close(); connection=None
            return recover(kind,args,idempotency_key,replay=True)
        if kind=='REDEEM':
            initial=connection.execute('SELECT product_id,card_id FROM wealth_holdings WHERE holding_id=?',(holding_id,)).fetchone()
            if not initial: return _failure('holding_not_found','模拟理财持仓不存在')
            product_id,card_id=initial['product_id'],initial['card_id']
        connection.raw.execute('SELECT pg02.lock_wealth_resources(%s)',(product_id,))
        card=connection.raw.execute('SELECT account_id FROM core.cards WHERE public_id=%s FOR UPDATE',(card_id,)).fetchone()
        if not card: return _failure('card_not_found','银行卡不存在')
        account=connection.raw.execute('SELECT * FROM core.accounts WHERE id=%s FOR UPDATE',(card['account_id'],)).fetchone()
        holding=connection.raw.execute('SELECT * FROM pg02.wealth_holdings WHERE user_id=%s AND product_id=%s AND card_id=%s FOR UPDATE',
            (principal.user_id,product_id,card_id)).fetchone()
        assert_permission_not_escalated(security_context,action(kind),args,security_context.policy_engine)
        try:
            assert_write_authorization(connection,security_context,action(kind),args,now=now)
        except Exception as error:
            connection.rollback()
            return authorization_failure(getattr(error,'message',None),getattr(error,'code',None))
        write_audit(security_context,connection,'revalidation','ok',detail={'revalidation':'authorization_consumed'})
        pg02.fault('authorization_consumed')
        if kind=='BUY':
            product,card,profile,code,message=_purchase_state(connection,product_id,card_id,amount_cents,owner_scope(principal))
            if code: return _failure(code,message)
            name=product['name']; before=holding['principal_cents'] if holding else 0
        else:
            holding,code,message=_redemption_state(connection,holding_id,amount_cents,owner_scope(principal))
            if code: return _failure(code,message)
            name=holding['product_name']; before=holding['principal_cents']
        delta=amount_cents if kind=='BUY' else -amount_cents
        after=before+delta; posted_after=account['posted_minor']-delta
        if kind=='BUY':
            changed=connection.execute("UPDATE cards SET balance_cents=balance_cents-? WHERE card_id=? AND status='normal' AND balance_cents>=?",
                (amount_cents,card_id,amount_cents))
            if changed.rowcount!=1: raise sqlite3.IntegrityError('wealth_debit_failed')
            pg02.fault('funds_changed')
        if holding is None:
            holding_id=connection.execute("INSERT INTO wealth_holdings(user_id,product_id,card_id,principal_cents,status,purchased_at,updated_at) VALUES(?,?,?,?,'active',?,?) RETURNING holding_id",
                (principal.user_id,product_id,card_id,after,timestamp,timestamp)).fetchone()['holding_id']
        else:
            holding_id=holding['holding_id']
            connection.execute('UPDATE wealth_holdings SET principal_cents=?,status=?,updated_at=? WHERE holding_id=?',
                (after,'active' if after else 'redeemed',timestamp,holding_id))
        pg02.fault('holding_changed')
        order_id=_new_order_id(kind,id_provider)
        connection.execute('''INSERT INTO wealth_orders(order_id,idempotency_key,order_type,user_id,owner_id,card_id,product_id,holding_id,
            amount_cents,principal_before_cents,principal_after_cents,card_balance_after_cents,posted_before_cents,posted_after_cents,held_cents,
            status,created_at,business_date,principal_id,session_id,authorization_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?)''',
            (order_id,idempotency_key,kind,principal.user_id,principal.user_id,card_id,product_id,holding_id,amount_cents,before,after,
             posted_after-account['held_minor'],account['posted_minor'],posted_after,account['held_minor'],timestamp,day,
             security_context.principal.principal_id,security_context.session_id,security_context.grant.authorization_id))
        if kind=='REDEEM':
            connection.raw.execute('SELECT pg02.credit_wealth_redemption(%s)',(order_id,))
            pg02.fault('funds_changed')
        pg02.fault('before_ledger')
        connection.execute('''INSERT INTO transactions(card_id,date,merchant,category,amount_cents,direction,owner_id,transaction_type,classification_source,source_wealth_order_id)
            VALUES(?,?,?,?,?,?,?,?,'wealth_service',?)''',
            (card_id,day,name,'理财购买' if kind=='BUY' else '理财赎回',amount_cents,'expense' if kind=='BUY' else 'income',principal.user_id,
             'wealth_buy' if kind=='BUY' else 'wealth_redeem',order_id))
        connection.execute('''INSERT INTO operation_logs(operation_type,target_type,target_id,target_display,before_json,after_json,result,message,created_at)
            VALUES(?,'wealth_holding',?,?,?,?, 'success',?,?)''',
            (action(kind),holding_id,name,json.dumps({'principal_cents':before}),json.dumps({'principal_cents':after}),'模拟理财交易完成',timestamp))
        write_audit(security_context,connection,'execution','ok')
        write_audit(security_context,connection,'verification','ok',detail={'write_verification':'passed'})
        completed=_completed_order(connection,idempotency_key)
        expected_order=dict(order_id=order_id,idempotency_key=idempotency_key,order_type=kind,user_id=principal.user_id,
            owner_id=principal.user_id,card_id=card_id,product_id=product_id,holding_id=holding_id,amount_cents=amount_cents,
            principal_before_cents=before,principal_after_cents=after,card_balance_after_cents=posted_after-account['held_minor'],
            posted_before_cents=account['posted_minor'],posted_after_cents=posted_after,held_cents=account['held_minor'],
            status='completed',created_at=timestamp,business_date=day,principal_id=principal.principal_id,
            session_id=security_context.session_id,authorization_id=security_context.grant.authorization_id)
        if completed is None or any(completed[k]!=v for k,v in expected_order.items()):
            raise sqlite3.IntegrityError('wealth_order_snapshot_mismatch')
        result=make_result(True,'模拟理财购买成功' if kind=='BUY' else '模拟理财赎回成功',mark_verified(_completed_order_data(completed,False)))
        pg02.fault('before_receipt')
        from lifecycle.service import record_commit
        record_commit(connection,security_context,result)
        # Independent exact current delta check is ONLY for the transaction being committed.
        latest=connection.raw.execute('SELECT posted_minor,held_minor FROM core.accounts WHERE id=%s',(account['id'],)).fetchone()
        actual=connection.execute('SELECT principal_cents,status FROM wealth_holdings WHERE holding_id=?',(holding_id,)).fetchone()
        if latest!={'posted_minor':posted_after,'held_minor':account['held_minor']} or actual!={'principal_cents':after,'status':'active' if after else 'redeemed'}:
            raise sqlite3.IntegrityError('wealth_delta_mismatch')
        if canonical(connection,completed)!=result: raise sqlite3.IntegrityError('wealth_receipt_mismatch')
        audits=connection.execute("SELECT stage FROM security_audit_records WHERE authorization_id=? AND outcome='ok' AND stage IN ('execution','verification')",(security_context.grant.authorization_id,)).fetchall()
        if {r['stage'] for r in audits}!={'execution','verification'}: raise sqlite3.IntegrityError('wealth_audit_missing')
        connection.financial=True
        connection.commit()
        return result
    except pg02.CommitUnknown:
        connection.close(); connection=None
        return recover(kind,args,idempotency_key)
    except Exception as error:
        if connection: connection.rollback()
        return _failure(getattr(error,'code',None) or 'wealth_execution_failed',getattr(error,'message',None) or '模拟理财执行未完成，事务已回滚')
    finally:
        if connection: connection.close()


def verify_result(result):
    connection=None
    try:
        connection=pg02.get_connection()
        from wealth_service import _completed_order
        order=connection.execute('SELECT idempotency_key FROM wealth_orders WHERE order_id=?',(result.data['order_id'],)).fetchone()
        if not order: return False,'理财订单不存在'
        canonical_result=canonical(connection,_completed_order(connection,order['idempotency_key']))
        actual=dict(result.data); actual['idempotent_replay']=False
        return actual==canonical_result['data'],'已核验不可变理财执行快照及来源，不将后续余额当作历史快照'
    except Exception:
        return False,'理财执行快照核验失败'
    finally:
        if connection: connection.close()
