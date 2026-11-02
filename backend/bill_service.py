"""从 SQLite 查询交易流水并完成账单统计（V0.7.0：按归属范围隔离）。"""

import sqlite3
from datetime import datetime

from database import get_connection
from storage.financial_reads import financial_read
from utils import cents_to_yuan, make_result

from security.schemas import Principal
from security.write_context import owner_scope


def _is_valid_month(month):
    if not isinstance(month, str) or len(month) != 7:
        return False
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
    except ValueError:
        return False
    return parsed_month.strftime("%Y-%m") == month


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


def _validate_query(connection, month, card_id, scope):
    if not _is_valid_month(month):
        return make_result(False, "月份格式错误，请使用 YYYY-MM 格式")
    if not _card_exists(connection, card_id, scope):
        return make_result(False, "指定的银行卡不存在")
    return None


def _transaction_rows(connection, month, card_id=None, scope=0):
    pattern = f"{month}-%"
    if card_id is None:
        return connection.execute(
            """
            SELECT t.*, c.card_number
            FROM visible_transactions AS t
            JOIN cards AS c ON c.card_id = t.card_id
            WHERE t.date LIKE ? AND t.owner_id = ? AND c.owner_id=t.owner_id
            ORDER BY t.date, t.event_at NULLS LAST, t.transaction_id
            """,
            (pattern, scope),
        ).fetchall()
    return connection.execute(
        """
        SELECT t.*, c.card_number
        FROM visible_transactions AS t
        JOIN cards AS c ON c.card_id = t.card_id
        WHERE t.date LIKE ? AND t.card_id = ? AND t.owner_id = ? AND c.owner_id=t.owner_id
        ORDER BY t.date, t.event_at NULLS LAST, t.transaction_id
        """,
        (pattern, card_id, scope),
    ).fetchall()


def _safe_transaction(row):
    return {
        "transaction_id": row["transaction_id"],
        "card_id": row["card_id"],
        "card_last_four": row["card_number"][-4:],
        "date": row["date"],
        "merchant": row["merchant"],
        "category": row["category"],
        "amount": cents_to_yuan(row["amount_cents"]),
        "direction": row["direction"],
        "event_at": row.get('event_at').isoformat(timespec='seconds') if row.get('event_at') else None,
        "time_source": row.get('time_source') or 'date_only',
        "transaction_type": row.get('transaction_type','unknown'),
        "status": 'success',
    }


