"""Trust one configured TLS proxy, never arbitrary forwarding headers."""
import ipaddress
from urllib.parse import urlsplit

class DeploymentBoundary:
    def __init__(self,app,*,origin,proxies,receiver=False):
        self.app=app;self.origin=origin;self.host=urlsplit(origin).netloc;self.proxies=set(proxies);self.receiver=receiver
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        if scope['path'] in {'/health/live','/health/ready','/health/deployment'} and not self.receiver:
            return await self.app(scope,receive,send)
        headers={}
        for k,v in scope['headers']:
            key=k.decode('latin1').lower()
            if key in headers and key in {'host','origin','x-forwarded-for','x-forwarded-proto'}:return await self.reject(send,400)
            headers[key]=v.decode('latin1')
        peer=(scope.get('client') or ('',0))[0]
        valid=peer in self.proxies and headers.get('host')==self.host and headers.get('x-forwarded-proto')=='https'
        origin=headers.get('origin')
        if origin is not None and origin!=self.origin:valid=False
        if self.receiver and scope['method'] not in {'GET','HEAD'} and origin!=self.origin:valid=False
        try:source=str(ipaddress.ip_address(headers.get('x-forwarded-for','')))
        except ValueError:valid=False;source='invalid'
        if not valid:return await self.reject(send,403)
        scope=dict(scope,client=(source,0),scheme='https',deployment_proxy_verified=True)
        return await self.app(scope,receive,send)
    async def reject(self,send,status):
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]})
        await send({'type':'http.response.body','body':b'{"error":"deployment_boundary_refused"}'})
