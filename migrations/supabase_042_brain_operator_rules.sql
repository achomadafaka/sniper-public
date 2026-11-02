-- Nightly doctrine reads include closed rules to avoid recreating withdrawn decisions.
-- Bound the migration's lock wait; this index changes no fact contents or lifecycle.
BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '15s';
CREATE INDEX IF NOT EXISTS brain_facts_operator_rules ON public.brain_facts (id)
    WHERE author = 'operator' AND kind = 'rule';
COMMIT;
