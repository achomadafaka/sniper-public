"""Static guards on migration 032: the database, not the client, enforces the brain's invariants.

The migration is applied by hand (plan Task 10), so these tests read the SQL text. Each assertion
names one rule a client could otherwise break; removing the rule from the SQL fails the test."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "migrations" / "supabase_032_brain_graph.sql"
RB = ROOT / "migrations" / "supabase_032_rollback_brain_graph.sql"
TABLES = ("brain_sections", "brain_episodes", "brain_entities", "brain_facts",
          "brain_links", "brain_views", "brain_health")
RPCS = ("brain_supersede", "brain_facts_in_sections", "brain_queue_count", "brain_facts_for_entities",
        "brain_match_facts", "brain_match_entities", "brain_set_embedding")
TRIGGER_FNS = ("brain_facts_before", "brain_facts_no_delete", "brain_episodes_append_only",
               "brain_entities_before")


def _sql():
    return SQL.read_text()


def _body(fn):
    """The text of one CREATE FUNCTION ... $$ ... $$ block."""
    m = re.search(r"CREATE OR REPLACE FUNCTION public\.%s\(.*?\$\$(.*?)\$\$" % fn, _sql(), re.S)
    assert m, fn
    return m.group(1)


def test_fact_checks_are_database_checks():
    s = _sql()
    assert "CHECK (verified_by IS NULL OR verified_by <> author)" in s
    assert "CHECK (kind = 'observation' OR jsonb_array_length(evidence) >= 1)" in s
    assert "CHECK (length(statement) BETWEEN 8 AND 600)" in s
    assert "cardinality(entity_ids) >= 1" in s
    assert "CHECK (valid_to IS NULL OR valid_to >= valid_from)" in s    # a backdated supersede fails loudly
    assert "CHECK (status NOT IN ('verified','verified_partial','refuted') OR verified_by IS NOT NULL)" in s
    assert "'config'" not in s                                        # R14: no config kind


def test_episode_parts_and_unknown_occurrence_time():
    s = _sql()
    assert "CHECK (length(body) <= 1800)" in s                        # R11
    assert re.search(r"occurred_at timestamptz,", s), "occurred_at must be nullable: NULL = unknown"
    assert "occurred_at timestamptz NOT NULL" not in s


def test_entities_need_a_section_and_a_known_type():
    s = _sql()
    assert "section_id uuid NOT NULL REFERENCES public.brain_sections(id)" in s   # else invisible at boot
    assert "CHECK (type IN (" in s and "'knob'" in s and "'tool'" in s
    assert "name_lc text GENERATED ALWAYS AS (lower(name)) STORED" in s
    assert "ON public.brain_entities (type, name_lc)" in s
    assert "alias_conflict" in _body("brain_entities_before")


def test_historical_032_excludes_codex_before_the_043_forward_migration():
    s = _sql()
    roles = re.findall(r"author text NOT NULL CHECK \(author IN \(([^)]*)\)\)", s)
    assert roles and all("'codex'" not in r for r in roles)


def test_history_cannot_be_rewritten():
    before = _body("brain_facts_before")
    assert "immutable; supersede instead" in before
    for col in ("statement", "kind", "author", "recorded_at", "entity_ids", "valid_from", "polarity"):
        assert f"NEW.{col}" in before, col
    assert "valid_to is write-once" in before
    assert "resolve the dispute first" in before
    assert "judge_pending" in before
    # a changed evidence reference voids an earlier verification; a changed check annotation does not
    assert "brain_evidence_refs(NEW.evidence) IS DISTINCT FROM public.brain_evidence_refs(OLD.evidence)" in before
    # new evidence reopens a refuted fact too; keeping 'refuted' with verified_by cleared breaks the CHECK
    assert "IF NEW.status IN ('verified', 'verified_partial', 'refuted') THEN" in before
    assert "brain_episodes_append_only" in _sql() and "brain_facts_no_delete" in _sql()


def test_verify_by_defaults_come_from_the_server():
    before = _body("brain_facts_before")
    for piece in ("interval '7 days'", "interval '30 days'", "interval '60 days'",
                  "interval '3 days'", "'infinity'::timestamptz"):
        assert piece in before


def test_supersede_keeps_the_fields_the_importer_sets():
    body = _body("brain_supersede")
    for field in ("valid_from", "verify_by", "judge_pending", "detail", "episode_ids"):
        assert f"p_new->'{field}'" in body or f"p_new->>'{field}'" in body, field
    # a backdated supersede is refused by name, never shifted
    assert "supersede_backdated" in body and "starts < old_from" in body


def test_disputed_status_is_inserted_atomically_by_supersede_upgrade():
    sql = (ROOT / "migrations" / "supabase_034_brain_supersede_dispute.sql").read_text()
    insert = sql.split("INSERT INTO public.brain_facts", 1)[1].split("RETURNING", 1)[0]
    assert "status" in insert and "p_new->>'status'" in insert


def test_section_reads_are_server_side_and_uncapped_by_url():
    body = _body("brain_facts_in_sections")
    assert "e.section_id = ANY(p_section_ids)" in body
    assert "p_oldest_first" in body and "p_queue_only" in body
    ent = _body("brain_facts_for_entities")
    assert "LIMIT" not in ent.upper().replace("DELIMIT", "")          # candidates see every fact


def test_rls_and_revokes():
    s = _sql()
    assert "ARRAY['brain_sections','brain_episodes','brain_entities','brain_facts'," in s
    assert "ENABLE ROW LEVEL SECURITY" in s and "USING (false)" in s
    for f in RPCS:
        assert re.search(r"REVOKE EXECUTE ON FUNCTION public\.%s\([^)]*\) FROM PUBLIC, anon, authenticated;" % f, s), f
        assert re.search(r"GRANT EXECUTE ON FUNCTION public\.%s\([^)]*\) TO service_role;" % f, s), f


def test_table_grants_are_explicit_and_append_only_tables_cannot_be_emptied():
    s = _sql()
    for t in TABLES:
        assert re.search(r"GRANT [A-Z, ]+ ON public\.%s TO service_role;" % t, s), t
    assert "REVOKE DELETE, TRUNCATE ON public.brain_facts, public.brain_episodes, public.brain_links FROM service_role;" in s
    assert not re.search(r"GRANT [A-Z, ]*DELETE[A-Z, ]* ON public\.brain_(facts|episodes)", s)


def test_rollback_drops_everything_it_creates():
    rb = RB.read_text()
    for t in TABLES:
        assert f"DROP TABLE IF EXISTS public.{t}" in rb
    for f in RPCS + TRIGGER_FNS + ("brain_evidence_refs",):
        assert f"DROP FUNCTION IF EXISTS public.{f}(" in rb, f


# ---- 039: `refines` against a closed older fact. These run without PostgreSQL. ----------------

def _judge_pair_body(name):
    text = (ROOT / "migrations" / name).read_text()
    start = text.index("CREATE OR REPLACE FUNCTION public.brain_judge_pair(")
    return text[start:text.index("END $$;", start) + len("END $$;")]


def test_039_changes_only_the_closed_fact_check_and_its_rollback_is_037():
    before = _judge_pair_body("supabase_037_brain_atomic_judgement.sql").splitlines()
    after = _judge_pair_body("supabase_039_brain_refines_closed.sql").splitlines()
    assert _judge_pair_body("supabase_039_rollback_brain_refines_closed.sql").splitlines() == before
    removed = [line for line in before if line not in after]
    added = [line for line in after if line not in before]
    assert len(removed) == 2 and removed[0].startswith("DECLARE ") and "RAISE EXCEPTION 'closed_fact'" in removed[1]
    assert [line.strip()[:12] for line in added] == ["DECLARE n pu", "n_closed:=n.", "o_closed:=o.", "-- 039: `ref", "-- CLOSED ol", "IF p_verdict"]
    assert added[-1].strip() == ("IF p_verdict<>'unrelated' AND (n_closed OR (o_closed AND p_verdict<>'refines')) "
                                 "THEN RAISE EXCEPTION 'closed_fact'; END IF;")
    live = "NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken')"
    assert added[1].strip() == "n_closed:=n.valid_to IS NOT NULL OR n.status " + live + ";"
    assert added[2].strip() == "o_closed:=o.valid_to IS NOT NULL OR o.status " + live + ";"


def test_039_and_its_rollback_keep_the_function_service_role_only():
    for name in ("supabase_039_brain_refines_closed.sql", "supabase_039_rollback_brain_refines_closed.sql"):
        text = (ROOT / "migrations" / name).read_text()
        assert "SECURITY INVOKER SET search_path=''" in text
        assert ("REVOKE ALL ON FUNCTION public.brain_judge_pair(uuid,uuid,int,int,text,text) "
                "FROM PUBLIC,anon,authenticated;") in text
        assert ("GRANT EXECUTE ON FUNCTION public.brain_judge_pair(uuid,uuid,int,int,text,text) "
                "TO service_role;") in text
        assert "DROP " not in text.upper() and "DELETE " not in text.upper()
