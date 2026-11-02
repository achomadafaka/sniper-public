-- supabase_025_harden_rls_and_search_path.sql
-- Security hardening pass (2026-06-20). Closes every Supabase SECURITY advisor:
--   * function_search_path_mutable on set_wftg_updated_at        (WARN)
--   * rls_policy_always_true x4 (anon FOR ALL USING(true) writes) (WARN)
--   * rls_enabled_no_policy on paper_card_enrollments            (INFO)
--
-- Writers authenticate with the service_role key (SUPABASE_SERVICE_KEY); both
-- bot/supabase_helper.py and the wallet-protocol ledger adapter resolve
-- `SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY` (service-first), and the service
-- role bypasses RLS. So removing the anon write policies does NOT affect the
-- live write path. Public reads remain via the existing *_select policies
-- (FOR SELECT TO public USING(true)).

-- 1. Pin the trigger function's search_path. Body only calls now() (pg_catalog,
--    always searched), so an empty search_path is safe.
ALTER FUNCTION public.set_wftg_updated_at() SET search_path = '';

-- 2. Drop the vestigial anon write policies. These granted anon unrestricted
--    INSERT/UPDATE/DELETE (and SELECT) — the RLS-bypass the advisor flagged.
--    Dropping them also clears the 4 multiple_permissive_policies (anon SELECT)
--    PERFORMANCE advisors: each table's only remaining anon-applicable policy
--    becomes its *_select read policy.
DROP POLICY IF EXISTS artifact_registry_write ON public.artifact_registry;
DROP POLICY IF EXISTS wallet_analysis_results_write ON public.wallet_analysis_results;
DROP POLICY IF EXISTS wallet_findings_write ON public.wallet_findings;
DROP POLICY IF EXISTS wftg_write ON public.wallet_flipper_tag_grain;

-- 3. Make paper_card_enrollments' service-role-only lock explicit. Migration 023
--    deliberately left RLS on with no policy (anon/authenticated see nothing,
--    service_role bypasses). This records that intent in the policy layer and
--    clears rls_enabled_no_policy WITHOUT granting any anon access surface.
CREATE POLICY paper_card_enrollments_no_anon ON public.paper_card_enrollments
    AS PERMISSIVE FOR ALL TO anon, authenticated
    USING (false) WITH CHECK (false);
