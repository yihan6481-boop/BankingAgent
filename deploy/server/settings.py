"""Strict, portable configuration. Parsing performs no network or file writes."""
from pathlib import Path
from urllib.parse import urlsplit
import json, re, os

ROOT=Path(__file__).resolve().parents[2]
class ConfigurationError(ValueError):pass
def fail(code):raise ConfigurationError(code)
def read_json(path):
    try:return json.loads(Path(path).read_text('utf-8-sig'))
    except (OSError,ValueError,TypeError):fail('configuration_unreadable')
def external_file(value):
    if not isinstance(value,str):fail('external_file_required')
    p=Path(value).expanduser()
    if not p.is_absolute() or p.resolve().is_relative_to(ROOT) or not p.is_file():fail('external_file_required')
    return p.resolve()
def environment_file(path):
    result={}
    for line in external_file(path).read_text('utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if '=' not in line:fail('environment_file_invalid')
        k,v=line.split('=',1)
        if not re.fullmatch('[A-Z][A-Z0-9_]*',k) or k in result:fail('environment_file_invalid')
        result[k]=v.strip().strip('\"\'')
    return result
def url(value,*,https=False):
    if not isinstance(value,str):fail('service_url_required')
    p=urlsplit(value)
    if p.scheme not in ({'https'} if https else {'http','https'}) or not p.hostname or p.username or p.password or p.fragment or p.query:fail('service_url_invalid')
    return p
def load(path):
    path=external_file(str(path)); c=read_json(path)
    try:
        if c['format']!=1 or c['kind'] not in {'server','isolated-test'}:fail('configuration_format_invalid')
        if c['layout'] not in {'compose','isolated-test'}:fail('layout_invalid')
        if not re.fullmatch('[a-zA-Z0-9._-]{8,96}',c['deployment_id']):fail('deployment_id_invalid')
        for section,pattern,role in [('database','banking_server_[a-z0-9_]+','banking_runtime')]:
            d=c[section]
            test=c['kind']=='isolated-test' and c['layout']=='isolated-test' and re.fullmatch('pg02_test_[0-9a-f]{32}',d['name'])
            if not test and not re.fullmatch(pattern,d['name']):fail('old_or_unscoped_database_forbidden')
            if d['role']!=role or not isinstance(d['host'],str) or not d['host'] or type(d['port']) is not int or not 1<=d['port']<=65535:fail('database_configuration_invalid')
            if c['layout']=='compose' and d['host'] in {'127.0.0.1','localhost','::1'}:fail('container_loopback_database_forbidden')
            external_file(d['passfile'])
        for k in ['totp_key','payment_key','spool_key','seat_credentials','model_environment','memory_fence','budget_config','restore_identity']:
            external_file(c['files'][k])
        if len(Path(c['files']['payment_key']).read_bytes().strip())<32:fail('payment_key_invalid')
        for k in ['state_dir','mobile_state_dir']:
            p=Path(c[k])
            if not p.is_absolute() or p.resolve().is_relative_to(ROOT) or not p.is_dir():fail('external_state_directory_required')
        if Path(c['files']['spool_key']).resolve()!=Path(c['mobile_state_dir'],'sms','spool.key').resolve():fail('sms_state_key_mismatch')
        for k in ['bank_origin','sms_origin']:
            p=url(c[k],https=True)
            if p.path not in {'','/'} or '*' in p.netloc:fail('public_origin_invalid')
        if c['bank_origin']==c['sms_origin']:fail('separate_receiver_origin_required')
        import ipaddress
        if not c['trusted_proxy_addresses']:fail('trusted_proxy_required')
        for ip in c['trusted_proxy_addresses']:ipaddress.ip_address(ip)
        models=environment_file(c['files']['model_environment'])
        for k in ['LLM_API_KEY','LLM_MODEL','LLM_BASE_URL','VISION_API_KEY','VISION_MODEL','VISION_BASE_URL','R4_PROVIDER_KEY','R4_TARGET_MODEL','R4_GATEWAY_TOKEN','R4_PROVIDER_BASE_URL']:
            if not models.get(k) or '<' in models[k]:fail('model_configuration_required')
        for k in ['LLM_BASE_URL','VISION_BASE_URL','R4_PROVIDER_BASE_URL']:url(models[k],https=True)
        if models['LLM_MODEL']!=models['R4_TARGET_MODEL']:fail('memory_chat_model_mismatch')
        from backend.recall_bridge.config import read_client
        memory_path=c['files'].get('memory_client')
        if not isinstance(memory_path,str):fail('memory_configuration_fields_invalid')
        mp=Path(memory_path).expanduser()
        if not mp.is_absolute() or mp.resolve().is_relative_to(ROOT):fail('memory_configuration_path_invalid')
        try:memory_client=read_client(mp)
        except ValueError as exc:fail(str(exc))  # reader emits only fixed, secret-free codes
        for value in [c['gateway_url'],memory_client['url']]:
            u=url(value)
            if c['layout']=='compose' and u.hostname in {'127.0.0.1','localhost','::1'}:fail('container_loopback_service_forbidden')
        client=memory_client
        if not client.get('token') or '<' in client['token']:fail('memory_client_credentials_required')
        budget=read_json(c['files']['budget_config'])
        if budget['profile']!='runtime' or not budget.get('batch_id'):fail('runtime_budget_required')
        ledger=external_file(budget['ledger'])
        if not isinstance(read_json(ledger).get('requests'),list):fail('existing_budget_ledger_required')
        for k in ['max_requests','max_tokens']:
            if budget[k] is not None and (type(budget[k]) is not int or budget[k]<=0):fail('budget_limit_invalid')
        identity=read_json(c['files']['restore_identity'])
        if identity['deployment_id']!=c['deployment_id']:fail('restore_identity_mismatch')
        if c['kind']=='server' and identity.get('transient_auth_retired') is not True:fail('restored_sessions_must_be_retired')
        if type(c.get('port',8039)) is not int or not 1<=c.get('port',8039)<=65535:fail('port_invalid')
        return c
    except ConfigurationError:raise
    except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('configuration_incomplete')

def install(c):
    # No legacy .env loader, first-run key creator, candidate helper or seed.
    files=c['files'];models=environment_file(files['model_environment'])
    for k in list(os.environ):
        if k.startswith(('BA_','BA07_','BA091_','LLM_','VISION_','R4_')):os.environ.pop(k,None)
    # ASR settings are loaded only from the existing external model config.
    # Do not preserve arbitrary BA_* variables from the container environment.
    asr_keys={'BA_ASR_ENABLED','BA_ASR_ENDPOINT','BA_ASR_MODEL','BA_ASR_API_KEY','BA_ASR_MIME_TYPES','BA_ASR_ALLOW_LOOPBACK_HTTP'}
    os.environ.update({k:v for k,v in models.items() if k.startswith(('LLM_','VISION_','R4_')) or k in asr_keys})
    from psycopg.conninfo import make_conninfo
    d=c['database']
    os.environ.update(BA_STORAGE_BACKEND='postgres',BA_PG_DSN=make_conninfo(host=d['host'],port=d['port'],dbname=d['name'],user=d['role'],passfile=d['passfile']),
        BA091_AUTH_MODE='secure',BA091_LLM_ACCESS='configured',BA_LLM_DISABLED='0',BA_CANDIDATE_MODE='experience',
        BA_R2_CONTEXT_BYTES='96000',BA_VISION_ENABLED='1',BA_VISION_USE_RUNTIME_BUDGET='1',BA_R4_ENABLED='1',
        BA_DEPLOYMENT_MODE='demo',BA_SMS_PROVIDER='mock',BA_PAYMENT_AUTH_ENABLED='1',
        BA091_TOTP_MASTER_KEY_FILE=files['totp_key'],BA_PAYMENT_AUTH_KEY_FILE=files['payment_key'],
        BA_MOBILE_STATE_DIR=c['mobile_state_dir'],BA_DEMO_SMS_DIR=str(Path(c['mobile_state_dir'])/'sms'),
        BA_SUPPORT_CREDENTIAL_FILE=files['seat_credentials'],BA_LLM_BUDGET_CONFIG=files['budget_config'],
        BA_R4_SERVICE_CONFIG=files['memory_client'],BA_R4_RECOVERY_FENCE=files['memory_fence'],
        BA07_CORS_ALLOW_ORIGINS=c['bank_origin'])
    return models

def load_service(path,component):
    c=read_json(external_file(str(path)))
    try:
        if c['format']!=1 or c['component']!=component:fail('service_configuration_invalid')
        if component=='sms':
            url(c['sms_origin'],https=True)
            if '*' in c['sms_origin'] or not c['trusted_proxy_addresses']:fail('receiver_origin_required')
            import ipaddress
            for ip in c['trusted_proxy_addresses']:ipaddress.ip_address(ip)
            external_file(str(Path(c['mobile_state_dir'])/'sms/spool.key'))
        elif component=='gateway':
            env=environment_file(c['environment'])
            for k in ['R4_PROVIDER_BASE_URL','R4_GATEWAY_TOKEN','R4_PROVIDER_KEY','R4_TARGET_MODEL']:
                if not env.get(k) or '<' in env[k]:fail('gateway_configuration_required')
            url(env['R4_PROVIDER_BASE_URL'],https=True)
            budget=read_json(external_file(c['budget_config']))
            external_file(budget['ledger'])
            if budget['profile']!='runtime':fail('runtime_budget_required')
        else:
            env=environment_file(c['environment']);d=c['database'];u=urlsplit(env['HINDSIGHT_API_DATABASE_URL'])
            if not re.fullmatch('r4_memory_server_[a-z0-9_]+',d['name']) or d['role']!='r4_hindsight_native':fail('old_memory_database_forbidden')
            if u.hostname!=d['host'] or (u.port or 5432)!=d['port'] or u.path!='/'+d['name'] or u.username!=d['role']:fail('memory_database_target_mismatch')
            if u.hostname in {'127.0.0.1','localhost','::1'}:fail('container_loopback_service_forbidden')
            if env.get('HINDSIGHT_API_TENANT_EXTENSION')!='hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension':fail('memory_tenant_authentication_required')
            for k in ['HINDSIGHT_API_LLM_API_KEY','HINDSIGHT_API_TENANT_API_KEY','HINDSIGHT_API_LLM_MODEL','HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL','HINDSIGHT_API_RERANKER_LOCAL_MODEL']:
                if not env.get(k) or '<' in env[k]:fail('memory_configuration_required')
            if url(env['HINDSIGHT_API_LLM_BASE_URL']).hostname in {'127.0.0.1','localhost','::1'}:fail('container_loopback_service_forbidden')
            for k in ['HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL','HINDSIGHT_API_RERANKER_LOCAL_MODEL']:
                if not Path(env[k]).is_dir():fail('memory_model_cache_required')
            external_file(c['restore_identity'])
        return c
    except ConfigurationError:raise
    except (OSError,ValueError,KeyError,TypeError,AttributeError):fail('service_configuration_incomplete')
