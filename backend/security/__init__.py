"""V0.7.0 security layer.

Modules
-------
``config``        thresholds, windows, TTLs and demo-account definitions
``schemas``       strict Pydantic models for decisions/principals/challenges/audit
``db``            durable security tables + demo-data ownership migration
``identity``      simulated principals, salted credentials, bound sessions
``ownership``     uniform object-ownership checks and owner-scoped queries
``policy``        unified Risk Engine / Permission Policy (green/yellow/red)
``strong_auth``   challenge store + MockOTPProvider (simulated strong auth)
``restrictions``  verification-failure lockout, suspicious limiting, breakers
``audit``         desensitized structured decision-chain records
``execution``     authorization grants, atomic consumption, idempotency
``executor``      the single security execution entry point
``mock_inbox``    isolated simulated OTP delivery channel
``service``       process-wide wiring used by the API and the CLI
"""

from .config import DEFAULT_SECURITY_CONFIG, POLICY_VERSION, SecurityConfig

__all__ = ["DEFAULT_SECURITY_CONFIG", "POLICY_VERSION", "SecurityConfig"]
