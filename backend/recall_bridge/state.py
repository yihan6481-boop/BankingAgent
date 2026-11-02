"""Owner-bound delivery metadata, CAS and tombstones; never a fact database."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from uuid import uuid4
from cryptography.fernet import Fernet,InvalidToken
from dialogue.store import transaction
from lifecycle.store import TaskError
from storage.pg02 import current_context


def checksum(value):return hashlib.sha256(value.encode()).hexdigest()


def cipher():
    try:
        raw=Path(os.environ['BA091_TOTP_MASTER_KEY_FILE']).read_bytes().strip()
        Fernet(raw)
        return Fernet(base64.urlsafe_b64encode(hmac.new(raw,b'BankingAgent:R4:memory-delivery:v1',hashlib.sha256).digest()))
    except (KeyError,OSError,ValueError):raise TaskError('memory_delivery_key_unavailable',status=503) from None


def pack(value):return cipher().encrypt(json.dumps(value,ensure_ascii=False).encode()).decode()


def unpack(value):
    try:return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken,ValueError,AttributeError):raise TaskError('memory_delivery_payload_invalid',status=503) from None


def public_operation(row):
    return dict(operation_ref=row['id'],operation_state=row['state'],effect_state=row['effect'],
        availability=row['availability'],source_ref=row['source_ref'],source_version=row['generation'],
        updated_at=row['updated_at'].isoformat(),error_code=row['error_code'],
        retry=False,next_action='restore_budget_then_recover_original' if row['error_code']=='paid_budget_exhausted' else 'read_operation' if row['state'] in {'queued','running','reconciling'} else
        'inspect_source' if row['state']=='succeeded' else 'inspect_operation_before_recovery')


class Store:
    def space(self,p,c,lock=False):
        ctx=current_context()
        if (ctx.user_id,ctx.principal_id)!=(p.user_id,p.principal_id):raise TaskError('memory_identity_denied',status=403)
        created=c.execute('INSERT INTO recall.spaces(owner_id,principal_id,bank_ref) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING bank_ref',
            (p.user_id,p.principal_id,'r4_'+uuid4().hex)).fetchone()
        row=c.execute('SELECT * FROM recall.spaces WHERE owner_id=%s AND principal_id=%s'+(' FOR UPDATE' if lock else ''),
            (p.user_id,p.principal_id)).fetchone()
        if not row:raise TaskError('memory_identity_denied',status=403)
        from .recovery_fence import check
        check(c,row,created=bool(created))
        return row

    def event(self,c,ref,action,version,origin):
        c.execute('INSERT INTO recall.events(reference,action,version,trusted_origin) VALUES(%s,%s,%s,%s)',(ref,action,version,origin))

    def source(self,p,ref,c,lock=False):
        self.space(p,c)
        row=c.execute('SELECT * FROM recall.sources WHERE id=%s'+(' FOR UPDATE' if lock else ''),(ref,)).fetchone()
        if not row:raise TaskError('memory_reference_not_found',status=404)
        return row

    def operation(self,p,ref,c=None):
        if c is None:
            with transaction() as conn:return self.operation(p,ref,conn)
        self.space(p,c)
        row=c.execute('SELECT * FROM recall.operations WHERE id=%s',(ref,)).fetchone()
        if not row:raise TaskError('memory_reference_not_found',status=404)
        self.source(p,row['source_ref'],c)
        return row

    def settings(self,p):
        with transaction() as c:
            r=self.space(p,c)
            pending=c.execute("SELECT count(*) AS n FROM recall.operations WHERE state IN ('queued','running','reconciling')").fetchone()['n']
            return dict(automatic=r['auto_enabled'],profile=r['profile_enabled'],version=r['config_version'],
                effective_at=r['updated_at'].isoformat(),pending_operations=pending,
                cleanup_state='pending' if pending else 'settled')

    def configure(self,p,*,version,automatic,profile,origin):
        with transaction() as c:
            r=self.space(p,c,True)
            if r['config_version']!=version:raise TaskError('stale_revision',status=409)
            c.execute('UPDATE recall.spaces SET auto_enabled=%s,profile_enabled=%s,config_version=config_version+1,epoch=epoch+1,updated_at=now()',
                (automatic,profile))
            self.event(c,'settings','consent_configuration',version+1,origin)
            if automatic!=r['auto_enabled']:self.event(c,'settings','automatic_enabled' if automatic else 'automatic_disabled',version+1,origin)
            if profile!=r['profile_enabled']:self.event(c,'settings','profile_enabled' if profile else 'profile_disabled',version+1,origin)
            if not automatic:
                # In-flight work stays reconcilable; only unsent old automatic
                # tasks can be cancelled here. Their sources remain tombstones.
                c.execute("UPDATE recall.sources SET state='invalidated',generation=generation+1 WHERE automatic AND state='pending'")
                c.execute("UPDATE recall.operations o SET state='cancelled',effect='none',availability='blocked',payload=NULL,updated_at=now() "
                    "FROM recall.sources s WHERE o.source_ref=s.id AND s.automatic AND o.state='queued' AND NOT o.submitted")
                c.execute("UPDATE recall.sources s SET state='deleted' WHERE s.automatic AND s.state='invalidated' AND NOT EXISTS "
                    "(SELECT 1 FROM recall.operations o WHERE o.source_ref=s.id AND o.submitted)")
                inflight=c.execute("SELECT s.id,s.generation FROM recall.sources s WHERE s.automatic AND s.state='invalidated' AND EXISTS "
                    "(SELECT 1 FROM recall.operations o WHERE o.source_ref=s.id AND o.submitted AND o.state IN ('queued','running','reconciling'))").fetchall()
                for source in inflight:
                    self._operation(c,source['id'],source['generation'],'delete',checksum(json.dumps([source['id'],source['generation'],'consent_revocation'])),None,'authenticated_settings_http')
            from .recovery_fence import advance
            advance(c)
        return self.settings(p)

    def permitted_origin(self,p,origin,version,c,partial=False):
        # Only immutable server messages are accepted by the explicit chat tool;
        # UI edits are bound to an already-owned source by replace().
        row=c.execute('SELECT t.id FROM pg02.dialogue_turns t JOIN pg02.dialogues d ON d.id=t.dialogue_id '
            'WHERE t.id=%s AND d.owner_id=%s AND d.principal_id=%s',(origin,p.user_id,p.principal_id)).fetchone()
        if not row or version!=1:raise TaskError('memory_source_denied',status=403)
        exclusion=c.execute('SELECT reason FROM recall.exclusions WHERE origin_ref=%s AND origin_version=%s',(origin,version)).fetchone()
        if exclusion and not (partial and exclusion['reason']=='source_invalidated' and not c.execute('SELECT 1 FROM recall.sources WHERE origin_ref=%s AND span_start IS NULL',(origin,)).fetchone()):
            raise TaskError('memory_source_excluded_clarification_required',status=409)

    def enqueue(self,p,content,origin,*,origin_version=1,automatic=False,origin_kind='user',related_reference=None):
        with transaction() as c:
            return self._enqueue(p,content,origin,c,origin_version,automatic,origin_kind,related_reference)

    def enqueue_many(self,p,contents,origin):
        with transaction() as c:
            self.space(p,c,True)
            return [self._enqueue(p,content,origin,c,1,False,'user',None) for content in contents]

    def enqueue_automatic(self,p,origin):
        """Claim the complement of exact explicit coverage under the owner lock.
        Positions refer to immutable server text, not inferred semantic facts.
        Deleted/replaced positions remain claimed to prevent historical replay.
        """
        with transaction() as c:
            self.space(p,c,True);self.permitted_origin(p,origin,1,c,partial=True)
            text=c.execute('SELECT user_text FROM pg02.dialogue_turns WHERE id=%s',(origin,)).fetchone()['user_text']
            spans=c.execute('SELECT span_start,span_end FROM recall.sources WHERE origin_ref=%s AND origin_version=1 AND span_start IS NOT NULL ORDER BY span_start',(origin,)).fetchall()
            result=[];pos=0
            for span in [*spans,dict(span_start=len(text),span_end=len(text))]:
                end=span['span_start']
                while pos<end:
                    stop=min(end,pos+4000)
                    if any(char.isalnum() for char in text[pos:stop]):result.append(self._enqueue(p,text[pos:stop],origin,c,1,True,'user',None,span=(pos,stop)))
                    pos=stop
                pos=max(pos,span['span_end'])
            return result

    def _enqueue(self,p,content,origin,c,origin_version,automatic,origin_kind,related_reference,span=None):
        space=self.space(p,c,True)
        self.permitted_origin(p,origin,origin_version,c,partial=automatic and span is not None)
        if automatic and not space['auto_enabled']:raise TaskError('automatic_memory_disabled',status=409)
        key=checksum(json.dumps([origin,origin_version,automatic,origin_kind,related_reference,checksum(content)]))
        old=c.execute('SELECT * FROM recall.operations WHERE dedup=%s',(key,)).fetchone()
        if old:return old
        if origin_kind=='user':
            text=c.execute('SELECT user_text FROM pg02.dialogue_turns WHERE id=%s',(origin,)).fetchone()['user_text']
            start=text.find(content)
            if span is None and start>=0 and text.find(content,start+1)<0:span=(start,start+len(content))
            # Upgrade only provable legacy identities: exact content hash or
            # whole immutable message. No semantic or amount/date matching.
            siblings=c.execute('SELECT * FROM recall.sources WHERE origin_ref=%s AND origin_version=%s FOR UPDATE',(origin,origin_version)).fetchall()
            for sibling in siblings:
                if sibling['coverage_retired']:continue
                if sibling['span_start'] is None:
                    proven=span if sibling['content_hash']==checksum(content) else (0,len(text)) if sibling['content_hash']==checksum(text) else None
                    if proven:
                        c.execute('UPDATE recall.sources SET span_start=%s,span_end=%s WHERE id=%s',(*proven,sibling['id']))
                        sibling.update(span_start=proven[0],span_end=proven[1])
                if span and (sibling['span_start'],sibling['span_end'])==span:
                    if not automatic:c.execute('UPDATE recall.sources SET automatic=false WHERE id=%s',(sibling['id'],))
                    return c.execute('SELECT * FROM recall.operations WHERE source_ref=%s ORDER BY created_at DESC,id DESC LIMIT 1',(sibling['id'],)).fetchone()
                if span and sibling['span_start'] is not None and span[0]<sibling['span_end'] and sibling['span_start']<span[1]:
                    if automatic:raise TaskError('memory_source_coverage_conflict',status=409)
                    if not sibling['automatic']:raise TaskError('memory_source_overlap_use_existing_reference',status=409)
                    # Auto-before-explicit races: fence the broad range and
                    # retain only its exact non-overlapping remainder. Cleanup
                    # uses the existing durable native operation machinery.
                    generation=sibling['generation']+1
                    c.execute("UPDATE recall.sources SET generation=%s,state='invalidated',coverage_retired=true WHERE id=%s",(generation,sibling['id']))
                    c.execute("UPDATE recall.operations SET state='cancelled',availability='blocked',payload=NULL WHERE source_ref=%s AND state='queued' AND NOT submitted",(sibling['id'],))
                    self._operation(c,sibling['id'],generation,'delete',checksum(json.dumps([sibling['id'],generation,'coverage_split'])),None,'trusted_source_coverage')
                    for left,right in [(sibling['span_start'],min(span[0],sibling['span_end'])),(max(span[1],sibling['span_start']),sibling['span_end'])]:
                        if left<right and any(char.isalnum() for char in text[left:right]):
                            # Existing superseded coverage is ignored below;
                            # replay suppression still retains its opaque span.
                            self._enqueue(p,text[left:right],origin,c,origin_version,True,origin_kind,related_reference,span=(left,right))
        ref='rm_'+uuid4().hex
        c.execute("INSERT INTO recall.sources(id,origin_ref,origin_version,origin_kind,related_reference,content_hash,state,automatic,config_version) "
            "VALUES(%s,%s,%s,%s,%s,%s,'pending',%s,%s)",(ref,origin,origin_version,origin_kind,related_reference,checksum(content),automatic,space['config_version']))
        if span:c.execute('UPDATE recall.sources SET span_start=%s,span_end=%s WHERE id=%s',(*span,ref))
        return self._operation(c,ref,1,'save',key,content,origin)

    def _operation(self,c,ref,generation,kind,key,content,origin):
        op='mo_'+uuid4().hex
        payload=pack(dict(source=ref,generation=generation,content=content)) if content is not None else None
        c.execute("INSERT INTO recall.operations(id,source_ref,generation,kind,dedup,external_ref,payload,state,effect,availability) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,'queued','none','pending')",(op,ref,generation,kind,key,str(uuid4()),payload))
        c.execute('UPDATE recall.spaces SET epoch=epoch+1')
        self.event(c,op,kind+'_queued',generation,origin)
        from .recovery_fence import advance
        advance(c)
        return c.execute('SELECT * FROM recall.operations WHERE id=%s',(op,)).fetchone()

    def change(self,p,ref,version,*,content=None,delete=False,origin):
        with transaction() as c:
            self.space(p,c,True);s=self.source(p,ref,c,True)
            key=checksum(json.dumps([ref,version,'delete' if delete else 'replace',checksum(content or '')]))
            old=c.execute('SELECT * FROM recall.operations WHERE dedup=%s',(key,)).fetchone()
            if old:return old
            if s['generation']!=version or s['state']=='deleted':raise TaskError('stale_revision',status=409)
            if s['span_start'] is not None:
                siblings=c.execute("SELECT * FROM recall.sources WHERE origin_ref=%s AND origin_version=%s AND id<>%s AND NOT coverage_retired AND state<>'deleted' FOR UPDATE",(s['origin_ref'],s['origin_version'],ref)).fetchall()
                text=c.execute('SELECT user_text FROM pg02.dialogue_turns WHERE id=%s',(s['origin_ref'],)).fetchone()['user_text']
                for other in siblings:
                    if other['span_start'] is None:
                        if other['automatic']:raise TaskError('legacy_source_overlap_unverified',status=409)
                        continue
                    if other['span_start']>=s['span_end'] or s['span_start']>=other['span_end']:continue
                    if other['generation']!=1:raise TaskError('legacy_source_overlap_unverified',status=409)
                    c.execute("UPDATE recall.sources SET generation=generation+1,state='invalidated',coverage_retired=true WHERE id=%s",(other['id'],))
                    c.execute("UPDATE recall.operations SET state='cancelled',availability='blocked',payload=NULL WHERE source_ref=%s AND state='queued' AND NOT submitted",(other['id'],))
                    self._operation(c,other['id'],2,'delete',checksum(json.dumps([other['id'],2,'coverage_split'])),None,'trusted_source_coverage')
                    for left,right in [(other['span_start'],min(s['span_start'],other['span_end'])),(max(s['span_end'],other['span_start']),other['span_end'])]:
                        if left<right and any(char.isalnum() for char in text[left:right]):self._enqueue(p,text[left:right],s['origin_ref'],c,s['origin_version'],other['automatic'],'user',None,span=(left,right))
            generation=version+1
            # Invalidation commits BEFORE any external request or result use.
            c.execute("UPDATE recall.sources SET generation=%s,state='invalidated',automatic=false,updated_at=now() WHERE id=%s",(generation,ref))
            self.event(c,ref,'explicit_source_claim',generation,origin)
            c.execute("INSERT INTO recall.exclusions(origin_ref,origin_version,reason) VALUES(%s,%s,'source_invalidated') ON CONFLICT DO NOTHING",
                (s['origin_ref'],s['origin_version']))
            c.execute("UPDATE recall.operations SET state='cancelled',availability='blocked',payload=NULL,updated_at=now() "
                "WHERE source_ref=%s AND state='queued' AND NOT submitted",(ref,))
            return self._operation(c,ref,generation,'delete' if delete else 'replace',key,content,origin)

    def epoch(self,p):
        with transaction() as c:return self.space(p,c)['epoch']

    def output_ready(self,p,c):
        space=self.space(p,c)
        pending=c.execute("SELECT 1 FROM recall.sources WHERE state IN ('pending','invalidated') LIMIT 1").fetchone()
        return space,not bool(pending)

    def exclude(self,p,origin,version=1):
        with transaction() as c:
            self.space(p,c,True)
            # Verify trusted origin before adding a durable suppression marker.
            row=c.execute('SELECT t.id FROM pg02.dialogue_turns t JOIN pg02.dialogues d ON d.id=t.dialogue_id '
                'WHERE t.id=%s AND d.owner_id=%s AND d.principal_id=%s',(origin,p.user_id,p.principal_id)).fetchone()
            if not row or version!=1:raise TaskError('memory_source_denied',status=403)
            refs=c.execute("SELECT id,generation FROM recall.sources WHERE origin_ref=%s AND origin_version=%s AND state<>'deleted' FOR UPDATE",(origin,version)).fetchall()
            c.execute("INSERT INTO recall.exclusions(origin_ref,origin_version,reason) VALUES(%s,%s,'temporary') ON CONFLICT DO NOTHING",(origin,version))
            operations=[]
            for r in refs:
                generation=r['generation']+1
                c.execute("UPDATE recall.sources SET state='invalidated',generation=%s,updated_at=now() WHERE id=%s",(generation,r['id']))
                c.execute("UPDATE recall.operations SET state='cancelled',availability='blocked',payload=NULL,updated_at=now() "
                    "WHERE source_ref=%s AND state='queued' AND NOT submitted",(r['id'],))
                operations.append(self._operation(c,r['id'],generation,'delete',checksum(json.dumps([r['id'],generation,'exclusion'])),None,'trusted_turn_exclusion'))
            c.execute('UPDATE recall.spaces SET epoch=epoch+1')
            self.event(c,origin,'temporary_exclusion',version,'authenticated_turn')
            from .recovery_fence import advance
            advance(c)
        return operations
