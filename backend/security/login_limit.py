"""Bounded login reservations keyed by trusted peer and normalized account.

No account-only lock: guessing a victim name cannot lock them on another peer.
Forwarded headers are deliberately ignored; deploy behind a trusted proxy only
with uvicorn proxy processing disabled or a separately audited trust boundary.
"""
import hashlib


def reserve(source, username, *, success=False, status_only=False):
    from security.service import get_security_services
    from storage.pg02 import store
    services = get_security_services()
    cfg = services.config
    peer = str(source or 'unknown')[:200]
    account = str(username).strip()  # Matches PgIdentityService.lookup_login.
    pair = hashlib.sha256(('pair\0' + peer + '\0' + account).encode()).hexdigest()
    shared = hashlib.sha256(('source\0' + peer).encode()).hexdigest()
    now = services.restrictions._now()
    with store()._pool.connection() as connection, connection.transaction():
        def gate(key, mode, threshold):
            return connection.execute('SELECT pg02.login_gate(%s,%s,%s,%s,%s,%s) AS value',
                (key, mode, now, threshold, cfg.suspicious_window_seconds,
                 min(3600, cfg.suspicious_lockout_seconds))).fetchone()['value']
        if success:
            gate(pair, 'success', cfg.suspicious_threshold)
            gate(shared, 'release', cfg.suspicious_threshold * 4)
            return 0
        if status_only:
            return max(gate(pair, 'status', cfg.suspicious_threshold),
                       gate(shared, 'status', cfg.suspicious_threshold * 4))
        # Check pair first: blocked repeats do not extend cooldown or exhaust
        # the shared-network allowance for unrelated accounts.
        wait = gate(pair, 'status', cfg.suspicious_threshold)
        if wait:
            return wait
        wait = gate(shared, 'acquire', cfg.suspicious_threshold * 4)
        if wait:
            return wait
        return gate(pair, 'acquire', cfg.suspicious_threshold)
