-- usage_observations (0001) was never written. It becomes the append-only ledger of provider-reported
-- usage: one row per observation, written in the same transaction as the agent.usage_observed event.
-- NULL means unknown (never 0). cost_usd is as reported by the provider; for a subscription it is an
-- API-equivalent estimate, not a charge.
ALTER TABLE usage_observations ADD COLUMN cost_usd REAL CHECK (cost_usd IS NULL OR cost_usd >= 0);
CREATE TRIGGER usage_observations_no_update BEFORE UPDATE ON usage_observations
BEGIN SELECT RAISE(ABORT, 'usage observations are append-only'); END;
CREATE TRIGGER usage_observations_no_delete BEFORE DELETE ON usage_observations
BEGIN SELECT RAISE(ABORT, 'usage observations are append-only'); END;
