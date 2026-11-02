"""Receipt-backed source proof for offline funded-lane fixtures."""
def proof_for(sig, *, got=None, pool=None, sqrt=None, venue="v4", paid_usd=None):
    return dict(proof_version="wallet-net-fill-v1", receipt_proof_version="direct-wallet-paid-v1",
                network_id="4663", tx_hash=sig["tx_hash"], block_number=sig["seq"],
                block_hash="0x" + "ab" * 32, wallet=sig["wallet"],
                source_user_id=sig["source_user_id"], mint=sig["mint"],
                payment_token="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
                payment_basis="wallet_quote_outflow",
                payment_atomic=str(sig.get("src_in_wei", 10**16)),
                acquired_atomic=str(got if got is not None else sig["src_out"]),
                payment_decimals=18, token_decimals=18,
                paid_usd=paid_usd if paid_usd is not None else sig.get("paid_usd", 65.3),
                source_post=dict(pool=pool or sig.get("pool"), venue=venue, ratio=1.0,
                                 receipt_routes_complete=True,
                                 mint_is_c1=True, sqrt_price_x96=str(sqrt or sig.get("post_sqrt")),
                                 pool_input_atomic="100", pool_output_atomic="200"))


def bind_native_prices(p):
    """A coherent source receipt and cap, independent arithmetic for production fixtures."""
    from fractions import Fraction
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    p["pool"] = v4_pool_id(p["key"])
    import math
    if p.get("paid_usd") is None or not math.isfinite(p["paid_usd"]):
        return p
    p["src_in_wei"] = int(Fraction(str(p["paid_usd"]))/Fraction(str(p["eth_usd"]))*10**18)
    p["source_fill"] = proof_for(p)
    post = Fraction(1 << 192, p["post_sqrt"]**2)
    bounds = []
    if cfg.MEMECOIN_REAL_CHASE_MAX_PCT > 0:
        bounds.append(post*(1+Fraction(str(cfg.MEMECOIN_REAL_CHASE_MAX_PCT))/100))
    if cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM > 0:
        bounds.append(Fraction(p["src_in_wei"],p["src_out"])*Fraction(str(cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM)))
    cap = min(bounds)
    p["cap_num"],p["cap_den"] = cap.numerator,cap.denominator
    return p
