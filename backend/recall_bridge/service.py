"""Thin delivery/reconciliation boundary. All semantics stay at Hindsight/LLM.

Bank tools and transaction authorization are deliberately absent from this file.
"""
import json
import os
from pathlib import Path
import time
from dialogue.store import transaction
from lifecycle.store import TaskError
from security.redaction import redact
from dialogue.projection import safe_text
from .state import Store,public_operation,unpack,checksum
from .transport import Hindsight,MemoryUnavailable

MISSION='保留用户确定自述的背景、交流偏好、安排和当前目标。假设、举例、引用、他人陈述和否定不作为用户事实。助手自称交易完成不是交易凭据。保留中文金额、日期和取消范围；记忆不赋予任何银行权限。'


def document_ref(ref,generation):return ref+'_g'+str(generation)


def configured():return os.getenv('BA_R4_ENABLED')=='1'


def transport():
    if not configured():raise MemoryUnavailable('memory_not_enabled')
    from .config import read_client,MemoryConfigurationError
    try:
        path=os.environ.get('BA_R4_SERVICE_CONFIG')
        if not path:raise MemoryConfigurationError('memory_configuration_path_missing')
        return Hindsight(**read_client(path))
    except MemoryConfigurationError as exc:raise MemoryUnavailable(str(exc)) from None


