"""操作审计日志查询服务。"""

import json
import sqlite3

from database import get_connection
from utils import make_result


def _read_json_safely(value):
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"解析提示": "日志内容无法解析"}
    return parsed if isinstance(parsed, dict) else {"内容": parsed}


def get_operation_logs(limit=20, operation_type=None):
    """按最新优先查询 1～100 条脱敏审计日志。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        return make_result(False, "日志条数必须是1到100之间的整数")
    if operation_type is not None:
        if not isinstance(operation_type, str) or not operation_type.strip():
            return make_result(False, "操作类型必须是非空文本")
        operation_type = operation_type.strip()

    connection = None
    try:
        connection = get_connection()
        if operation_type is None:
            rows = connection.execute(
                """
                SELECT * FROM operation_logs
                ORDER BY log_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM operation_logs
                WHERE operation_type = ?
                ORDER BY log_id DESC
                LIMIT ?
                """,
                (operation_type, limit),
            ).fetchall()
        logs = []
        for row in rows:
            logs.append(
                {
                    "log_id": row["log_id"],
                    "operation_type": row["operation_type"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                    "target_display": row["target_display"],
                    "before": _read_json_safely(row["before_json"]),
                    "after": _read_json_safely(row["after_json"]),
                    "result": row["result"],
                    "message": row["message"],
                    "created_at": row["created_at"],
                }
            )
        return make_result(True, f"共查询到 {len(logs)} 条操作记录", logs)
    except sqlite3.Error:
        return make_result(False, "操作记录查询失败，请稍后重试")
    finally:
        if connection is not None:
            connection.close()
