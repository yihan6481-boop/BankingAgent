"""Phone-addressed mailboxes shared by the mock sender and demo receiver."""
import hashlib
import hmac
import re


def normalize_phone(value):
    if not isinstance(value, str):
        raise ValueError('invalid_phone')
    phone = value.strip()
    if not re.fullmatch(r'1[3-9][0-9]{9}', phone):
        raise ValueError('invalid_phone')
    return phone


def phone_inbox(phone, spool_key):
    """Keep phone numbers out of spool filenames and browser session records."""
    phone = normalize_phone(phone)
    digest = hmac.new(spool_key, ('mock-sms-phone:' + phone).encode(), hashlib.sha256).hexdigest()
    return 'phone-' + digest
