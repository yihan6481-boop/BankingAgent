"""银行卡查询、挂失和限额修改业务（V0.7.0：归属检查 + 可信授权上下文）。"""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from database import get_connection
from storage.errors import CommitUnknown
from utils import (
    cents_to_yuan,
    make_result,
    mask_card_number,
    safe_card_info,
    yuan_to_cents,
)

from security.args import report_card_lost_args, set_daily_limit_args
from security.audit import AuditFailure
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


def _failure(error_code, message, data=None):
    result = make_result(False, message, data)
    result["error_code"] = error_code
    return result


def _get_card(connection, card_id):
    return connection.execute(
        "SELECT * FROM cards WHERE card_id = ?", (card_id,)
    ).fetchone()


def _safe_audit_card(card):
    """生成允许写入审计日志的脱敏卡片快照。"""
    return {
        "card_id": card["card_id"],
        "bank_name": card["bank_name"],
        "masked_card_number": mask_card_number(card["card_number"]),
        "status": card["status"],
        "daily_limit": cents_to_yuan(card["daily_limit_cents"]),
    }


def _insert_log(
    connection, operation_type, card, before_data, after_data, message
):
    connection.execute(
        """
        INSERT INTO operation_logs (
            operation_type, target_type, target_id, target_display,
            before_json, after_json, result, message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_type,
            "card",
            card["card_id"],
            mask_card_number(card["card_number"]),
            json.dumps(before_data, ensure_ascii=False),
            json.dumps(after_data, ensure_ascii=False),
            "success",
            message,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def get_cards(principal: Principal | None = None):
    """查询当前身份归属范围内的全部银行卡安全展示信息。"""
    connection = None
    try:
        connection = get_connection()
        rows = connection.execute(
            "SELECT * FROM cards WHERE owner_id = ? ORDER BY card_id",
            (owner_scope(principal),),
        ).fetchall()
        cards = [safe_card_info(row) for row in rows]
        return make_result(True, f"共查询到 {len(cards)} 张银行卡", cards)
    except sqlite3.Error:
        return make_result(False, "银行卡查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def find_card_by_last_four(last_four, principal: Principal | None = None):
    """通过四位尾号查询银行卡，不对同尾号卡片做默认选择。"""
    last_four_text = str(last_four).strip()
    if len(last_four_text) != 4 or not last_four_text.isdigit():
        return make_result(False, "银行卡尾号必须是4位数字")

    connection = None
    try:
        connection = get_connection()
        rows = connection.execute(
            "SELECT * FROM cards WHERE substr(card_number, -4) = ? "
            "AND owner_id = ? ORDER BY card_id",
            (last_four_text, owner_scope(principal)),
        ).fetchall()
        matched_cards = [safe_card_info(row) for row in rows]
        if not matched_cards:
            return make_result(False, "未找到该尾号的银行卡")
        if len(matched_cards) > 1:
            return make_result(
                True,
                "找到多张相同尾号的银行卡，请根据银行卡编号选择",
                matched_cards,
            )
        return make_result(True, "银行卡查询成功", matched_cards[0])
    except sqlite3.Error:
        return make_result(False, "银行卡查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()


def report_card_lost(card_id, principal: Principal | None = None, *,
                     security_context=None, confirmed=None):
    """预览或在事务内挂失银行卡并同时记录脱敏审计日志。

    预览阶段只做只读校验；执行阶段必须携带可信 ``security_context``：它绑定
    服务器端身份、Session、操作参数摘要和一次性授权行。V0.6.4 的
    ``confirmed=True`` 布尔值不再构成充分授权证据，单独传入会被拒绝。
    """
    connection = None
    try:
        connection = get_connection()
        scope = owner_scope(principal)
        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (card_id, scope),
        ).fetchone()
        if card is None:
            return _failure("card_not_found", "银行卡不存在")
        if card["status"] == "lost":
            return _failure(
                "card_already_lost", "银行卡已经挂失，不能重复操作"
            )
        if card["status"] != "normal":
            return _failure(
                "card_unavailable", "当前银行卡状态不允许挂失"
            )

        preview = {
            "card_id": card["card_id"],
            "bank_name": card["bank_name"],
            "masked_card_number": mask_card_number(card["card_number"]),
            "requires_confirmation": True,
        }
        if security_context is None:
            if confirmed is True:
                # 旧内部参数不再被当作授权证据
                return authorization_failure()
            return make_result(True, "银行卡挂失尚未执行，请确认后继续", preview)

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
                "report_card_lost",
                report_card_lost_args(card_id),
                now=now,
            )
        except Exception as error:
            connection.rollback()
            return authorization_failure(
                getattr(error, "message", None), getattr(error, "code", None)
            )

        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (card_id, scope),
        ).fetchone()
        if card is None or card["status"] != "normal":
            connection.rollback()
            return make_result(False, "银行卡状态已变化，挂失未执行")

        write_audit(security_context, connection, "revalidation", "ok",
                    detail={"revalidation": "card_state_checked"})
        before_data = _safe_audit_card(card)
        connection.execute(
            "UPDATE cards SET status = ? WHERE card_id = ? AND status = 'normal'",
            ("lost", card_id),
        )
        updated_card = _get_card(connection, card_id)
        after_data = _safe_audit_card(updated_card)
        message = "银行卡挂失成功"
        _insert_log(
            connection,
            "report_card_lost",
            updated_card,
            before_data,
            after_data,
            message,
        )
        if updated_card is None or updated_card["status"] != "lost":
            connection.rollback()
            return make_result(False, "银行卡挂失核验失败，数据未发生变化")
        data = mark_verified({
            "card_id": updated_card["card_id"],
            "masked_card_number": mask_card_number(updated_card["card_number"]),
            "status": "已挂失",
        })
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
        return _failure(error.code, error.message)
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
        return make_result(False, "银行卡挂失失败，数据未发生变化")
    finally:
        if connection is not None:
            connection.close()


def _validate_limit(new_limit):
    if isinstance(new_limit, bool):
        return None, "新限额必须是有效数字"
    try:
        decimal_limit = Decimal(str(new_limit))
    except (InvalidOperation, TypeError, ValueError):
        return None, "新限额必须是有效数字"
    if not decimal_limit.is_finite():
        return None, "新限额必须是有效数字"
    if decimal_limit <= 0:
        return None, "新限额必须大于0元"
    if decimal_limit > Decimal("50000"):
        return None, "新限额最高不能超过50000元"
    try:
        cents = yuan_to_cents(decimal_limit)
    except ValueError:
        return None, "新限额必须是有效数字"
    if cents <= 0:
        return None, "新限额必须大于0元"
    return cents, None


def set_daily_limit(card_id, new_limit, principal: Principal | None = None, *,
                    security_context=None, confirmed=None):
    """预览或在事务内修改正常银行卡的每日交易限额。"""
    new_limit_cents, validation_error = _validate_limit(new_limit)
    if validation_error:
        return make_result(False, validation_error)

    connection = None
    try:
        connection = get_connection()
        scope = owner_scope(principal)
        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (card_id, scope),
        ).fetchone()
        if card is None:
            return _failure("card_not_found", "银行卡不存在")
        if card["status"] != "normal":
            return _failure(
                "card_unavailable",
                "只有状态正常的银行卡才能修改每日限额",
            )

        old_limit = cents_to_yuan(card["daily_limit_cents"])
        checked_limit = cents_to_yuan(new_limit_cents)
        preview = {
            "card_id": card["card_id"],
            "masked_card_number": mask_card_number(card["card_number"]),
            "old_limit": old_limit,
            "new_limit": checked_limit,
            "requires_confirmation": True,
        }
        if security_context is None:
            if confirmed is True:
                return authorization_failure()
            return make_result(
                True, "每日交易限额修改尚未执行，请确认后继续", preview
            )

        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        from storage.config import pg02_enabled
        if pg02_enabled(): connection.financial = True
        now = datetime.now(timezone.utc)
        if pg02_enabled():
            from security.service import get_security_services
            now=get_security_services().identity._now()
        # 归属校验必须先于授权校验：跨用户资源应报为不存在而不是权限异常。
        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (card_id, scope),
        ).fetchone()
        if card is None:
            connection.rollback()
            return _failure("card_not_found", "银行卡不存在")
        try:
            assert_write_authorization(
                connection,
                security_context,
                "set_daily_limit",
                set_daily_limit_args(card_id, new_limit_cents),
                now=now,
            )
            assert_permission_not_escalated(
                security_context,
                "set_daily_limit",
                set_daily_limit_args(card_id, new_limit_cents),
                getattr(security_context, "policy_engine", None),
            )
        except PermissionEscalated as error:
            connection.rollback()
            return authorization_failure(error.message, error.code)
        except Exception as error:
            connection.rollback()
            return authorization_failure(
                getattr(error, "message", None), getattr(error, "code", None)
            )

        card = connection.execute(
            "SELECT * FROM cards WHERE card_id = ? AND owner_id = ?",
            (card_id, scope),
        ).fetchone()
        if card is None or card["status"] != "normal":
            connection.rollback()
            return _failure(
                "card_unavailable", "银行卡状态已变化，限额修改未执行"
            )
        old_limit = cents_to_yuan(card["daily_limit_cents"])
        write_audit(security_context, connection, "revalidation", "ok",
                    detail={"revalidation": "card_state_checked"})

        before_data = _safe_audit_card(card)
        connection.execute(
            "UPDATE cards SET daily_limit_cents = ? WHERE card_id = ? "
            "AND status = 'normal'",
            (new_limit_cents, card_id),
        )
        updated_card = _get_card(connection, card_id)
        after_data = _safe_audit_card(updated_card)
        message = "每日交易限额修改成功"
        _insert_log(
            connection,
            "set_daily_limit",
            updated_card,
            before_data,
            after_data,
            message,
        )
        if updated_card is None or updated_card["daily_limit_cents"] != new_limit_cents:
            connection.rollback()
            return make_result(False, "每日交易限额核验失败，数据未发生变化")
        data = mark_verified({
            "card_id": updated_card["card_id"],
            "masked_card_number": mask_card_number(updated_card["card_number"]),
            "old_limit": old_limit,
            "new_limit": checked_limit,
        })
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
        return _failure(error.code, error.message)
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
        return make_result(False, "每日交易限额修改失败，数据未发生变化")
    finally:
        if connection is not None:
            connection.close()