class Service:
    def __init__(self,client=None):self.store=Store();self.client=client

    def remote(self):
        if self.client is None:self.client=transport()
        return self.client

    def close(self):
        if self.client:self.client.close()

    def _children(self,p,row,status):
        # Native batch parents do NOT cascade-delete their child task payloads.
        # Persist only opaque child references in the existing audit/outbox, so
        # a crash after partial cleanup never loses the remaining handles.
        from uuid import UUID
        with transaction() as c:
            self.store.space(p,c,True);self.store.operation(p,row['id'],c)
            refs=[r['trusted_origin'] for r in c.execute("SELECT DISTINCT trusted_origin FROM recall.events WHERE reference=%s AND action='native_child_link'",(row['id'],))]
            bound=c.execute("SELECT 1 FROM recall.events WHERE reference=%s AND action='native_children_bound'",(row['id'],)).fetchone()
            if status is not None:
                children=status.get('child_operations') or []
                expected=(status.get('result_metadata') or {}).get('num_sub_batches',len(children))
                if type(expected) is not int or not 0<=expected<=32:raise MemoryUnavailable('native_child_scope_unverified',effect='unknown')
                for child in children:
                    ref=str(UUID(child['operation_id']))
                    if ref not in refs:
                        self.store.event(c,row['id'],'native_child_link',row['generation'],ref);refs.append(ref)
                if len(refs)<expected:raise MemoryUnavailable('native_child_scope_unverified',effect='unknown')
                if not bound:self.store.event(c,row['id'],'native_children_bound',row['generation'],'verified_native_parent')
            elif not bound and row.get('submitted'):
                raise MemoryUnavailable('native_child_scope_unverified',effect='unknown')
            return refs

    def save(self,p,content,origin,*,automatic=False):
        content=safe_text(content) if isinstance(content,str) else content
        if not isinstance(content,str) or not content.strip() or len(content)>4000:raise TaskError('invalid_memory_content',status=422)
        row=self.store.enqueue(p,content,origin,automatic=automatic)
        return public_operation(row)

    def save_business(self,p,reference,origin):
        from lifecycle.store import TaskStore
        from dialogue.projection import project
        record=TaskStore().load(reference,p)  # includes schema/digest/receipt validation
        if record.status!='completed':raise TaskError('memory_verified_receipt_required',status=409)
        steps=[dict(step_id=step.step_id,tool=step.tool_name,data=project(step.result.data))
               for step in record.task.steps if step.status=='completed' and step.result and step.result.success and step.verification_status=='passed']
        if not steps:raise TaskError('memory_verified_receipt_required',status=409)
        content='工具核实的历史业务结果摘要，非当前余额、非未来授权：'+json.dumps(dict(
            task_reference=reference,revision=record.revision,historical=True,results=steps),ensure_ascii=False)
        if len(content)>4000:raise TaskError('memory_business_source_too_large',status=409)
        return public_operation(self.store.enqueue(p,redact(content),origin,origin_kind='verified_tool',related_reference=reference))

    def change(self,p,ref,version,content=None,*,delete=False,origin='authenticated_ui'):
        if not delete:
            content=safe_text(content) if isinstance(content,str) else content
            if not isinstance(content,str) or not content.strip() or len(content)>4000:raise TaskError('invalid_memory_content',status=422)
        self.bind_legacy_coverage(p,ref)
        return public_operation(self.store.change(p,ref,version,content=content,delete=delete,origin=origin))

    def bind_legacy_coverage(self,p,ref):
        """Read-only native documents prove old exact quotes; bind positions
        with CAS. Ambiguous/unprovable old content is never guessed or erased.
        """
        with transaction() as c:
            target=self.store.source(p,ref,c);space=self.store.space(p,c)
            rows=c.execute('SELECT * FROM recall.sources WHERE origin_ref=%s AND origin_kind=\'user\' AND NOT coverage_retired',(target['origin_ref'],)).fetchall()
            text=c.execute('SELECT user_text FROM pg02.dialogue_turns WHERE id=%s',(target['origin_ref'],)).fetchone()['user_text']
        for row in rows:
            if row['span_start'] is not None or row['generation']!=1:continue
            if row['content_hash']==checksum(text):quote=text
            elif row['state']=='active':
                doc=self.remote().document(space['bank_ref'],document_ref(row['id'],1))
                quote=(doc or {}).get('original_text','')
                if checksum(quote)!=row['content_hash']:continue
            else:
                with transaction() as c:
                    self.store.space(p,c)
                    op=c.execute('SELECT payload FROM recall.operations WHERE source_ref=%s AND generation=1 AND payload IS NOT NULL ORDER BY created_at LIMIT 1',(row['id'],)).fetchone()
                quote=unpack(op['payload'])['content'] if op else ''
            start=text.find(quote)
            if not quote or start<0 or text.find(quote,start+1)>=0:continue
            with transaction() as c:
                self.store.space(p,c,True)
                c.execute('UPDATE recall.sources SET span_start=%s,span_end=%s WHERE id=%s AND generation=1 AND content_hash=%s AND span_start IS NULL',(start,start+len(quote),row['id'],row['content_hash']))

    def state(self,p,ref):
        with transaction() as c:
            row=self.store.operation(p,ref,c);s=self.store.source(p,row['source_ref'],c)
            result=public_operation(row)
            if row['kind']!='delete' and (row['generation']!=s['generation'] or s['state']!='active'):
                result['availability']='blocked' if result['availability']=='blocked' or row['generation']!=s['generation'] or s['state'] in {'invalidated','deleted'} else 'pending'
            return result

    def recover(self,p,ref):
        # Explicit owned recovery only. Native UUID/document/generation remain
        # unchanged; no second retain and no automatic paid-budget fallback.
        with transaction() as c:
            self.store.space(p,c,True);row=self.store.operation(p,ref,c)
            valid,_,_=self._valid(p,row,c)
            if not valid or row['error_code']!='paid_budget_exhausted':raise TaskError('memory_recovery_not_available',status=409)
            if c.execute("SELECT 1 FROM recall.events WHERE reference=%s AND action='native_retry_sent'",(ref,)).fetchone():raise TaskError('memory_recovery_limit',status=409)
            c.execute("UPDATE recall.operations SET error_code='native_recovery_requested',retry_at=NULL,updated_at=now() WHERE id=%s",(ref,))
            self.store.event(c,ref,'native_recovery_requested',row['generation'],'authenticated_recovery_http')
        return self.state(p,ref)

    def _claim(self,p):
        with transaction() as c:
            space=self.store.space(p,c,True)
            if c.execute("SELECT 1 FROM recall.operations WHERE lease_until>now() LIMIT 1").fetchone():return None
            row=c.execute("SELECT o.* FROM recall.operations o JOIN recall.sources s ON s.id=o.source_ref "
                "WHERE o.state IN ('queued','running','reconciling') AND (o.retry_at IS NULL OR o.retry_at<=now()) "
                "AND (o.error_code IS DISTINCT FROM 'paid_budget_exhausted' OR s.generation<>o.generation OR s.state='deleted' OR o.kind='delete') "
                "AND NOT EXISTS(SELECT 1 FROM recall.operations older WHERE older.source_ref=o.source_ref AND older.state IN ('queued','running','reconciling') AND (older.created_at,older.id)<(o.created_at,o.id)) "
                "AND (o.submitted OR NOT s.automatic OR o.kind='delete' OR %s::timestamptz IS NULL OR %s::timestamptz<now()-interval '30 seconds') "
                "ORDER BY CASE WHEN o.kind='delete' OR o.state='reconciling' OR s.state='invalidated' THEN 0 ELSE 1 END,o.created_at,o.id LIMIT 1 FOR UPDATE OF o",(space['last_auto_submitted'],space['last_auto_submitted'])).fetchone()
            if not row:return None
            row=c.execute("UPDATE recall.operations SET lease_generation=lease_generation+1,lease_until=now()+interval '15 seconds',updated_at=now() WHERE id=%s RETURNING *",(row['id'],)).fetchone()
            return space,row

    def _event_update(self,p,row,*,state,effect,availability,error=None,done=False):
        with transaction() as c:
            self.store.space(p,c,True)
            result=c.execute('UPDATE recall.operations SET state=%s,effect=%s,availability=%s,error_code=%s,lease_until=NULL, '
                'payload=CASE WHEN %s THEN NULL ELSE payload END,updated_at=now() '
                'WHERE id=%s AND lease_generation=%s RETURNING *',
                (state,effect,availability,error,done,row['id'],row['lease_generation'])).fetchone()
            if result:self.store.event(c,row['id'],state,row['generation'],'memory_worker')
            if result:
                if error and ('unavailable' in error or 'memory_http_' in error):
                    c.execute("UPDATE recall.operations SET failures=failures+1,retry_at=CASE WHEN failures>=2 THEN now()+interval '30 seconds' ELSE NULL END WHERE id=%s",(row['id'],))
                else:c.execute('UPDATE recall.operations SET failures=0,retry_at=NULL WHERE id=%s',(row['id'],))
            return result

    def _valid(self,p,row,c):
        space=self.store.space(p,c)
        source=self.store.source(p,row['source_ref'],c)
        valid=source['generation']==row['generation'] and source['state']!='deleted'
        if row['kind']=='save':
            # Origin tombstones forbid a fresh historical replay. Existing
            # sibling fragments remain valid after a partial correction; whole
            # origin withdrawal advances every affected source generation under
            # the same owner lock, including queued/in-flight fragments.
            if source['automatic']:
                # A profile-only consent revision must not revoke automatic
                # retention. Automatic withdrawal fences pending generations.
                valid=valid and space['auto_enabled']
        return bool(valid),source,space

    def _retain(self,p,row,bank,payload):
        remote=self.remote();remote.create(bank,MISSION)
        with transaction() as c:
            self.store.space(p,c,True);valid,_,_=self._valid(p,row,c)
            current=c.execute('SELECT lease_generation FROM recall.operations WHERE id=%s',(row['id'],)).fetchone()
            if current['lease_generation']!=row['lease_generation']:return None
            if valid:
                # Only the bounded async enqueue HTTP request holds this lock,
                # never the native model job. Withdrawal either precedes this
                # submission or fences its in-flight result after it returns.
                remote.retain(bank,payload['content'],document_ref(row['source_ref'],row['generation']),str(row['external_ref']),source=row['source_ref'],version=row['generation'])
            else:c.execute('UPDATE recall.operations SET submitted=false WHERE id=%s AND lease_generation=%s',(row['id'],row['lease_generation']))
        if not valid:self._event_update(p,row,state='cancelled',effect='none',availability='blocked',done=True)
        return valid

    def advance(self,p):
        claimed=self._claim(p)
        if not claimed:return False
        space,row=claimed;ref=row['source_ref'];bank=space['bank_ref']
        try:
            remote=self.remote()
            with transaction() as c:
                valid,source,space=self._valid(p,row,c)
                old_claim=c.execute('SELECT lease_generation FROM recall.operations WHERE id=%s',(row['id'],)).fetchone()
                if old_claim['lease_generation']!=row['lease_generation']:return False
                if not row['submitted'] and valid:
                    c.execute("UPDATE recall.operations SET submitted=true,state='running',effect='unknown' WHERE id=%s AND lease_generation=%s",(row['id'],row['lease_generation']))
                    if source['automatic'] and row['kind']=='save':c.execute('UPDATE recall.spaces SET last_auto_submitted=now()')
            if not row['submitted']:
                if not valid:
                    with transaction() as c:
                        self.store.space(p,c,True)
                        pending=c.execute("SELECT 1 FROM recall.operations WHERE source_ref=%s AND id<>%s AND state IN ('queued','running','reconciling')",(ref,row['id'])).fetchone()
                        if row['kind']=='save' and not pending:c.execute("UPDATE recall.sources SET state='deleted',content_hash='',updated_at=now() WHERE id=%s AND state IN ('pending','invalidated') AND EXISTS(SELECT 1 FROM recall.operations WHERE id=%s AND lease_generation=%s)",(ref,row['id'],row['lease_generation']))
                    self._event_update(p,row,state='cancelled',effect='none',availability='blocked',done=True)
                    return True
                # Native document identities are immutable across generations.
                # A late old DELETE can never target a newly corrected document.
                # Remove a bounded old generation per pass before replacement.
                with transaction() as c:
                    self.store.space(p,c)
                    prior=c.execute("SELECT * FROM recall.operations WHERE source_ref=%s AND kind<>'delete' AND generation<%s AND generation>%s ORDER BY generation LIMIT 1",
                        (ref,row['generation'],row['cleanup_generation'])).fetchone()
                if prior:
                    with transaction() as c:
                        self.store.space(p,c,True)
                        c.execute('UPDATE recall.operations SET submitted=false WHERE id=%s AND lease_generation=%s',(row['id'],row['lease_generation']))
                    old_doc=document_ref(ref,prior['generation'])
                    remote.delete(bank,old_doc)
                    if remote.document(bank,old_doc) is not None:raise MemoryUnavailable('memory_cleanup_unverified',effect='unknown')
                    native=remote.operation(bank,str(prior['external_ref']))
                    if native is not None and native.get('status') not in {'completed','failed'}:raise MemoryUnavailable('memory_old_task_not_terminal',effect='unknown')
                    for child_ref in self._children(p,prior,native):
                        child=remote.operation(bank,child_ref)
                        if child is None:continue
                        if child.get('status') not in {'completed','failed'}:raise MemoryUnavailable('memory_old_child_not_terminal',effect='unknown')
                        remote.delete_operation(bank,child_ref)
                        if remote.operation(bank,child_ref) is not None:raise MemoryUnavailable('memory_child_cleanup_unverified',effect='unknown')
                        # One child per worker pass; bounded wait and restartable.
                        self._event_update(p,row,state='reconciling',effect='partial',availability='blocked')
                        return True
                    # Terminal native task payloads contain old source text too.
                    # Purge them only after a persisted terminal local operation;
                    # restart can safely repeat this exact bounded cleanup.
                    if prior['state'] not in {'succeeded','failed','cancelled'}:raise MemoryUnavailable('memory_old_task_not_terminal',effect='unknown')
                    remote.delete_operation(bank,str(prior['external_ref']))
                    if remote.operation(bank,str(prior['external_ref'])) is not None:raise MemoryUnavailable('memory_task_cleanup_unverified',effect='unknown')
                    with transaction() as c:
                        self.store.space(p,c,True)
                        c.execute('UPDATE recall.operations SET submitted=false,cleanup_generation=%s,lease_until=NULL WHERE id=%s AND lease_generation=%s',
                            (prior['generation'],row['id'],row['lease_generation']))
                    return True
                if row['kind']!='delete':
                    payload=unpack(row['payload'])
                    if (payload['source'],payload['generation'])!=(ref,row['generation']):raise TaskError('memory_delivery_payload_invalid')
                    submitted=self._retain(p,row,bank,payload)
                    if not submitted:return submitted is False
                    self._event_update(p,row,state='running',effect='unknown',availability='pending')
                    return True
            if row['kind']!='delete':
                status=remote.operation(bank,str(row['external_ref']))
                if status is None:
                    with transaction() as c:
                        self.store.space(p,c,True)
                        unfinished=c.execute("SELECT 1 FROM recall.operations WHERE source_ref=%s AND kind<>'delete' AND generation<%s AND generation>%s LIMIT 1",(ref,row['generation'],row['cleanup_generation'])).fetchone()
                        if unfinished:c.execute("UPDATE recall.operations SET submitted=false,lease_until=NULL,state='reconciling',error_code='resume_prior_cleanup' WHERE id=%s AND lease_generation=%s",(row['id'],row['lease_generation']))
                    if unfinished:return True
                    # Exact UUID read first. Lost submit ack can only be retried
                    # through the same native idempotent identity, once.
                    with transaction() as c:
                        self.store.space(p,c,True)
                        retried=c.execute("SELECT 1 FROM recall.events WHERE reference=%s AND action='original_submit_recovery'",(row['id'],)).fetchone()
                        valid,_,_=self._valid(p,row,c)
                        if not retried and valid:self.store.event(c,row['id'],'original_submit_recovery',row['generation'],'memory_worker')
                    if retried or not valid:
                        self._event_update(p,row,state='reconciling',effect='unknown',availability='blocked',error='native_operation_not_found')
                        return False
                    payload=unpack(row['payload'])
                    submitted=self._retain(p,row,bank,payload)
                    if not submitted:return submitted is False
                    self._event_update(p,row,state='reconciling',effect='unknown',availability='pending')
                    return True
                status_name=status.get('status')
                self._children(p,row,status)
                if status_name=='failed' and valid and 'paid_budget_exhausted' in str(status.get('error_message','')):
                    if row['error_code']=='native_recovery_requested':
                        with transaction() as c:
                            self.store.space(p,c,True);valid,_,_=self._valid(p,row,c)
                            already=c.execute("SELECT 1 FROM recall.events WHERE reference=%s AND action='native_retry_sent'",(row['id'],)).fetchone()
                            if valid and not already:self.store.event(c,row['id'],'native_retry_sent',row['generation'],'verified_failed_native_uuid')
                        if valid and not already:
                            remote.retry_operation(bank,str(row['external_ref']))
                            self._event_update(p,row,state='reconciling',effect='partial',availability='blocked',error='native_retry_verifying')
                            return True
                    self._event_update(p,row,state='reconciling',effect='partial',availability='blocked',error='paid_budget_exhausted')
                    return False
                if status_name not in {'completed','failed'}:
                    self._event_update(p,row,state='reconciling' if not valid else 'running',effect='unknown',availability='blocked' if not valid else 'pending')
                    return False
                # Never treat cooperative cancellation as proof an in-flight
                # model has stopped; cancelled is intentionally not terminal here.
                with transaction() as c:valid,source,space=self._valid(p,row,c)
                if status_name=='failed' or not valid:
                    remote.delete(bank,document_ref(ref,row['generation']))
                    if remote.document(bank,document_ref(ref,row['generation'])) is not None:raise MemoryUnavailable('memory_cleanup_unverified',effect='unknown')
                    with transaction() as c:
                        self.store.space(p,c,True)
                        pending=c.execute("SELECT 1 FROM recall.operations WHERE source_ref=%s AND id<>%s AND state IN ('queued','running','reconciling')",(ref,row['id'])).fetchone()
                        if not pending:c.execute("UPDATE recall.sources SET state='deleted',content_hash='',updated_at=now() WHERE id=%s AND state IN ('pending','invalidated') AND EXISTS(SELECT 1 FROM recall.operations WHERE id=%s AND lease_generation=%s)",(ref,row['id'],row['lease_generation']))
                    self._event_update(p,row,state='failed' if valid else 'cancelled',effect='committed' if status_name=='completed' else 'partial',availability='blocked',
                        error='native_operation_failed_cleaned' if valid else 'source_invalidated_cleaned',done=True)
                    return True
            if row['kind']=='delete':
                # Earlier in-flight writes must have reached actual completion
                # before _claim permits this later cleanup to run.
                # Old write generations have each been checked above. There is
                # no document for this deletion generation itself.
                with transaction() as c:
                    self.store.space(p,c,True);valid,_,_=self._valid(p,row,c)
                    if valid:c.execute("UPDATE recall.sources SET state='deleted',content_hash='',updated_at=now() WHERE id=%s",(ref,))
                self._event_update(p,row,state='succeeded' if valid else 'cancelled',effect='committed',availability='blocked',done=True)
                return True
            document=remote.document(bank,document_ref(ref,row['generation']))
            payload=unpack(row['payload'])
            if not document or document.get('original_text')!=payload['content'] or document.get('document_metadata',{}).get('source_version')!=str(row['generation']):
                raise MemoryUnavailable('memory_readiness_unverified',effect='committed')
            if not document.get('memory_unit_count'):
                # No extracted fact is not a successful available memory. Remove
                # its raw document rather than retaining irrelevant whole chat.
                remote.delete(bank,document_ref(ref,row['generation']))
                if remote.document(bank,document_ref(ref,row['generation'])) is not None:raise MemoryUnavailable('memory_cleanup_unverified',effect='unknown')
                with transaction() as c:
                    self.store.space(p,c,True);c.execute("UPDATE recall.sources SET state='deleted',content_hash='',updated_at=now() WHERE id=%s AND generation=%s AND EXISTS(SELECT 1 FROM recall.operations WHERE id=%s AND lease_generation=%s)",(ref,row['generation'],row['id'],row['lease_generation']))
                self._event_update(p,row,state='failed',effect='partial',availability='blocked',error='no_eligible_memory_extracted_cleaned',done=True)
                return True
            # Check generation and accept under one locked transaction. A
            # concurrent deletion cannot slip between this check and activation.
            with transaction() as c:
                self.store.space(p,c,True);valid,_,_=self._valid(p,row,c)
                current=c.execute('SELECT lease_generation FROM recall.operations WHERE id=%s',(row['id'],)).fetchone()
                if current['lease_generation']!=row['lease_generation']:return False
                if valid:
                    c.execute("UPDATE recall.sources SET state='active',content_hash=%s,updated_at=now() WHERE id=%s",(checksum(payload['content']),ref))
                    c.execute("UPDATE recall.operations SET state='succeeded',effect='committed',availability='available',payload=NULL,lease_until=NULL,updated_at=now() WHERE id=%s",(row['id'],))
                    self.store.event(c,row['id'],'succeeded',row['generation'],'native_completed_document_and_index')
                    return True
            self._event_update(p,row,state='reconciling',effect='committed',availability='blocked',error='source_changed_before_acceptance')
            return True
        except MemoryUnavailable as e:
            self._event_update(p,row,state='reconciling',effect=e.effect,availability='blocked' if row['kind']!='save' else 'pending',error=str(e))
            return False
        except TaskError as e:
            self._event_update(p,row,state='reconciling',effect='unknown',availability='blocked',error=e.code)
            return False

    def recall(self,p,query):
        with transaction() as c:
            space,ready=self.store.output_ready(p,c)
            space=self.store.space(p,c,True)
            if c.execute('SELECT circuit_until>now() AS open FROM recall.spaces').fetchone()['open']:
                return dict(availability='unknown',reason='memory_circuit_open',items=[])
            if space['availability_failures']>=3:c.execute("UPDATE recall.spaces SET circuit_until=now()+interval '3 seconds'")
        try:result=self.remote().recall(space['bank_ref'],redact(query))
        except MemoryUnavailable:
            with transaction() as c:
                self.store.space(p,c,True)
                c.execute("UPDATE recall.spaces SET availability_failures=availability_failures+1,circuit_until=CASE WHEN availability_failures>=2 THEN now()+interval '30 seconds' ELSE NULL END")
            raise
        with transaction() as c:
            self.store.space(p,c,True);c.execute('UPDATE recall.spaces SET availability_failures=0,circuit_until=NULL')
        if result is None:return dict(availability='available' if ready else 'blocked',items=[],epoch=space['epoch'])
        with transaction() as c:
            current,ready=self.store.output_ready(p,c)
            items=[]
            for r in result.get('results',[]):
                source=c.execute("SELECT id,generation,origin_ref,origin_kind,updated_at FROM recall.sources WHERE id=%s AND state='active'",((r.get('metadata') or {}).get('source_ref'),)).fetchone()
                if (source and str(source['generation'])==(r.get('metadata') or {}).get('source_version')
                        and r.get('document_id')==document_ref(source['id'],source['generation'])):
                    items.append(dict(reference=source['id'],version=source['generation'],text=redact(r['text']),
                        origin_ref=source['origin_ref'],origin_kind=source['origin_kind'],updated_at=source['updated_at'].isoformat()))
                if len(items)>=8:break
            return dict(availability='available' if items or ready else 'blocked',items=items,epoch=current['epoch'],scope='untrusted_background_not_authorization')

    def read(self,p,ref):
        with transaction() as c:
            space=self.store.space(p,c);source=self.store.source(p,ref,c)
            if source['state']!='active':return dict(reference=ref,version=source['generation'],availability='blocked',state=source['state'])
        doc=self.remote().document(space['bank_ref'],document_ref(ref,source['generation']))
        with transaction() as c:
            now=self.store.source(p,ref,c)
            if now['state']!='active' or now['generation']!=source['generation']:raise TaskError('memory_changed_during_read',status=409)
        if not doc:raise MemoryUnavailable('memory_document_unavailable')
        if ((doc.get('document_metadata') or {}).get('source_version')!=str(source['generation'])
                or checksum(doc.get('original_text',''))!=source['content_hash']):
            raise MemoryUnavailable('memory_document_version_unverified')
        return dict(reference=ref,version=source['generation'],availability='available',text=redact(doc['original_text']),
            origin_ref=source['origin_ref'],origin_kind=source['origin_kind'],updated_at=source['updated_at'].isoformat())

    def listing(self,p,offset=0,limit=20,search=None):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:raise TaskError('invalid_pagination',status=422)
        with transaction() as c:space,ready=self.store.output_ready(p,c)
        result=self.remote().documents(space['bank_ref'],offset=offset,limit=limit,search=search)
        result=result or {'items':[],'total':0}
        docs=result.get('items',result.get('documents',[]));items=[]
        for doc in docs:
            try:items.append(self.read(p,doc['id'].rsplit('_g',1)[0]))
            except TaskError as e:
                if e.status!=404:raise
        from .context_guard import valid
        items=[item for item in items if item.get('availability')=='available' and valid(p,{item['reference']:item['version']})]
        return dict(items=items,total=result.get('total',len(items)),offset=offset,limit=limit,availability='available' if items or ready else 'blocked')
