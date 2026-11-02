"""Independent authenticator, using PyOTP (RFC 6238) and Fernet at rest.

No reset/recovery endpoint exists. Enrollment requires the account password
again and never replaces even an unconfirmed factor. Secrets are only returned
by the authorized enrollment call, not by challenge/status/business APIs.
"""
from __future__ import annotations

import hmac
import json
import os
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from .audit import AuditChain, AuditRecorder
from .db import read_connection, write_transaction
from .strong_auth import StrongAuthService, StrongAuthError
from .schemas import ChallengePublicView

independent_protocol = ContextVar('independent_strong_auth', default=False)
INTERVAL = 30
WINDOW = 1


def migrate(connection):
    # The old V2 JSON contract remains frozen. Only the internal durable store
    # expands its method set; preserve existing challenges and their indexes.
    from .db import SECURITY_SCHEMA_SQL
    connection.execute('BEGIN IMMEDIATE')
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='security_challenges'").fetchone()[0]
    if "'totp'" not in sql:
        new_sql = sql.replace('security_challenges', 'security_challenges_091', 1).replace(
            "'mock_otp', 'confirmation'", "'mock_otp', 'confirmation', 'totp'")
        connection.execute(new_sql)
        connection.execute('INSERT INTO security_challenges_091 SELECT * FROM security_challenges')
        connection.execute('DROP TABLE security_challenges')
        connection.execute('ALTER TABLE security_challenges_091 RENAME TO security_challenges')
        for statement in SECURITY_SCHEMA_SQL.split(';'):
            if 'CREATE INDEX' in statement and ' ON security_challenges' in statement.replace('\n', ' '):
                connection.execute(statement)
    connection.execute('''CREATE TABLE IF NOT EXISTS authenticator_factors(
        user_id INTEGER PRIMARY KEY, principal_id TEXT NOT NULL UNIQUE,
        encrypted_secret TEXT NOT NULL, enrollment_session TEXT NOT NULL,
        enrollment_expires_at TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
        last_used_step INTEGER NOT NULL DEFAULT -1, created_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS authenticator_bindings(
        challenge_id TEXT PRIMARY KEY REFERENCES security_challenges(challenge_id),
        task_id TEXT NOT NULL, step_id TEXT NOT NULL, preview_id TEXT NOT NULL,
        binding_json TEXT NOT NULL)''')
    connection.commit()


