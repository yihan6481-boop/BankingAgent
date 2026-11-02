"""Process-wide wiring of the V0.7.0 security components.

The API layer, the CLI and the deterministic demo paths all obtain their
identity service, risk engine, strong-auth service and execution gateway from
here so there is exactly one configured security stack per process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from .config import DEFAULT_SECURITY_CONFIG, SecurityConfig, load_security_config
from .identity import IdentityService
from .restrictions import SecurityRestrictions
from .strong_auth import MockOTPProvider, StrongAuthService
from .executor import SecurityExecutionGateway
from .policy import RiskEngine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SecurityServices:
    """Container so tests can build an isolated, time-injected stack."""

    def __init__(self, config: SecurityConfig | None = None, now_provider=None):
        self.config = config or DEFAULT_SECURITY_CONFIG
        self.now_provider = now_provider or utcnow
        from storage.config import pg02_enabled as enabled
        if enabled():
            if self.config.authentication_mode != 'secure':
                raise ValueError('pg02_requires_secure_mode')
            from storage.pg02_identity import PgIdentityService
            self.identity = PgIdentityService(self.config, now_provider=self.now_provider)
        else:
            self.identity = IdentityService(self.config, now_provider=self.now_provider)
        self.policy = RiskEngine(self.config, now_provider=self.now_provider)
        from .mock_inbox import inbox

        from .totp import TOTPService, AuthAdapter
        self.totp = TOTPService(self.config, now_provider=self.now_provider)
        from .payment_auth import PaymentAuthService
        self.payment_auth = PaymentAuthService(self.config, now_provider=self.now_provider)
        self.strong_auth = AuthAdapter(
            self.config,
            totp=self.totp,
            provider=MockOTPProvider(inbox=inbox),
            now_provider=self.now_provider,
            inbox=inbox,
        )
        self.strong_auth.payment_auth = self.payment_auth
        self.restrictions = SecurityRestrictions(
            self.config, now_provider=self.now_provider
        )
        self.gateway = SecurityExecutionGateway(
            self.config,
            policy=self.policy,
            strong_auth=self.strong_auth,
            restrictions=self.restrictions,
            now_provider=self.now_provider,
        )


_lock = RLock()
_services: SecurityServices | None = None


def get_security_services() -> SecurityServices:
    global _services
    with _lock:
        if _services is None:
            _services = SecurityServices(load_security_config())
        return _services


def current_security_config() -> SecurityConfig:
    """Read configuration without initializing identity/migrations in a tx."""
    with _lock:
        return _services.config if _services is not None else load_security_config()


def reset_security_services(services: SecurityServices | None = None):
    """Rebuild the process-wide stack (used by tests after a config change)."""
    global _services
    with _lock:
        _services = services


__all__ = [
    "SecurityServices",
    "get_security_services",
    "reset_security_services",
    "utcnow",
]
