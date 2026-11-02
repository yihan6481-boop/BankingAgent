"""V0.7.0 离线模拟理财内核：查询、预览、幂等事务和脱敏审计。

V0.6.4 -> V0.7.0 的变化：

* 持仓和订单按 ``owner_id`` 归属隔离，查询和写入都检查归属；
* 执行阶段必须携带可信 ``security_context``（旧 ``confirmed`` 布尔不再可用）；
* 授权消费、业务状态复核、资金/持仓写入与核验在同一事务内完成。
"""

import json
import re
import sqlite3
import uuid

from database import get_connection
from storage.financial_reads import financial_read
from transfer_service import (
    IDEMPOTENCY_KEY_PATTERN,
    _normalize_now,
    _now_datetime,
    _strict_amount_to_cents,
)
from utils import cents_to_yuan, make_result, mask_card_number

from security.args import wealth_purchase_args, wealth_redemption_args
from security.audit import AuditFailure
from security.schemas import Principal
from security.write_context import (
    assert_write_authorization,
    authorization_failure,
    mark_verified,
    owner_scope,
    write_audit,
)


DEMO_USER_ID = 1
RISK_NAMES = {
    "R1": "保守型",
    "R2": "稳健型",
    "R3": "平衡型",
    "R4": "成长型",
    "R5": "进取型",
}
ORDER_ID_PATTERN = re.compile(
    r"(?:wo|wo_(?:buy|redeem)|wealth_(?:order|buy|redeem|rollback))_"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,111}\Z"
)


def strict_wealth_amount_to_cents(amount):
    """与转账保持一致的 Decimal 语义，不进行静默四舍五入。"""
    return _strict_amount_to_cents(amount)


def _failure(error_code, message, data=None):
    result = make_result(False, message, data)
    result["error_code"] = error_code
    return result


def _rate_percent(bps):
    return cents_to_yuan(bps)


def _safe_product(row):
    return {
        "product_id": row["product_id"],
        "name": row["name"],
        "risk_level": row["risk_level"],
        "min_purchase": cents_to_yuan(row["min_purchase_cents"]),
        "status": row["status"],
        "reference_annual_rate": _rate_percent(
            row["reference_annual_rate_bps"]
        ),
        "duration_days": row["duration_days"],
        "notice": "参考年化收益率不代表实际收益，也不构成保证。",
    }


def _safe_holding(row):
    return {
        "holding_id": row["holding_id"],
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "card_id": row["card_id"],
        "principal": cents_to_yuan(row["principal_cents"]),
        "status": row["status"],
        "purchased_at": row["purchased_at"],
        "notice": "该金额是模拟持仓本金，不是实时市值；当前版本不计算投资收益。",
    }


def _risk_profile_row(connection, scope):
    return connection.execute(
        "SELECT risk_level, updated_at FROM user_risk_profiles WHERE user_id = ?",
        (scope,),
    ).fetchone()


