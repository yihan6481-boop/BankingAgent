"""Candidate payment authentication, on the existing challenge/transaction kernel.

This is a controlled DEMO protocol, not proof of phone possession or production
MFA. The six-digit payment PIN and gesture are both knowledge factors.
"""
from datetime import datetime,timedelta
from pathlib import Path
import hashlib,hmac,json,os,re,secrets
from .totp import TOTPService
from .strong_auth import StrongAuthError,StrongAuthService
from .db import write_transaction,read_connection
from .sms_mailbox import normalize_phone,phone_inbox

def enabled():
    return os.environ.get('BA_PAYMENT_AUTH_ENABLED')=='1'

def guard():
    if not enabled():raise StrongAuthError('payment_auth_disabled','新支付验证尚未启用')
    if os.environ.get('BA_DEPLOYMENT_MODE')!='demo' or os.environ.get('BA_SMS_PROVIDER')!='mock':
        raise StrongAuthError('mock_forbidden','Mock验证仅允许在明确的演示模式启用')

def key():
    guard()
    p=Path(os.environ.get('BA_PAYMENT_AUTH_KEY_FILE','')).expanduser().resolve()
    root=Path(__file__).resolve().parents[2]
    if p.is_relative_to(root) or not p.is_file():raise StrongAuthError('auth_key_missing','支付验证密钥必须在工程外独立配置')
    value=p.read_bytes().strip()
    if len(value)<32:raise StrongAuthError('auth_key_invalid','支付验证密钥无效')
    return value

def keyed(value):return hmac.new(key(),value.encode(),hashlib.sha256).hexdigest()
def verifier(value,salt,purpose):
    peppered=hmac.new(key(),(purpose+'\0'+value).encode(),hashlib.sha256).digest()
    return hashlib.pbkdf2_hmac('sha256',peppered,bytes.fromhex(salt),310000).hex()
def mask(phone):return phone[:3]+'****'+phone[-4:]

def simulated_external(recipient_id,connection=None):
    if not enabled() or not isinstance(recipient_id,int):return False
    if connection is not None:
        raw=getattr(connection,'raw',connection)
        return bool(raw.execute('SELECT 1 FROM mobile_security.external_accounts WHERE recipient_id=%s',(recipient_id,)).fetchone())
    with read_connection() as db:return simulated_external(recipient_id,db)

class SmsProvider:
    def send(self,**message):raise NotImplementedError

class MockSmsProvider(SmsProvider):
    """Encrypted delivery spool, readable only via the separate receiver process."""
    def send(self,*,owner,phone,code,challenge_id,expires_at,version):
        guard()
        from cryptography.fernet import Fernet
        directory=Path(os.environ.get('BA_DEMO_SMS_DIR','')).expanduser().resolve()
        if directory.is_relative_to(Path(__file__).resolve().parents[2]) or not directory.is_dir():
            raise StrongAuthError('sms_delivery_unavailable','独立演示短信接收器未配置，发送未完成')
        phone=normalize_phone(phone)
        spool_key=(directory/'spool.key').read_bytes().strip()
        cipher=Fernet(spool_key)
        target=directory/(phone_inbox(phone,spool_key)+'.sms')
        value=dict(phone=mask(phone),code=code,challenge_id=challenge_id,expires_at=expires_at.isoformat(),version=version)
        temporary=directory/(secrets.token_hex(16)+'.tmp')
        try:
            with temporary.open('xb') as stream:stream.write(cipher.encrypt(json.dumps(value).encode()))
            os.replace(temporary,target)
        finally:
            if temporary.exists():temporary.unlink()

