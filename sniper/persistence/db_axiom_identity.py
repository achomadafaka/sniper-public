"""Short database writes for Axiom identity evidence."""


def insert_historical_candidates(conn, rows, now):
    """Insert unverified historical candidates. Preserve all existing proofs."""
    before = conn.total_changes
    with conn:
        conn.executemany(
            "INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, "
            "network_id, address, verify_status, matches, contradictions, no_data, checked, "
            "verified_at, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_user_id, network_id) DO NOTHING",
            [(r[0], r[1], r[2], r[3], r[4], "unverified", 0, r[7], r[8], r[9], now, r[10])
             for r in rows])
    return conn.total_changes - before


def proof_snapshot(conn):
    """Read proof identities before remote reads start."""
    return {(r[0], r[1]): tuple(r[2:]) for r in conn.execute(
        "SELECT source_user_id,network_id,address,verify_status,verified_at,detail "
        "FROM axiom_verified_addresses")}


def save_verification_results(conn, results, *, expected, now, require_all=False):
    """Write unchanged proofs; require_all refuses a whole enrollment batch on any conflict."""
    rank = {"unverified": 0, "shared_executor": 1, "REFUTED": 2, "axiom_confirmed": 3}
    selected = {}
    for result in results:
        key = (result["source_user_id"], result["chain"])
        previous = selected.get(key)
        if not result["skip_reason"] and (previous is None or
                rank[result["verify_status"]] > rank[previous["verify_status"]]):
            selected[key] = result
    written = 0
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = proof_snapshot(conn)
        if require_all and (len(selected) != len(results) or any(
                current.get(key) != expected.get(key) for key in selected)):
            raise ValueError('proof batch changed')
        shared = {(r["chain"], r["address"] if r["chain"] == "solana" else r["address"].lower())
                  for r in results if r["verify_status"] == "shared_executor"}
        for old_key, old in current.items():
            address = old[0] if old_key[1] == "solana" else old[0].lower()
            if (old_key[1], address) in shared and old == expected.get(old_key):
                conn.execute("UPDATE axiom_verified_addresses "
                             "SET verify_status='shared_executor',verified_at=? "
                             "WHERE source_user_id=? AND network_id=?", (now, *old_key))
        for result in selected.values():
            key = (result["source_user_id"], result["chain"])
            if result["skip_reason"] or current.get(key) != expected.get(key):
                continue
            conn.execute(
                "INSERT INTO axiom_verified_addresses(source_user_id,handle,x_handle,network_id,"
                "address,verify_status,matches,contradictions,no_data,checked,verified_at,detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_user_id,network_id) DO UPDATE SET handle=excluded.handle,"
                "x_handle=excluded.x_handle,address=excluded.address,verify_status=excluded.verify_status,"
                "matches=excluded.matches,contradictions=excluded.contradictions,no_data=excluded.no_data,"
                "checked=excluded.checked,verified_at=excluded.verified_at,detail=excluded.detail",
                (result["source_user_id"], result["handle"], result["x_handle"], result["chain"],
                 result["address"], result["verify_status"], result["matches"],
                 result["contradictions"], result["no_data"], result["checked"], now, result["detail"]))
            result["lane_written"] = True
            written += 1
    return written
