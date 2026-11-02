-- Supabase audit: find live database objects still referencing agent_runs.notes
--
-- Run in Supabase SQL Editor after applying migrations/supabase_002_agent_runs.sql.
-- This is read-only.

-- 1) Sanity: show the live agent_runs columns.
SELECT
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'agent_runs'
ORDER BY ordinal_position;

-- 2) Views / materialized views that still reference agent_runs.notes.
SELECT
    'view' AS object_type,
    schemaname AS schema_name,
    viewname AS object_name,
    definition
FROM pg_views
WHERE definition ILIKE '%agent_runs%'
  AND definition ILIKE '%notes%'

UNION ALL

SELECT
    'materialized_view' AS object_type,
    schemaname AS schema_name,
    matviewname AS object_name,
    definition
FROM pg_matviews
WHERE definition ILIKE '%agent_runs%'
  AND definition ILIKE '%notes%'
ORDER BY object_type, schema_name, object_name;

-- 3) Functions / procedures with stale references.
SELECT
    CASE p.prokind
        WHEN 'f' THEN 'function'
        WHEN 'p' THEN 'procedure'
        ELSE 'routine'
    END AS object_type,
    n.nspname AS schema_name,
    p.proname AS object_name,
    pg_get_functiondef(p.oid) AS definition
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'p')
  AND pg_get_functiondef(p.oid) ILIKE '%agent_runs%'
  AND pg_get_functiondef(p.oid) ILIKE '%notes%'
ORDER BY schema_name, object_name;

-- 4) Trigger functions attached to agent_runs or mentioning notes.
SELECT
    t.tgname AS trigger_name,
    ns.nspname AS table_schema,
    cls.relname AS table_name,
    pns.nspname AS function_schema,
    p.proname AS function_name,
    pg_get_functiondef(p.oid) AS function_definition
FROM pg_trigger t
JOIN pg_class cls ON cls.oid = t.tgrelid
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_proc p ON p.oid = t.tgfoid
JOIN pg_namespace pns ON pns.oid = p.pronamespace
WHERE NOT t.tgisinternal
  AND p.prokind IN ('f', 'p')
  AND (
      (ns.nspname = 'public' AND cls.relname = 'agent_runs')
      OR pg_get_functiondef(p.oid) ILIKE '%agent_runs%'
      OR pg_get_functiondef(p.oid) ILIKE '%notes%'
  )
ORDER BY table_schema, table_name, trigger_name;

-- 5) Constraints / generated expressions / defaults that mention notes.
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    a.attname AS column_name,
    pg_get_expr(d.adbin, d.adrelid) AS default_expression
FROM pg_attrdef d
JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
JOIN pg_class c ON c.oid = d.adrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE pg_get_expr(d.adbin, d.adrelid) ILIKE '%agent_runs%'
   OR pg_get_expr(d.adbin, d.adrelid) ILIKE '%notes%'
ORDER BY schema_name, table_name, column_name;

-- 6) Quick grep-style catalog search across common SQL-bearing objects.
WITH corpus AS (
    SELECT
        'view'::text AS object_type,
        schemaname AS schema_name,
        viewname AS object_name,
        definition AS body
    FROM pg_views
    UNION ALL
    SELECT
        'materialized_view'::text,
        schemaname,
        matviewname,
        definition
    FROM pg_matviews
    UNION ALL
    SELECT
        'function'::text,
        n.nspname,
        p.proname,
        pg_get_functiondef(p.oid)
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prokind IN ('f', 'p')
)
SELECT object_type, schema_name, object_name
FROM corpus
WHERE body ILIKE '%agent_runs.notes%'
   OR body ILIKE '%agent_runs%notes%'
ORDER BY object_type, schema_name, object_name;

-- If all result sets above are empty but Postgres logs still show
-- "column agent_runs.notes does not exist", the caller is likely:
--   - a one-off SQL query run manually in the dashboard
--   - an external app/script not in this repo
--   - cached query text from a client or BI/dashboard tool
