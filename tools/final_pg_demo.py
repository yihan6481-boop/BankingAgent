"""Deterministic author data, controlled migrator import, independent read-only audit."""
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import random
import secrets
from uuid import UUID, uuid5

from final_pg import ROOT, local_config

VERSION='SZ-PG-FINAL-1'
SNAPSHOT='2026-10-15'
SPEC=ROOT/'demo/spec/demo_data_design_v1.json'
STAMP='2026-10-15T12:00:00+08:00'

def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def design(): return json.loads(SPEC.read_text(encoding='utf-8'))

def generate():
    d=design(); rng=random.Random(d['transaction_generation_seed'])
    users={u['key']:u for u in d['users']}
    cards={c['key']:dict(c,public_id=n) for n,c in enumerate(d['cards'],1)}
    byuser={u:[c for c in cards.values() if c['owner_key']==u] for u in users}
    rows=[]
    fixed={'房租':5,'个人承担房租':5,'房贷':15,'车贷':20,'电动车租赁':8,
           '托育':6,'托管':6,'给母亲生活费':28,'给母亲带娃补贴':28,'父母生活费':28,
           '给父母生活费':28,'给妻子家庭开支':10,'手机费':9,'通信':9,'工资':10,'公司薪酬':10,
           '配送结算1':5,'配送结算2':15,'配送结算3':25,'绩效':22}
    merchants={'餐饮':['南山快餐店','社区面馆','沙县小吃','深圳茶餐厅'],
      '餐饮买菜':['社区生鲜店','钱大妈','家常菜馆','深圳菜市场'],
      '交通':['深圳地铁','深圳公交','网约车'],'购物':['淘宝','京东','社区百货'],
      '超市':['永辉超市','社区超市'],'日用品':['社区百货','便利店'],
      '婴儿用品':['母婴用品店','京东母婴'],'充电':['骑手充电站','社区充电柜']}
    def append(owner,month,day,category,amount,direction,kind,merchant,card=None,**detail):
        c=card or byuser[owner][0]
        key=f"{d['dataset_key']}:{owner}:{month}:{len(rows)+1:05}"
        rows.append(dict(key=key,owner_key=owner,owner_uuid=users[owner]['id'],card_id=c['public_id'],
            date=f'2026-{month:02}-{day:02}',category=category,amount=amount,direction=direction,
            kind=kind,merchant=merchant,detail=detail))
    for owner,groups in d['monthly_category_targets'].items():
        for month in [7,8,9,10]:
            maxday=15 if month==10 else (31 if month in [7,8] else 30)
            for flow,direction in [('inflows','income'),('outflows','expense')]:
                for item in groups[flow]:
                    cat=item['category']; total=item['amount_minor']
                    if month!=9:
                        factor={7:92,8:105,10:100}[month]
                        if direction=='expense' and cat not in fixed: total=total*factor//100
                        if owner=='U01' and cat=='工资' and month==7:
                            total=900000*27//31  # author start 2026-07-05, calendar-day proration
                        if owner=='U02' and cat=='绩效': total={7:120000,8:210000,10:160000}[month]
                        if owner=='U03' and cat=='妻子家庭转入': total={7:200000,8:300000,10:400000}[month]
                        if owner=='U05' and cat.startswith('配送结算'):
                            total=total*{7:94,8:106,10:101}[month]//100
                    day=fixed.get(cat)
                    if cat=='工资' and owner=='U02': day=1  # simulated school payroll
                    if cat=='工资' and owner=='U03': day=5  # simulated employer payroll
                    if day and day>maxday: continue
                    if day: pieces=[(day,total)]
                    elif cat=='退款': pieces=[(min(maxday,26),total)]
                    elif direction=='income': pieces=[(min(maxday,18),total)]
                    else:
                        n=24 if cat.startswith('餐饮') else 8 if cat in merchants else 2
                        n=min(n,maxday)
                        if month==10: total=total*15//30
                        weights=[rng.randrange(70,131) for _ in range(n)]
                        amounts=[total*w//sum(weights) for w in weights]
                        amounts[-1]+=total-sum(amounts)
                        pieces=[(1+i*(maxday-1)//max(1,n-1),a) for i,a in enumerate(amounts)]
                    if cat=='退款': kind='refund'
                    elif direction=='income': kind='income'
                    elif ('生活费' in cat or '补贴' in cat or '家庭开支' in cat or cat in {'礼金','房贷','车贷'}): kind='transfer'
                    else: kind='purchase'
                    card=byuser[owner][0]
                    if owner=='U04' and cat in {'给妻子家庭开支','孩子教育'}: card=byuser[owner][2]
                    if owner=='U02' and cat=='婴儿用品': card=byuser[owner][2]
                    for i,(day,amount) in enumerate(pieces):
                        merchant=(merchants.get(cat,[cat])[i%len(merchants.get(cat,[cat]))])
                        if cat=='退款': merchant='淘宝'
                        append(owner,month,day,cat,amount,direction,kind,merchant,card=card)
            if month==9:
                cf=next(t for t in d['monthly_cashflow_targets'] if t['owner_key']==owner)
                position=next(t for t in d['position_principal_targets'] if t['owner_key']==owner)
                if owner=='U02': position=d['position_principal_targets'][2]
                for kind,amount,day in [('wealth_redeem',cf['investment_redemption_minor'],12),('wealth_buy',cf['investment_purchase_minor'],23)]:
                    if amount:
                        product=next(p for p in d['product_references'] if p['key']==position['product_key'])
                        append(owner,month,day,'理财赎回' if kind=='wealth_redeem' else '理财买入',amount,
                            'income' if kind=='wealth_redeem' else 'expense',kind,product['reference_name'],
                            product_key=position['product_key'])
    # Employment/moving and family summer expenses are explicit July events,
    # independent of the frozen September categories, not unexplained adjustments.
    append('U01',7,6,'租房押金',240000,'expense','transfer','赵丽（模拟租房押金）')
    append('U01',7,16,'入职笔记本电脑',598000,'expense','purchase','京东（模拟办公设备）')
    append('U01',8,18,'暑期探亲交通住宿',320000,'expense','purchase','铁路及民宿（模拟探亲）')
    append('U03',7,18,'家庭暑期出游',620000,'expense','purchase','深圳旅行服务（模拟）')
    # Link every refund to an actual earlier purchase without adding a balancing row.
    for r in rows:
        if r['kind']=='refund':
            choices=[p for p in rows if p['owner_key']==r['owner_key'] and p['kind']=='purchase'
                     and p['category'] in {'购物','婴儿用品','日用品'} and p['date'][:7]==r['date'][:7]
                     and p['date']<=r['date'] and p['amount']>=r['amount']]
            # Boss refund can exceed a single split purchase: merge two planned
            # shopping instalments, preserving the category/month total exactly.
            if not choices:
                candidates=[p for p in rows if p['owner_key']==r['owner_key'] and p['category'] in {'购物','婴儿用品','日用品'}
                            and p['date'][:7]==r['date'][:7] and p['date']<=r['date']]
                first=candidates[0]
                for p in candidates[1:]:
                    if first['amount']>=r['amount']: break
                    take=min(p['amount']-1,r['amount']-first['amount'])
                    first['amount']+=take;p['amount']-=take
                assert first['amount']>=r['amount']
                choices=[first]
            purchase=choices[0];purchase['merchant']='淘宝'
            r['merchant']='淘宝';r['refund_key']=purchase['key']
    rows.sort(key=lambda r:(r['date'],r['direction']=='expense',r['key']))
    delta=lambda r:r['amount']*(1 if r['direction']=='income' else -1)
    initial={};september={};current={};reconciliation=[]
    accounts={a['key']:a for a in d['personal_accounts']}
    for c in cards.values():
        cid=c['public_id']; target=accounts[c['account_key']]['posted_minor']
        history=[r for r in rows if r['card_id']==cid]
        opening=target-sum(delta(r) for r in history if r['date']<'2026-10-01')
        assert opening>=0,(c['key'],'negative opening',opening)
        initial[cid]=opening;balance=opening
        for month in [7,8,9,10]:
            mr=[r for r in history if r['date'].startswith(f'2026-{month:02}')]
            before=balance
            for r in mr:
                r['detail']['bank_before_minor']=balance
                balance+=delta(r)
                assert balance>=0,(c['key'],r['date'],'overdraft',balance)
                r['detail']['bank_after_minor']=balance
            reconciliation.append(dict(card_id=cid,key=c['key'],month=f'2026-{month:02}',opening=before,
                inflow=sum(r['amount'] for r in mr if r['direction']=='income'),
                outflow=sum(r['amount'] for r in mr if r['direction']=='expense'),closing=balance))
            if month==9: assert balance==target
        current[cid]=balance;september[cid]=target
    positions=[]
    for n,p in enumerate(d['position_principal_targets'],1):
        events=[r for r in rows if r['owner_key']==p['owner_key'] and r['detail'].get('product_key')==p['product_key']]
        initial_principal=p['principal_minor']-sum(r['amount']*(1 if r['kind']=='wealth_buy' else -1) for r in events)
        principal=initial_principal
        for r in events:
            r['detail'].update(holding_id=n,principal_before_minor=principal)
            principal+=r['amount']*(1 if r['kind']=='wealth_buy' else -1)
            r['detail']['principal_after_minor']=principal
        assert principal==p['principal_minor']
        positions.append(dict(p,holding_id=n,card_id=byuser[p['owner_key']][0]['public_id'],opening=initial_principal))
    # September oracle is read from the accepted file, independently of generator totals.
    for owner,groups in d['monthly_category_targets'].items():
        sep=[r for r in rows if r['owner_key']==owner and r['date'].startswith('2026-09')]
        for flow,direction in [('inflows','income'),('outflows','expense')]:
            actual=Counter()
            for r in sep:
                if r['direction']==direction and not r['kind'].startswith('wealth_'): actual[r['category']]+=r['amount']
            assert actual==Counter({x['category']:x['amount_minor'] for x in groups[flow]})
    return dict(design=d,cards=cards,rows=rows,opening=initial,current=current,september=september,
                reconciliation=reconciliation,positions=positions,generator_version=VERSION,
                recipient_mapping={x['key']:dict(source_ref=x['synthetic_destination_ref'],
                    simulated_account='990000000000'+x['key'][1:].zfill(4),pre_registered=True)
                    for x in d['contacts'] if x['owner_scope_key']!='ORG01'},
                execution_profiles=dict(risk=['R1','R1','R2','R3','R4'],minimum=[100,100,10000,10000,10000],rate_bps=[150,160,210,240,280]))

def provision():
    from psycopg import sql
    from storage.postgres import connect
    from storage.migrations import migrate
    with connect(local_config('postgres','admin')) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname='banking_demo'").fetchone():
            c.execute('CREATE DATABASE banking_demo TEMPLATE template0')
            c.execute('REVOKE ALL ON DATABASE banking_demo FROM PUBLIC')
            c.execute('GRANT CONNECT,CREATE ON DATABASE banking_demo TO banking_migrator')
            c.execute('GRANT CONNECT ON DATABASE banking_demo TO banking_runtime')
    with connect(local_config('banking_demo','admin')) as c:
        c.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
    migrate(local_config(role='migration'),apply=True)

def reset_owned_initial():
    """Explicit reset of this task's new, unused Demo only; never foundation."""
    from storage.postgres import connect
    with connect(local_config(role='migration')) as c,c.transaction():
        c.execute('SET TRANSACTION READ ONLY')
        row=c.execute('SELECT dataset_key FROM ops.demo_datasets').fetchall()
        if row!=[{'dataset_key':'sz-banking-demo-v1'}]:raise ValueError('not_owned_initial_dataset')
        for table in ['transfer_orders','wealth_orders','mutation_orders','lifecycle_tasks','auth_sessions','authenticator_factors']:
            if c.execute('SELECT count(*) AS n FROM pg02.'+table).fetchone()['n']:
                raise ValueError('demo_has_runtime_operations_reset_refused')
    with connect(local_config('postgres','admin')) as c:
        c.execute('DROP DATABASE banking_demo')
    login=ROOT/'demo/LOCAL_LOGINS.md'
    if login.is_file():login.unlink()

def seed(config=None, *, password_for=None, include_legacy_memory=True):
    from storage.postgres import connect
    from security.identity import hash_password
    from psycopg.types.json import Jsonb
    g=generate();d=g['design'];content_hash=digest(g if include_legacy_memory else dict(mode='native_public_without_legacy_memory',data=g))
    namespace=UUID(d['deterministic_namespace'])
    uid=lambda key:str(uuid5(namespace,d['dataset_key']+':'+key))
    usermap={u['key']:dict(u,public_id=n) for n,u in enumerate(d['users'],1)}
    login_file=ROOT/'demo/LOCAL_LOGINS.md'
    with connect(config or local_config(role='migration')) as c,c.transaction():
        c.execute('SELECT pg_advisory_xact_lock(174835501)')
        previous=c.execute('SELECT content_hash FROM ops.demo_datasets WHERE dataset_key=%s',(d['dataset_key'],)).fetchone()
        if previous:
            if previous['content_hash']!=content_hash: raise ValueError('dataset_content_conflict')
            return dict(status='unchanged',dataset_key=d['dataset_key'],content_hash=content_hash)
        manifest=dict(dataset_key=d['dataset_key'],version=VERSION,snapshot_date=SNAPSHOT,
          coverage=['2026-07-01','2026-10-16'],spec_sha256=hashlib.sha256(SPEC.read_bytes()).hexdigest(),
          transaction_rows_status='generated_and_imported',login_credentials_status='generated_hashed',
          persona_memory_status='loaded_retractable_author_sources' if include_legacy_memory else 'not_imported_native_public_demo',
          future_modules=d['future_modules'],biometric_data_status='none',automatic_payment_authorizations=[],
          employee_roster_status='background_only_not_required',execution_model='PG04 principal only; simulated R1-R5, minimum and reference rate',
          july_employment_start='2026-07-05',july_salary_proration='900000 * 27 // 31 calendar days',
          row_count=len(g['rows']),reconciliation=g['reconciliation'],positions=g['positions'])
        c.execute('INSERT INTO ops.demo_datasets(dataset_key,content_hash,generator_version,manifest) VALUES(%s,%s,%s,%s)',
                  (d['dataset_key'],content_hash,VERSION,Jsonb(manifest)))
        logins=[]
        for key,u in usermap.items():
            c.execute('INSERT INTO core.users(id,public_id,display_name) VALUES(%s,%s,%s)',(u['id'],u['public_id'],u['name']))
            c.execute("INSERT INTO pg02.principals VALUES(%s,%s,'simulated_user',%s,'active',%s,%s)",
                (uid(key+':principal'),u['public_id'],u['name'],STAMP,u['id']))
            password=password_for(key) if password_for else 'Demo!'+secrets.token_urlsafe(12)
            salt,h,algorithm,iters=hash_password(password,210000)
            username='demo'+str(u['public_id'])
            c.execute('''INSERT INTO pg02.credentials(credential_id,principal_id,username,password_salt,password_hash,
                hash_algorithm,hash_iterations,created_at,owner_uuid) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (uid(key+':credential'),uid(key+':principal'),username,salt,h,algorithm,iters,STAMP,u['id']))
            logins.append(f"| {u['name']} | {username} | {password} |")
            c.execute('INSERT INTO pg02.user_risk_profiles VALUES(%s,%s,%s,%s)',
                      (u['public_id'],{'U01':'R2','U02':'R3','U03':'R3','U04':'R4','U05':'R2'}[key],STAMP,u['id']))
        accounts={a['key']:a for a in d['personal_accounts']}
        for card in g['cards'].values():
            a=accounts[card['account_key']];u=usermap[card['owner_key']];cid=card['public_id']
            c.execute('INSERT INTO core.accounts(id,public_id,owner_id,posted_minor,held_minor) VALUES(%s,%s,%s,%s,%s)',
                      (a['id'],cid,u['id'],g['current'][cid],a['held_minor']))
            c.execute('''INSERT INTO core.cards(id,public_id,owner_id,account_id,masked_number,status,daily_limit_minor,per_tx_limit_minor,bank_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(card['id'],cid,u['id'],a['id'],'**** '+card['display_tail'],
                card['status'],card['daily_limit_minor'],card['per_tx_limit_minor'],card['bank_display_name']))
        for contact in d['contacts']:
            if contact['owner_scope_key']=='ORG01': continue
            u=usermap[contact['owner_scope_key']]
            c.execute('''INSERT INTO pg02.recipients(recipient_id,name,bank_name,account_number,status,trusted,created_at,owner_uuid,owner_id,id)
                VALUES(%s,%s,%s,%s,'active',1,%s,%s,%s,%s)''',(int(contact['key'][1:]),contact['display_name'],
                '模拟目标·'+contact['relationship'],g['recipient_mapping'][contact['key']]['simulated_account'],STAMP,u['id'],u['public_id'],contact['id']))
        for n,p in enumerate(d['product_references'],1):
            c.execute('INSERT INTO pg02.wealth_products VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (n,p['reference_name'],['R1','R1','R2','R3','R4'][n-1],[100,100,10000,10000,10000][n-1],'active',[150,160,210,240,280][n-1],0))
        for p in g['positions']:
            u=usermap[p['owner_key']]
            c.execute('''INSERT INTO pg02.wealth_holdings(holding_id,user_id,product_id,card_id,principal_cents,status,purchased_at,updated_at,owner_uuid)
                OVERRIDING SYSTEM VALUE VALUES(%s,%s,%s,%s,%s,'active',%s,%s,%s)''',
                (p['holding_id'],u['public_id'],int(p['product_key'][1:]),p['card_id'],p['principal_minor'],'2026-07-01T00:00:00+08:00',STAMP,u['id']))
        c.execute("SELECT setval('pg02.wealth_holdings_holding_id_seq',6)")
        # Full profiles, contact context, product references and isolated enterprise
        # background are ordinary retractable, owner-bound author source messages.
        sources=[]
        for key,u in usermap.items():
            private={k:v for k,v in u.items() if k!='public_id'}
            private['family_relationships']=[x for x in d['contacts'] if x['associated_user_key']==key and x['owner_scope_key']!='ORG01']
            private['accounts']=[x for x in d['personal_accounts'] if x['owner_key']==key]
            private['cards']=[x for x in d['cards'] if x['owner_key']==key]
            private['loan_background']=[x for x in d['personal_loan_targets'] if x['owner_key']==key]
            if key=='U01': private['employment_start']='2026-07-05'
            # Keep messages bounded for existing memory/UI contract.
            for category,value in private.items():
                sources.append((key,'PROFILE:'+key+':'+category,canonical({category:value})))
            if key=='U04':
                sources.append((key,'ORG01',canonical(d['business_background'])))
                for x in d['contacts']:
                    if x['owner_scope_key']=='ORG01': sources.append((key,x['key'],canonical(x)))
        for p in d['product_references']:
            for key in usermap: sources.append((key,'REFERENCE:'+p['key'],canonical(p)))
        sources += [(f['owner_key'],f['key'],f['text']) for f in d['persona_memory_source']]
        for n,(key,source,text) in enumerate(sources if include_legacy_memory else [],1):
            u=usermap[key];mid=uid(key+':source:'+source)
            content='[作者模拟资料 '+source+'；'+VERSION+'；不是用户授权] '+text
            c.execute('''INSERT INTO pg02.memory_messages(id,owner_id,conversation_id,sequence,role,content,created_at,updated_at,status,supersedes,owner_uuid)
             VALUES(%s,%s,%s,%s,'assistant',%s,%s,%s,'active',NULL,%s)''',
             (mid,u['public_id'],uid(key+':author'),n,content,STAMP,STAMP,u['id']))
            c.execute('INSERT INTO pg02.memory_sources VALUES(%s,%s,%s,%s,%s)',(u['id'],mid,source,'demo_persona_fixture',VERSION))
        for key,u in usermap.items():
            card=next(ca for ca in g['cards'].values() if ca['owner_key']==key)
            category='手机费' if key in {'U01','U02'} else '通信'
            amount=next(x['amount_minor'] for x in d['monthly_category_targets'][key]['outflows'] if x['category']==category)
            c.execute('INSERT INTO pg02.subscriptions VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (u['public_id'],u['public_id'],'模拟通信月套餐·'+u['name'],amount,'monthly','2026-11-09',card['public_id'],'active',key+':mobile',VERSION,u['id']))
        # Opening bank/principal states are snapshots, never artificial income.
        for cid,opening in g['opening'].items():
            card=next(x for x in g['cards'].values() if x['public_id']==cid)
            c.execute('INSERT INTO pg02.demo_events(event_key,dataset_key,owner_uuid,card_id,event_kind,amount_cents,business_date,source_version,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (d['dataset_key']+':opening:'+str(cid),d['dataset_key'],usermap[card['owner_key']]['id'],cid,'opening',opening,'2026-07-01',VERSION,Jsonb({'purpose':'opening snapshot, not income'})))
        ids={}
        for r in g['rows']:
            c.execute('INSERT INTO pg02.demo_events(event_key,dataset_key,owner_uuid,card_id,event_kind,amount_cents,business_date,source_version,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (r['key'],d['dataset_key'],r['owner_uuid'],r['card_id'],r['kind'],r['amount'],r['date'],VERSION,Jsonb(r['detail'])))
            row=c.execute('''INSERT INTO pg02.transactions(card_id,date,merchant,category,amount_cents,direction,owner_uuid,owner_id,transaction_type,
                classification_source,external_source_id,refund_of_transaction_id,merchant_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'demo_seed_v1',%s,%s,%s) RETURNING transaction_id''',
                (r['card_id'],r['date'],r['merchant'],r['category'],r['amount'],r['direction'],r['owner_uuid'],usermap[r['owner_key']]['public_id'],
                 r['kind'],r['key'],ids.get(r.get('refund_key')),'taobao' if r['merchant']=='淘宝' else None)).fetchone()
            ids[r['key']]=row['transaction_id']
            c.execute('UPDATE pg02.demo_events SET transaction_id=%s WHERE event_key=%s',(row['transaction_id'],r['key']))
        if config is None:
            # Write once before transaction commit; no plaintext in logs or DB.
            with login_file.open('x',encoding='utf-8') as f:
                f.write('# 五人本地演示登录\n\n仅供本机虚构 Demo；初始口令独立随机生成，数据库只存 PBKDF2 哈希。\n\n| 姓名 | 用户名 | 初始密码 |\n|---|---|---|\n'+'\n'.join(logins)+'\n\nTOTP 请登录后经原验证器登记流程绑定；没有万能验证码。\n')
    return dict(status='imported',dataset_key=d['dataset_key'],content_hash=content_hash,rows=len(g['rows']))

def validate(config=None):
    from storage.postgres import connect
    g=generate();d=g['design'];evidence=[]
    # Dedicated migrator read-only snapshot for complete dataset reconciliation.
    with connect(config or local_config(role='migration')) as c,c.transaction():
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        registry=c.execute('SELECT * FROM ops.demo_datasets WHERE dataset_key=%s',(d['dataset_key'],)).fetchone()
        assert registry and registry['content_hash']==digest(g)
        actual_rows=c.execute('''SELECT t.*,p.external_source_id AS refund_key FROM pg02.transactions t
            LEFT JOIN pg02.transactions p ON p.transaction_id=t.refund_of_transaction_id AND p.owner_uuid=t.owner_uuid
            WHERE t.classification_source='demo_seed_v1' ''').fetchall()
        expected_rows={r['key']:r for r in g['rows']}
        assert len(actual_rows)==len(expected_rows)
        for row in actual_rows:
            r=expected_rows[row['external_source_id']]
            assert (row['card_id'],row['date'],row['merchant'],row['category'],row['amount_cents'],row['direction'],row['transaction_type'],row['refund_key'])==(
                r['card_id'],r['date'],r['merchant'],r['category'],r['amount'],r['direction'],r['kind'],r.get('refund_key'))
            event=c.execute('SELECT transaction_id,detail FROM pg02.demo_events WHERE event_key=%s',(r['key'],)).fetchone()
            assert event['transaction_id']==row['transaction_id'] and event['detail']==r['detail']
        for card in g['cards'].values():
            cid=card['public_id']
            actual=c.execute('SELECT * FROM core.accounts WHERE public_id=%s',(cid,)).fetchone()
            card_row=c.execute('SELECT * FROM core.cards WHERE public_id=%s',(cid,)).fetchone()
            assert (str(card_row['id']),card_row['bank_name'],card_row['masked_number'],card_row['daily_limit_minor'],card_row['per_tx_limit_minor'])==(
                card['id'],card['bank_display_name'],'**** '+card['display_tail'],card['daily_limit_minor'],card['per_tx_limit_minor'])
            imported=c.execute('SELECT COALESCE(sum(CASE WHEN direction=\'income\' THEN amount_cents ELSE -amount_cents END),0) AS delta FROM pg02.transactions WHERE card_id=%s',(cid,)).fetchone()['delta']
            assert actual['posted_minor']==g['opening'][cid]+imported,(card['key'],'current reconcile')
            for row in [r for r in g['reconciliation'] if r['card_id']==cid]:
                sep_delta=c.execute('SELECT COALESCE(sum(CASE WHEN direction=\'income\' THEN amount_cents ELSE -amount_cents END),0) AS delta FROM pg02.transactions WHERE card_id=%s AND date<%s',(cid,row['month']+'-01')).fetchone()['delta']
                assert row['opening']==g['opening'][cid]+sep_delta
                evidence.append(row)
        for uid,u in enumerate(d['users'],1):
            for flow,direction in [('inflows','income'),('outflows','expense')]:
                expected={x['category']:x['amount_minor'] for x in d['monthly_category_targets'][u['key']][flow]}
                actual=c.execute("SELECT category,sum(amount_cents) AS n FROM pg02.transactions WHERE owner_id=%s AND date>='2026-09-01' AND date<'2026-10-01' AND direction=%s AND transaction_type NOT IN ('wealth_buy','wealth_redeem') GROUP BY category",(uid,direction)).fetchall()
                assert {r['category']:r['n'] for r in actual}==expected
        facts=[]
        for fact in d['persona_memory_source']:
            row=c.execute('''SELECT m.content,s.source_key,m.owner_id,m.status FROM pg02.memory_sources s JOIN pg02.memory_messages m
                ON m.owner_uuid=s.owner_uuid AND m.id=s.message_id WHERE s.source_key=%s''',(fact['key'],)).fetchone()
            assert row and fact['text'] in row['content'] and row['status']=='active'
            facts.append(dict(row))
        counts={t:c.execute('SELECT count(*) AS n FROM '+t).fetchone()['n'] for t in
                ['core.users','core.accounts','core.cards','pg02.recipients','pg02.wealth_products','pg02.wealth_holdings',
                 'pg02.transactions','pg02.subscriptions','pg02.memory_messages','pg02.memory_sources','pg02.demo_events']}
        assert [counts[t] for t in ['core.users','core.accounts','core.cards','pg02.recipients','pg02.wealth_products','pg02.wealth_holdings']]==[5,13,13,43,5,6]
        for p in g['positions']:
            row=c.execute('SELECT principal_cents FROM pg02.wealth_holdings WHERE holding_id=%s',(p['holding_id'],)).fetchone()
            assert row['principal_cents']==p['principal_minor']
        return dict(status='passed',database='banking_demo',snapshot_date=SNAPSHOT,coverage=['2026-07-01','2026-10-16'],counts=counts,
                    card_months=evidence,facts=facts,positions=g['positions'],source_top_level_keys=list(d),
                    content_hash=registry['content_hash'])

def manage(command):
    if command=='init': provision()
    if command in {'init','seed'}: print(canonical(seed()))
    if command in {'init','validate'}:
        result=validate()
        print(canonical({k:v for k,v in result.items() if k not in {'facts','card_months','positions','source_top_level_keys'}}))
    if command=='ready':
        import os
        os.environ.update(BA_STORAGE_BACKEND='postgres',BA_PG_DSN=local_config().dsn)
        from storage.pg02 import readiness
        print(canonical(readiness()))
    return 0
