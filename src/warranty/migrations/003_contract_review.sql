ALTER TABLE contract_revisions ADD COLUMN review_id TEXT REFERENCES review_snapshots(review_id);
CREATE TRIGGER contract_review_trade_guard BEFORE INSERT ON contract_revisions WHEN NEW.review_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM review_snapshots WHERE trade_id=NEW.trade_id AND review_id=NEW.review_id
) BEGIN SELECT RAISE(ABORT,'adopted contract review must belong to the same trade'); END;
