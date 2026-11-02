"""Isolated simulated OTP delivery channel.

The verification code must reach the human operator through a channel that is
**not** the normal API surface:

* it is not registered as an Agent tool, so the LLM can never read a code;
* it is not a freely readable public endpoint — the HTTP route only returns
  codes for the local demo when ``BA07_MOCK_OTP_INBOX_TOKEN`` matches, and the
  primary interface is the local CLI ``python -m security.otp_inbox``;
* it is never written into the business database, the audit chain, fixtures,
  ``/state``, error payloads or logs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock

DEFAULT_INBOX_PATH = Path(__file__).resolve().parent.parent / ".mock_otp_inbox.jsonl"


@dataclass
class MockInboxEntry:
    challenge_id: str
    code: str
    action: str
    principal_id: str
    user_id: int
    session_id: str
    created_at: str
    expires_at: str


class MockOTPInbox:
    """Append-only simulated delivery channel for verification codes."""

    def __init__(self, path: Path | None = None, max_entries: int = 200):
        self.path = Path(path) if path else DEFAULT_INBOX_PATH
        self.max_entries = max_entries
        self._lock = RLock()
        self._memory: list[dict] = []
        self.persist = os.environ.get("BA07_MOCK_OTP_INBOX_PERSIST", "1") != "0"

    def reset(self):
        with self._lock:
            self._memory = []
            if self.persist and self.path.exists():
                try:
                    self.path.unlink()
                except OSError:
                    pass

    def record(self, principal, session_id, challenge_id, code, expires_at, action):
        entry = MockInboxEntry(
            challenge_id=challenge_id,
            code=code,
            action=action,
            principal_id=principal.principal_id,
            user_id=int(principal.user_id),
            session_id=session_id,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            expires_at=expires_at.isoformat(),
        )
        payload = entry.__dict__
        with self._lock:
            self._memory.append(payload)
            if len(self._memory) > self.max_entries:
                self._memory = self._memory[-self.max_entries:]
            if self.persist:
                try:
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                except OSError:
                    # 模拟通道不可用时不影响安全闭环：代码仍是仅服务端可推导的。
                    self.persist = False

    def entries(self) -> list[dict]:
        with self._lock:
            if self._memory:
                return list(self._memory)
            if not self.persist or not self.path.exists():
                return []
            result = []
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                return []
            return result[-self.max_entries:]

    def latest(
        self,
        challenge_id: str | None = None,
        *,
        principal_id: str | None = None,
        session_id: str | None = None,
    ) -> dict | None:
        for entry in reversed(self.entries()):
            if challenge_id is not None and entry.get("challenge_id") != challenge_id:
                continue
            if principal_id is not None and entry.get("principal_id") != principal_id:
                continue
            if session_id is not None and entry.get("session_id") != session_id:
                continue
            return entry
        return None


inbox = MockOTPInbox()

__all__ = [
    "DEFAULT_INBOX_PATH",
    "MockInboxEntry",
    "MockOTPInbox",
    "inbox",
]
