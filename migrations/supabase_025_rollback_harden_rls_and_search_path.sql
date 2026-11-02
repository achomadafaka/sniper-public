-- Rollback for supabase_025_harden_rls_and_search_path.sql
-- Restores the pre-hardening state (anon write policies + mutable search_path).
-- Only needed if a writer is ever switched back to the anon key.

ALTER FUNCTION public.set_wftg_updated_at() RESET search_path;

DROP POLICY IF EXISTS paper_card_enrollments_no_anon ON public.paper_card_enrollments;

CREATE POLICY artifact_registry_write ON public.artifact_registry
    FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY wallet_analysis_results_write ON public.wallet_analysis_results
    FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY wallet_findings_write ON public.wallet_findings
    FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY wftg_write ON public.wallet_flipper_tag_grain
    FOR ALL TO anon USING (true) WITH CHECK (true);
