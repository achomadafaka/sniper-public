-- Rollback for supabase_027_drop_orphaned_sao_indexes.sql
-- Re-add if a query that filters/orders by these columns is introduced.
CREATE INDEX IF NOT EXISTS strategy_agent_observations_confidence_idx
    ON public.strategy_agent_observations USING btree (confidence);
CREATE INDEX IF NOT EXISTS strategy_agent_observations_strategy_ids_idx
    ON public.strategy_agent_observations USING btree (strategy_ids);
