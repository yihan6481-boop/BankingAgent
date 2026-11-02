"""Reviewed invoice -> existing private bookkeeping REVIEW, with no debit.

No second OCR call, inferred payment date or success status. The user still
reviews missing date/precision and explicitly confirms the existing bill ledger.
"""
from uuid import uuid4
import hashlib,json
from payments.service import transaction,Jsonb
from payments.documents import _document,_digest,_extraction
from payments.models import selected_amount_cents
from payments.errors import PaymentError
from billvision.models import Candidate
from billvision.service import public,save_version,invoice_source_current

def prepare(principal,document_id,revision,digest):
    with transaction(principal,True) as c:
        d=_document(c,document_id,lock=True);extraction=_extraction(d)
        if d['revision']!=revision or _digest(d,extraction)!=digest:raise PaymentError('document_version_changed','发票版本已变化，请重新读取核对',409)
        if d['kind']!='invoice':raise PaymentError('invoice_required','仅发票可进入此记账核对入口',422)
        if not d['user_confirmed_own_bill']:raise PaymentError('own_invoice_confirmation_required','请先确认这是本人的发票并保存核对内容',422)
        old=c.execute('SELECT * FROM billvision.documents WHERE image_hash=%s FOR UPDATE',(d['image_hash'],)).fetchone()
        if old:
            if old['platform']!='invoice':raise PaymentError('existing_bill_review','该图片已有其他账单记录，请先打开原记录核对',409)
            if not invoice_source_current(c,old):raise PaymentError('invoice_source_changed','原发票已改变，旧记账来源失效；请先在记账页核对并明确删除旧导入，再按当前版本重新办理',409)
            return dict(document=public(c,old,True),replayed=True,bank_ledger_unchanged=True)
        number=extraction.bill_number.value;institution=extraction.institution.value
        event=hashlib.sha256(json.dumps(['invoice',institution,number,extraction.period_label.value],ensure_ascii=False).encode()).hexdigest() if number and institution else None
        candidate=Candidate(merchant=institution,amount_cents=selected_amount_cents(extraction,list(d['selected_item_ids'])),currency='CNY',direction='expense',kind='purchase',status='pending',precision='unknown',event_number=event,evidence='已核对的发票来源；付款状态未核实，日期及各项内容仍须本人逐项核对。')
        rows=[dict(row_id='r1',value=candidate.model_dump(),included=True,reviewed=False,duplicate_resolution='unresolved')]
        identifier='bv_'+uuid4().hex
        original=dict(source='reviewed_invoice',payment_document_id=document_id,source_revision=revision,source_digest=digest,payment_verified=False)
        created=c.execute('''INSERT INTO billvision.documents(document_id,image_hash,image,mime,width,height,state,platform,original,rows)
          VALUES(%s,%s,%s,%s,%s,%s,'review','invoice',%s,%s) RETURNING *''',(identifier,d['image_hash'],d['image'],d['mime'],d['width'],d['height'],Jsonb(original),Jsonb(rows))).fetchone()
        save_version(c,created,'invoice_review')
        return dict(document=public(c,created),replayed=False,bank_ledger_unchanged=True)
