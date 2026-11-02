BEGIN;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '15s';
DROP INDEX IF EXISTS public.brain_facts_operator_rules;
COMMIT;