class TOTPService(StrongAuthService):
    def cipher(self):
        # Master key must be supplied outside DB/source/image. Never generate a
        # fallback; errors do not echo environment values or filesystem paths.
        filename = os.environ.get('BA091_TOTP_MASTER_KEY_FILE', '')
        try:
            if not filename:
                raise ValueError()
            return Fernet(Path(filename).read_bytes().strip())
        except (OSError, ValueError):
            raise StrongAuthError('factor_key_unavailable', '验证器主密钥未配置或不可用') from None

    def _secret(self, row):
        try:
            payload = json.loads(self.cipher().decrypt(row['encrypted_secret'].encode()))
            if payload['user_id'] != row['user_id'] or payload['principal_id'] != row['principal_id']:
                raise ValueError()
            return payload['secret']
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise StrongAuthError('factor_key_unavailable', '验证器密钥无法解密') from None

    def status(self, principal):
        with read_connection() as db:
            row = db.execute('SELECT * FROM authenticator_factors WHERE user_id=? AND principal_id=?',
                             (principal.user_id, principal.principal_id)).fetchone()
        pending = bool(row is not None and not row['confirmed'])
        expires = None
        expired = False
        if pending:
            try:
                expires = datetime.fromisoformat(row['enrollment_expires_at'])
                expired = expires <= self._now()
            except (TypeError, ValueError):
                expired = True
        return {'contract_version': 'strong-auth-1.0', 'method': 'totp',
                'bound': row is not None, 'confirmed': bool(row and row['confirmed']),
                'enrollment_pending': pending,
                'enrollment_expires_at': expires,
                'enrollment_expired': expired,
                # Only an unconfirmed enrollment may be re-created after an
                # account-password recheck; a confirmed factor is immutable.
                'can_reenroll': pending,
                'interval_seconds': INTERVAL, 'window_steps': WINDOW,
                'mode': self.config.authentication_mode}

    def enroll(self, principal, session_id, password):
        from .identity import verify_password
        cipher = self.cipher()
        with write_transaction() as db:
            credential = db.execute('SELECT * FROM credentials WHERE credential_id=? AND principal_id=? AND revoked=0',
                (principal.credential_id, principal.principal_id)).fetchone()
            if credential is None or not verify_password(password, credential['password_salt'],
                    credential['password_hash'], credential['hash_iterations']):
                raise StrongAuthError('invalid_credentials', '账号密码复核失败')
            existing = db.execute('SELECT * FROM authenticator_factors WHERE user_id=?', (principal.user_id,)).fetchone()
            secret = pyotp.random_base32()
            encrypted = cipher.encrypt(json.dumps({'user_id': principal.user_id,
                'principal_id': principal.principal_id, 'secret': secret}).encode()).decode()
            now = self._now()
            expires = now + timedelta(minutes=10)
            if existing is not None and existing['confirmed']:
                raise StrongAuthError('factor_already_bound', '已有验证器，当前版本不支持恢复或重绑')
            if existing is not None:
                # Unconfirmed (possibly expired) enrollment: password recheck done
                # above; replace only the pending secret so refresh/new-device can
                # restart the same first-time binding. The old QR becomes invalid.
                db.execute('UPDATE authenticator_factors SET encrypted_secret=?,enrollment_session=?,'
                           'enrollment_expires_at=?,last_used_step=-1 WHERE user_id=?',
                           (encrypted, session_id, expires.isoformat(), principal.user_id))
                self._audit(db, principal, session_id, 'pending', reason='factor_enrollment_restart')
            else:
                db.execute('INSERT INTO authenticator_factors VALUES(?,?,?,?,?,0,-1,?)',
                    (principal.user_id, principal.principal_id, encrypted, session_id, expires.isoformat(), now.isoformat()))
                self._audit(db, principal, session_id, 'pending', reason='factor_enrollment')
        return {'contract_version': 'strong-auth-1.0', 'method': 'totp',
                'otpauth_uri': pyotp.TOTP(secret, interval=INTERVAL).provisioning_uri(
                    name=principal.principal_id, issuer_name='Banking Agent'), 'expires_at': expires}

    def _factor(self, db, principal):
        row = db.execute('SELECT * FROM authenticator_factors WHERE user_id=? AND principal_id=?',
                         (principal.user_id, principal.principal_id)).fetchone()
        if row is None:
            raise StrongAuthError('factor_not_bound', '请先绑定并确认独立验证器')
        return row

    def _check_code(self, db, principal, factor, code):
        from .restrictions import SecurityRestrictions
        restrictions = SecurityRestrictions(self.config, now_provider=self.now_provider)
        lock = restrictions.strong_auth_lock(principal)
        if lock and lock.active:
            return 'strong_auth_locked'
        matched = None
        if isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit():
            totp = pyotp.TOTP(self._secret(factor), interval=INTERVAL)
            current = int(self._now().timestamp()) // INTERVAL
            for step in range(current - WINDOW, current + WINDOW + 1):
                if step >= 0 and hmac.compare_digest(totp.at(step * INTERVAL), code):
                    matched = step
        if matched is None or matched <= factor['last_used_step']:
            restrictions.record_strong_auth_failure(principal, connection=db)
            return 'code_replayed' if matched is not None else 'invalid_code'
        # The transaction lock and CAS protect the user's high-water mark,
        # including concurrent challenges, enrollment, and process restarts.
        updated = db.execute('UPDATE authenticator_factors SET last_used_step=? WHERE user_id=? AND last_used_step<?',
                            (matched, principal.user_id, matched))
        if updated.rowcount != 1:
            return 'code_replayed'
        restrictions.clear_strong_auth_failures(principal, connection=db)
        return None

    def confirm_enrollment(self, principal, session_id, code):
        with write_transaction() as db:
            factor = self._factor(db, principal)
            if factor['confirmed']:
                raise StrongAuthError('factor_already_bound', '验证器已确认，不支持重绑')
            if factor['enrollment_session'] != session_id:
                raise StrongAuthError('factor_session_mismatch', '绑定流程不属于当前会话')
            if datetime.fromisoformat(factor['enrollment_expires_at']) <= self._now():
                raise StrongAuthError('enrollment_expired', '绑定流程已过期，需要管理员线下核查')
            error = self._check_code(db, principal, factor, code)
            if not error:
                db.execute('UPDATE authenticator_factors SET confirmed=1 WHERE user_id=?', (principal.user_id,))
            self._audit(db, principal, session_id, 'denied' if error else 'ok', error=error, reason='factor_confirmation')
        if error:
            raise StrongAuthError(error, '验证器绑定确认失败')
        return self.status(principal)

    def create_challenge(self, principal, session_id, action, action_digest, decision_id, policy_version, permission_level):
        import secrets
        self.cipher()
        with write_transaction() as db:
            factor = self._factor(db, principal)
            if not factor['confirmed']:
                raise StrongAuthError('factor_not_confirmed', '验证器绑定尚未确认')
            # Verify that the deployed key can decrypt before issuing a challenge.
            self._secret(factor)
            now = self._now()
            challenge_id = 'ch_' + secrets.token_hex(12)
            db.execute('''INSERT INTO security_challenges
                (challenge_id,session_id,principal_id,user_id,action,action_digest,decision_id,
                 policy_version,permission_level,method,status,max_attempts,created_at,expires_at)
                 VALUES(?,?,?,?,?,?,?,?,?,'totp','pending',?,?,?)''',
                (challenge_id,session_id,principal.principal_id,principal.user_id,action,action_digest,decision_id,
                 policy_version,permission_level,self.config.otp_max_attempts,now.isoformat(),
                 (now+timedelta(seconds=self.config.challenge_ttl_seconds)).isoformat()))
        return self.load_view(challenge_id), None

    def _view_from_row(self, row):
        view = super()._view_from_row(row)
        return view.model_copy(update={'reason_codes': ['independent_totp'],
                                      'masked_delivery_hint': '用户持有的独立验证器'})

    def bind_preview(self, challenge_id, binding):
        from lifecycle.store import digest
        value = binding.model_dump(mode='json')
        with write_transaction() as db:
            db.execute('INSERT OR IGNORE INTO authenticator_bindings VALUES(?,?,?,?,?)',
                (challenge_id,binding.task_id,binding.step_id,digest(value),json.dumps(value)))

    def assert_preview(self, db, challenge_id, principal, *, task_id=None, revision=None, preview_id=None):
        from lifecycle.store import TaskStore, TaskError
        from lifecycle.service import matches_preview
        from agent.schemas import TaskPreviewBinding
        binding = db.execute('SELECT * FROM authenticator_bindings WHERE challenge_id=?', (challenge_id,)).fetchone()
        if binding is None or task_id is not None and binding['task_id'] != task_id or preview_id is not None and binding['preview_id'] != preview_id:
            raise StrongAuthError('stale_preview', '验证挑战未绑定当前任务预览')
        row = db.execute('SELECT * FROM lifecycle_tasks WHERE task_id=?', (binding['task_id'],)).fetchone()
        if revision is not None and row and row['lease_token'] and row['lease_until'] > self._now().isoformat():
            raise StrongAuthError('task_busy', '任务正在处理，请重新读取后重试')
        try:
            record = TaskStore()._decode(db, row, principal)
        except TaskError:
            raise StrongAuthError('stale_preview', '任务预览已失效') from None
        if (record.status not in {'active', 'waiting'} or revision is not None and record.revision != revision or
                not matches_preview(record, TaskPreviewBinding.model_validate_json(binding['binding_json']))):
            raise StrongAuthError('stale_preview', '任务预览已变化')
        return binding

    def verify(self, principal, session_id, challenge_id, code, *, action_digest=None,
               task_id=None, revision=None, preview_id=None):
        error = None
        with write_transaction() as db:
            row = db.execute('SELECT * FROM security_challenges WHERE challenge_id=?', (challenge_id,)).fetchone()
            if row is None or row['principal_id'] != principal.principal_id or row['method'] != 'totp':
                raise StrongAuthError('challenge_not_found', '验证挑战不存在')
            if row['session_id'] != session_id:
                raise StrongAuthError('challenge_session_mismatch', '挑战不属于当前会话')
            binding = self.assert_preview(db, challenge_id, principal, task_id=task_id, revision=revision, preview_id=preview_id)
            if row['status'] != 'pending':
                raise StrongAuthError('challenge_already_used' if row['status'] in {'verified','consumed'} else 'challenge_cancelled', '挑战已结束')
            if datetime.fromisoformat(row['expires_at']) <= self._now():
                db.execute("UPDATE security_challenges SET status='expired' WHERE challenge_id=?", (challenge_id,))
                error = 'challenge_expired'
            elif action_digest is not None and row['action_digest'] != action_digest:
                error = 'challenge_parameter_changed'
            else:
                factor = self._factor(db, principal)
                if not factor['confirmed']:
                    raise StrongAuthError('factor_not_confirmed', '验证器绑定尚未确认')
                error = self._check_code(db, principal, factor, code)
                if error and error != 'strong_auth_locked':
                    attempts = row['attempt_count'] + 1
                    db.execute('UPDATE security_challenges SET attempt_count=?,status=? WHERE challenge_id=?',
                        (attempts, 'failed' if attempts >= row['max_attempts'] else 'pending', challenge_id))
                    if attempts >= row['max_attempts']:
                        error = 'challenge_attempts_exhausted'
                elif not error:
                    db.execute("UPDATE security_challenges SET status='verified',verified_at=? WHERE challenge_id=? AND status='pending'",
                               (self._now().isoformat(),challenge_id))
            self._audit(db, principal, session_id, 'denied' if error else 'ok', error=error,
                        challenge=row, binding=binding)
        if error:
            raise StrongAuthError(error, '独立第二因子校验失败')
        return self.load_view(challenge_id)

    def _audit(self, db, principal, session_id, outcome, *, error=None, reason=None, challenge=None, binding=None):
        AuditRecorder(self.now_provider).record(AuditChain(), 'strong_auth', outcome, connection=db,
            principal=principal, session_id=session_id, authentication_method='totp', error_code=error,
            task_id=binding['task_id'] if binding else None, step_id=binding['step_id'] if binding else None,
            challenge_id=challenge['challenge_id'] if challenge else None,
            action=challenge['action'] if challenge else None,
            decision_id=challenge['decision_id'] if challenge else None,
            detail={'reason': reason, 'preview_id': binding['preview_id'] if binding else None})


