"""V0.7.0 转账业务内核：预览、统一权限策略、幂等、事务与脱敏审计。

与 V0.6.4 的差异（旧业务行为见注释）：

* 旧的分档策略（``low``/``medium``/``high`` 且高金额直接拒绝）已替换为统一权限
  政策：``low`` 对应 yellow（预览+确认），``medium``/``high`` 对应 red
  （预览+确认+模拟强验证），由后端基于 Shanghai 业务日累计额计算；
* 累计额按**用户所有归属卡片**计算，不再按单张卡分别计算；银行卡原有每日
  限额仍独立检查；
* 预览阶段只生成待办，不写入任何业务表，也不占用累计额；
* 执行阶段必须携带可信 ``security_context``：授权消费、累计额重算、余额校验、
  资金写入、业务单号与脱敏审计在同一 ``BEGIN IMMEDIATE`` 事务内提交；
* 旧 ``confirmed`` 布尔值不再被接受为授权证据。
"""

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, Overflow as DecimalOverflow

from database import get_connection
from utils import cents_to_yuan, make_result, mask_card_number

from security.args import transfer_args
from security.audit import AuditFailure
from security.policy import business_date
from security.schemas import Principal
from security.write_context import (
    PermissionEscalated,
    assert_permission_not_escalated,
    assert_write_authorization,
    authorization_failure,
    mark_verified,
    owner_scope,
    write_audit,
)

# 旧业务风险字段仍然存在，但只作为说明性/兼容字段暴露给 V1 UI。
LOW_RISK_MAX_CENTS = 100_000
MEDIUM_RISK_MAX_CENTS = 300_000
MAX_AMOUNT_TEXT_LENGTH = 64
MAX_AMOUNT_SIGNIFICANT_DIGITS = 18
MAX_AMOUNT_EXPONENT = 7
MAX_SAFE_AMOUNT_YUAN = Decimal("10000000")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
TRANSFER_ID_PATTERN = re.compile(
    r"(?:tr|transfer)_[A-Za-z0-9][A-Za-z0-9._:-]{0,118}\Z"
)


def _mask_account_number(account_number):
    return mask_card_number(account_number)


def _failure(error_code, message, data=None):
    result = make_result(False, message, data)
    result["error_code"] = error_code
    return result


def _safe_recipient(recipient):
    return {
        "recipient_id": recipient["recipient_id"],
        "name": recipient["name"],
        "bank_name": recipient["bank_name"],
        "masked_account": _mask_account_number(recipient["account_number"]),
        "trusted": bool(recipient["trusted"]),
    }


def _strict_amount_to_cents(amount):
    if isinstance(amount, bool):
        return None
    try:
        amount_text = str(amount).strip()
    except Exception:
        return None
    if not amount_text or len(amount_text) > MAX_AMOUNT_TEXT_LENGTH:
        return None
    try:
        decimal_amount = amount if isinstance(amount, Decimal) else Decimal(amount_text)
    except (InvalidOperation, DecimalOverflow, OverflowError, TypeError, ValueError):
        return None
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        return None
    decimal_parts = decimal_amount.as_tuple()
    if (
        len(decimal_parts.digits) > MAX_AMOUNT_SIGNIFICANT_DIGITS
        or decimal_parts.exponent < -2
        or decimal_parts.exponent > MAX_AMOUNT_EXPONENT
        or decimal_amount > MAX_SAFE_AMOUNT_YUAN
    ):
        return None
    try:
        cents_value = decimal_amount * Decimal("100")
        if cents_value != cents_value.to_integral_value():
            return None
        return int(cents_value)
    except (InvalidOperation, DecimalOverflow, OverflowError, TypeError, ValueError):
        return None


def _normalize_remark(remark):
    if remark is None:
        return None, None
    if not isinstance(remark, str):
        return None, "转账备注必须是文本"
    normalized = remark.strip()
    if not normalized:
        return None, None
    if len(normalized) > 100:
        return None, "转账备注不能超过100个字符"
    return normalized, None


