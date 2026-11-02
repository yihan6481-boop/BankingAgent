"""Local CLI for the isolated simulated OTP channel.

Usage (from ``backend/``)::

    python -m security.otp_inbox list
    python -m security.otp_inbox latest
    python -m security.otp_inbox watch

This command is the documented way for a human operator to read a simulated
verification code.  It is deliberately **not** an Agent tool, not part of the
LLM tool catalogue, and not reachable through the chat workflow.
"""

from __future__ import annotations

import argparse
import sys
import time

from .mock_inbox import inbox, mock_inbox_token


def _print_entry(entry: dict):
    print(
        f"[{entry.get('created_at', '?')}] action={entry.get('action', '?')} "
        f"challenge={entry.get('challenge_id', '?')} "
        f"user={entry.get('user_id', '?')} code={entry.get('code', '?')} "
        f"expires={entry.get('expires_at', '?')}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m security.otp_inbox")
    parser.add_argument(
        "command", nargs="?", default="list", choices=["list", "latest", "watch", "token"]
    )
    parser.add_argument("--challenge-id", default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    arguments = parser.parse_args(argv)

    if arguments.command == "token":
        print(mock_inbox_token())
        return 0

    if arguments.command == "latest":
        entry = inbox.latest(arguments.challenge_id)
        if entry is None:
            print("模拟验证码收件箱为空。")
            return 1
        _print_entry(entry)
        return 0

    if arguments.command == "watch":
        print("监听模拟验证码通道（Ctrl+C 退出）...")
        seen = None
        try:
            while True:
                entry = inbox.latest()
                if entry is not None and entry.get("challenge_id") != seen:
                    seen = entry.get("challenge_id")
                    _print_entry(entry)
                time.sleep(max(arguments.interval, 0.2))
        except KeyboardInterrupt:
            print("\n已停止监听。")
            return 0

    entries = inbox.entries()
    if not entries:
        print("模拟验证码收件箱为空。")
        return 1
    for entry in entries:
        _print_entry(entry)
    print(f"共 {len(entries)} 条。该通道仅用于本机模拟演示。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
