"""从 SQLite 查询订阅并管理商户自动扣款授权（V0.7.0：归属 + 授权上下文）。"""

import json
import sqlite3
from datetime import datetime, timezone

from database import get_connection
from storage.errors import CommitUnknown
from utils import cents_to_yuan, make_result

from security.args import cancel_subscription_args
from security.audit import AuditFailure
from security.schemas import Principal
from security.write_context import (
    assert_write_authorization,
    authorization_failure,
    mark_verified,
    owner_scope,
    write_audit,
)


def _card_exists(connection, card_id, scope):
    if card_id is None:
        return True
    return (
        connection.execute(
            "SELECT 1 FROM cards WHERE card_id = ? AND owner_id = ?",
            (card_id, scope),
        ).fetchone()
        is not None
    )


def _get_subscription(connection, subscription_id, scope):
    return connection.execute(
        """
        SELECT s.*, c.card_number
        FROM subscriptions AS s
        JOIN cards AS c ON c.card_id = s.card_id
        WHERE s.subscription_id = ? AND s.owner_id = ?
        """,
        (subscription_id, scope),
    ).fetchone()


def _safe_subscription(row):
    return {
        "subscription_id": row["subscription_id"],
        "name": row["name"],
        "amount": cents_to_yuan(row["amount_cents"]),
        "cycle": row["cycle"],
        "next_charge_date": row["next_charge_date"],
        "card_id": row["card_id"],
        "card_last_four": row["card_number"][-4:],
        "status": "有效" if row["status"] == "active" else "已停止",
    }


def _safe_audit_subscription(row):
    return {
        "subscription_id": row["subscription_id"],
        "name": row["name"],
        "amount": cents_to_yuan(row["amount_cents"]),
        "card_id": row["card_id"],
        "card_last_four": row["card_number"][-4:],
        "status": row["status"],
    }


def _active_rows(connection, card_id=None, scope=0):
    if card_id is None:
        return connection.execute(
            """
            SELECT s.*, c.card_number
            FROM subscriptions AS s
            JOIN cards AS c ON c.card_id = s.card_id
            WHERE s.status = ? AND s.owner_id = ?
            ORDER BY s.subscription_id
            """,
            ("active", scope),
        ).fetchall()
    return connection.execute(
        """
        SELECT s.*, c.card_number
        FROM subscriptions AS s
        JOIN cards AS c ON c.card_id = s.card_id
        WHERE s.status = ? AND s.card_id = ? AND s.owner_id = ?
        ORDER BY s.subscription_id
        """,
        ("active", card_id, scope),
    ).fetchall()


