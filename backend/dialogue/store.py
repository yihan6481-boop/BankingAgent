"""Short PG transactions, persistent conversation ownership, dedup and leases."""
from contextlib import contextmanager
import hashlib
import json
from uuid import uuid4
from psycopg.types.json import Jsonb
from storage.pg02 import current_context, store as pg_store
from lifecycle.store import TaskError
from foundation.snapshot import session_reference


@contextmanager
def transaction():
    context=current_context()
    with pg_store()._pool.connection() as c, c.transaction():
        c.execute("SELECT set_config('banking.owner_id',%s,true)",(str(context.owner_uuid),))
        yield c


class DialogueStore:
    def begin_answer_retry(self,p,cid,tid,request_id,lease_seconds,*,authorization_revision=None):
        """Explicit bounded expression attempt; original calls are immutable."""
        with transaction() as c:
            conv=self.load(p,cid,c,True)
            row=c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s AND dialogue_id=%s FOR UPDATE',(tid,cid)).fetchone()
            if not row:raise TaskError('turn_not_found',status=404)
            old=row['usage'] or {};prior=old.get('answer_retry') or {}
            if authorization_revision is not None:
                from .read_results import evidence_revision
                calls=c.execute('SELECT * FROM pg02.dialogue_calls WHERE turn_id=%s',(tid,)).fetchall()
                if authorization_revision!=evidence_revision(calls,p,cid,tid):raise TaskError('authorization_evidence_changed')
                if old.get('authorization_answer',{}).get('revision')==authorization_revision:return row,False
                if row['status']=='running':raise TaskError('dialogue_busy')
            elif prior.get('request_id')==request_id:return row,False
            elif prior:raise TaskError('answer_retry_exhausted','这条消息已尝试重新组织，请保留结果或发起新的只读查询。')
            elif row['status']!='failed' or row['error_code'] not in {'answer_contract_invalid','answer_semantically_unverified','semantic_review_unavailable','semantic_review_protocol_invalid','semantic_review_budget_exhausted','answer_correction_service_failure','model_timeout','model_http_429','loop_budget_exhausted','no_progress','dialogue_service_failure'}:
                raise TaskError('answer_retry_not_available')
            if conv['active_turn']:raise TaskError('dialogue_busy')
            if c.execute("SELECT 1 FROM pg02.dialogue_calls WHERE turn_id=%s AND (status!='completed' OR outcome='unknown') AND resolution IS NULL LIMIT 1",(tid,)).fetchone():raise TaskError('answer_retry_requires_verification','本轮有结果未知的操作，请先核实原引用，未重新执行。')
            from recall_bridge.context_guard import valid
            if not valid(p,old.get('r4_dependencies',{}),c,profile=old.get('r4_profile_used',False)):raise TaskError('memory_changed_during_answer')
            generation=int(old.get('lease_generation',0))+1;self.leases[tid]=generation
            usage=dict(old,prior_generation_requests=old.get('prior_generation_requests',0)+old.get('requests',0),requests=0,semantic_checks=0,
                lease_generation=generation)
            attempt=dict(request_id=request_id,original_error=row['error_code'],original_requests=old.get('requests',0),original_termination_reason=old.get('termination_reason'))
            if authorization_revision is not None:
                usage['authorization_answer']=dict(attempt,revision=authorization_revision)
            else:usage['answer_retry']=attempt
            # A new expression attempt must not reuse stale fallback parts or
            # stale rejection diagnostics as current evidence.
            for key in ['answer_fallback','r4_answer_parts','answer_failure_code','answer_contract_reason','semantic_review_result','semantic_review_error','failure_kind','termination_reason','stopped_calls']:
                usage.pop(key,None)
            c.execute("UPDATE pg02.dialogue_turns SET status='running',answer='',error_code=NULL,usage=%s WHERE id=%s",(Jsonb(usage),tid))
            c.execute("UPDATE pg02.dialogues SET active_turn=%s,lease_until=now()+(%s * interval '1 second') WHERE id=%s",(tid,lease_seconds,cid))
            return c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s',(tid,)).fetchone(),True

    def __init__(self):self.leases={}
    def call_read(self,p,cid,tid,call_id):
        with transaction() as c:
            self.load(p,cid,c)
            row=c.execute('SELECT a.* FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE t.dialogue_id=%s AND t.id=%s AND a.call_id=%s',(cid,tid,call_id)).fetchone()
            if not row:raise TaskError('reference_not_found',status=404)
            return row

    def authorized_reads(self,p,cid):
        with transaction() as c:
            self.load(p,cid,c)
            return c.execute("SELECT a.* FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE t.dialogue_id=%s AND a.resolution->>'kind'='authorized_query' ORDER BY a.created_at DESC LIMIT 4",(cid,)).fetchall()

    def reserve_preparation(self,tid,call_id):
        with transaction() as c:
            ref=str(uuid4())
            row=c.execute('UPDATE pg02.dialogue_calls SET preparation_reference=COALESCE(preparation_reference,%s) WHERE turn_id=%s AND call_id=%s RETURNING preparation_reference',(ref,tid,call_id)).fetchone()
            if not row:raise TaskError('invalid_association_source')
            return row['preparation_reference']

    def resolve_task_preparation(self,p,cid,ref,tid,call_id):
        # A reserved UUID is exact provenance. A list/name/amount is not proof.
        with transaction() as c:
            self.load(p,cid,c)
            c.execute("UPDATE pg02.dialogue_calls a SET resolution=%s FROM pg02.dialogue_turns t WHERE a.turn_id=t.id AND t.dialogue_id=%s AND a.preparation_reference=%s AND a.outcome='unknown' AND a.resolution IS NULL",
                (Jsonb(dict(source_turn=tid,source_call=call_id,verification_reference=ref)),cid,ref))

    def create(self,p,sid):
        cid='dc_'+uuid4().hex
        with transaction() as c:
            c.execute("INSERT INTO pg02.dialogues(id,owner_id,principal_id,engine,session_hash) VALUES(%s,%s,%s,'r2',%s)",
                      (cid,p.user_id,p.principal_id,session_reference(sid)))
        return cid

    def load(self,p,cid,c=None,lock=False):
        if c is None:
            with transaction() as conn: return self.load(p,cid,conn,lock)
        row=c.execute('SELECT * FROM pg02.dialogues WHERE id=%s AND owner_id=%s AND principal_id=%s'+(' FOR UPDATE' if lock else ''),
                      (cid,p.user_id,p.principal_id)).fetchone()
        if not row: raise TaskError('dialogue_not_found',status=404)
        return row

    def recent(self,p,cid,limit=12):
        """Bounded history read for the model context window (not the UI page)."""
        self.load(p,cid)
        with transaction() as c:
            return list(reversed(c.execute('SELECT * FROM pg02.dialogue_turns WHERE dialogue_id=%s ORDER BY number DESC LIMIT %s',(cid,limit)).fetchall()))

    def history(self,p,cid,limit=50,before=None):
        """Page read for the conversation-history UI.

        This is deliberately independent from the model context window: the UI
        may page through every stored turn (bounded per page) while the model
        still receives only the recent bounded window.
        """
        self.load(p,cid)
        with transaction() as c:
            total=c.execute('SELECT count(*) AS n FROM pg02.dialogue_turns WHERE dialogue_id=%s',(cid,)).fetchone()['n']
            if before is None:
                rows=c.execute('SELECT * FROM pg02.dialogue_turns WHERE dialogue_id=%s ORDER BY number DESC LIMIT %s',(cid,limit)).fetchall()
            else:
                rows=c.execute('SELECT * FROM pg02.dialogue_turns WHERE dialogue_id=%s AND number<%s ORDER BY number DESC LIMIT %s',(cid,before,limit)).fetchall()
        turns=list(reversed(rows))
        oldest=turns[0]['number'] if turns else None
        has_more=bool(oldest is not None and oldest>1)
        return turns,dict(total=total,returned=len(turns),has_more=has_more,
            next_before=oldest if has_more else None,oldest_number=oldest)

    def resume(self,p,cid,tid,lease_seconds):
        with transaction() as c:
            conv=self.load(p,cid,c,True)
            row=c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s AND dialogue_id=%s',(tid,cid)).fetchone()
            if not row:raise TaskError('turn_not_found',status=404)
            if row['status']=='completed':return row,False
            if conv['active_turn'] and c.execute('SELECT lease_until>now() AS live FROM pg02.dialogues WHERE id=%s',(cid,)).fetchone()['live']:
                raise TaskError('dialogue_busy',status=409)
            if row['number']!=conv['next_turn']-1 or row['status']=='cancelled' or row['cancel_requested']:
                raise TaskError('turn_not_resumable',status=409)
            c.execute("UPDATE pg02.dialogue_calls SET status='unknown',outcome='unknown' WHERE turn_id=%s AND status='started'",(tid,))
            generation=int(row['usage'].get('lease_generation',0))+1
            usage=dict(row['usage'],lease_generation=generation)
            self.leases[tid]=generation
            c.execute("UPDATE pg02.dialogue_turns SET status='running',answer='',error_code=NULL,usage=%s WHERE id=%s",(Jsonb(usage),tid))
            c.execute("UPDATE pg02.dialogues SET active_turn=%s,lease_until=now()+(%s * interval '1 second') WHERE id=%s",(tid,lease_seconds,cid))
            return c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s',(tid,)).fetchone(),True

    def stage(self,tid,assistant,invalid_ids,usage):
        if not self.active(tid):raise TaskError('generation_cancelled')
        with transaction() as c:
            changed=c.execute("UPDATE pg02.dialogue_turns SET pending_exchange=%s,usage=%s WHERE id=%s AND status='running' AND COALESCE((usage->>'lease_generation')::int,0)=%s",
                (Jsonb(dict(assistant=assistant,invalid_ids=invalid_ids)),Jsonb(usage),tid,self.leases.get(tid,0))).rowcount
            if changed!=1:raise TaskError('turn_lease_expired')

    def request_started(self,tid,usage):
        with transaction() as c:
            n=c.execute("UPDATE pg02.dialogue_turns SET usage=%s WHERE id=%s AND status='running' AND COALESCE((usage->>'lease_generation')::int,0)=%s",
                (Jsonb(usage),tid,self.leases.get(tid,0))).rowcount
            if n!=1:raise TaskError('turn_lease_expired')

    def begin(self,p,cid,client_id,text,lease_seconds=130):
        hashed=hashlib.sha256(text.encode()).hexdigest()
        with transaction() as c:
            conv=self.load(p,cid,c,True)
            from . import quota
            quota.lock(c)
            admission=quota.existing(c,client_id)
            if admission and (admission['dialogue_id']!=cid or admission['input_hash']!=hashed):
                raise TaskError('client_message_conflict',status=409)
            old=c.execute('SELECT * FROM pg02.dialogue_turns WHERE dialogue_id=%s AND client_id=%s',(cid,client_id)).fetchone()
            if old:
                if old['input_hash']!=hashed: raise TaskError('client_message_conflict',status=409)
                # Expired work is never dispatched again, including the same HTTP ID.
                if old['status']=='running' and c.execute('SELECT lease_until>now() AS live FROM pg02.dialogues WHERE id=%s',(cid,)).fetchone()['live'] is not True:
                    self._interrupt(c,old['id'])
                    c.execute('UPDATE pg02.dialogues SET active_turn=NULL,lease_until=NULL WHERE id=%s',(cid,))
                    old=c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s',(old['id'],)).fetchone()
                return old,False
            if conv['active_turn']:
                if c.execute('SELECT lease_until>now() AS live FROM pg02.dialogues WHERE id=%s',(cid,)).fetchone()['live']:
                    raise TaskError('dialogue_busy',status=409)
                self._interrupt(c,conv['active_turn'])
            tid='dt_'+uuid4().hex
            c.execute("INSERT INTO pg02.dialogue_turns(id,dialogue_id,client_id,number,input_hash,user_text,status) VALUES(%s,%s,%s,%s,%s,%s,'running')",
                      (tid,cid,client_id,conv['next_turn'],hashed,text))
            # The turn INSERT and quota admission commit or roll back together.
            # No tools / business tasks / paid HTTP can precede this commit.
            quota.admit(c,client_id,hashed,cid=cid,tid=tid)
            c.execute("UPDATE pg02.dialogues SET next_turn=next_turn+1,active_turn=%s,lease_until=now()+(%s * interval '1 second') WHERE id=%s",(tid,lease_seconds,cid))
            self.leases[tid]=1
            c.execute('UPDATE pg02.dialogue_turns SET usage=%s WHERE id=%s',(Jsonb(dict(lease_generation=1)),tid))
            return c.execute('SELECT * FROM pg02.dialogue_turns WHERE id=%s',(tid,)).fetchone(),True

    def _interrupt(self,c,tid):
        c.execute("UPDATE pg02.dialogue_turns SET status='interrupted',error_code='interrupted_unknown',answer='上一轮等待已中断。已完成的工具结果已保留；未确认的调用结果需读取原草稿或任务核实，未自动重放。' WHERE id=%s AND status='running'",(tid,))
        c.execute("UPDATE pg02.dialogue_calls SET status='unknown',outcome='unknown',protocol_status='unpaired' WHERE turn_id=%s AND status='started'",(tid,))

    def active(self,tid):
        with transaction() as c:
            row=c.execute('SELECT t.cancel_requested,t.status,t.usage,d.lease_until>now() AS live FROM pg02.dialogue_turns t JOIN pg02.dialogues d ON d.active_turn=t.id WHERE t.id=%s',(tid,)).fetchone()
        if not row or not row['live'] or row['status']!='running' or tid in self.leases and self.leases[tid]!=row['usage'].get('lease_generation',0): raise TaskError('turn_lease_expired')
        return not row['cancel_requested']

    def cancel(self,p,cid):
        with transaction() as c:
            conv=self.load(p,cid,c,True)
            if conv['active_turn']: c.execute('UPDATE pg02.dialogue_turns SET cancel_requested=true WHERE id=%s',(conv['active_turn'],))

    def progress(self,tid,label):
        with transaction() as c: c.execute('UPDATE pg02.dialogue_turns SET progress=%s WHERE id=%s AND status=\'running\'',(label,tid))

    def start_call(self,tid,call_id,name,args):
        if not self.active(tid): raise TaskError('generation_cancelled')
        with transaction() as c:
            n=c.execute("INSERT INTO pg02.dialogue_calls(turn_id,call_id,name,arguments,status) SELECT %s,%s,%s,%s,'started' FROM pg02.dialogue_turns WHERE id=%s AND status='running' AND COALESCE((usage->>'lease_generation')::int,0)=%s ON CONFLICT DO NOTHING",(tid,call_id,name,Jsonb(args),tid,self.leases.get(tid,0))).rowcount
            if n!=1: raise TaskError('duplicate_call_id')

    def finish_call(self,tid,call_id,result):
        outcome=('unknown' if result.get('status')=='commit_unknown' else
                 'succeeded' if result.get('status') in {'completed','saved_incomplete','ambiguous'} or result.get('effects') else 'failed')
        with transaction() as c:
            c.execute("UPDATE pg02.dialogue_calls SET status='completed',outcome=%s,result=%s WHERE turn_id=%s AND call_id=%s",(outcome,Jsonb(result),tid,call_id))

    def calls(self,tid):
        with transaction() as c: return c.execute('SELECT call_id,name,arguments,status,result,outcome,protocol_status,resolution,preparation_reference FROM pg02.dialogue_calls WHERE turn_id=%s ORDER BY created_at,call_id',(tid,)).fetchall()

    def exchange(self,tid,exchanges):
        with transaction() as c:
            changed=c.execute('UPDATE pg02.dialogue_turns SET exchanges=%s,pending_exchange=NULL WHERE id=%s AND status=\'running\' AND COALESCE((usage->>\'lease_generation\')::int,0)=%s',(Jsonb(exchanges),tid,self.leases.get(tid,0))).rowcount
            if changed!=1:raise TaskError('turn_lease_expired')
            for m in exchanges:
                if m['role']=='tool':c.execute("UPDATE pg02.dialogue_calls SET protocol_status='paired' WHERE turn_id=%s AND call_id=%s",(tid,m['tool_call_id']))

    def finish(self,tid,status,answer,error=None,usage=None):
        with transaction() as c:
            generation=self.leases.get(tid)
            changed=c.execute("UPDATE pg02.dialogue_turns SET status=%s,answer=%s,error_code=%s,usage=%s,progress='已结束' WHERE id=%s AND status='running' AND (%s::int IS NULL OR COALESCE((usage->>'lease_generation')::int,0)=%s)",(status,answer,error,Jsonb(dict(usage or {},**({'lease_generation':generation} if generation is not None else {}))),tid,generation,generation)).rowcount
            if changed!=1:return
            c.execute('UPDATE pg02.dialogues SET active_turn=NULL,lease_until=NULL WHERE active_turn=%s',(tid,))
            c.execute("UPDATE pg02.dialogue_calls SET status='unknown',outcome='unknown',protocol_status='unpaired' WHERE turn_id=%s AND status='started'",(tid,))

    def rebind(self,p,cid,sid):
        # Server-owned hash; raw login credentials never enter dialogue storage.
        new=session_reference(sid)
        with transaction() as c:
            conv=self.load(p,cid,c,True)
            if conv['active_turn'] and c.execute('SELECT lease_until>now() AS live FROM pg02.dialogues WHERE id=%s',(cid,)).fetchone()['live'] is not True:
                self._interrupt(c,conv['active_turn'])
                c.execute('UPDATE pg02.dialogues SET active_turn=NULL,lease_until=NULL WHERE id=%s',(cid,))
                conv=dict(conv,active_turn=None)
            if conv['session_hash']!=new:
                if conv['active_turn'] and c.execute('SELECT lease_until>now() AS live FROM pg02.dialogues WHERE id=%s',(cid,)).fetchone()['live']:
                    raise TaskError('dialogue_busy',status=409)
                c.execute('UPDATE pg02.dialogues SET session_hash=%s WHERE id=%s',(new,cid))
        self.restore_links(p,cid)

    def associate(self,p,cid,ref,tid,call_id,context=None):
        from foundation.snapshot import resolve
        value=resolve(p,ref)  # Business ownership checked independently of dialogue.
        payload=context if isinstance(context,dict) else {}
        with transaction() as c:
            self.load(p,cid,c)
            source=c.execute('SELECT 1 FROM pg02.dialogue_turns t JOIN pg02.dialogue_calls a ON a.turn_id=t.id WHERE t.id=%s AND t.dialogue_id=%s AND a.call_id=%s',(tid,cid,call_id)).fetchone()
            if not source:raise TaskError('invalid_association_source')
            # Keep the first exact source turn; fill context only when absent.
            c.execute('''INSERT INTO pg02.dialogue_links(dialogue_id,reference,kind,source_turn,source_call,context)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(dialogue_id,reference) DO UPDATE SET context=
                CASE WHEN pg02.dialogue_links.context='{}'::jsonb THEN EXCLUDED.context
                     ELSE pg02.dialogue_links.context END''',
                (cid,ref,value['kind'],tid,call_id,Jsonb(payload)))

    def links(self,p,cid):
        with transaction() as c:
            self.load(p,cid,c)
            return c.execute('SELECT * FROM pg02.dialogue_links WHERE dialogue_id=%s ORDER BY associated_at,reference',(cid,)).fetchall()

    def focus(self,p,cid,ref):
        with transaction() as c:
            self.load(p,cid,c,True)
            if ref and not c.execute('SELECT 1 FROM pg02.dialogue_links WHERE dialogue_id=%s AND reference=%s',(cid,ref)).fetchone():raise TaskError('reference_not_associated')
            c.execute('UPDATE pg02.dialogue_links SET focused=false WHERE dialogue_id=%s',(cid,))
            if ref:c.execute('UPDATE pg02.dialogue_links SET focused=true WHERE dialogue_id=%s AND reference=%s',(cid,ref))

    def restore_links(self,p,cid):
        from .links import references
        # Exact persisted tool evidence migrates historical dialogues lazily.
        # No owner-wide/session-wide import. Repeated restore is idempotent.
        with transaction() as c:
            self.load(p,cid,c)
            rows=c.execute('SELECT a.*,t.id AS tid FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE t.dialogue_id=%s AND a.result IS NOT NULL ORDER BY t.number,a.created_at',(cid,)).fetchall()
        from .links import payment_context
        known={x['reference'] for x in self.links(p,cid)}
        for a in rows:
            context=payment_context(a['name'],a['arguments'],a['result'])
            for ref in references(a['name'],a['arguments'],a['result']):
                if ref not in known:
                    self.associate(p,cid,ref,a['tid'],a['call_id'],
                        context=context if context and ref==context.get('task_id') else None)
                    known.add(ref)

    def unresolved(self,p,cid):
        with transaction() as c:
            self.load(p,cid,c)
            rows=c.execute("SELECT a.*,t.number,ARRAY(SELECT l.reference FROM pg02.dialogue_links l WHERE l.source_turn=a.turn_id AND l.source_call=a.call_id ORDER BY l.reference) AS associated_references FROM pg02.dialogue_calls a JOIN pg02.dialogue_turns t ON t.id=a.turn_id WHERE (t.dialogue_id=%s OR a.arguments->>'reference' IN (SELECT reference FROM pg02.dialogue_links WHERE dialogue_id=%s) OR EXISTS(SELECT 1 FROM pg02.dialogue_links original JOIN pg02.dialogue_links current ON current.reference=original.reference WHERE original.source_turn=a.turn_id AND original.source_call=a.call_id AND current.dialogue_id=%s)) AND (a.outcome='unknown' OR a.status IN ('started','unknown')) AND a.resolution IS NULL ORDER BY t.number,a.created_at",(cid,cid,cid)).fetchall()
        return [dict(turn_id=a['turn_id'],call_id=a['call_id'],operation=a['name'],arguments=a['arguments'],
                     protocol_status=a['protocol_status'],outcome='unknown',result=a['result'],
                     related_references=list(dict.fromkeys([*a['associated_references'],*([a['arguments']['reference']] if a['arguments'].get('reference') else []),*([a['preparation_reference']] if a.get('preparation_reference') else [])])),
                     verification_reference=a.get('preparation_reference'),
                     observed_at=a['created_at'].isoformat(),scope='original operation outcome unproven; do not replay',
                     verification=['task_verify'] if a.get('preparation_reference') else ['task_recover'] if a['name']=='task_prepare' else ['task_verify'] if a['name'].startswith('task_') else ['draft_read','operator investigation if no exact reference']) for a in rows]

    def source_messages(self,p,cid,references):
        if not references:return []
        with transaction() as c:
            self.load(p,cid,c)
            # With an unfinished goal, do not infer that unstructured user
            # constraints became obsolete. Preserve exact source text until
            # the goal becomes terminal. These notes never assert business facts.
            return c.execute("SELECT id,number,user_text FROM pg02.dialogue_turns WHERE dialogue_id=%s AND number >= (SELECT min(t.number) FROM pg02.dialogue_links l JOIN pg02.dialogue_turns t ON t.id=l.source_turn WHERE l.dialogue_id=%s AND l.reference=ANY(%s)) ORDER BY number",(cid,cid,list(references))).fetchall()

    def resolve_preparation(self,p,cid,ref,result,tid,call_id):
        # Only the core recovery service's exact reserved UUID proves preparation.
        if result.get('status')!='completed' or result.get('data',{}).get('task_created') is not True:return
        with transaction() as c:
            self.load(p,cid,c)
            c.execute("UPDATE pg02.dialogue_calls a SET resolution=%s WHERE EXISTS(SELECT 1 FROM pg02.dialogue_links l WHERE l.dialogue_id=%s AND l.reference=%s) AND a.name IN ('task_prepare','task_recover') AND a.outcome='unknown' AND a.resolution IS NULL AND a.arguments->>'reference'=%s",(Jsonb(dict(source_turn=tid,source_call=call_id,verification_reference=ref)),cid,ref,ref))

    def resolve_created_reference(self,p,cid,ref,tid,call_id):
        # A source-call association proves an object was durably created. A
        # name/amount/time match never does. Called only after an owned read.
        with transaction() as c:
            self.load(p,cid,c)
            c.execute("UPDATE pg02.dialogue_calls a SET resolution=%s FROM pg02.dialogue_links l WHERE l.source_turn=a.turn_id AND l.source_call=a.call_id AND l.reference=%s AND EXISTS(SELECT 1 FROM pg02.dialogue_links current WHERE current.dialogue_id=%s AND current.reference=l.reference) AND a.outcome='unknown' AND a.resolution IS NULL AND ((a.name IN ('transfer_create','query_create') AND l.kind IN ('transfer','query')) OR (a.name='query_execute' AND l.kind='result'))",(Jsonb(dict(source_turn=tid,source_call=call_id,verification_reference=ref)),ref,cid))
