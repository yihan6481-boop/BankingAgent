"""Secret-free configuration errors and one URL policy for all memory readers."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


class MemoryConfigurationError(ValueError):pass


def read_json_file(path):
    try:raw=Path(path).read_text('utf-8-sig')
    except FileNotFoundError:raise MemoryConfigurationError('memory_configuration_path_missing') from None
    except PermissionError:raise MemoryConfigurationError('memory_configuration_permission_denied') from None
    except UnicodeError:raise MemoryConfigurationError('memory_configuration_encoding_invalid') from None
    except OSError:raise MemoryConfigurationError('memory_configuration_unreadable') from None
    try:return json.loads(raw)
    except ValueError:raise MemoryConfigurationError('memory_configuration_json_invalid') from None


def validate_client(url,token,internal_http_service=None):
    if not isinstance(token,str) or not token.strip() or '<' in token:
        raise MemoryConfigurationError('memory_configuration_fields_invalid')
    if internal_http_service is not None and (not isinstance(internal_http_service,str) or
            not re.fullmatch(r'[a-z][a-z0-9-]{0,62}:[1-9][0-9]{0,4}',internal_http_service)):
        raise MemoryConfigurationError('memory_configuration_http_boundary_invalid')
    try:
        u=urlsplit(url) if isinstance(url,str) else None
        port=u.port if u else None
        if not u or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {'','/'}:
            raise ValueError()
        if u.scheme=='https':return
        loopback=u.hostname in {'127.0.0.1','localhost','::1'}
        internal=internal_http_service is not None and u.netloc==internal_http_service and port is not None
        if u.scheme=='http' and (loopback and port is not None or internal):return
    except (ValueError,TypeError):pass
    raise MemoryConfigurationError('memory_configuration_url_policy_denied')


def read_client(path):
    cfg=read_json_file(path)
    if not isinstance(cfg,dict) or not {'url','token'}<=cfg.keys():
        raise MemoryConfigurationError('memory_configuration_fields_invalid')
    validate_client(cfg['url'],cfg['token'],cfg.get('internal_http_service'))
    return {k:cfg[k] for k in ['url','token','internal_http_service'] if k in cfg}