def _normalize_now(now_provider):
    """Return ``(iso_timestamp, business_date)``.

    ``business_date`` is always the Asia/Shanghai banking day; it is never a
    slice of the UTC timestamp (00:00-07:59 Shanghai belongs to the previous
    UTC day).
    """
    from storage.config import pg02_enabled as enabled
    if enabled() and now_provider is None:
        from security.service import get_security_services
        now_provider=get_security_services().now_provider
    try:
        value = now_provider() if now_provider is not None else datetime.now(timezone.utc)
    except Exception:
        return None, None
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None or value.utcoffset() is None:
                return None, None
        except Exception:
            return None, None
        current = value
    elif isinstance(value, date):
        current = datetime.combine(value, time.min, tzinfo=timezone.utc)
    else:
        return None, None
    current = current.replace(microsecond=0)
    return current.isoformat(), business_date(current)


def _now_datetime(now_provider):
    from storage.config import pg02_enabled as enabled
    if enabled() and now_provider is None:
        from security.service import get_security_services
        now_provider=get_security_services().now_provider
    try:
        value = now_provider() if now_provider is not None else datetime.now(timezone.utc)
    except Exception:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=timezone.utc)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.replace(microsecond=0)


def _risk_level(amount_cents):
    """旧业务风险说明字段；不再决定是否允许执行。"""
    if amount_cents <= LOW_RISK_MAX_CENTS:
        return "low"
    if amount_cents <= MEDIUM_RISK_MAX_CENTS:
        return "medium"
    return "high"


def _user_daily_transfer_total(connection, owner_id, day):
    """用户所有归属卡片的已完成转账合计（不按单卡分别计算）。"""
    row = connection.execute(
        """
        SELECT COALESCE(SUM(t.amount_cents), 0) AS total
        FROM transfer_orders AS t
        JOIN cards AS c ON c.card_id = t.from_card_id
        WHERE c.owner_id = ?
          AND t.status = 'completed'
          AND COALESCE(t.business_date, substr(t.completed_at, 1, 10)) = ?
        """,
        (owner_id, day),
    ).fetchone()
    return int(row["total"])


def _card_daily_transfer_total(connection, from_card_id, day):
    row = connection.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS total
        FROM transfer_orders
        WHERE from_card_id = ?
          AND status = 'completed'
          AND COALESCE(business_date, substr(completed_at, 1, 10)) = ?
        """,
        (from_card_id, day),
    ).fetchone()
    return int(row["total"])


def _transfer_state_error(card, recipient, amount_cents, card_daily_total_cents):
    """业务拒绝（无法通过强验证绕过）与旧银行卡限额的独立检查。"""
    if card is None:
        return "card_not_found", "银行卡不存在"
    if card["status"] != "normal":
        return "card_unavailable", "当前银行卡状态不允许转账"
    if recipient is None:
        return "recipient_not_found", "收款人不存在"
    if recipient["status"] != "active":
        return "recipient_disabled", "当前收款人已停用，不能转账"
    from security.payment_auth import simulated_external
    if recipient["trusted"] != 1 and not simulated_external(recipient['recipient_id']):
        return "recipient_untrusted", "当前收款人未被标记为可信，不能转账"
    if card["balance_cents"] < amount_cents:
        return "insufficient_balance", "银行卡余额不足，不能转账"
    if 'per_tx_limit_cents' in card.keys() and amount_cents > card['per_tx_limit_cents']:
        return 'per_transaction_limit_exceeded', '本次转账超过银行卡单笔限额'
    # 银行卡原有每日限额独立检查：无论权限等级如何都必须满足。
    if card_daily_total_cents + amount_cents > card["daily_limit_cents"]:
        return (
            "daily_limit_exceeded",
            "本次转账将超过银行卡当日限额，不能转账",
        )
    return None, None


def _preview_data(card, recipient, amount_cents, remark, risk_level, idempotency_key=None):
    data = {
        "from_card_id": card["card_id"],
        "from_bank_name": card["bank_name"],
        "from_masked_card_number": mask_card_number(card["card_number"]),
        "recipient_id": recipient["recipient_id"],
        "recipient_name": recipient["name"],
        "recipient_bank_name": recipient["bank_name"],
        "recipient_masked_account": _mask_account_number(
            recipient["account_number"]
        ),
        "amount": cents_to_yuan(amount_cents),
        "amount_cents": amount_cents,
        "remark": remark,
        "risk_level": risk_level,
        "requires_confirmation": True,
    }
    if idempotency_key is not None:
        # 内部执行字段：由后端生成，供统一安全入口绑定授权，不下发给 LLM。
        data["_idempotency_key"] = idempotency_key
    return data


def new_idempotency_key():
    return f"transfer_{uuid.uuid4().hex}"


def _completed_transfer_data(row, idempotent_replay):
    return {
        "transfer_id": row["transfer_id"],
        "from_card_id": row["from_card_id"],
        "from_masked_card_number": mask_card_number(row["card_number"]),
        "recipient_id": row["recipient_id"],
        "recipient_name": row["recipient_name"],
        "recipient_masked_account": _mask_account_number(
            row["account_number"]
        ),
        "amount": cents_to_yuan(row["amount_cents"]),
        "remark": row["remark"],
        "risk_level": row["risk_level"],
        "balance_before": cents_to_yuan(row["balance_before_cents"]),
        "balance_after": cents_to_yuan(row["balance_after_cents"]),
        "completed_at": row["completed_at"],
        "idempotent_replay": idempotent_replay,
    }


def _get_completed_transfer(connection, idempotency_key):
    return connection.execute(
        """
        SELECT transfer_orders.*, cards.card_number,
               recipients.name AS recipient_name,
               recipients.account_number
        FROM transfer_orders
        JOIN cards ON cards.card_id = transfer_orders.from_card_id
        JOIN recipients ON recipients.recipient_id = transfer_orders.recipient_id
        WHERE transfer_orders.idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()


