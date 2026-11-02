"""项目通用工具函数。"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


STATUS_TEXT = {
    "normal": "正常",
    "lost": "已挂失",
    "frozen": "已冻结",
    "active": "有效",
    "cancelled": "已停止",
}

FIELD_LABELS = {
    "card_id": "银行卡编号",
    "bank_name": "银行名称",
    "masked_card_number": "银行卡号",
    "card_last_four": "银行卡尾号",
    "balance": "可用余额",
    "status": "状态",
    "daily_limit": "每日交易限额",
    "overseas_enabled": "境外支付",
    "online_payment_enabled": "网络支付",
    "transaction_id": "流水编号",
    "date": "日期",
    "merchant": "商户",
    "category": "类别",
    "amount": "金额",
    "direction": "收支方向",
    "month": "月份",
    "transaction_count": "支出流水条数",
    "total_spending": "月度总支出",
    "card_specified": "是否指定银行卡",
    "categories": "分类统计",
    "percentage": "占比",
    "base_month": "基准月份",
    "target_month": "对比月份",
    "base_spending": "基准月支出",
    "target_spending": "对比月支出",
    "amount_change": "金额变化",
    "change_direction": "变化方向",
    "change_percentage": "环比百分比",
    "subscription_id": "订阅编号",
    "name": "订阅名称",
    "cycle": "扣款周期",
    "next_charge_date": "下次扣款日期",
    "active_count": "有效订阅数量",
    "monthly_cost": "每月订阅总费用",
    "annual_cost": "预计全年费用",
    "subscriptions": "订阅明细",
    "requires_confirmation": "需要确认",
    "old_limit": "原每日限额",
    "new_limit": "新每日限额",
    "log_id": "日志编号",
    "operation_type": "操作类型",
    "target_type": "目标类型",
    "target_id": "目标编号",
    "target_display": "目标显示",
    "before": "修改前",
    "after": "修改后",
    "result": "执行结果",
    "message": "说明",
    "created_at": "创建时间",
}

MONEY_FIELDS = {
    "balance",
    "daily_limit",
    "amount",
    "total_spending",
    "base_spending",
    "target_spending",
    "amount_change",
    "monthly_cost",
    "annual_cost",
    "old_limit",
    "new_limit",
}


def mask_card_number(card_number):
    """银行卡号脱敏，仅保留后四位。"""
    number_text = str(card_number)
    last_four = number_text[-4:] if len(number_text) >= 4 else number_text
    return f"**** **** **** {last_four}"


def yuan_to_cents(value):
    """把人民币元安全转换为整数分，不直接使用浮点数乘 100。"""
    if isinstance(value, bool):
        raise ValueError("金额必须是有效数字")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("金额必须是有效数字") from error
    if not amount.is_finite():
        raise ValueError("金额必须是有效数字")
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def cents_to_yuan(value):
    """把数据库整数分转换为兼容 V0.0 接口的两位小数金额。"""
    cents = int(value)
    return float((Decimal(cents) / Decimal(100)).quantize(Decimal("0.01")))


def format_money(amount):
    """将金额格式化为带千位分隔符的两位小数。"""
    return f"¥{float(amount):,.2f}"


def status_to_chinese(status):
    """将内部状态值转换为中文。"""
    return STATUS_TEXT.get(status, status)


def switch_to_chinese(value):
    """将布尔开关转换为中文。"""
    return "已开启" if value else "已关闭"


def safe_card_info(card):
    """返回可展示的银行卡信息，绝不包含完整卡号。"""
    return {
        "card_id": card["card_id"],
        "bank_name": card["bank_name"],
        "masked_card_number": mask_card_number(card["card_number"]),
        "balance": cents_to_yuan(card["balance_cents"]),
        "status": status_to_chinese(card["status"]),
        "daily_limit": cents_to_yuan(card["daily_limit_cents"]),
        "overseas_enabled": switch_to_chinese(card["overseas_enabled"]),
        "online_payment_enabled": switch_to_chinese(
            card["online_payment_enabled"]
        ),
    }


def make_result(success, message, data=None):
    """创建统一格式的业务返回值。"""
    return {"success": success, "message": message, "data": data}


def _format_value(key, value):
    """根据字段含义格式化单个展示值。"""
    if key in MONEY_FIELDS and isinstance(value, (int, float)):
        return format_money(value)
    if key in {"percentage", "change_percentage"} and value is not None:
        return f"{value:.2f}%"
    if key == "direction":
        return "收入" if value == "income" else "支出"
    if key == "cycle":
        return "每月" if value == "monthly" else value
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "无法计算"
    return str(value)


def _print_data(data, indent=0):
    """递归展示列表和字典，供 print_result 使用。"""
    prefix = " " * indent
    if isinstance(data, list):
        if not data:
            print(f"{prefix}（无数据）")
            return
        for index, item in enumerate(data, start=1):
            print(f"{prefix}--- 第 {index} 项 ---")
            _print_data(item, indent + 2)
        return

    if isinstance(data, dict):
        if not data:
            print(f"{prefix}（无数据）")
            return
        for key, value in data.items():
            label = FIELD_LABELS.get(key, key)
            if isinstance(value, (dict, list)):
                print(f"{prefix}{label}：")
                _print_data(value, indent + 2)
            else:
                print(f"{prefix}{label}：{_format_value(key, value)}")
        return

    print(f"{prefix}{data}")


def print_result(result):
    """清晰展示统一返回字典中的消息和数据。"""
    marker = "成功" if result.get("success") else "失败"
    print(f"\n[{marker}] {result.get('message', '')}")
    if result.get("data") is not None:
        _print_data(result["data"], indent=2)
