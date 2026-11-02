"""Current step labels from validated business parameters, never model prose."""
from decimal import Decimal


def step_title(step):
    args=step.arguments
    labels={'get_cards':'查询银行卡余额','get_wealth_products':'查询理财产品',
        'get_wealth_holdings':'查询最新持仓','get_risk_profile':'查询风险等级',
        'query_data':'查询账单','query_financial_records':'查询收支统计',
        'get_monthly_spending':'查询月度支出','get_monthly_cash_flow_report':'查询月度收支',
        'get_transactions_by_month':'查询月度流水','get_category_spending':'查询分类支出',
        'get_expense_clues':'查询消费线索'}
    name=step.tool_name
    if name in {'transfer_money','buy_wealth_product','redeem_wealth_product'}:
        amount=args.get('amount')
        money=f'{Decimal(str(amount)):.2f}元' if amount is not None else '金额待明确'
        if name=='transfer_money':return f"从{args.get('from_card_id','待明确')}号卡转账{money}"
        if name=='buy_wealth_product':return f"从{args.get('card_id','待明确')}号卡购买产品{args.get('product_id','待明确')}，{money}"
        return f"赎回持仓{args.get('holding_id','待明确')}，{money}"
    return labels.get(name,'办理已登记业务')
