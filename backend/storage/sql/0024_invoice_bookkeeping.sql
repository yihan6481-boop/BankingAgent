-- Reuse the existing private bill-review ledger for invoices. An invoice is a
-- source document, never payment-success evidence and never a bank debit.
ALTER TABLE billvision.documents DROP CONSTRAINT documents_platform_check;
ALTER TABLE billvision.documents ADD CONSTRAINT documents_platform_check
 CHECK(platform IN ('wechat','alipay','unknown','invoice'));
