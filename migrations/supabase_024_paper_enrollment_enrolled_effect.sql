-- supabase_024_paper_enrollment_enrolled_effect.sql
-- D4 S2 pre-req: store the in-sample effect magnitude at enrollment so the forward
-- decay gate (forward effect >= enrolled_effect * decay_fraction) can apply. Source is
-- unambiguous: ThesisCard.effect — the sizing edge (dollar-wtd − equal-wtd) for
-- sizing_card, the dead−live drift gap for timing_card — i.e. exactly the statistic D4
-- re-computes forward. Nullable: pre-D4 rows and any card without an effect simply can't
-- promote (no decay baseline).

ALTER TABLE public.paper_card_enrollments
    ADD COLUMN IF NOT EXISTS enrolled_effect double precision;
