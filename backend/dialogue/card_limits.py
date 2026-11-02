"""Read existing execution limit bases; never grant or promise a payable amount."""
from decimal import Decimal
from security.config import BUSINESS_TIMEZONE
from security.service import get_security_services
from .store import transaction


def enrich(cards):
    now=get_security_services().now_provider().astimezone(BUSINESS_TIMEZONE)
    day=now.date().isoformat()
    with transaction() as c:
        rows=c.execute('''SELECT k.public_id AS card_id,a.posted_minor-a.held_minor AS balance_cents,
            k.daily_limit_minor AS daily_limit_cents,k.per_tx_limit_minor AS per_tx_limit_cents,
            coalesce((SELECT sum(o.amount_cents) FROM pg02.transfer_orders o
                WHERE o.owner_uuid=core.require_owner() AND o.from_card_id=k.public_id
                AND o.status='completed'
                AND coalesce(o.business_date,substr(o.completed_at,1,10))=%s),0) AS used
            FROM core.cards k JOIN core.accounts a ON a.id=k.account_id AND a.owner_id=k.owner_id
            WHERE k.owner_id=core.require_owner()''',(day,)).fetchall()
    by_id={r['card_id']:r for r in rows}
    money=lambda n:format(Decimal(n)/100,'.2f')
    result=[]
    for card in cards:
        row=by_id.get(card['card_id'])
        if row is None:continue
        result.append(dict(card,balance=float(money(row['balance_cents'])),daily_limit=float(money(row['daily_limit_cents'])),
            per_tx_limit=money(row['per_tx_limit_cents']),daily_used=money(row['used']),
            daily_remaining=money(max(0,row['daily_limit_cents']-row['used'])),limit_business_date=day))
    return result