@financial_read
def get_transactions_by_month(month, card_id=None, principal: Principal | None = None):
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        error = _validate_query(connection, month, card_id, scope)
        if error:
            return error
        matched = [
            _safe_transaction(row)
            for row in _transaction_rows(connection, month, card_id, scope)
        ]
        if not matched:
            return make_result(True, f"{month} 该期间已记录数据中未发现相关流水；数据覆盖未知", [])
        return make_result(True, f"{month} 共查询到 {len(matched)} 条流水", matched)
    except sqlite3.Error:
        return make_result(False, "交易流水查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def _expense_summary(connection, month, card_id=None, scope=0):
    pattern = f"{month}-%"
    if card_id is None:
        return connection.execute(
            """
            SELECT COUNT(*) AS expense_count,
                   COALESCE(SUM(amount_cents), 0) AS total_cents
            FROM visible_transactions
            WHERE date LIKE ? AND direction = ? AND owner_id = ?
              AND EXISTS (SELECT 1 FROM cards c WHERE c.card_id=visible_transactions.card_id AND c.owner_id=visible_transactions.owner_id)
            """,
            (pattern, "expense", scope),
        ).fetchone()
    return connection.execute(
        """
        SELECT COUNT(*) AS expense_count,
               COALESCE(SUM(amount_cents), 0) AS total_cents
        FROM visible_transactions
        WHERE date LIKE ? AND direction = ? AND card_id = ? AND owner_id = ?
          AND EXISTS (SELECT 1 FROM cards c WHERE c.card_id=visible_transactions.card_id AND c.owner_id=visible_transactions.owner_id)
        """,
        (pattern, "expense", card_id, scope),
    ).fetchone()


@financial_read
def get_monthly_spending(month, card_id=None, principal: Principal | None = None):
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        error = _validate_query(connection, month, card_id, scope)
        if error:
            return error
        summary = _expense_summary(connection, month, card_id, scope)
        total = cents_to_yuan(summary["total_cents"])
        message = (
            "月度支出统计完成"
            if summary["expense_count"]
            else f"{month} 该期间已记录数据中未发现相关流水；记录支出汇总为零，数据覆盖未知"
        )
        return make_result(
            True,
            message,
            {
                "month": month,
                "transaction_count": summary["expense_count"],
                "total_spending": total,
                "card_specified": card_id is not None,
                "card_id": card_id,
            },
        )
    except sqlite3.Error:
        return make_result(False, "月度支出统计失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


@financial_read
def get_category_spending(month, card_id=None, principal: Principal | None = None):
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        error = _validate_query(connection, month, card_id, scope)
        if error:
            return error
        pattern = f"{month}-%"
        if card_id is None:
            rows = connection.execute(
                """
                SELECT category, SUM(amount_cents) AS total_cents
                FROM visible_transactions
                WHERE date LIKE ? AND direction = ? AND owner_id = ?
                GROUP BY category
                ORDER BY total_cents DESC, category
                """,
                (pattern, "expense", scope),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT category, SUM(amount_cents) AS total_cents
                FROM visible_transactions
                WHERE date LIKE ? AND direction = ? AND card_id = ?
                  AND owner_id = ?
                GROUP BY category
                ORDER BY total_cents DESC, category
                """,
                (pattern, "expense", card_id, scope),
            ).fetchall()
        total_cents = sum(row["total_cents"] for row in rows)
        categories = {}
        for row in rows:
            percentage = 0.0
            if total_cents:
                percentage = round(row["total_cents"] / total_cents * 100, 2)
            categories[row["category"]] = {
                "amount": cents_to_yuan(row["total_cents"]),
                "percentage": percentage,
            }
        message = "消费分类统计完成" if categories else f"{month} 该期间已记录数据中未发现相关流水；分类记录汇总为零，数据覆盖未知"
        return make_result(
            True,
            message,
            {
                "month": month,
                "card_id": card_id,
                "total_spending": cents_to_yuan(total_cents),
                "categories": categories,
            },
        )
    except sqlite3.Error:
        return make_result(False, "消费分类统计失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


@financial_read
def compare_monthly_spending(
    base_month, target_month, card_id=None, principal: Principal | None = None
):
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        error = _validate_query(connection, base_month, card_id, scope)
        if error:
            return error
        error = _validate_query(connection, target_month, card_id, scope)
        if error:
            return error
        base_cents = _expense_summary(
            connection, base_month, card_id, scope
        )["total_cents"]
        target_cents = _expense_summary(
            connection, target_month, card_id, scope
        )["total_cents"]
        change_cents = target_cents - base_cents
        if change_cents > 0:
            direction = "增加"
        elif change_cents < 0:
            direction = "减少"
        else:
            direction = "持平"
        if base_cents == 0:
            change_percentage = None
            message = "月份消费对比完成；基准月支出为0，无法计算环比增长率"
        else:
            change_percentage = round(change_cents / base_cents * 100, 2)
            message = "月份消费对比完成"
        return make_result(
            True,
            message,
            {
                "base_month": base_month,
                "target_month": target_month,
                "base_spending": cents_to_yuan(base_cents),
                "target_spending": cents_to_yuan(target_cents),
                "amount_change": cents_to_yuan(change_cents),
                "change_direction": direction,
                "change_percentage": change_percentage,
                "card_id": card_id,
            },
        )
    except sqlite3.Error:
        return make_result(False, "月份消费对比失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()
