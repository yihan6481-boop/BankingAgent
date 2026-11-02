"""Cross-process paid-request ledger, reserve before HTTP, conservative failures."""
from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import time
from uuid import uuid4


class BudgetError(RuntimeError): pass


class Budget:
    def __init__(self,path=None,max_requests=None,max_tokens=None):
        config_path=os.environ.get('BA_LLM_BUDGET_CONFIG')
        default=Path(__file__).resolve().parents[2]/'deploy/llm_runtime_budget.json'
        try:
            config=json.loads(Path(config_path or default).read_text('utf-8-sig'))
            if config.get('profile') not in {'runtime','acceptance'}:raise ValueError()
            self.profile=config['profile'];self.batch_id=config['batch_id']
            if not isinstance(self.batch_id,str) or not self.batch_id:raise ValueError()
            # An explicit configuration takes precedence over the legacy R2
            # path. Direct constructor overrides remain useful for offline tests.
            legacy=os.environ.get('BA_R2_BUDGET_FILE') if not config_path else None
            selected=path or (os.environ.get('BA_LLM_BUDGET_FILE') if not config_path else None) or legacy or config['ledger']
            self.path=Path(selected).expanduser().resolve()
            def limit(override,key,env):
                value=override if override is not None else int(os.environ[env]) if not config_path and env in os.environ else config[key]
                if value is None and self.profile=='runtime':return None
                if type(value) is not int or value<=0:raise ValueError()
                return value
            self.max_requests=limit(max_requests,'max_requests','BA_LLM_BUDGET_MAX_REQUESTS')
            self.max_tokens=limit(max_tokens,'max_tokens','BA_LLM_BUDGET_MAX_TOKENS')
            if self.profile=='acceptance' and os.environ.get('BA_LLM_DISABLED')!='1':
                if (self.path!=Path(config['ledger']).expanduser().resolve() or self.max_requests>config['max_requests'] or self.max_tokens>config['max_tokens']):
                    raise BudgetError('budget_configuration_mismatch')
        except (OSError,ValueError,TypeError,KeyError):raise BudgetError('budget_configuration_invalid') from None

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(exist_ok=True,parents=True)
        lock=self.path.with_suffix('.lock')
        with lock.open('a+b') as stream:
            stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
            if os.name=='nt':
                import msvcrt
                deadline=time.monotonic()+5
                while True:
                    try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);break
                    except OSError:
                        if time.monotonic()>=deadline:raise BudgetError('budget_lock_unavailable')
                        time.sleep(.02)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            try:
                try:
                    data=json.loads(self.path.read_text('utf8')) if self.path.exists() else {'requests':[]}
                    if not isinstance(data.get('requests'),list):raise ValueError()
                except (ValueError,TypeError,AttributeError):raise BudgetError('budget_ledger_invalid') from None
                yield data
                temporary=self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf8');os.replace(temporary,self.path)
            finally:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    def reserve(self,payload):
        # UTF-8 bytes plus framing >= token count for this text/JSON protocol.
        upper=len(json.dumps(payload,ensure_ascii=False).encode())+payload.get('max_tokens',2048)+2048
        with self.locked() as ledger:
            rows=ledger['requests']; charged=sum(r.get('charged_tokens',r['reserved_tokens']) for r in rows)
            declared=ledger.get('limits',{})
            # A shared acceptance ledger cannot be enlarged by another worker's
            # constructor/environment. Configurable limits are not unlimited.
            requests=min(self.max_requests,declared.get('requests',self.max_requests)) if self.profile=='acceptance' else self.max_requests
            tokens=min(self.max_tokens,declared.get('total_tokens',self.max_tokens)) if self.profile=='acceptance' else self.max_tokens
            if (requests is not None and len(rows)>=requests) or (tokens is not None and charged+upper>tokens):raise BudgetError('paid_budget_exhausted')
            if ledger.get('profile')=='acceptance' and (self.profile!='acceptance' or ledger.get('batch_id')!=self.batch_id):raise BudgetError('budget_configuration_mismatch')
            ledger.setdefault('profile',self.profile);ledger.setdefault('batch_id',self.batch_id)
            ledger.setdefault('limits',dict(requests=self.max_requests,total_tokens=self.max_tokens))
            if self.profile=='runtime':ledger['limits']=dict(requests=requests,total_tokens=tokens)
            rid=uuid4().hex
            rows.append(dict(id=rid,at=datetime.now(timezone.utc).isoformat(),requested_model=payload['model'],reserved_tokens=upper,status='reserved',
                thinking=payload.get('thinking'),reasoning_effort=payload.get('reasoning_effort'),max_output_tokens=payload.get('max_tokens'),usage_status='unverified',
                assistant_messages=sum(m.get('role')=='assistant' for m in payload.get('messages',[])),
                assistant_reasoning_fields=sum(m.get('role')=='assistant' and isinstance(m.get('reasoning_content'),str) for m in payload.get('messages',[]))))
        return rid

    def status(self):
        """Read-only budget view for authenticated status endpoints.

        The ledger is never rewritten here: usage is only appended by reserve/
        settle. Unknown/failed reservations keep their reserved tokens, so the
        remaining value can only shrink until an audited configuration change.
        """
        try:
            data=json.loads(self.path.read_text('utf8')) if self.path.exists() else {'requests':[]}
            rows=data.get('requests')
            if not isinstance(rows,list):raise ValueError()
            total=0
            for row in rows:
                if not isinstance(row,dict):raise ValueError()
                value=row.get('charged_tokens',row.get('reserved_tokens'))
                if not isinstance(value,int) or isinstance(value,bool) or value<0:raise ValueError()
                total+=value
        except (OSError,ValueError,TypeError):raise BudgetError('budget_ledger_invalid') from None
        return dict(profile=self.profile,batch_id=self.batch_id,configured=True,
            limit_requests=self.max_requests,limit_tokens=self.max_tokens,
            used_requests=len(rows),reserved_tokens=total,
            remaining_requests=(None if self.max_requests is None else max(0,self.max_requests-len(rows))),
            remaining_tokens=(None if self.max_tokens is None else max(0,self.max_tokens-total)))

    def settle(self,rid,response=None,elapsed=0,error=None):
        with self.locked() as ledger:
            row=next(r for r in ledger['requests'] if r['id']==rid)
            row.update(status='failed' if error else 'completed',latency_seconds=round(elapsed,3))
            if error:row['error']=error
            if response:
                row['actual_model']=response.get('model')
                usage=response.get('usage') or {}
                safe={k:v for k,v in usage.items() if isinstance(v,int) and not isinstance(v,bool) and v>=0}
                for key,fields in [('completion_tokens_details',('reasoning_tokens',)),('prompt_tokens_details',('cached_tokens',))]:
                    details=usage.get(key)
                    if isinstance(details,dict):
                        safe[key]={k:details[k] for k in fields if isinstance(details.get(k),int) and not isinstance(details[k],bool) and details[k]>=0}
                row['usage']=safe
                finish=(response.get('choices') or [{}])[0].get('finish_reason')
                if finish in {'stop','tool_calls','length','content_filter'}:row['finish_reason']=finish
                prompt=safe.get('prompt_tokens');completion=safe.get('completion_tokens')
                if prompt is not None and completion is not None:
                    # completion_tokens includes reasoning in DeepSeek's OpenAI
                    # protocol. total_tokens is checked, never added a second time.
                    row['charged_tokens']=max(prompt+completion,safe.get('total_tokens',0))
                    row['usage_status']='verified'

