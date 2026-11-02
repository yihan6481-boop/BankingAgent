"""Encrypted, owner-scoped provider continuation in existing conversation storage.

Opaque reasoning is never projected, logged, exported, or used as authority.
The external master key already used by this installation derives a distinct key.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from cryptography.fernet import Fernet,InvalidToken
from psycopg.types.json import Jsonb
from .store import transaction

TTL=7*24*3600


def digest(message):
    return hashlib.sha256(json.dumps({k:v for k,v in message.items() if k!='reasoning_content'},
        sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def cipher():
    from .provider import ModelError
    try:
        raw=Path(os.environ['BA091_TOTP_MASTER_KEY_FILE']).read_bytes().strip()
        Fernet(raw)  # Fail closed on a missing/invalid installation key.
        key=hmac.new(raw,b'BankingAgent:R3:provider-continuation:v1',hashlib.sha256).digest()
        return Fernet(base64.urlsafe_b64encode(key))
    except (KeyError,OSError,ValueError):raise ModelError('thinking_key_unavailable') from None


def owned(c,cid,tid):
    from storage.pg02 import current_context
    from .provider import ModelError
    p=current_context()
    row=c.execute('SELECT t.protocol_data FROM pg02.dialogue_turns t JOIN pg02.dialogues d ON d.id=t.dialogue_id WHERE t.id=%s AND d.id=%s AND d.owner_id=%s AND d.principal_id=%s',
        (tid,cid,p.user_id,p.principal_id)).fetchone()
    if not row:raise ModelError('thinking_history_unavailable')
    return row


def retain(cid,tid,slot,message,reasoning,*,generation=None):
    from .provider import ModelError
    if not isinstance(reasoning,str):raise ModelError('thinking_reasoning_missing')
    if len(reasoning.encode())>512000:raise ModelError('thinking_protocol_size_exceeded')
    payload=dict(dialogue=cid,turn=tid,slot=str(slot),digest=digest(message),reasoning=reasoning)
    encrypted=cipher().encrypt(json.dumps(payload,ensure_ascii=False).encode()).decode()
    with transaction() as c:
        owned(c,cid,tid)
        # Expired material is removed on access; no background cache service.
        c.execute("UPDATE pg02.dialogue_turns SET protocol_data='{}' WHERE created_at<now()-interval '7 days' AND protocol_data<>'{}'")
        changed=c.execute("UPDATE pg02.dialogue_turns SET protocol_data=protocol_data || %s WHERE id=%s AND (%s::int IS NULL OR COALESCE((usage->>'lease_generation')::int,0)=%s)",(Jsonb({str(slot):encrypted}),tid,generation,generation)).rowcount
        if changed!=1:raise ModelError('turn_lease_expired')


def attach(cid,tid,slot,message):
    from .provider import ModelError
    with transaction() as c:row=owned(c,cid,tid)
    try:
        encrypted=row['protocol_data'][str(slot)]
        payload=json.loads(cipher().decrypt(encrypted.encode(),ttl=TTL))
        expected=(cid,tid,str(slot),digest(message))
        actual=tuple(payload[k] for k in ['dialogue','turn','slot','digest'])
        if actual!=expected or not isinstance(payload['reasoning'],str):raise ValueError()
        return dict(message,reasoning_content=payload['reasoning'])
    except KeyError:raise ModelError('thinking_history_unavailable') from None
    except InvalidToken:
        try:
            if time.time()-cipher().extract_timestamp(encrypted.encode())>TTL:raise ModelError('thinking_history_expired')
        except InvalidToken:pass
        raise ModelError('thinking_history_mismatch') from None
    except (ValueError,TypeError,AttributeError):raise ModelError('thinking_history_mismatch') from None