@financial_read
def get_risk_profile(principal: Principal | None = None):
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        row = _risk_profile_row(connection, scope)
        if row is None:
            return _failure("risk_profile_not_found", "未找到模拟用户风险等级")
        return make_result(
            True,
            "模拟用户风险等级查询成功",
            {
                "risk_level": row["risk_level"],
                "risk_name": RISK_NAMES[row["risk_level"]],
                "updated_at": row["updated_at"],
                "notice": "该风险等级仅用于本地模拟理财演示。",
            },
        )
    except sqlite3.Error:
        return _failure("risk_profile_query_error", "风险等级查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


@financial_read
def get_wealth_products(
    risk_level=None, status=None, suitable_only=False,
    principal: Principal | None = None,
):
    """公开模拟产品目录：允许跨用户读取（唯一明确的归属例外）。"""
    if risk_level is not None and risk_level not in RISK_NAMES:
        return _failure("invalid_risk_level", "理财产品风险等级必须为 R1 至 R5")
    if status is not None and status not in {"active", "inactive"}:
        return _failure("invalid_product_status", "理财产品状态必须为 active 或 inactive")
    if not isinstance(suitable_only, bool):
        return _failure("invalid_suitable_filter", "风险适配筛选必须是布尔值")
    connection = None
    try:
        connection = get_connection()
        clauses = []
        parameters = []
        if risk_level is not None:
            clauses.append("risk_level = ?")
            parameters.append(risk_level)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if suitable_only:
            profile = _risk_profile_row(connection, owner_scope(principal))
            if profile is None:
                return _failure("risk_profile_not_found", "未找到模拟用户风险等级")
            clauses.append("CAST(substr(risk_level, 2, 1) AS INTEGER) <= ?")
            parameters.append(int(profile["risk_level"][1]))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM wealth_products{where} ORDER BY product_id",
            parameters,
        ).fetchall()
        return make_result(
            True,
            f"共查询到 {len(rows)} 个模拟理财产品",
            [_safe_product(row) for row in rows],
        )
    except sqlite3.Error:
        return _failure("wealth_product_query_error", "模拟理财产品查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


@financial_read
def get_wealth_holdings(principal: Principal | None = None):
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        rows = connection.execute(
            """
            SELECT h.*, p.name AS product_name
            FROM wealth_holdings h
            JOIN wealth_products p ON p.product_id = h.product_id
            WHERE h.user_id = ?
            ORDER BY h.holding_id
            """,
            (scope,),
        ).fetchall()
        return make_result(
            True,
            f"共查询到 {len(rows)} 笔模拟理财持仓",
            [_safe_holding(row) for row in rows],
        )
    except sqlite3.Error:
        return _failure("wealth_holding_query_error", "模拟理财持仓查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def _purchase_state(connection, product_id, card_id, amount_cents, scope):
    product = connection.execute(
        "SELECT * FROM wealth_products WHERE product_id = ?", (product_id,)
    ).fetchone()
    card = connection.execute(
        "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
        (card_id, scope),
    ).fetchone()
    profile = _risk_profile_row(connection, scope)
    if product is None:
        return None, None, None, "product_not_found", "模拟理财产品不存在"
    if product["status"] != "active":
        return product, card, profile, "product_inactive", "该模拟理财产品已停用"
    if 'category' in product.keys() and product['category']=='insurance' and amount_cents!=product['min_purchase_cents']:
        return product,card,profile,'insurance_premium_invalid','模拟保险采用固定保费，请按产品说明核对金额'
    if profile is None:
        return product, card, None, "risk_profile_not_found", "未找到模拟用户风险等级"
    if int(product["risk_level"][1]) > int(profile["risk_level"][1]):
        return product, card, profile, "risk_mismatch", "产品风险等级高于模拟用户风险承受等级"
    if amount_cents < product["min_purchase_cents"]:
        return product, card, profile, "below_minimum", "购买金额低于产品最低购买金额"
    if card is None:
        return product, None, profile, "card_not_found", "付款银行卡不存在"
    if card["status"] != "normal":
        return product, card, profile, "card_unavailable", "当前银行卡状态不允许购买模拟理财"
    if card["balance_cents"] < amount_cents:
        return product, card, profile, "insufficient_balance", "银行卡余额不足"
    return product, card, profile, None, None


def prepare_wealth_purchase(
    product_id, card_id, amount, now_provider=None,
    principal: Principal | None = None, idempotency_key=None,
):
    if (
        isinstance(product_id, bool)
        or not isinstance(product_id, int)
        or product_id <= 0
        or isinstance(card_id, bool)
        or not isinstance(card_id, int)
        or card_id <= 0
    ):
        return _failure("invalid_identifier", "产品编号和银行卡编号必须是正整数")
    amount_cents = strict_wealth_amount_to_cents(amount)
    if amount_cents is None:
        return _failure("invalid_amount", "购买金额必须是大于0且最多两位小数的有限数字")
    timestamp, _business_date = _normalize_now(now_provider)
    if timestamp is None:
        return _failure("invalid_time", "理财购买时间无效")
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        product, card, profile, error_code, message = _purchase_state(
            connection, product_id, card_id, amount_cents, scope
        )
        if error_code:
            return _failure(error_code, message)
        preview = {
            "product_id": product["product_id"],
            "product_name": product["name"],
            "product_risk_level": product["risk_level"],
            "user_risk_level": profile["risk_level"],
            "amount": cents_to_yuan(amount_cents),
            "amount_cents": amount_cents,
            "card_id": card["card_id"],
            "masked_card_number": mask_card_number(card["card_number"]),
            "reference_annual_rate": _rate_percent(
                product["reference_annual_rate_bps"]
            ),
            "duration_days": product["duration_days"],
            "requires_confirmation": True,
            "notice": "参考收益不代表实际收益，也不构成保证。",
        }
        if idempotency_key is not None:
            preview["_idempotency_key"] = idempotency_key
        return make_result(True, "模拟理财购买尚未执行，请确认后继续", preview)
    except sqlite3.Error:
        return _failure("wealth_purchase_preview_error", "模拟理财购买预览失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def _completed_order(connection, idempotency_key):
    return connection.execute(
        """
        SELECT o.*, p.name AS product_name, c.card_number
        FROM wealth_orders o
        JOIN wealth_products p ON p.product_id = o.product_id
        JOIN cards c ON c.card_id = o.card_id
        WHERE o.idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()


def _completed_order_data(row, idempotent_replay):
    return {
        "order_id": row["order_id"],
        "order_type": row["order_type"],
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "holding_id": row["holding_id"],
        "card_id": row["card_id"],
        "masked_card_number": mask_card_number(row["card_number"]),
        "amount": cents_to_yuan(row["amount_cents"]),
        "principal_after": cents_to_yuan(row["principal_after_cents"]),
        "card_balance_after": cents_to_yuan(row["card_balance_after_cents"]),
        "completed_at": row["created_at"],
        "idempotent_replay": idempotent_replay,
        "notice": "这是模拟理财交易；当前版本不计算投资收益。",
    }


def new_idempotency_key(action: str) -> str:
    return f"{action}_{uuid.uuid4().hex}"


def _new_order_id(order_type, id_provider):
    raw = (
        id_provider()
        if id_provider is not None
        else f"wo_{order_type.lower()}_{uuid.uuid4().hex}"
    )
    order_id = str(raw).strip()
    if not ORDER_ID_PATTERN.fullmatch(order_id):
        raise ValueError("invalid generated wealth order id")
    return order_id


def _validate_execution_input(identifier_values, amount_cents, idempotency_key):
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in identifier_values
    ):
        return "理财业务编号必须是正整数"
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        return "理财交易金额必须是正整数分"
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        return "理财交易幂等键格式无效"
    return None


def execute_wealth_purchase(
    product_id,
    card_id,
    amount_cents,
    idempotency_key,
    principal: Principal | None = None,
    security_context=None,
    confirmed=None,
    now_provider=None,
    id_provider=None,
):
    invalid = _validate_execution_input(
        (product_id, card_id), amount_cents, idempotency_key
    )
    if invalid:
        return _failure("invalid_execution_arguments", invalid)
    if security_context is None:
        return authorization_failure()
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.pg04 import execute
        return execute('BUY',product_id=product_id,card_id=card_id,amount_cents=amount_cents,
            idempotency_key=idempotency_key,principal=principal,security_context=security_context,
            now_provider=now_provider,id_provider=id_provider)
    timestamp, business_day = _normalize_now(now_provider)
    if timestamp is None:
        return _failure("invalid_time", "理财购买时间无效")
    now_dt = _now_datetime(now_provider)
    if now_dt is None:
        return _failure("invalid_time", "理财购买时间无效")
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            assert_write_authorization(
                connection,
                security_context,
                "buy_wealth_product",
                wealth_purchase_args(product_id, card_id, amount_cents, idempotency_key),
                now=now_dt,
            )
        except Exception as error:
            connection.rollback()
            return authorization_failure(
                getattr(error, "message", None), getattr(error, "code", None)
            )
        write_audit(security_context, connection, "revalidation", "ok",
                    detail={"revalidation": "authorization_consumed"})

        existing = _completed_order(connection, idempotency_key)
        if existing is not None:
            same = (
                existing["order_type"] == "BUY"
                and int(existing["owner_id"]) == scope
                and existing["product_id"] == product_id
                and existing["card_id"] == card_id
                and existing["amount_cents"] == amount_cents
            )
            if not same:
                connection.rollback()
                return _failure("idempotency_conflict", "同一幂等键对应的模拟理财请求不一致")
            replay = mark_verified(_completed_order_data(existing, True))
            connection.commit()
            return make_result(True, "模拟理财购买已完成，本次返回原交易结果", replay)

        product, card, profile, error_code, message = _purchase_state(
            connection, product_id, card_id, amount_cents, scope
        )
        if error_code:
            connection.rollback()
            return _failure(error_code, message)

        holding = connection.execute(
            """
            SELECT * FROM wealth_holdings
            WHERE user_id = ? AND product_id = ? AND card_id = ?
            """,
            (scope, product_id, card_id),
        ).fetchone()
        principal_before = holding["principal_cents"] if holding else 0
        principal_after = principal_before + amount_cents
        card_balance_after = card["balance_cents"] - amount_cents
        update = connection.execute(
            """
            UPDATE cards SET balance_cents = balance_cents - ?
            WHERE card_id = ? AND status = 'normal' AND balance_cents >= ?
            """,
            (amount_cents, card_id, amount_cents),
        )
        if update.rowcount != 1:
            raise sqlite3.IntegrityError("conditional wealth purchase debit failed")
        if holding is None:
            cursor = connection.execute(
                """
                INSERT INTO wealth_holdings (
                    user_id, product_id, card_id, principal_cents, status,
                    purchased_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (scope, product_id, card_id, amount_cents, timestamp, timestamp),
            )
            holding_id = cursor.lastrowid
        else:
            holding_id = holding["holding_id"]
            connection.execute(
                """
                UPDATE wealth_holdings
                SET principal_cents = ?, status = 'active', updated_at = ?
                WHERE holding_id = ?
                """,
                (principal_after, timestamp, holding_id),
            )
        order_id = _new_order_id("BUY", id_provider)
        connection.execute(
            """
            INSERT INTO wealth_orders (
                order_id, idempotency_key, order_type, user_id, card_id,
                product_id, holding_id, amount_cents, principal_before_cents,
                principal_after_cents, card_balance_after_cents, status, created_at,
                owner_id, business_date, principal_id, session_id, authorization_id
            ) VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, idempotency_key, scope, card_id, product_id,
                holding_id, amount_cents, principal_before, principal_after,
                card_balance_after, timestamp,
                scope, business_day,
                getattr(getattr(security_context, "principal", None), "principal_id", None),
                getattr(security_context, "session_id", None),
                getattr(getattr(security_context, "grant", None), "authorization_id", None),
            ),
        )
        connection.execute(
            """
            INSERT INTO transactions (
                card_id, date, merchant, category, amount_cents, direction, owner_id, transaction_type, classification_source, source_order_id
            ) VALUES (?, ?, ?, '理财购买', ?, 'expense', ?, 'wealth_buy', 'wealth_service', ?)
            """,
            (card_id, business_day, product["name"], amount_cents, scope, order_id),
        )
        masked_card = mask_card_number(card["card_number"])
        audit_common = {
            "order_id": order_id,
            "product_id": product_id,
            "product_name": product["name"],
            "holding_id": holding_id,
            "card_id": card_id,
            "masked_card_number": masked_card,
            "amount": cents_to_yuan(amount_cents),
            "simulation": True,
        }
        connection.execute(
            """
            INSERT INTO operation_logs (
                operation_type, target_type, target_id, target_display,
                before_json, after_json, result, message, created_at
            ) VALUES ('buy_wealth_product', 'wealth_holding', ?, ?, ?, ?,
                      'success', '模拟理财购买成功', ?)
            """,
            (
                holding_id,
                f"{product['name']}（{masked_card}）",
                json.dumps({**audit_common, "principal_before": cents_to_yuan(principal_before)}, ensure_ascii=False),
                json.dumps({**audit_common, "principal_after": cents_to_yuan(principal_after)}, ensure_ascii=False),
                timestamp,
            ),
        )
        completed = _completed_order(connection, idempotency_key)
        latest_card = connection.execute(
            "SELECT balance_cents FROM cards WHERE card_id = ?", (card_id,)
        ).fetchone()
        latest_holding = connection.execute(
            "SELECT principal_cents FROM wealth_holdings WHERE holding_id = ?", (holding_id,)
        ).fetchone()
        if (
            completed is None
            or latest_card is None
            or latest_holding is None
            or latest_card["balance_cents"] != card_balance_after
            or latest_holding["principal_cents"] != principal_after
        ):
            raise sqlite3.IntegrityError("wealth purchase verification failed")
        write_audit(security_context, connection, "execution", "ok")
        write_audit(security_context, connection, "verification", "ok",
                    detail={"write_verification": "passed"})
        result = mark_verified(_completed_order_data(completed, False))
        from lifecycle.service import record_commit
        record_commit(connection, security_context, make_result(True, "模拟理财购买成功", result))
        connection.commit()
        return make_result(True, "模拟理财购买成功", result)
    except AuditFailure as error:
        if connection is not None:
            connection.rollback()
        return _failure(error.code, error.message)
    except Exception:
        if connection is not None:
            connection.rollback()
        return _failure("wealth_purchase_failed", "模拟理财购买失败，资金和业务数据未被视为成功修改")
    finally:
        if connection is not None:
            connection.close()


def _redemption_state(connection, holding_id, amount_cents, scope):
    holding = connection.execute(
        """
        SELECT h.*, p.name AS product_name, p.status AS product_status,
               c.card_number, c.status AS card_status, c.balance_cents
        FROM wealth_holdings h
        JOIN wealth_products p ON p.product_id = h.product_id
        JOIN cards c ON c.card_id = h.card_id
        WHERE h.holding_id = ? AND h.user_id = ?
        """,
        (holding_id, scope),
    ).fetchone()
    if holding is None:
        return None, "holding_not_found", "模拟理财持仓不存在"
    product=connection.execute('SELECT * FROM wealth_products WHERE product_id=?',(holding['product_id'],)).fetchone()
    if product and 'category' in product.keys() and product['category']=='insurance':
        return holding,'insurance_redemption_unsupported','模拟保险不支持按理财赎回；保费不代表可退现金价值'
    if holding["status"] != "active":
        return holding, "holding_redeemed", "该模拟理财持仓已赎回"
    if holding["card_status"] != "normal":
        return holding, "card_unavailable", "关联银行卡状态不允许赎回入账"
    if amount_cents > holding["principal_cents"]:
        return holding, "amount_exceeds_principal", "赎回金额不能超过模拟持仓本金"
    return holding, None, None


def prepare_wealth_redemption(
    holding_id, amount, now_provider=None, principal: Principal | None = None,
    idempotency_key=None,
):
    if isinstance(holding_id, bool) or not isinstance(holding_id, int) or holding_id <= 0:
        return _failure("invalid_holding_id", "持仓编号必须是正整数")
    amount_cents = strict_wealth_amount_to_cents(amount)
    if amount_cents is None:
        return _failure("invalid_amount", "赎回金额必须是大于0且最多两位小数的有限数字")
    timestamp, _business_date = _normalize_now(now_provider)
    if timestamp is None:
        return _failure("invalid_time", "理财赎回时间无效")
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        holding, error_code, message = _redemption_state(
            connection, holding_id, amount_cents, scope
        )
        if error_code:
            return _failure(error_code, message)
        remaining = holding["principal_cents"] - amount_cents
        preview = {
            "holding_id": holding["holding_id"],
            "product_id": holding["product_id"],
            "product_name": holding["product_name"],
            "card_id": holding["card_id"],
            "masked_card_number": mask_card_number(holding["card_number"]),
            "current_principal": cents_to_yuan(holding["principal_cents"]),
            "redemption_amount": cents_to_yuan(amount_cents),
            "remaining_principal": cents_to_yuan(remaining),
            "amount_cents": amount_cents,
            "requires_confirmation": True,
            "notice": "当前版本不计算投资收益，赎回到账金额等于本次赎回本金。",
        }
        if idempotency_key is not None:
            preview["_idempotency_key"] = idempotency_key
        return make_result(True, "模拟理财赎回尚未执行，请确认后继续", preview)
    except sqlite3.Error:
        return _failure("wealth_redemption_preview_error", "模拟理财赎回预览失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def execute_wealth_redemption(
    holding_id,
    amount_cents,
    idempotency_key,
    principal: Principal | None = None,
    security_context=None,
    confirmed=None,
    now_provider=None,
    id_provider=None,
):
    invalid = _validate_execution_input((holding_id,), amount_cents, idempotency_key)
    if invalid:
        return _failure("invalid_execution_arguments", invalid)
    if security_context is None:
        return authorization_failure()
    from storage.config import pg02_enabled
    if pg02_enabled():
        from storage.pg04 import execute
        return execute('REDEEM',holding_id=holding_id,amount_cents=amount_cents,
            idempotency_key=idempotency_key,principal=principal,security_context=security_context,
            now_provider=now_provider,id_provider=id_provider)
    timestamp, business_day = _normalize_now(now_provider)
    if timestamp is None:
        return _failure("invalid_time", "理财赎回时间无效")
    now_dt = _now_datetime(now_provider)
    if now_dt is None:
        return _failure("invalid_time", "理财赎回时间无效")
    scope = owner_scope(principal)
    connection = None
    try:
        connection = get_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            assert_write_authorization(
                connection,
                security_context,
                "redeem_wealth_product",
                wealth_redemption_args(holding_id, amount_cents, idempotency_key),
                now=now_dt,
            )
        except Exception as error:
            connection.rollback()
            return authorization_failure(
                getattr(error, "message", None), getattr(error, "code", None)
            )
        write_audit(security_context, connection, "revalidation", "ok",
                    detail={"revalidation": "authorization_consumed"})

        existing = _completed_order(connection, idempotency_key)
        if existing is not None:
            same = (
                existing["order_type"] == "REDEEM"
                and int(existing["owner_id"]) == scope
                and existing["holding_id"] == holding_id
                and existing["amount_cents"] == amount_cents
            )
            if not same:
                connection.rollback()
                return _failure("idempotency_conflict", "同一幂等键对应的模拟理财请求不一致")
            replay = mark_verified(_completed_order_data(existing, True))
            connection.commit()
            return make_result(True, "模拟理财赎回已完成，本次返回原交易结果", replay)

        holding, error_code, message = _redemption_state(
            connection, holding_id, amount_cents, scope
        )
        if error_code:
            connection.rollback()
            return _failure(error_code, message)
        principal_before = holding["principal_cents"]
        principal_after = principal_before - amount_cents
        new_status = "redeemed" if principal_after == 0 else "active"
        card_balance_after = holding["balance_cents"] + amount_cents
        update_holding = connection.execute(
            """
            UPDATE wealth_holdings
            SET principal_cents = ?, status = ?, updated_at = ?
            WHERE holding_id = ? AND user_id = ? AND status = 'active'
              AND principal_cents >= ?
            """,
            (
                principal_after, new_status, timestamp, holding_id,
                scope, amount_cents,
            ),
        )
        if update_holding.rowcount != 1:
            raise sqlite3.IntegrityError("conditional wealth redemption failed")
        update_card = connection.execute(
            """
            UPDATE cards SET balance_cents = balance_cents + ?
            WHERE card_id = ? AND status = 'normal'
            """,
            (amount_cents, holding["card_id"]),
        )
        if update_card.rowcount != 1:
            raise sqlite3.IntegrityError("conditional wealth redemption credit failed")
        order_id = _new_order_id("REDEEM", id_provider)
        connection.execute(
            """
            INSERT INTO wealth_orders (
                order_id, idempotency_key, order_type, user_id, card_id,
                product_id, holding_id, amount_cents, principal_before_cents,
                principal_after_cents, card_balance_after_cents, status, created_at,
                owner_id, business_date, principal_id, session_id, authorization_id
            ) VALUES (?, ?, 'REDEEM', ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, idempotency_key, scope, holding["card_id"],
                holding["product_id"], holding_id, amount_cents,
                principal_before, principal_after, card_balance_after, timestamp,
                scope, business_day,
                getattr(getattr(security_context, "principal", None), "principal_id", None),
                getattr(security_context, "session_id", None),
                getattr(getattr(security_context, "grant", None), "authorization_id", None),
            ),
        )
        connection.execute(
            """
            INSERT INTO transactions (
                card_id, date, merchant, category, amount_cents, direction, owner_id, transaction_type, classification_source, source_order_id
            ) VALUES (?, ?, ?, '理财赎回', ?, 'income', ?, 'wealth_redeem', 'wealth_service', ?)
            """,
            (holding["card_id"], business_day, holding["product_name"], amount_cents, scope, order_id),
        )
        masked_card = mask_card_number(holding["card_number"])
        audit_common = {
            "order_id": order_id,
            "product_id": holding["product_id"],
            "product_name": holding["product_name"],
            "holding_id": holding_id,
            "card_id": holding["card_id"],
            "masked_card_number": masked_card,
            "amount": cents_to_yuan(amount_cents),
            "simulation": True,
        }
        connection.execute(
            """
            INSERT INTO operation_logs (
                operation_type, target_type, target_id, target_display,
                before_json, after_json, result, message, created_at
            ) VALUES ('redeem_wealth_product', 'wealth_holding', ?, ?, ?, ?,
                      'success', '模拟理财赎回成功', ?)
            """,
            (
                holding_id,
                f"{holding['product_name']}（{masked_card}）",
                json.dumps({**audit_common, "principal_before": cents_to_yuan(principal_before)}, ensure_ascii=False),
                json.dumps({**audit_common, "principal_after": cents_to_yuan(principal_after)}, ensure_ascii=False),
                timestamp,
            ),
        )
        completed = _completed_order(connection, idempotency_key)
        latest_card = connection.execute(
            "SELECT balance_cents FROM cards WHERE card_id = ?", (holding["card_id"],)
        ).fetchone()
        latest_holding = connection.execute(
            "SELECT principal_cents, status FROM wealth_holdings WHERE holding_id = ?", (holding_id,)
        ).fetchone()
        if (
            completed is None
            or latest_card is None
            or latest_holding is None
            or latest_card["balance_cents"] != card_balance_after
            or latest_holding["principal_cents"] != principal_after
            or latest_holding["status"] != new_status
        ):
            raise sqlite3.IntegrityError("wealth redemption verification failed")
        write_audit(security_context, connection, "execution", "ok")
        write_audit(security_context, connection, "verification", "ok",
                    detail={"write_verification": "passed"})
        result = mark_verified(_completed_order_data(completed, False))
        from lifecycle.service import record_commit
        record_commit(connection, security_context, make_result(True, "模拟理财赎回成功", result))
        connection.commit()
        return make_result(True, "模拟理财赎回成功", result)
    except AuditFailure as error:
        if connection is not None:
            connection.rollback()
        return _failure(error.code, error.message)
    except Exception:
        if connection is not None:
            connection.rollback()
        return _failure("wealth_redemption_failed", "模拟理财赎回失败，资金和业务数据未被视为成功修改")
    finally:
        if connection is not None:
            connection.close()
