"""Dependency probes; no provider/model requests or business messages."""
import sys
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from deploy.server.settings import read_json,environment_file,ConfigurationError


def probe(kind,filename):
    c=read_json(filename)
    if kind=='sms':
        import socket
        if not Path(c['mobile_state_dir'],'sms/spool.key').is_file() or not Path(c['mobile_state_dir'],'receiver_tokens.json').is_file():
            raise ConfigurationError('receiver_state_unavailable')
        with socket.create_connection(('127.0.0.1',8040),timeout=4):pass
    else:
        env=environment_file(c['environment'])
        request=urllib.request.Request('http://127.0.0.1:18889/v1/default/banks',headers={'Authorization':'Bearer '+env['HINDSIGHT_API_TENANT_API_KEY']})
        with urllib.request.urlopen(request,timeout=8) as r:
            if r.status!=200:raise ConfigurationError('memory_health_unavailable')


if __name__=='__main__':
    try:probe(*sys.argv[1:])
    except Exception as exc:
        print(str(exc) if isinstance(exc,ConfigurationError) else 'dependency_health_unavailable',file=sys.stderr)
        raise SystemExit(1)
