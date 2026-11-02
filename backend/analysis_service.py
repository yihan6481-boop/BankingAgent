"""Deterministic, read-only financial facts over owned simulated data.

No subscription lookups, permissions, profile mutations or business writes.
Rule thresholds are demonstration parameters, not bank-validated risk models.
"""
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from pydantic import TypeAdapter, ValidationError

from analysis_models import (MonthReportArgs, YearReportArgs, AnomalyArgs, CompareArgs,
    SuitabilityArgs, AnalysisResult, Report, Anomalies, ProductComparison, Suitability, QueryArgs, QueryResult)
from bill_service import compare_monthly_spending
from database import get_connection
from storage.financial_reads import financial_read
from security.write_context import owner_scope
from utils import cents_to_yuan, make_result
from wealth_service import _risk_profile_row, _purchase_state, _safe_product

LARGE_EXPENSE_CENTS = 100_000  # >= 1000 CNY, simulated demonstration parameter
DUPLICATE_COUNT = 2
RULE_VERSION = 'demo-1.0'
EVIDENCE_LIMIT = 100
NOTICE = ('仅描述已记录的本地模拟流水，覆盖完整性未知；缺少记录不能证明实际没有消费。'
          '净现金流不是利润，理财申购和赎回不解释为收益。')
ADAPTER = TypeAdapter(AnalysisResult)


class AnalysisError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _safe_text(value):
    return re.sub(r'(?<!\d)(?:\d[ -]?){7,}\d(?!\d)', '***', str(value or '未提供'))[:100]