class PaymentAuthService(TOTPService):
    def __init__(self,*args,provider=None,**kwargs):
        super().__init__(*args,**kwargs);self.provider=provider or MockSmsProvider()

    def _audit_mobile(self,db,cid,operation,outcome,reason=None):
        db.raw.execute('INSERT INTO mobile_security.audit(challenge_id,operation,outcome,reason) VALUES(%s,%s,%s,%s)',(cid,operation,outcome,reason))

    def status(self,principal):
        guard()
        with read_connection() as db:
            row=db.raw.execute('SELECT phone,generation FROM mobile_security.credentials').fetchone()
        return dict(enabled=True,configured=bool(row),phone_masked=mask(row['phone']) if row else None,
                    demo=True,method='payment',generation=row['generation'] if row else None,
                    sms_ttl_seconds=300,resend_cooldown_seconds=60,max_attempts=3,
                    sms_threshold_cents=int(os.environ.get('BA_PAYMENT_SMS_THRESHOLD_CENTS','300000')),
                    notice='支付密码与手势均为知识因素；Mock短信不证明真实手机号持有权。')

    def _limit(self,db,scope,identity,kind,maximum,*,increment=False):
        now=self._now();hashed=keyed(scope+':'+identity)
        if scope=='source':
            if not db.raw.execute('SELECT mobile_security.source_rate(%s,%s,%s,%s) ok',(hashed,kind,increment,now)).fetchone()['ok']:
                raise StrongAuthError('payment_auth_locked','来源请求过于频繁，请稍后重试')
            return
        row=db.raw.execute('SELECT * FROM mobile_security.limits WHERE scope=%s AND scope_hash=%s AND kind=%s FOR UPDATE',(scope,hashed,kind)).fetchone()
        active=bool(row and row['window_start']+timedelta(minutes=10)>now)
        count=row['count'] if active else 0
        if count>=maximum:raise StrongAuthError('payment_auth_locked','验证尝试过于频繁，请在10分钟窗口后重试')
        if increment:
            db.raw.execute('''INSERT INTO mobile_security.limits(scope,scope_hash,kind,window_start,count) VALUES(%s,%s,%s,%s,1)
              ON CONFLICT(owner_uuid,scope,scope_hash,kind) DO UPDATE SET count=%s,window_start=%s''',
              (scope,hashed,kind,now,count+1,row['window_start'] if active else now))

    def configure(self,principal,sid,*,bootstrap,phone,payment_pin,gesture):
        guard();error=None;reset=False
        if not re.fullmatch(r'1[3-9][0-9]{9}',phone) or not re.fullmatch(r'[0-9]{6}',payment_pin) or not re.fullmatch(r'[1-9]{4,9}',gesture) or len(set(gesture))!=len(gesture):
            raise StrongAuthError('invalid_security_credential','手机号、六位支付密码或至少四个不重复节点的手势无效')
        with write_transaction() as db:
            self._limit(db,'account',principal.principal_id,'bootstrap',8)
            row=db.raw.execute('SELECT * FROM mobile_security.bootstrap WHERE token_hash=%s AND used_at IS NULL AND expires_at>%s FOR UPDATE',(keyed('bootstrap:'+bootstrap),self._now())).fetchone()
            prior=db.raw.execute('SELECT generation FROM mobile_security.credentials FOR UPDATE').fetchone()
            if not row or (prior is not None)!=(bool(row and row['purpose']=='reset')):
                self._limit(db,'account',principal.principal_id,'bootstrap',8,increment=True)
                self._audit_mobile(db,None,'configure','denied','bootstrap_invalid');error='bootstrap_invalid'
            else:
                ps=secrets.token_hex(24);gs=secrets.token_hex(24);generation=(prior['generation']+1) if prior else 1;reset=prior is not None
                db.raw.execute('''INSERT INTO mobile_security.credentials(phone,payment_salt,payment_hash,gesture_salt,gesture_hash,generation)
                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_uuid) DO UPDATE SET phone=excluded.phone,payment_salt=excluded.payment_salt,
                   payment_hash=excluded.payment_hash,gesture_salt=excluded.gesture_salt,gesture_hash=excluded.gesture_hash,generation=excluded.generation,updated_at=now()''',
                   (phone,ps,verifier(payment_pin,ps,'payment'),gs,verifier(gesture,gs,'gesture'),generation))
                db.raw.execute('UPDATE mobile_security.bootstrap SET used_at=%s WHERE token_hash=%s',(self._now(),row['token_hash']))
                db.raw.execute("UPDATE pg02.security_challenges SET status='cancelled',cancelled_at=%s WHERE status IN ('pending','verified')",(self._now().isoformat(),))
                db.raw.execute('UPDATE pg02.security_authorizations SET consumed=1,consumed_at=%s WHERE consumed=0',(self._now().isoformat(),))
                if reset:db.raw.execute('UPDATE pg02.auth_sessions SET revoked=1')
                else:db.raw.execute('UPDATE pg02.auth_sessions SET revoked=1 WHERE session_id<>%s',(sid,))
                self._audit_mobile(db,None,'reset' if reset else 'enroll','ok')
        if error:raise StrongAuthError(error,'引导凭据无效、已使用或已过期；请向测试负责人独立领取')
        return dict(configured=True,reauthentication_required=reset,phone_masked=mask(phone),demo=True)

    def requirements(self,permission_level,arguments,action):
        from decimal import Decimal
        raw=arguments.get('amount_cents')
        if raw is None:
            raw=int(Decimal(str(arguments.get('amount','0')))*100)
        threshold=int(os.environ.get('BA_PAYMENT_SMS_THRESHOLD_CENTS','300000'))
        if threshold<=0:raise StrongAuthError('payment_policy_invalid','短信分级配置无效')
        gesture=permission_level=='red'
        sms=gesture and (int(raw)>threshold or action in {'report_card_lost','set_daily_limit'})
        if action=='transfer_money' and simulated_external(arguments.get('recipient_id')):gesture=sms=True
        if action=='buy_wealth_product':
            with read_connection() as db:product=db.raw.execute('SELECT risk_level FROM pg02.wealth_products WHERE product_id=%s',(arguments.get('product_id'),)).fetchone()
            if product and product['risk_level']=='R5':gesture=sms=True
        return gesture,sms

    def create_challenge(self,principal,sid,action,action_digest,decision_id,policy_version,permission_level,*,arguments=None):
        guard();arguments=arguments or {}
        with write_transaction() as db:
            row=db.raw.execute('SELECT * FROM mobile_security.credentials').fetchone()
            if row is None:raise StrongAuthError('factor_not_bound','请先在安全中心设置支付密码和交易手势')
            self._limit(db,'account',principal.principal_id,'failure',8)
            self._limit(db,'account',principal.principal_id,'challenge',30,increment=True)
            cid='ch_'+secrets.token_hex(12);now=self._now();gesture,sms=self.requirements(permission_level,arguments,action)
            db.raw.execute('''INSERT INTO pg02.security_challenges(challenge_id,session_id,principal_id,user_id,action,action_digest,decision_id,
               policy_version,permission_level,method,status,max_attempts,created_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'payment','pending',3,%s,%s)''',
               (cid,sid,principal.principal_id,principal.user_id,action,action_digest,decision_id,policy_version,permission_level,now.isoformat(),(now+timedelta(minutes=5)).isoformat()))
            db.raw.execute('INSERT INTO mobile_security.challenges(challenge_id,credential_generation,gesture_required,sms_required) VALUES(%s,%s,%s,%s)',(cid,row['generation'],gesture,sms))
            self._audit_mobile(db,cid,'challenge','created')
        return self.load_view(cid),None

    def _view_from_row(self,row):
        return StrongAuthService._view_from_row(self,row).model_copy(update={'reason_codes':['payment_auth_demo'],'masked_delivery_hint':'交易支付验证 · 演示通道'})

    def _bound(self,db,principal,sid,cid,binding,device=None):
        row=db.raw.execute('SELECT * FROM pg02.security_challenges WHERE challenge_id=%s FOR UPDATE',(cid,)).fetchone()
        if not row or row['principal_id']!=principal.principal_id or row['method']!='payment':raise StrongAuthError('challenge_not_found','验证挑战不存在')
        if row['session_id']!=sid:raise StrongAuthError('challenge_session_mismatch','验证挑战不属于当前会话')
        self.assert_preview(db,cid,principal,task_id=binding['task_id'],revision=binding['revision'],preview_id=binding['preview_id'])
        meta=db.raw.execute('SELECT * FROM mobile_security.challenges WHERE challenge_id=%s FOR UPDATE',(cid,)).fetchone()
        credential=db.raw.execute('SELECT * FROM mobile_security.credentials').fetchone()
        if not meta or not credential or meta['credential_generation']!=credential['generation']:raise StrongAuthError('security_generation_changed','安全凭据已变更，请重新预览')
        if device and meta['device_hash'] and not hmac.compare_digest(meta['device_hash'],device):raise StrongAuthError('device_context_changed','浏览器上下文已变化，请重新预览')
        if row['status']!='pending':raise StrongAuthError('challenge_already_used' if row['status'] in {'verified','consumed'} else 'challenge_ended','验证挑战已结束')
        if datetime.fromisoformat(row['expires_at'])<=self._now():raise StrongAuthError('challenge_expired','验证挑战已过期，请重新预览')
        return row,meta,credential

    def describe(self,principal,sid,cid,binding):
        guard()
        with write_transaction() as db:
            row,meta,cred=self._bound(db,principal,sid,cid,binding)
            return dict(challenge_id=cid,method='payment',payment_ok=meta['payment_ok'],gesture_ok=meta['gesture_ok'],gesture_required=meta['gesture_required'],sms_required=meta['sms_required'],phone_masked=mask(cred['phone']),send_status=meta['send_status'],resend_available_at=(meta['sent_at']+timedelta(seconds=60)).isoformat() if meta['sent_at'] else None,attempts_remaining=row['max_attempts']-row['attempt_count'],expires_at=row['expires_at'],demo=True)

    def proof(self,principal,sid,cid,binding,stage,value,device,source):
        guard();error=None
        with write_transaction() as db:
            row,meta,cred=self._bound(db,principal,sid,cid,binding,device)
            for scope,identity,maximum in [('account',principal.principal_id,8),('session',sid,6),('source',source,20)]:self._limit(db,scope,identity,'failure',maximum)
            if stage=='payment':
                valid=hmac.compare_digest(verifier(value,cred['payment_salt'],'payment'),cred['payment_hash'])
            elif stage=='gesture':
                if not meta['payment_ok'] or not meta['gesture_required']:raise StrongAuthError('payment_sequence_invalid','请先校验支付密码')
                valid=hmac.compare_digest(verifier(value,cred['gesture_salt'],'gesture'),cred['gesture_hash'])
            elif stage=='otp':
                if not meta['payment_ok'] or (meta['gesture_required'] and not meta['gesture_ok']) or not meta['sms_required']:raise StrongAuthError('payment_sequence_invalid','请先完成支付密码与必要手势验证')
                valid=bool(meta['send_status']=='sent' and meta['otp_hash'] and hmac.compare_digest(keyed('otp:'+cid+':'+str(meta['otp_version'])+':'+value),meta['otp_hash']))
            else:raise StrongAuthError('invalid_auth_stage','不支持的验证步骤')
            if not valid:
                count=row['attempt_count']+1
                db.raw.execute("UPDATE pg02.security_challenges SET attempt_count=%s,status=%s WHERE challenge_id=%s",(count,'failed' if count>=3 else 'pending',cid))
                for scope,identity,maximum in [('account',principal.principal_id,8),('session',sid,6),('source',source,20)]:self._limit(db,scope,identity,'failure',maximum,increment=True)
                self._audit_mobile(db,cid,stage,'denied','credential_invalid')
                error='challenge_attempts_exhausted' if count>=3 else 'invalid_code'
            else:
                if stage=='payment':
                    db.raw.execute('UPDATE mobile_security.challenges SET payment_ok=true,device_hash=COALESCE(device_hash,%s) WHERE challenge_id=%s',(device,cid));meta=dict(meta,payment_ok=True)
                if stage=='gesture':
                    db.raw.execute('UPDATE mobile_security.challenges SET gesture_ok=true WHERE challenge_id=%s',(cid,));meta=dict(meta,gesture_ok=True)
                if stage=='otp':db.raw.execute('UPDATE mobile_security.challenges SET sms_ok=true,otp_hash=NULL WHERE challenge_id=%s',(cid,))
                complete=meta['payment_ok'] and (not meta['gesture_required'] or meta['gesture_ok']) and (not meta['sms_required'] or stage=='otp')
                if complete:db.raw.execute("UPDATE pg02.security_challenges SET status='verified',verified_at=%s WHERE challenge_id=%s AND status='pending'",(self._now().isoformat(),cid))
                self._audit_mobile(db,cid,stage,'ok')
        if error:raise StrongAuthError(error,'验证未通过；尝试次数累计，重发或新建任务不会清除账号限流')
        return dict(verified=complete,challenge=self.load_view(cid).model_dump(mode='json'))

    def assert_requirements(self,challenge_id,decision,arguments,connection=None):
        """A proof cannot survive cumulative-risk or credential escalation."""
        def check(db):
            meta=db.raw.execute('SELECT m.*,c.generation FROM mobile_security.challenges m JOIN mobile_security.credentials c USING(owner_uuid) WHERE challenge_id=%s',(challenge_id,)).fetchone()
            gesture,sms=self.requirements(decision.permission_level,arguments,decision.action)
            if (not meta or meta['generation']!=meta['credential_generation'] or not meta['payment_ok']
                    or gesture and not meta['gesture_ok'] or sms and not meta['sms_ok']):
                raise StrongAuthError('payment_policy_escalated','当前风险或凭据已变化，旧验证不足，请重新读取任务并预览')
        if connection:check(connection)
        else:
            with read_connection() as db:check(db)

    def send_sms(self,principal,sid,cid,binding,device,source):
        guard();error=False
        with write_transaction() as db:
            row,meta,cred=self._bound(db,principal,sid,cid,binding,device)
            if not meta['payment_ok'] or (meta['gesture_required'] and not meta['gesture_ok']) or not meta['sms_required']:raise StrongAuthError('payment_sequence_invalid','先完成支付密码与必要手势验证后才能发短信')
            self._limit(db,'account',principal.principal_id,'failure',8)
            now=self._now()
            if meta['sent_at'] and meta['sent_at']+timedelta(seconds=60)>now:raise StrongAuthError('resend_too_soon','重发冷却60秒，请稍后重试')
            for scope,identity,maximum in [('account',principal.principal_id,5),('session',sid,5),('source',source,15)]:self._limit(db,scope,identity,'send',maximum,increment=True)
            code=f'{secrets.randbelow(1000000):06d}';version=meta['otp_version']+1
            try:self.provider.send(owner=db.context.owner_uuid,phone=cred['phone'],code=code,challenge_id=cid,expires_at=datetime.fromisoformat(row['expires_at']),version=version)
            except Exception:
                error=True
                db.raw.execute("UPDATE mobile_security.challenges SET send_status='failed',otp_hash=NULL WHERE challenge_id=%s",(cid,));self._audit_mobile(db,cid,'sms','failed','delivery_failed')
            else:
                db.raw.execute("UPDATE mobile_security.challenges SET otp_hash=%s,otp_version=%s,sent_at=%s,send_status='sent' WHERE challenge_id=%s",(keyed('otp:'+cid+':'+str(version)+':'+code),version,now,cid))
                db.raw.execute('UPDATE pg02.security_challenges SET resend_available_at=%s WHERE challenge_id=%s',((now+timedelta(seconds=60)).isoformat(),cid));self._audit_mobile(db,cid,'sms','sent')
        if error:raise StrongAuthError('sms_delivery_failed','演示短信发送失败，未显示发送成功；请核查接收器配置')
        return dict(send_status='sent',phone_masked=mask(cred['phone']),demo=True,resend_available_at=(now+timedelta(seconds=60)).isoformat())