def get_active_subscriptions(card_id=None, principal: Principal | None = None):
    connection = None
    try:
        connection = get_connection()
        scope = owner_scope(principal)
        if not _card_exists(connection, card_id, scope):
            return make_result(False, "指定的银行卡不存在")
        items = [
            _safe_subscription(row)
            for row in _active_rows(connection, card_id, scope)
        ]
        if not items:
            return make_result(True, "当前没有有效订阅", [])
        return make_result(True, f"共查询到 {len(items)} 项有效订阅", items)
    except sqlite3.Error:
        return make_result(False, "有效订阅查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def get_monthly_subscription_cost(card_id=None, principal: Principal | None = None):
    connection = None
    try:
        connection = get_connection()
        scope = owner_scope(principal)
        if not _card_exists(connection, card_id, scope):
            return make_result(False, "指定的银行卡不存在")
        rows = _active_rows(connection, card_id, scope)
        # Accumulate the exact annual amount first; never multiply a rounded
        # monthly average back into an annual total. Half a cent rounds up.
        known = [row for row in rows if row['cycle'] in {'monthly', 'yearly'}]
        annual_cents = sum(int(row['amount_cents']) *
                           (12 if row['cycle'] == 'monthly' else 1) for row in known)
        monthly_cents = (annual_cents + 6) // 12
        complete = len(known) == len(rows)
        return make_result(
            True,
            ("月均及年度折算成本统计完成（非本月实际扣款）" if complete else
             "存在未知订阅周期，仅统计已知周期，无法完整统计") if rows else "当前没有有效订阅费用",
            {
                "active_count": len(rows),
                "monthly_cost": cents_to_yuan(monthly_cents),
                "annual_cost": cents_to_yuan(annual_cents),
                "annual_cost_cents": annual_cents,
                "cost_basis": "annualized_active_subscriptions",
                "monthly_rounding": "half_up_to_cent",
                "complete": complete,
                "unknown_cycle_count": len(rows) - len(known),
                "subscriptions": [_safe_subscription(row) for row in rows],
            },
        )
    except sqlite3.Error:
        return make_result(False, "订阅费用统计失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def cancel_subscription(subscription_id, principal: Principal | None = None, *,
                        security_context=None, confirmed=None):
    """预览或在事务内停止后续自动扣款授权并记录脱敏审计日志。"""
    connection = None
    try:
        connection = get_connection()
        scope = owner_scope(principal)
        subscription = _get_subscription(connection, subscription_id, scope)
        if subscription is None:
            return make_result(False, "订阅不存在")
        if subscription["status"] == "cancelled":
            return make_result(
                False, "该订阅的自动扣款授权已经停止，不能重复操作"
            )

        preview = _safe_subscription(subscription)
        preview["requires_confirmation"] = True
        if security_context is None:
            if confirmed is True:
                return authorization_failure()
            return make_result(
                True,
                "自动扣款授权尚未停止，请确认后继续；此操作不等于取消商户会员",
                preview,
            )

        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        from storage.config import pg02_enabled
        if pg02_enabled(): connection.financial = True
        now = datetime.now(timezone.utc)
        if pg02_enabled():
            from security.service import get_security_services
            now=get_security_services().identity._now()
        try:
            assert_write_authorization(
                connection,
                security_context,
                "cancel_subscription",
                cancel_subscription_args(subscription_id),
                now=now,
            )
        except Exception as error:
            connection.rollback()
            return authorization_failure(
                getattr(error, "message", None), getattr(error, "code", None)
            )

        subscription = _get_subscription(connection, subscription_id, scope)
        if subscription is None:
            connection.rollback()
            return make_result(False, "订阅状态已变化，未执行任何操作")
        if subscription["status"] == "cancelled":
            # Another request won after this preview. This authorization has
            # no atomic receipt, so it must not report a completed write.
            connection.rollback()
            result=make_result(False,"订阅状态已变化，本次未执行新的取消；请核对原任务回执")
            result['error_code']='subscription_state_changed'
            return result
        write_audit(security_context, connection, "revalidation", "ok",
                    detail={"revalidation": "subscription_state_checked"})

        before_data = _safe_audit_subscription(subscription)
        connection.execute(
            "UPDATE subscriptions SET status = ? WHERE subscription_id = ? "
            "AND status = 'active'",
            ("cancelled", subscription_id),
        )
        updated = _get_subscription(connection, subscription_id, scope)
        after_data = _safe_audit_subscription(updated)
        message = "已停止该商户的后续自动扣款授权；如需取消会员，请联系商户"
        connection.execute(
            """
            INSERT INTO operation_logs (
                operation_type, target_type, target_id, target_display,
                before_json, after_json, result, message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cancel_subscription",
                "subscription",
                updated["subscription_id"],
                f"{updated['name']}（银行卡尾号 {updated['card_number'][-4:]}）",
                json.dumps(before_data, ensure_ascii=False),
                json.dumps(after_data, ensure_ascii=False),
                "success",
                message,
                now.isoformat(timespec="seconds"),
            ),
        )
        if updated is None or updated["status"] != "cancelled":
            connection.rollback()
            return make_result(False, "订阅状态核验失败，数据未发生变化")
        data = mark_verified(_safe_subscription(updated))
        write_audit(security_context, connection, "execution", "ok")
        write_audit(security_context, connection, "verification", "ok",
                    detail={"write_verification": "passed"})
        from lifecycle.service import record_commit
        record_commit(connection, security_context, make_result(True, message, data))
        connection.commit()
        return make_result(True, message, data)
    except CommitUnknown:
        connection.close()
        connection=None
        from storage.application import recover_mutation
        return recover_mutation(security_context)
    except AuditFailure as error:
        if connection is not None:
            connection.rollback()
        result = make_result(False, error.message)
        result["error_code"] = error.code
        return result
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
        return make_result(False, "停止自动扣款授权失败，数据未发生变化")
    finally:
        if connection is not None:
            connection.close()
