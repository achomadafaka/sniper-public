-- Rollback for supabase_024_paper_enrollment_enrolled_effect.sql
ALTER TABLE public.paper_card_enrollments DROP COLUMN IF EXISTS enrolled_effect;