class AuthAdapter(StrongAuthService):
    """Reuse the challenge store and kernel; select the independent protocol."""
    def __init__(self, *args, totp, **kwargs):
        super().__init__(*args, **kwargs)
        self.totp = totp

    def create_challenge(self, *args, **kwargs):
        from .payment_auth import enabled
        arguments=kwargs.pop('mobile_arguments',None)
        if enabled():
            if not independent_protocol.get():
                raise StrongAuthError('unsupported_authentication_method','请使用绑定任务的支付验证协议')
            return self.payment_auth.create_challenge(*args,arguments=arguments,**kwargs)
        if independent_protocol.get():
            return self.totp.create_challenge(*args, **kwargs)
        if self.config.authentication_mode == 'secure':
            raise StrongAuthError('unsupported_authentication_method', '安全模式请使用 /api/secure 的独立TOTP合同')
        return super().create_challenge(*args, **kwargs)

    def load_view(self, challenge_id):
        row = self._row(challenge_id)
        if row and row['method'] == 'payment':
            return self.payment_auth._view_from_row(row)
        if row and row['method'] == 'totp':
            return self.totp._view_from_row(row)
        return super().load_view(challenge_id)

    def verify(self, *args, **kwargs):
        row = self._row(args[2] if len(args)>2 else kwargs['challenge_id'])
        if self.config.authentication_mode == 'secure' or row and row['method'] == 'totp':
            raise StrongAuthError('unsupported_authentication_method', '旧接口不支持独立TOTP验证')
        return super().verify(*args, **kwargs)

    def resend(self, *args, **kwargs):
        row = self._row(args[2] if len(args)>2 else kwargs['challenge_id'])
        if self.config.authentication_mode == 'secure' or row and row['method'] == 'totp':
            raise StrongAuthError('unsupported_authentication_method', 'TOTP由用户验证器生成，不支持模拟重发')
        return super().resend(*args, **kwargs)
