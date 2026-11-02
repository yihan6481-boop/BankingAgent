"""Field-aware redaction shared by volatile/model and durable chat boundaries."""
import re
import json
import unicodedata

# Normalize a detection copy only. Offsets stay one-to-one with the original.
_SEPARATORS=' -\t\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u202f\u205f\u3000\u2010\u2011\u2012\u2013\u2212\uff0d'
_ACCOUNT=re.compile(r'(?<![A-Za-z0-9_*])[0-9](?:[ \-\t]{0,3}[0-9]){11,23}(?![A-Za-z0-9_]|[ \-\t]{0,3}[0-9])')


def account_spans(value):
    copy=''.join(str(unicodedata.decimal(c)) if c.isdecimal() else ' ' if c in _SEPARATORS and c not in '-\t' else c for c in value)
    for match in _ACCOUNT.finditer(copy):
        candidate=match[0]
        # Valid ISO calendar values remain dates, even when placed side by side.
        dates=candidate.split()
        try:
            from datetime import date
            if dates and all(date.fromisoformat(s).isoformat()==s for s in dates):continue
        except ValueError:pass
        yield match.span()


def redact_accounts(value):
    for start,end in reversed(list(account_spans(value))):value=value[:start]+'[账号已脱敏]'+value[end:]
    return value

SECRET_FIELDS = {'password', 'code', 'otp', 'totp', 'secret', 'api_key', 'credential',
    'credential_id', 'session_id', 'principal_id', 'confirmation_phrase', 'confirmation_message',
    'authorization_id', 'authorization', 'grant', 'pending_challenge', 'challenge_id',
    'lifecycle_binding', 'request_idempotency_key', 'idempotency_key', 'confirmed', 'confirm_text',
    'otp_code', 'totp_code', 'totp_secret', 'totp_key', 'access_token', 'refresh_token', 'authorization_object'}


def redact(value):
    if isinstance(value, dict):
        return {k: ('****'+str(v)[-4:] if k.lower() in {'card_number','account_number'} else
                    '[验证码已移除]' if k=='content' and isinstance(v,str) and re.fullmatch(r'\d{6}',v.strip()) else redact(v))
                for k, v in value.items() if k.lower() not in SECRET_FIELDS}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    # A pasted JSON document remains data even when nested in a message string.
    # Filter its fields before it can become an escaped transport/history value.
    try:
        parsed=json.loads(value)
    except (ValueError,TypeError,RecursionError):
        parsed=None
    if isinstance(parsed,(dict,list)):
        safe=redact(parsed)
        if safe!=parsed:return json.dumps(safe,ensure_ascii=False)
    # JSON embedded inside prose must use the same field policy. Decode balanced
    # values rather than allowing a same-named key to exempt arbitrary text.
    decoder=json.JSONDecoder();pieces=[];cursor=0;index=0
    while index<len(value):
        if value[index] not in '{[':index+=1;continue
        try:embedded,length=decoder.raw_decode(value[index:])
        except ValueError:index+=1;continue
        safe=redact(embedded)
        if safe!=embedded:
            pieces.extend([value[cursor:index],json.dumps(safe,ensure_ascii=False)]);cursor=index+length
        index+=length
    if pieces:value=''.join(pieces)+value[cursor:]
    value = re.sub(r'确认(?:转账|操作)\s*\d{6}|确认导入已核对账单', '[确认短语已移除]', value)
    value = re.sub(r'otpauth://\S+', '[验证配置已移除]', value)
    value = re.sub(r'(?i)\bBearer\s+\S+|\bs[12]_[\w-]+', '[登录凭据已移除]', value)
    value = re.sub(r'(?i)\b(?:sk|api)[-_][A-Za-z0-9_-]{12,}', '[登录凭据已移除]', value)
    value = re.sub(r'''(?i)((?:["']?)(?:密码|口令|凭据|验证码|校验码|动态码|TOTP(?:密钥|码|配置)?|OTP|password|credential|api[_-]?key|secret|access_token|authorization)(?:["']?)(?:[\s：:=]+|为|是)["']?)([^\r\n，。；,;]+)''',
                   r'\1[敏感值已移除]', value)
    value = re.sub(r'(?i)((?:验证码|校验码|动态码|TOTP码|OTP)\s*)(\d{6})',r'\1[敏感值已移除]',value)
    # Account labels only: six-digit amounts and long business IDs survive.
    value = re.sub(r'((?:银行卡号|卡号|收款账号|账号|account_number)[\s：:=为是]*)(\d{8,24})',
                   lambda m: m[1] + '****' + m[2][-4:], value)
    return redact_accounts(value)
