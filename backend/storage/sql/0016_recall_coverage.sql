-- Exact trusted-message coverage, not another fact store. Preserve 0015.
ALTER TABLE recall.sources ADD COLUMN span_start INTEGER;
ALTER TABLE recall.sources ADD COLUMN span_end INTEGER;
ALTER TABLE recall.sources ADD COLUMN coverage_retired BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE recall.sources ADD CONSTRAINT recall_source_span CHECK
 ((span_start IS NULL AND span_end IS NULL) OR (span_start >= 0 AND span_end > span_start));
CREATE INDEX recall_source_spans ON recall.sources(owner_uuid,origin_ref,origin_version,span_start,span_end);