def get_transfer_recipients(principal: Principal | None = None):
    """只返回当前身份归属范围内有效收款人的脱敏信息。"""
    connection = None
    try:
        connection = get_connection()
        rows = connection.execute(
            """
            SELECT * FROM recipients
            WHERE status = 'active' AND owner_id = ?
            ORDER BY recipient_id
            """,
            (owner_scope(principal),),
        ).fetchall()
        recipients = [_safe_recipient(row) for row in rows]
        return make_result(True, f"共查询到 {len(recipients)} 位有效收款人", recipients)
    except sqlite3.Error:
        return make_result(False, "收款人查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def prepare_transfer(
    from_card_id,
    recipient_name,
    amount,
    remark=None,
    now_provider=None,
    recipient_id=None,
    principal: Principal | None = None,
    idempotency_key=None,
):
    """只读校验并生成转账预览，不创建订单、不修改任何银行数据。"""
    if isinstance(from_card_id, bool) or not isinstance(from_card_id, int):
        return _failure("invalid_card_id", "银行卡编号必须是整数")
    if recipient_name is not None and (
        not isinstance(recipient_name, str) or not recipient_name.strip()
    ):
        return _failure("invalid_recipient_name", "收款人姓名必须是非空文本")
    if recipient_id is not None and (
        isinstance(recipient_id, bool)
        or not isinstance(recipient_id, int)
        or recipient_id <= 0
    ):
        return _failure("invalid_recipient_id", "收款人编号必须是正整数")
    if recipient_name is None and recipient_id is None:
        return _failure(
            "recipient_required", "收款人姓名和收款人编号至少需要提供一个"
        )
    normalized_name = recipient_name.strip() if recipient_name is not None else None
    amount_cents = _strict_amount_to_cents(amount)
    if amount_cents is None:
        return _failure(
            "invalid_amount", "转账金额必须是大于0且最多两位小数的有限数字"
        )
    normalized_remark, remark_error = _normalize_remark(remark)
    if remark_error:
        return _failure("invalid_remark", remark_error)
    _timestamp, day = _normalize_now(now_provider)
    if day is None:
        return _failure("invalid_time", "转账时间无效，不能生成预览")

    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (from_card_id, scope),
        ).fetchone()
        if card is None:
            return _failure("card_not_found", "银行卡不存在")
        if card["status"] != "normal":
            return _failure("card_unavailable", "当前银行卡状态不允许转账")

        if recipient_id is not None:
            recipient = connection.execute(
                "SELECT * FROM recipients WHERE recipient_id = ? AND owner_id = ?",
                (recipient_id, scope),
            ).fetchone()
            if recipient is None:
                return _failure("recipient_not_found", "收款人不存在")
            if normalized_name is not None and recipient["name"] != normalized_name:
                return _failure(
                    "recipient_mismatch",
                    "收款人编号与指定姓名不一致，拒绝生成转账预览",
                )
        else:
            recipients = connection.execute(
                "SELECT * FROM recipients WHERE name = ? AND owner_id = ? "
                "ORDER BY recipient_id",
                (normalized_name, scope),
            ).fetchall()
            if not recipients:
                return _failure(
                    "recipient_not_found", "未找到该姓名的预登记收款人"
                )
            if len(recipients) > 1:
                return _failure(
                    "recipient_ambiguous",
                    "存在多位同名收款人，请根据脱敏信息明确选择收款人编号",
                    {"candidates": [_safe_recipient(row) for row in recipients]},
                )
            recipient = recipients[0]
        card_daily_total = _card_daily_transfer_total(connection, from_card_id, day)
        error_code, state_error = _transfer_state_error(
            card, recipient, amount_cents, card_daily_total
        )
        if state_error:
            return _failure(error_code, state_error)
        preview = _preview_data(
            card,
            recipient,
            amount_cents,
            normalized_remark,
            _risk_level(amount_cents),
            idempotency_key,
        )
        return make_result(True, "转账尚未执行，请确认后继续", preview)
    except sqlite3.Error:
        return _failure("transfer_preview_error", "转账预览失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def execute_transfer(
    from_card_id,
    recipient_id,
    amount_cents,
    idempotency_key,
    remark=None,
    principal: Principal | None = None,
    security_context=None,
    confirmed=None,
    now_provider=None,
    id_provider=None,
):
    """确认后在单个立即事务中完成授权消费、扣款、订单、流水和脱敏审计。"""
    if (
        isinstance(from_card_id, bool)
        or not isinstance(from_card_id, int)
        or isinstance(recipient_id, bool)
        or not isinstance(recipient_id, int)
    ):
        return make_result(False, "银行卡编号和收款人编号必须是整数")
    if (
        isinstance(amount_cents, bool)
        or not isinstance(amount_cents, int)
        or amount_cents <= 0
    ):
        return make_result(False, "转账金额必须是正整数分")
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(
        idempotency_key
    ):
        return make_result(False, "转账幂等键格式无效")
    normalized_remark, remark_error = _normalize_remark(remark)
    if remark_error:
        return make_result(False, remark_error)
    timestamp, day = _normalize_now(now_provider)
    if timestamp is None:
        return make_result(False, "转账时间无效，不能执行")
    now_dt = _now_datetime(now_provider)
    if now_dt is None:
        return make_result(False, "转账时间无效，不能执行")
    if security_context is None:
        # 旧 confirmed 布尔不再是授权证据
        return authorization_failure()

    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        connection.execute("BEGIN IMMEDIATE")

        if hasattr(connection,'lock_transfer'):
            connection.assert_identity(security_context,now_dt)
            connection.lock_transfer(from_card_id,recipient_id)
            # Legal retry uses the same authenticated owner and exact parameters,
            # before attempting to consume an already-spent authorization.
            prior=_get_completed_transfer(connection,idempotency_key)
            if prior is not None:
                connection.rollback()
                if (prior['from_card_id'],prior['recipient_id'],prior['amount_cents'],prior['remark']) != (
                        from_card_id,recipient_id,amount_cents,normalized_remark):
                    return _failure('idempotency_conflict','请求键已绑定其他参数')
                from storage.pg02 import recover_transfer
                connection.close()
                connection=None
                return recover_transfer(idempotency_key,from_card_id,recipient_id,amount_cents,normalized_remark,replay=True)

        # 1) 授权消费：绑定身份、Session、操作参数摘要，一次性。
        try:
            assert_write_authorization(
                connection,
                security_context,
                "transfer_money",
                transfer_args(
                    from_card_id, recipient_id, amount_cents, idempotency_key,
                    normalized_remark,
                ),
                now=now_dt,
            )
        except Exception as error:
            connection.rollback()
            return authorization_failure(
                getattr(error, "message", None), getattr(error, "code", None)
            )
        write_audit(security_context, connection, "revalidation", "ok",
                    detail={"revalidation": "authorization_consumed"})
        if hasattr(connection,'lock_transfer'):
            from storage.pg02 import fault
            fault('authorization_consumed')

        # 2) 幂等：可信身份 + 参数一致时返回既有结果，不重复扣款。
        existing = _get_completed_transfer(connection, idempotency_key)
        if existing is not None:
            same_request = (
                existing["from_card_id"] == from_card_id
                and existing["recipient_id"] == recipient_id
                and existing["amount_cents"] == amount_cents
                and existing["remark"] == normalized_remark
                and int(existing["owner_id"]) == scope
            )
            if not same_request:
                connection.rollback()
                return _failure(
                    "idempotency_conflict",
                    "同一幂等键对应的转账内容不一致，拒绝执行。",
                )
            replay = mark_verified(_completed_transfer_data(existing, True))
            connection.commit()
            return make_result(True, "转账已完成，本次返回原转账结果", replay)

        # 3) 事务内重新校验业务状态与本卡当日限额。
        # 归属校验必须先于授权/权限校验，否则跨用户资源会被报成权限异常而泄露存在性。
        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (from_card_id, scope),
        ).fetchone()
        recipient = connection.execute(
            "SELECT * FROM recipients WHERE recipient_id = ? AND owner_id = ?",
            (recipient_id, scope),
        ).fetchone()
        if card is None:
            connection.rollback()
            return _failure("card_not_found", "银行卡不存在")
        if recipient is None:
            connection.rollback()
            return _failure("recipient_not_found", "收款人不存在")
        card_daily_total = _card_daily_transfer_total(connection, from_card_id, day)
        error_code, state_error = _transfer_state_error(
            card, recipient, amount_cents, card_daily_total
        )
        if state_error:
            connection.rollback()
            return _failure(error_code, state_error)

        # 事务内重新计算累计额与所需授权等级：等待期间由 yellow 升为 red 时
        # 停止执行，要求重新预览、确认并完成强验证（不得降级执行）。
        try:
            assert_permission_not_escalated(
                security_context,
                "transfer_money",
                transfer_args(
                    from_card_id, recipient_id, amount_cents, idempotency_key,
                    normalized_remark,
                ),
                getattr(security_context, "policy_engine", None),
            )
        except PermissionEscalated as error:
            connection.rollback()
            return _failure(
                "permission_escalated",
                error.message,
                {"detail_code": "permission_escalated_to_strong_auth"},
            )

        raw_transfer_id = (
            id_provider() if id_provider is not None else f"tr_{uuid.uuid4().hex}"
        )
        transfer_id = str(raw_transfer_id).strip()
        if not TRANSFER_ID_PATTERN.fullmatch(transfer_id):
            raise ValueError("invalid generated transfer id")

        balance_before_cents = card["balance_cents"]
        balance_after_cents = balance_before_cents - amount_cents
        update = connection.execute(
            """
            UPDATE cards
            SET balance_cents = balance_cents - ?
            WHERE card_id = ?
              AND status = 'normal'
              AND balance_cents >= ?
            """,
            (amount_cents, from_card_id, amount_cents),
        )
        if update.rowcount != 1:
            raise sqlite3.IntegrityError("conditional balance update failed")
        if hasattr(connection,'lock_transfer'):
            from storage.pg02 import fault
            fault('debited')

        connection.execute(
            """
            INSERT INTO transfer_orders (
                transfer_id, idempotency_key, from_card_id, recipient_id,
                amount_cents, remark, risk_level, status,
                balance_before_cents, balance_after_cents,
                created_at, completed_at,
                owner_id, business_date, principal_id, session_id, authorization_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transfer_id,
                idempotency_key,
                from_card_id,
                recipient_id,
                amount_cents,
                normalized_remark,
                _risk_level(amount_cents),
                balance_before_cents,
                balance_after_cents,
                timestamp,
                timestamp,
                scope,
                day,
                getattr(getattr(security_context, "principal", None), "principal_id", None),
                getattr(security_context, "session_id", None),
                getattr(getattr(security_context, "grant", None), "authorization_id", None),
            ),
        )
        if hasattr(connection,'lock_transfer'):
            from storage.pg02 import fault
            fault('before_ledger')
        connection.execute(
            """
            INSERT INTO transactions (
                card_id, date, merchant, category, amount_cents, direction,
                owner_id, transaction_type, classification_source, source_order_id
            ) VALUES (?, ?, ?, '转账', ?, 'expense', ?, 'transfer', 'transfer_service', ?)
            """,
            (from_card_id, day, recipient["name"], amount_cents, scope, transfer_id),
        )

        masked_card = mask_card_number(card["card_number"])
        masked_account = _mask_account_number(recipient["account_number"])
        audit_common = {
            "transfer_id": transfer_id,
            "from_card_id": from_card_id,
            "from_masked_card_number": masked_card,
            "recipient_id": recipient_id,
            "recipient_name": recipient["name"],
            "recipient_masked_account": masked_account,
            "amount": cents_to_yuan(amount_cents),
            "risk_level": _risk_level(amount_cents),
        }
        before_data = {
            **audit_common,
            "balance_before": cents_to_yuan(balance_before_cents),
        }
        after_data = {
            **audit_common,
            "balance_after": cents_to_yuan(balance_after_cents),
        }
        connection.execute(
            """
            INSERT INTO operation_logs (
                operation_type, target_type, target_id, target_display,
                before_json, after_json, result, message, created_at
            ) VALUES ('transfer_money', 'transfer', NULL, ?, ?, ?,
                      'success', '转账成功', ?)
            """,
            (
                f"{recipient['name']}（{masked_account}）",
                json.dumps(before_data, ensure_ascii=False),
                json.dumps(after_data, ensure_ascii=False),
                timestamp,
            ),
        )

        completed = _get_completed_transfer(connection, idempotency_key)
        latest_balance = connection.execute(
            "SELECT balance_cents FROM cards WHERE card_id = ?", (from_card_id,)
        ).fetchone()
        if (
            completed is None
            or latest_balance is None
            or completed["status"] != "completed"
            or latest_balance["balance_cents"] != balance_after_cents
        ):
            raise sqlite3.IntegrityError("transfer verification failed")
        write_audit(security_context, connection, "execution", "ok",
                    detail={"amount_cents": amount_cents})
        write_audit(security_context, connection, "verification", "ok",
                    detail={"write_verification": "passed"})
        result_data = mark_verified(_completed_transfer_data(completed, False))
        from lifecycle.service import record_commit
        if hasattr(connection,'lock_transfer'):
            from storage.pg02 import fault
            fault('before_receipt')
        record_commit(connection, security_context, make_result(True, "转账成功", result_data))
        if hasattr(connection,'lock_transfer'):
            connection.assert_transfer_integrity(security_context,idempotency_key,amount_cents,
                balance_before_cents,balance_after_cents,make_result(True,"转账成功",result_data))
            connection.financial=True
        connection.commit()
        return make_result(True, "转账成功", result_data)
    except AuditFailure as error:
        if connection is not None:
            connection.rollback()
        return _failure(error.code, error.message)
    except Exception as error:
        if getattr(error,'pg02_commit_unknown',False):
            if connection is not None:
                connection.close()
                connection=None
            from storage.pg02 import recover_transfer
            return recover_transfer(idempotency_key,from_card_id,recipient_id,amount_cents,normalized_remark)
        if hasattr(connection,'lock_transfer'):
            import logging
            logging.getLogger('pg02').error('PG02 transfer rejected: %s',type(error).__name__)
        if connection is not None:
            connection.rollback()
        return make_result(False, "转账失败，资金和业务数据未发生变化")
    finally:
        if connection is not None:
            connection.close()
