"""Formal identity adapter; mappings and credentials are authoritative PG rows."""
from datetime import timedelta
import secrets
from uuid import UUID

from security.identity import IdentityService,IdentityError,verify_password
from security.schemas import Principal,SessionBinding
from .pg02 import (lookup,verified_session,principal_scope,VerifiedContext,get_connection)


class PgIdentityService(IdentityService):
    def __init__(self,config,now_provider):
        self.config,self.now_provider=config,now_provider
        self._legacy_session_id=None

    def login(self,username,password):
        if not isinstance(username,str) or not username.strip() or not isinstance(password,str) or not password:
            raise IdentityError('invalid_credentials','用户名或密码不正确')
        row=lookup('lookup_login',username.strip())
        if row is None or not verify_password(password,row['password_salt'],row['password_hash'],row['hash_iterations']):
            raise IdentityError('invalid_credentials','用户名或密码不正确')
        context=VerifiedContext(UUID(row['owner_uuid']),row['principal_id'],row['principal_user_id'],row['credential_id'],None)
        now=self._now()
        sid='s2_'+secrets.token_hex(self.config.session_token_bytes)
        expires=now+timedelta(seconds=self.config.credential_ttl_seconds)
        principal=Principal(principal_id=context.principal_id,user_id=context.user_id,
            display_name=row['principal_display_name'],kind=row['principal_kind'],
            authenticated_at=now,credential_id=context.credential_id)
        binding=SessionBinding(session_id=sid,principal_id=context.principal_id,user_id=context.user_id,
            credential_id=context.credential_id,protocol='v2',created_at=now,last_seen_at=now,
            credential_expires_at=expires)
        with principal_scope(context):
            db=get_connection()
            try:
                db.execute('BEGIN IMMEDIATE')
                fresh=db.execute("SELECT c.* FROM credentials c JOIN principals p ON p.principal_id=c.principal_id "
                                 "JOIN core.users u ON u.id=p.owner_uuid WHERE c.credential_id=? AND c.revoked=0 "
                                 "AND p.status='active' AND u.status='active'",(context.credential_id,)).fetchone()
                if fresh is None or not verify_password(password,fresh['password_salt'],fresh['password_hash'],fresh['hash_iterations']):
                    raise IdentityError('invalid_credentials','用户名或密码不正确')
                db.execute('INSERT INTO auth_sessions(session_id,principal_id,user_id,credential_id,protocol,'
                           'created_at,last_seen_at,credential_expires_at,revoked) VALUES(?,?,?,?,?,?,?,?,0)',
                           (sid,context.principal_id,context.user_id,context.credential_id,'v2',now.isoformat(),now.isoformat(),expires.isoformat()))
                db.commit()
            finally: db.close()
        return principal,binding,'unused'

    def legacy_demo_principal(self):
        raise IdentityError('legacy_demo_disabled','PG02只支持已认证转账入口')

    def create_legacy_session(self):
        return self.legacy_demo_principal()
