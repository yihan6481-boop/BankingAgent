"""Existing explicit user confirmation protocol, never model authorization."""
import re
CONFIRM_WORDS = {
    "确认",
    "确认执行",
    "确定执行",
    "confirm",
    "confirmaction",
}
TOOL_CONFIRM_WORDS = {
    "cancel_subscription": {"确认取消", "确认停止", "确认停止扣款"},
    "report_card_lost": {"确认挂失"},
}
def _normalized_confirmation(text):
    return re.sub(r"[\s。．.!！?？,，;；:：]+", "", text).casefold()


def _is_explicit_confirmation(text, pending_action=None):
    normalized = _normalized_confirmation(text)
    if normalized in CONFIRM_WORDS:
        return True
    if pending_action is None:
        return any(
            normalized in words for words in TOOL_CONFIRM_WORDS.values()
        )
    return normalized in TOOL_CONFIRM_WORDS.get(pending_action.tool_name, set())


def _looks_like_transfer_confirmation(text):
    return re.fullmatch(r"确认转账\d{6}", _normalized_confirmation(text)) is not None


def _looks_like_confirmation_attempt(text):
    normalized = _normalized_confirmation(text)
    return (
        _is_explicit_confirmation(text)
        or normalized.startswith("确认")
        or normalized.startswith("confirm")
    )


