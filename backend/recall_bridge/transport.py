"""Pinned Hindsight 1.1 HTTP API; no engine tables and no language parsing.

Only the server-side service may construct this client from its identity mapping.
Never register this raw transport or its bank argument as a model/HTTP user tool.
"""
import re
import time
from urllib.parse import quote
import httpx
from .config import validate_client


# Recall includes local embedding/reranking; a healthy server can exceed 3s.
RECALL_TIMEOUT_SECONDS=8
INTERACTION_WAIT_SECONDS=16


class MemoryUnavailable(RuntimeError):
    def __init__(self, code, *, effect='none'):
        super().__init__(code)
        self.effect=effect


class Hindsight:
    version='1.1.0'

    def __init__(self, url, token, *, transport=None, internal_http_service=None):
        # An explicitly named single-label Compose service is the only non-local
        # HTTP exception. Redirects stay disabled and TLS verification stays on.
        validate_client(url,token,internal_http_service)
        self.client=httpx.Client(base_url=url.rstrip('/'),headers={'Authorization':'Bearer '+token},
                                 transport=transport,trust_env=False,follow_redirects=False)

    def close(self):
        self.client.close()

    def health(self):
        try:r=self.client.get('/v1/default/banks',timeout=8)
        except httpx.HTTPError:raise MemoryUnavailable('memory_service_unavailable') from None
        if r.status_code!=200:raise MemoryUnavailable('memory_http_'+str(r.status_code))

    def request(self, method, bank, path='', *, payload=None, params=None, timeout=3):
        if not isinstance(bank,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',bank):
            raise ValueError('invalid_server_space')
        mutation=method not in {'GET','HEAD'} and path not in {'/memories/recall','/reflect'}
        from .limits import budget
        waiting=budget.get()
        if waiting is not None:
            remaining=INTERACTION_WAIT_SECONDS-waiting.get('memory_wait_seconds',0)
            if remaining<=0:raise MemoryUnavailable('memory_interaction_budget_exhausted')
            timeout=min(timeout,remaining)
        started=time.monotonic()
        try:
            r=self.client.request(method,'/v1/default/banks/'+bank+path,json=payload,params=params,timeout=timeout)
        except httpx.HTTPError:
            raise MemoryUnavailable('memory_service_unavailable',effect='unknown' if mutation else 'none') from None
        finally:
            if waiting is not None:waiting['memory_wait_seconds']=round(waiting.get('memory_wait_seconds',0)+time.monotonic()-started,3)
        if r.status_code==404:return None
        if r.status_code>=300:
            raise MemoryUnavailable('memory_http_'+str(r.status_code),effect='unknown' if mutation and r.status_code>=500 else 'none')
        try:return r.json()
        except ValueError:raise MemoryUnavailable('memory_response_invalid',effect='unknown' if mutation else 'none') from None

    def create(self, bank, mission):
        return self.request('PUT',bank,payload={'retain_mission':mission},timeout=5)

    def retain(self, bank, content, document, operation, *, source, version):
        return self.request('POST',bank,'/memories',payload={'items':[dict(content=content,
            document_id=document,update_mode='replace',metadata={'source_ref':source,'source_version':str(version)})],
            'async':True,'operation_id':operation},timeout=5)

    def operation(self, bank, operation):
        result=self.request('GET',bank,'/operations/'+quote(operation,safe=''))
        # 1.1 returns HTTP200/status=not_found after native task deletion.
        return None if result is not None and result.get('status')=='not_found' else result

    def retry_operation(self, bank, operation):
        return self.request('POST',bank,'/operations/'+quote(operation,safe='')+'/retry',timeout=5)

    def delete_operation(self, bank, operation):
        return self.request('DELETE',bank,'/operations/'+quote(operation,safe='')+'/delete',timeout=5)

    def document(self, bank, document):
        return self.request('GET',bank,'/documents/'+quote(document,safe=''))

    def documents(self, bank, *, offset=0, limit=20, search=None):
        if type(limit) is not int or not 1<=limit<=50 or type(offset) is not int or offset<0:
            raise ValueError('invalid_pagination')
        params={'limit':limit,'offset':offset}
        if search:params['q']=search
        return self.request('GET',bank,'/documents',params=params)

    def delete(self, bank, document):
        return self.request('DELETE',bank,'/documents/'+quote(document,safe=''),timeout=5)

    def recall(self, bank, query):
        return self.request('POST',bank,'/memories/recall',payload={'query':query,'max_tokens':2000,'budget':'low',
            'include':{'chunks':{'max_tokens':1500}}},timeout=RECALL_TIMEOUT_SECONDS)

    def reflect(self, bank, query):
        return self.request('POST',bank,'/reflect',payload={'query':query,'budget':'low','exclude_mental_models':True},timeout=5)