def _pct(current, base):
    if base == 0:
        return None
    return float((Decimal(current) * 100 / Decimal(base)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def _range(start, end, card_id):
    return {'start': start, 'end_exclusive': end, 'timezone': 'Asia/Shanghai', 'card_id': card_id}


def _month_range(month, card_id):
    year, number = map(int, month.split('-'))
    end = f'{year + (number == 12):04d}-{number % 12 + 1:02d}-01'
    return _range(month + '-01', end, card_id)


def _rows(connection, span, principal):
    scope = owner_scope(principal)
    if span['card_id'] is not None and connection.execute(
        'SELECT 1 FROM cards WHERE card_id=? AND owner_id=?', (span['card_id'], scope)
    ).fetchone() is None:
        raise AnalysisError('card_not_found')
    return [dict(row) for row in connection.execute('''
        SELECT t.*, c.card_number FROM visible_transactions t JOIN cards c ON t.card_id=c.card_id
        WHERE t.owner_id=? AND c.owner_id=? AND t.date>=? AND t.date<?
          AND (CAST(? AS INTEGER) IS NULL OR t.card_id=?) ORDER BY t.date, t.transaction_id
        ''', (scope, scope, span['start'], span['end_exclusive'], span['card_id'], span['card_id']))]


def _evidence(row):
    return dict(transaction_id=row['transaction_id'], card_id=row['card_id'],
        card_last_four=row['card_number'][-4:], date=row['date'],
        merchant=_safe_text(row['merchant']), category=_safe_text(row['category']),
        amount=cents_to_yuan(row['amount_cents']), direction=row['direction'])


def _coverage(rows):
    evidence = [_evidence(r) for r in rows]
    return dict(source='local_simulated_transactions', completeness='recorded_only',
        first_recorded=rows[0]['date'] if rows else None,
        last_recorded=rows[-1]['date'] if rows else None,
        recorded_months=sorted({r['date'][:7] for r in rows}), note=NOTICE,
        evidence=evidence[:EVIDENCE_LIMIT], evidence_truncated=len(rows) > EVIDENCE_LIMIT,
        ledger_sha256=hashlib.sha256(json.dumps(evidence, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest())


def _totals(rows):
    income = sum(r['amount_cents'] for r in rows if r['direction'] == 'income')
    expense = sum(r['amount_cents'] for r in rows if r['direction'] == 'expense')
    ic = sum(r['direction'] == 'income' for r in rows)
    return dict(income=cents_to_yuan(income), expense=cents_to_yuan(expense),
        net_cash_flow=cents_to_yuan(income-expense), transaction_count=len(rows),
        income_count=ic, expense_count=len(rows)-ic)


def _report(connection, args, principal):
    annual = isinstance(args, YearReportArgs)
    if annual:
        span = _range(f'{args.year:04d}-01-01', f'{args.year+1:04d}-01-01', args.card_id)
        previous = _range(f'{args.year-1:04d}-01-01', span['start'], args.card_id) if args.year > 1 else None
    else:
        span = _month_range(args.month, args.card_id)
        y, m = map(int, args.month.split('-'))
        previous = _month_range(f'{y-(m==1):04d}-{12 if m==1 else m-1:02d}', args.card_id) if y > 1 or m > 1 else None
    rows = _rows(connection, span, principal)
    base_rows = _rows(connection, previous, principal) if previous else []
    totals, base = _totals(rows), _totals(base_rows)
    category_cents, merchants = defaultdict(int), defaultdict(lambda: [0, 0])
    for row in rows:
        category_cents[(row['direction'], _safe_text(row['category']))] += row['amount_cents']
        if row['direction'] == 'expense':
            merchant = merchants[_safe_text(row['merchant'])]
            merchant[0] += row['amount_cents']
            merchant[1] += 1
    categories = []
    for (direction, name), cents in sorted(category_cents.items(), key=lambda x: (x[0][0], -x[1], x[0][1])):
        denominator = sum(r['amount_cents'] for r in rows if r['direction'] == direction)
        categories.append(dict(name=name, direction=direction, amount=cents_to_yuan(cents),
            percentage=_pct(cents, denominator), percentage_reason='zero_denominator' if denominator == 0 else None))
    changes = []
    for metric in ('income', 'expense', 'net_cash_flow', 'transaction_count'):
        old, current = Decimal(str(base[metric])), Decimal(str(totals[metric]))
        changes.append(dict(metric=metric, base=float(old), current=float(current), change=float(current-old),
            percentage=_pct(current-old, old), percentage_reason='zero_denominator' if old == 0 else None))
    # Reuse the established monthly expense comparison, while checking its facts
    # against the ownership-filtered snapshot before exposing them.
    if not annual and previous:
        compared = compare_monthly_spending(previous['start'][:7], args.month, args.card_id, principal)
        if not compared['success'] or compared['data']['base_spending'] != base['expense'] or compared['data']['target_spending'] != totals['expense']:
            raise AnalysisError('analysis_snapshot_changed')
    months = [dict(month=f'{args.year:04d}-{m:02d}', totals=_totals([r for r in rows if r['date'].startswith(f'{args.year:04d}-{m:02d}-')])) for m in range(1, 13)] if annual else []
    return dict(kind='annual_cash_flow_report' if annual else 'monthly_cash_flow_report',
        range=span, currency='CNY', totals=totals, categories=categories,
        merchants=[dict(name=name, expense=cents_to_yuan(values[0]), transaction_count=values[1])
            for name, values in sorted(merchants.items(), key=lambda x: (-x[1][0], x[0]))[:args.top_n]],
        coverage=_coverage(rows), comparison=dict(range=previous, totals=base,
            coverage=_coverage(base_rows), changes=changes), months=months,
        explanation=(f'已记录数据：收入 {totals["income"]:.2f} 元，支出 {totals["expense"]:.2f} 元，'
            f'净现金流 {totals["net_cash_flow"]:.2f} 元，共 {len(rows)} 笔。' if rows else '已记录数据中无流水。') + NOTICE)


def _duplicate_key(row):
    try:
        day = date.fromisoformat(row.get('date'))  # date-only; no invented minute windows
    except (ValueError, TypeError):
        return None
    if not row.get('merchant') or not row.get('card_id') or not row.get('amount_cents'):
        return None
    return row['card_id'], day.isoformat(), row['merchant'], row['amount_cents']


def _anomalies(connection, args, principal):
    span = _month_range(args.month, args.card_id)
    rows = _rows(connection, span, principal)
    expenses = [r for r in rows if r['direction'] == 'expense']
    groups = defaultdict(list)
    for row in expenses:
        key = _duplicate_key(row)
        if key:
            groups[key].append(row['transaction_id'])
    clues = []
    for row in expenses:
        triggers = []
        if row['amount_cents'] >= LARGE_EXPENSE_CENTS:
            triggers.append(dict(rule_id='large_expense', rule_version=RULE_VERSION,
                actual=row['amount_cents'], threshold=LARGE_EXPENSE_CENTS, unit='cents',
                reason='单笔支出达到模拟演示阈值，需用户核查。', transaction_ids=[row['transaction_id']]))
        key = _duplicate_key(row)
        ids = groups[key] if key else []
        if len(ids) >= DUPLICATE_COUNT:
            triggers.append(dict(rule_id='same_day_duplicate_candidate', rule_version=RULE_VERSION,
                actual=len(ids), threshold=DUPLICATE_COUNT, unit='count',
                reason='同卡同一天同商户同金额的重复支出候选；日期没有时分秒。', transaction_ids=ids[:EVIDENCE_LIMIT]))
        if triggers:
            clues.append(dict(transaction=_evidence(row), triggers=triggers))
    missing = sum(_duplicate_key(r) is None for r in expenses)
    return dict(kind='expense_clues', range=span, currency='CNY', rule_version=RULE_VERSION,
        matched_transaction_count=len(clues), clues=clues[:args.top_n], truncated=len(clues)>args.top_n,
        applicability=[dict(rule_id='large_expense', applicable_count=len(expenses), inapplicable_count=0, reason=None),
            dict(rule_id='same_day_duplicate_candidate', applicable_count=len(expenses)-missing,
                inapplicable_count=missing, reason='缺少有效日期、卡片、商户或金额，规则不适用。' if missing else None)],
        coverage=_coverage(rows), explanation='仅为需核查线索，不能认定诈骗、盗刷或重复扣款；不改变账户、卡片、订阅或交易。阈值为模拟演示参数，未经金融机构验证。')


def _fact(code, state, text, **facts):
    return dict(reason_code=code, state=state, explanation=text, **facts)


def _suitability(connection, args, principal):
    product = connection.execute('SELECT * FROM wealth_products WHERE product_id=?', (args.product_id,)).fetchone()
    if product is None:
        raise AnalysisError('product_not_found')
    scope = owner_scope(principal)
    profile = _risk_profile_row(connection, scope) if principal is not None else None
    risk = profile['risk_level'] if profile else None
    matches = int(risk[1]) >= int(product['risk_level'][1]) if risk else None
    risk_fact = _fact('risk_profile_missing' if matches is None else 'risk_match' if matches else 'risk_mismatch',
        'unknown' if matches is None else 'pass' if matches else 'fail',
        '风险画像未提供，不能默认适合。' if matches is None else '复用现有购买风险等级规则。',
        user_risk_level=risk, product_risk_level=product['risk_level'])
    conditions = [_fact('product_active' if product['status']=='active' else 'product_inactive',
        'pass' if product['status']=='active' else 'fail', '产品当前状态来自模拟目录。')]
    if args.amount is None:
        conditions.append(_fact('amount_not_provided', 'unknown', '未提供金额，未评估起购金额。', minimum=cents_to_yuan(product['min_purchase_cents'])))
    else:
        enough = args.amount * 100 >= product['min_purchase_cents']
        conditions.append(_fact('minimum_met' if enough else 'below_minimum', 'pass' if enough else 'fail',
            '拟购买金额与起购金额比较。', amount=float(args.amount), minimum=cents_to_yuan(product['min_purchase_cents'])))
    if args.card_id is None:
        conditions.append(_fact('card_not_provided', 'unknown', '未提供付款卡，未评估卡状态和余额。'))
    else:
        card = connection.execute('SELECT * FROM cards WHERE card_id=? AND owner_id=?', (args.card_id, scope)).fetchone()
        if principal is None or card is None:
            raise AnalysisError('card_not_found')
        conditions.append(_fact('card_unavailable' if card['status']!='normal' else 'card_available',
            'fail' if card['status']!='normal' else 'pass', '付款卡状态来自当前用户卡片。', card_status=card['status']))
        if args.amount is None:
            conditions.append(_fact('payment_not_evaluated', 'unknown', '未提供金额，未评估余额是否足够。'))
        else:
            sufficient = card['balance_cents'] >= args.amount * 100
            conditions.append(_fact('balance_sufficient' if sufficient else 'insufficient_balance', 'pass' if sufficient else 'fail',
                '余额仅为查询时事实，执行前必须复核。', balance=cents_to_yuan(card['balance_cents']), amount=float(args.amount)))
            # Assert agreement with the existing purchase kernel; no preview or authorization.
            _, _, _, error, _ = _purchase_state(connection, args.product_id, args.card_id, int(args.amount*100), scope)
            if error is None and (matches is not True or any(c['state'] != 'pass' for c in conditions)):
                raise AnalysisError('purchase_fact_mismatch')
    states = [c['state'] for c in conditions] + [risk_fact['state']]
    assessment = 'conditions_not_met' if 'fail' in states else 'not_fully_evaluated' if 'unknown' in states else 'conditions_met_at_query'
    conditions.append(_fact('purchase_preview_required', 'unknown', '分析不是购买授权，实际购买仍需预览、权限重算、确认/模拟OTP、执行及核验。'))
    return dict(kind='wealth_suitability', product_id=args.product_id, user_risk_level=risk,
        product_risk_level=product['risk_level'], risk_matches=matches, product_active=product['status']=='active',
        risk_fact=risk_fact, purchase_conditions=conditions, purchase_assessment=assessment, authorization='not_granted')


def _compare(connection, args, principal):
    products = []
    for identifier in args.product_ids:
        row = connection.execute('SELECT * FROM wealth_products WHERE product_id=?', (identifier,)).fetchone()
        if row is None:
            raise AnalysisError('product_not_found')
        products.append(dict(**_safe_product(row), liquidity='not_provided', early_redemption='not_provided', fees='not_provided',
            suitability=_suitability(connection, SuitabilityArgs(product_id=identifier), principal)))
    return dict(kind='wealth_product_comparison', products=products, source='local_simulated_product_catalog',
        explanation='按明确选择顺序比较，不按最高参考收益宣布最好。参考年化不是实际或保证收益。duration_days仅为目录期限，不等于锁定期或赎回到账时间；流动性、提前赎回和费用未提供。')


def _query_period(connection, args, principal, start, end, aliases):
    scope = owner_scope(principal)
    span = _range(start,end,args.card_id)
    if args.card_id is not None and connection.execute('SELECT 1 FROM cards WHERE card_id=? AND owner_id=?',(args.card_id,scope)).fetchone() is None:
        raise AnalysisError('card_not_found')
    where = 't.owner_id=? AND c.owner_id=? AND t.date>=? AND t.date<? AND (CAST(? AS INTEGER) IS NULL OR t.card_id=?)'
    params = [scope,scope,start,end,args.card_id,args.card_id]
    if aliases is not None:
        where += ' AND t.merchant IN (' + ','.join('?' for _ in aliases) + ')'
        params.extend(aliases)
    source = ' FROM visible_transactions t JOIN cards c ON c.card_id=t.card_id WHERE '+where
    predicate = "direction='expense'" + (" AND transaction_type='purchase'" if args.metric=='purchase_expense' else '')
    totals = dict(connection.execute(f'''SELECT
        COALESCE(SUM(CASE WHEN {predicate} THEN amount_cents ELSE 0 END),0) amount_cents,
        SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) matched_count, COUNT(*) recorded_count,
        COALESCE(SUM(CASE WHEN transaction_type='refund' THEN amount_cents ELSE 0 END),0) refund_cents,
        SUM(CASE WHEN transaction_type='refund' THEN 1 ELSE 0 END) refund_count,
        SUM(CASE WHEN transaction_type='refund' AND refund_of_transaction_id IS NULL THEN 1 ELSE 0 END) unlinked_refund_count,
        COALESCE(SUM(CASE WHEN transaction_type='unknown' THEN amount_cents ELSE 0 END),0) unclassified_cents,
        SUM(CASE WHEN transaction_type='unknown' THEN 1 ELSE 0 END) unclassified_count
        '''+source,params).fetchone())
    totals = {k:v or 0 for k,v in totals.items()}
    evidence = []
    # Aggregates are SQL-based; evidence is bounded even for a large ledger.
    for r in connection.execute('SELECT t.*'+source+' ORDER BY t.date,t.transaction_id LIMIT 100',params):
        evidence.append(dict(transaction_id=r['transaction_id'],card_id=r['card_id'],date=r['date'],
            merchant=_safe_text(r['merchant']),transaction_type=r['transaction_type'],classification_source=_safe_text(r['classification_source']),
            direction=r['direction'],amount_cents=r['amount_cents'],refund_of_transaction_id=r['refund_of_transaction_id']))
    return dict(**totals,range=span,evidence=evidence,evidence_truncated=totals['recorded_count']>100,
        classification='no_records' if not totals['recorded_count'] else 'partially_classified' if totals['unclassified_count'] else 'known_recorded',
        ledger_sha256=hashlib.sha256(json.dumps(dict(totals=totals,evidence=evidence),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest())


def _query(connection, args, principal):
    aliases = None
    key = None
    if args.merchant is not None:
        row = connection.execute('SELECT merchant_key FROM merchant_aliases WHERE alias=?',(args.merchant,)).fetchone()
        if row:
            key = row[0]
            aliases = [r[0] for r in connection.execute('SELECT alias FROM merchant_aliases WHERE merchant_key=? ORDER BY alias',(key,))]
        else:
            scope = owner_scope(principal)
            exact = connection.execute('''SELECT 1 FROM visible_transactions t JOIN cards c ON c.card_id=t.card_id
                WHERE t.owner_id=? AND c.owner_id=? AND t.merchant=? LIMIT 1''',(scope,scope,args.merchant)).fetchone()
            if exact is None:
                raise AnalysisError('query_scope_unclear')
            # An exact recorded name is usable as itself. It never establishes
            # another alias or a purchase classification.
            aliases = [args.merchant]
    current = _query_period(connection,args,principal,args.start,args.end_exclusive,aliases)
    comparison = None
    if args.compare_previous:
        from business_calendar import month_before
        base = _query_period(connection,args,principal,month_before(args.start[:7])+'-01',args.start,aliases)
        change = current['amount_cents']-base['amount_cents']
        comparison = dict(base=base,change_cents=change,percentage=_pct(change,base['amount_cents']),
                          percentage_reason='zero_denominator' if not base['amount_cents'] else None)
    merchant = _safe_text(args.merchant) if args.merchant else None
    label = '购买支出总额' if args.metric=='purchase_expense' else '已记录现金流出（包含转账、申购及未知类型支出）'
    # Fixed formatting uses cents throughout; never asks the model to polish facts.
    def money(n):
        whole,minor=divmod(abs(n),100)
        # Group very large displayed amounts so plain digit strings cannot be
        # confused with account numbers by the unchanged sensitive-text guard.
        major=f'{whole:,}' if whole>=100_000_000_000 else str(whole)
        return ('-' if n<0 else '')+f'{major}.{minor:02d}'
    text = (f"上海期间 [{args.start}, {args.end_exclusive})，商户 {merchant or '全部'}，账户 {args.card_id or '全部归属卡'}："
            f"{label} {money(current['amount_cents'])} 元，{current['matched_count']} 笔；"
            f"退款单列 {money(current['refund_cents'])} 元，{current['refund_count']} 笔，其中未关联 {current['unlinked_refund_count']} 笔。"
            f"范围内记录 {current['recorded_count']} 笔，未知类型 {current['unclassified_count']} 笔/{money(current['unclassified_cents'])} 元。")
    if comparison:
        b = comparison['base']
        text += (f"比较期间 [{b['range']['start']}, {b['range']['end_exclusive']}) 同商户、账户及口径 {money(b['amount_cents'])} 元，"
                 f"差额 {money(comparison['change_cents'])} 元；退款 {money(b['refund_cents'])} 元，未知类型 {b['unclassified_count']} 笔。")
        if comparison['percentage'] is None:
            text += '比较基数为零，不计算百分比。'
    if not current['recorded_count']:
        text += '已记录数据中无相关流水，不能断言实际零消费。'
    text += '仅统计已记录数据，实际覆盖未知；未知类型不能认定购买。退款不扣减，不支持精确净消费。'
    return dict(metric=args.metric,merchant=merchant,merchant_key=key,aliases=aliases or [],current=current,comparison=comparison,explanation=text)


CATALOG = {
    'query_financial_records': (QueryArgs, _query, QueryResult, 'bill'),
    'get_monthly_cash_flow_report': (MonthReportArgs, _report, Report, 'bill'),
    'get_annual_cash_flow_report': (YearReportArgs, _report, Report, 'bill'),
    'get_expense_clues': (AnomalyArgs, _anomalies, Anomalies, 'bill'),
    'compare_wealth_products': (CompareArgs, _compare, ProductComparison, 'wealth'),
    'explain_wealth_suitability': (SuitabilityArgs, _suitability, Suitability, 'wealth'),
}


@financial_read
def analyze(name, arguments, principal=None):
    if name not in CATALOG:
        return dict(success=False, error_code='unknown_analysis_tool', message='未知分析工具', data=None)
    model, handler, result_model, _ = CATALOG[name]
    if principal is None and name not in {'compare_wealth_products', 'explain_wealth_suitability'}:
        return dict(success=False, error_code='principal_required', message='缺少服务器端身份', data=None)
    connection = None
    try:
        args = model.model_validate(arguments.model_dump() if hasattr(arguments, 'model_dump') else arguments)
        connection = get_connection()
        connection.execute('BEGIN')
        data = result_model.model_validate(handler(connection, args, principal)).model_dump(mode='json')
        return make_result(True, data.get('explanation', '适当性事实查询完成'), data)
    except ValidationError:
        return dict(success=False, error_code='invalid_analysis_arguments', message='分析字段校验失败', data=None)
    except AnalysisError as error:
        return dict(success=False, error_code=error.code,
            message='商户别名未登记，请明确已登记的商户名称；尚未执行查询。' if error.code=='query_scope_unclear' else error.code, data=None)
    except sqlite3.Error:
        return dict(success=False, error_code='analysis_data_unavailable', message='分析数据不可用', data=None)
    finally:
        if connection:
            connection.close()


def verify_result(result):
    try:
        data = ADAPTER.validate_python(result.data)
        # Evidence identity, bounds and trigger values supplement schema/arithmetic checks.
        if isinstance(data, Anomalies):
            ids = [c.transaction.transaction_id for c in data.clues]
            if len(ids) != len(set(ids)) or data.matched_transaction_count < len(ids):
                return False, 'duplicate_clue'
            for clue in data.clues:
                for trigger in clue.triggers:
                    if clue.transaction.transaction_id not in trigger.transaction_ids or trigger.actual < trigger.threshold:
                        return False, 'trigger_fact_mismatch'
        if isinstance(data, Suitability):
            expected = None if data.user_risk_level is None else data.user_risk_level >= data.product_risk_level
            if data.risk_matches != expected:
                return False, 'risk_fact_mismatch'
        return True, '分析Schema和关键事实核验通过'
    except (ValidationError, ValueError, TypeError):
        return False, 'analysis_verification_failed'
