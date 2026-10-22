CREATE TRIGGER consent_target_guard BEFORE INSERT ON consents BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM trades WHERE trade_id=NEW.trade_id AND NEW.actor IN (buyer_id,seller_id)) THEN RAISE(ABORT,'consent requires a trade party') END;
    SELECT CASE WHEN NOT (
        (NEW.target_type IN ('contract','cancel') AND NEW.target_id=NEW.trade_id AND EXISTS(SELECT 1 FROM contract_revisions WHERE trade_id=NEW.trade_id AND revision=NEW.revision AND hash=NEW.target_hash))
        OR (NEW.target_type='proposal' AND EXISTS(SELECT 1 FROM settlement_proposals WHERE trade_id=NEW.trade_id AND proposal_id=NEW.target_id AND revision=NEW.revision AND hash=NEW.target_hash))
        OR (NEW.target_type='resolution' AND EXISTS(SELECT 1 FROM resolution_revisions WHERE trade_id=NEW.trade_id AND resolution_id=NEW.target_id AND revision=NEW.revision AND hash=NEW.target_hash))
    ) THEN RAISE(ABORT,'consent target revision or hash mismatch') END;
END;
CREATE TRIGGER evidence_confirmation_guard BEFORE INSERT ON evidence_confirmations BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM trades WHERE trade_id=NEW.trade_id AND NEW.actor IN (buyer_id,seller_id)) THEN RAISE(ABORT,'evidence confirmation requires a trade party') END;
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM evidence_bundles WHERE trade_id=NEW.trade_id AND evidence_id=NEW.evidence_id AND hash=NEW.target_hash) THEN RAISE(ABORT,'evidence confirmation hash mismatch') END;
END;
CREATE TRIGGER resolution_consent_guard BEFORE INSERT ON resolution_consents BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM trades WHERE trade_id=NEW.trade_id AND NEW.actor IN (buyer_id,seller_id)) THEN RAISE(ABORT,'resolution consent requires a trade party') END;
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM resolution_revisions WHERE trade_id=NEW.trade_id AND resolution_id=NEW.resolution_id AND hash=NEW.target_hash) THEN RAISE(ABORT,'resolution consent hash mismatch') END;
END;
CREATE TRIGGER receipt_buyer_guard BEFORE INSERT ON receipt_confirmations WHEN NOT EXISTS(SELECT 1 FROM trades WHERE trade_id=NEW.trade_id AND buyer_id=NEW.actor) BEGIN SELECT RAISE(ABORT,'receipt confirmation requires the buyer'); END;
CREATE TRIGGER operation_amount_guard BEFORE INSERT ON settlement_operations WHEN NOT EXISTS(
    SELECT 1 FROM allocations WHERE proposal_id=NEW.proposal_id AND component=NEW.component AND NEW.amount=CASE WHEN NEW.direction='PAY' THEN pay_won ELSE refund_won END
) BEGIN SELECT RAISE(ABORT,'operation must match the approved allocation'); END;
CREATE TRIGGER operation_identity_guard BEFORE UPDATE ON settlement_operations BEGIN
    SELECT CASE WHEN NEW.operation_id!=OLD.operation_id OR NEW.trade_id!=OLD.trade_id OR NEW.group_id!=OLD.group_id OR NEW.proposal_id!=OLD.proposal_id OR NEW.revision!=OLD.revision OR NEW.component!=OLD.component OR NEW.direction!=OLD.direction OR NEW.amount!=OLD.amount OR NEW.created_at!=OLD.created_at THEN RAISE(ABORT,'logical operation identity is immutable') END;
    SELECT CASE WHEN NEW.attempt_no!=OLD.attempt_no AND NOT(NEW.attempt_no=OLD.attempt_no+1 AND OLD.status='FAILED' AND NEW.status='RESERVED') THEN RAISE(ABORT,'only a failed operation may start its next attempt') END;
END;
CREATE TRIGGER ledger_current_attempt_guard BEFORE INSERT ON ledger_groups WHEN NEW.operation_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM settlement_operations WHERE trade_id=NEW.trade_id AND operation_id=NEW.operation_id AND attempt_no=NEW.attempt_no
) BEGIN SELECT RAISE(ABORT,'ledger event must match the current operation attempt'); END;
CREATE TRIGGER funded_deposit_immutable BEFORE UPDATE ON component_balances WHEN NEW.deposited!=OLD.deposited AND EXISTS(SELECT 1 FROM trades WHERE trade_id=OLD.trade_id AND funded=1) BEGIN SELECT RAISE(ABORT,'funded amounts are immutable'); END;
CREATE TRIGGER funded_trade_no_unfund BEFORE UPDATE ON trades WHEN OLD.funded=1 AND NEW.funded!=1 BEGIN SELECT RAISE(ABORT,'funding history is immutable'); END;
CREATE TRIGGER funded_contract_amount_guard BEFORE INSERT ON contract_revisions WHEN EXISTS(SELECT 1 FROM trades WHERE trade_id=NEW.trade_id AND funded=1) BEGIN
    SELECT CASE WHEN NEW.D!=(SELECT deposited FROM component_balances WHERE trade_id=NEW.trade_id AND component='D') OR NEW.W!=(SELECT deposited FROM component_balances WHERE trade_id=NEW.trade_id AND component='W') THEN RAISE(ABORT,'funded contract amounts are immutable') END;
END;
CREATE TRIGGER contract_item_guard BEFORE INSERT ON contract_revisions WHEN NOT EXISTS(SELECT 1 FROM trades WHERE trade_id=NEW.trade_id AND item_id=NEW.item_id) BEGIN SELECT RAISE(ABORT,'contract item must belong to the trade'); END;
CREATE TRIGGER deposit_amount_guard BEFORE INSERT ON deposit_records WHEN NEW.amount!=(SELECT D+W FROM contract_revisions WHERE trade_id=NEW.trade_id AND revision=NEW.contract_revision) BEGIN SELECT RAISE(ABORT,'deposit must fund the complete contract'); END;
CREATE TRIGGER deposit_record_no_update BEFORE UPDATE ON deposit_records BEGIN SELECT RAISE(ABORT,'deposit record is immutable'); END;
CREATE TRIGGER deposit_record_no_delete BEFORE DELETE ON deposit_records BEGIN SELECT RAISE(ABORT,'deposit record is immutable'); END;
CREATE TRIGGER resolution_consent_no_update BEFORE UPDATE ON resolution_consents BEGIN SELECT RAISE(ABORT,'resolution consent is immutable'); END;
CREATE TRIGGER resolution_consent_no_delete BEFORE DELETE ON resolution_consents BEGIN SELECT RAISE(ABORT,'resolution consent is immutable'); END;
CREATE TRIGGER receipt_no_update BEFORE UPDATE ON receipt_confirmations BEGIN SELECT RAISE(ABORT,'receipt confirmation is immutable'); END;
CREATE TRIGGER receipt_no_delete BEFORE DELETE ON receipt_confirmations BEGIN SELECT RAISE(ABORT,'receipt confirmation is immutable'); END;
