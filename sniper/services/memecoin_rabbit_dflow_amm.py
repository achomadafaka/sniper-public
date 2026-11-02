"""DFlow -> pump_amm pre-execution decoder: one RabbitBuyer handler (Alpha GO 2026-10-17).

WHAT IT READS. The armed wallets that trade through the Fomo app send USDC-paid DFlow routes: Fomo's
executor pays the fee (signer 0), the owner co-signs (signer 1), DFlow swaps USDC -> SOL first and then
buys the coin on its canonical PumpSwap pool (PumpFunAmmBuyV2). 118 armed buys in the 7 days to
2026-10-17: all USDC-paid, 108 version-1. A pre-execution frame (a RabbitStream shred, Lambda's preconf
feed) carries no logs and no balances, so the source's fill is MODELLED from one read of the pool.

THE BINDING (the source's fill is bound to the frame and the read, or refused):
- match (memory only): exactly one DFlow swap whose arguments decode exactly (IDL-hash fail closed);
  one coin leg and it is PumpSwap (dflow_swap.buy_leg; 51 of 58 DLMM-shaped routes were split buys);
  the owner a required signer; USDC in (the owner's USDC account writable and named by the swap);
  the coin = the one account whose canonical PumpSwap pool the swap also names; the owner's coin
  account writable. sol_in = PumpFunAmmBuyV2.amount: what DFlow plans to put into the pool, within
  -0.9%..+0.05% of what it put on all 118 (p10 0.9997). DFlow's quoted_out is NOT used (executed out /
  quoted: p10 0.39), nor is quoted x (1 - slippage): sources landed below it. The USDC spend is not
  read either: the swap has no input amount, and routes split USDC over parallel legs (the owner's
  fall exceeded the first leg by 7-71% on several), so the source is valued on its SOL leg.
- read (one getMultipleAccounts, processed): the pool, the coin mint, the FeeConfig, both vaults
  under both token programs, the owner's coin account under both programs, and the context slot.
- verdict:
  (1) identity: the pool is PumpSwap's, names this coin and WSOL, is pump-migrated, and its vault
      fields are the vaults read;
  (2) PRE-NESS, the hard gate: the read's context slot is BEFORE the frame's slot; else the owner's
      coin account holds 0 and no other instruction names it; else read_not_before_source. A read
      that already holds the source's own buy would double-count it and loosen our cap. And no older
      than MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS (2) before the frame, with a known context
      slot, else read_stale (Gamma LOW-1: a stale read misses later sells, a looser cap too);
  (3) post = pre + sol_in through PumpSwap's fee math: amm_fee_bps from the FeeConfig (an unreadable
      config or tier fails closed), expected_base_out (exact on live buys given the reserves);
  (4) the impact ratio within the routed path's plausible band.
  The model reads the pool before the source's slot-mates, so its post price is at or below the real
  one: our cap and min-out come out stricter (more min-out reverts), never looser.
- build: the source as a direct PumpSwap buy (a BuyEvent line; no RoutedBuy), so the lane prices it on
  the coin leg's SOL as it prices any direct PumpSwap source. Upstream USDC router fees are left out,
  which lowers the source average and makes the copy cap stricter, never looser. Marked
  pre-execution: proof solana-pre-execution-owner-signed-v1, provenance rabbit_pre_execution, route
  dflow_pump_amm.
- after landing: observe_processed joins each fire to its processed BuyEvent and pages once on either
  of the two things this model owns going wrong: the fee tier it chose differs from the bps the pool
  charged, or the model run on the EXECUTED SOL, pre-reserves and fees (with the virtual quote it
  read) misses the executed out by more than MODEL_TOLERANCE_BPS. On two real buys at the trade's own
  virtual quote the model is exact to 1e-11 relative; the bound absorbs the virtual quote's drift
  between our read and execution (it is a live, signed pool field: 10.0 SOL at one trade, 17.5 SOL
  days later). The drift of our read from the executed pre-state is counted, never paged.
Not handled: SOL-paid DFlow routes (0 of 118), split and non-canonical pools (refused by name), a v0
preconf frame with lookup tables (Lambda's feed never sends one).
"""
from __future__ import annotations

import base64
import collections
import logging
import struct
import threading

logger = logging.getLogger(__name__)

ROUTE = "dflow_pump_amm"
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
AMM_BUY_ACTIONS = ("PumpFunAmmBuy", "PumpFunAmmBuyV2")
MODEL_TOLERANCE_BPS = 25       # the virtual quote's read-to-execution drift; beyond it the model is wrong
JOIN_MAX = 2048


def knob_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", False))


def canonical_pool(mint):
    """The pump-migrated PumpSwap pool of `mint` against WSOL (index 0, the pool-authority creator)."""
    from sniper.execution.pump_curve_buy import WSOL_MINT
    from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda, _pk
    authority = _pda([b"pool-authority", bytes(_pk(mint))], PUMP_PROGRAM)
    return str(_pda([b"pool", (0).to_bytes(2, "little"), bytes(authority), bytes(_pk(mint)),
                     bytes(_pk(WSOL_MINT))], AMM_PROGRAM))


def fee_config_address():
    from sniper.execution.pump_native_exit import FEE_PROGRAM, _pda
    from sniper.execution.pumpswap_buy import AMM_FEE_CONFIG_SEED
    return str(_pda([b"fee_config", AMM_FEE_CONFIG_SEED], FEE_PROGRAM))


def _ata(owner, mint, program):
    from sniper.execution.pump_native_exit import ata
    return str(ata(owner, mint, program))


def _values(decoded, names):
    """The field dicts of every action named in `names`, in route order."""
    out = []
    for name, value in decoded.get("action_values") or ():
        if name in names:
            out.append((value[0] if isinstance(value, (list, tuple)) and value else value) or {})
    return out


def _token_amount(account, *, program, mint, owner=None):
    """An SPL token account's amount when it is `program`'s account of `mint` (and of `owner`), 0 when
    absent, None when it is something else."""
    if account is None:
        return 0
    from solders.pubkey import Pubkey
    try:
        raw = base64.b64decode((account.get("data") or [None])[0] or "")
        if account.get("owner") != program or len(raw) < 72 or raw[:32] != bytes(Pubkey.from_string(mint)):
            return None
        if owner is not None and raw[32:64] != bytes(Pubkey.from_string(owner)):
            return None
        return struct.unpack_from("<Q", raw, 64)[0]
    except Exception:
        return None


class DflowAmmHandler:
    """The RabbitBuyer handler for DFlow -> PumpSwap routes (Lambda's seam: match/read/verdict/build).
    `read_fn(method, params)` is the buyer's funded read role; `alert_fn(title, text)` pages."""

    route = ROUTE
    router_program = "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH"     # dflow_swap.DFLOW_PROGRAM
    page_title = "RABBIT DFLOW AMM MODEL"
    prior_holder_refusal = "read_not_before_source"         # the RPC path's label for a held coin

    def __init__(self, *, read_fn, alert_fn=None):
        if alert_fn is None:
            from sniper.services.memecoin_alerts import send_alert as alert_fn
        self.read_fn, self.alert_fn = read_fn, alert_fn
        self._lock = threading.Lock()
        self._fires = collections.OrderedDict()          # signature -> the fill this handler modelled
        self.stats = dict(matched=0, refused={}, fills=0, joined=0, math_mismatch=0, paged=0,
                          drift_base_bps=collections.deque(maxlen=256))

    def status(self):
        """The handler's counters for the heartbeat, JSON-safe (the drift samples become count and p50/p90)."""
        with self._lock:
            out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.stats.items()
                   if not isinstance(v, collections.deque)}
            for k, v in self.stats.items():
                if isinstance(v, collections.deque):
                    ordered = sorted(v)
                    out[k] = dict(n=len(ordered), p50=ordered[(len(ordered) - 1) // 2] if ordered else None,
                                  p90=ordered[-(-9 * len(ordered) // 10) - 1] if ordered else None)
        return out

    def _refuse(self, reason):
        with self._lock:
            self.stats["refused"][reason] = self.stats["refused"].get(reason, 0) + 1
        return None, reason

    # -- match: memory only -------------------------------------------------------------------
    def match(self, candidate, update=None):
        from sniper.execution import dflow_swap
        from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
        if not knob_on():
            return None
        swaps = [ix for ix in candidate.get("instructions") or () if ix[0] == dflow_swap.DFLOW_PROGRAM]
        if not swaps:
            return None
        if len(swaps) != 1:
            return self._refuse("dflow_multi_swap")
        _program, accounts, data = swaps[0]
        decoded = dflow_swap.decode_swap(bytes(data))
        if decoded is None:
            return self._refuse("dflow_undecoded")
        if not any(name in AMM_BUY_ACTIONS for name in decoded["actions"]):
            return None                                        # not a PumpSwap route: not this handler's
        if dflow_swap.buy_leg(decoded) != "pump_amm":
            return self._refuse("dflow_not_single_pump_amm")
        amm = _values(decoded, AMM_BUY_ACTIONS)
        sol_in = amm[0].get("amount") if len(amm) == 1 else None
        if type(sol_in) is not int or sol_in <= 0:
            return self._refuse("dflow_amm_amount_unread")
        owner = candidate.get("owner")
        if owner not in tuple(candidate.get("signers") or ()):
            return self._refuse("owner_not_signer")
        keys, writable = list(candidate.get("account_keys") or ()), candidate.get("writable")
        if writable is None or len(writable) != len(keys):
            return self._refuse("writable_unknown")
        is_writable = {key: bool(flag) for key, flag in zip(keys, writable)}
        refs = set(accounts)
        usdc_account = _ata(owner, USDC_MINT, TOKEN_PROGRAM)
        if usdc_account not in refs or not is_writable.get(usdc_account):
            return self._refuse("input_not_usdc")
        mints = []
        for ref in refs:
            try:
                if canonical_pool(ref) in refs:
                    mints.append(ref)
            except Exception:
                continue
        if len(mints) != 1:
            return self._refuse("pool_unbound")
        mint = mints[0]
        holders = {program: _ata(owner, mint, program) for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)}
        if not any(is_writable.get(account) for account in holders.values()):
            return self._refuse("holder_unbound")
        pool = canonical_pool(mint)
        plan = dict(owner=owner, mint=mint, pool=pool, sol_in=sol_in, holders=holders, frame_slot=candidate.get("slot"), signature=candidate.get("signature"),
                    fee_config=fee_config_address(), quote_vault=_ata(pool, WSOL_MINT, TOKEN_PROGRAM),
                    base_vaults={p: _ata(pool, mint, p) for p in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)},
                    writable_holders=tuple(p for p, a in holders.items() if is_writable.get(a)))
        with self._lock:
            self.stats["matched"] += 1
        return plan, None

    # -- read: one worker-thread read ---------------------------------------------------------
    def read_keys(self, plan):
        from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
        return [plan["pool"], plan["mint"], plan["fee_config"], plan["quote_vault"],
                plan["base_vaults"][TOKEN_PROGRAM], plan["base_vaults"][TOKEN_2022_PROGRAM],
                plan["holders"][TOKEN_PROGRAM], plan["holders"][TOKEN_2022_PROGRAM]]

    def read(self, plan):
        keys = self.read_keys(plan)
        res = self.read_fn("getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "processed"}]) or {}
        values = list(res.get("value") or ())
        if len(values) != len(keys):
            raise ValueError("read_short")
        return {"slot": (res.get("context") or {}).get("slot"), "accounts": dict(zip(keys, values))}

    # -- the pool tape (memecoin_rabbit_tape): the same state, from memory --------------------
    def tape_read(self, plan, tape, frame_slot, lane_head):
        """The read's state at the end of the frame slot's parent: the pool, the mint, the FeeConfig,
        the quote vault and the base vault under the mint's own program, all from one parent slot.
        Every other key of read_keys (the other program's vault, the owner's coin accounts) reads as
        absent: the state is from a slot before the source's, so its coin account cannot hold the
        source's buy and verdict's pre-ness takes the read_slot < frame_slot branch."""
        from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
        head = [plan["pool"], plan["mint"], plan["fee_config"], plan["quote_vault"]]
        accounts, parent = tape.lookup(head, frame_slot, lane_head=lane_head)
        if accounts is None:
            return None, parent
        program = (accounts.get(plan["mint"]) or {}).get("owner")
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            return None, "mint_program"
        accounts, parent2 = tape.lookup(head + [plan["base_vaults"][program]], frame_slot, lane_head=lane_head)
        if accounts is None or parent2 != parent:
            return None, parent2 if accounts is None else "parent_moved"
        state = {key: None for key in self.read_keys(plan)}
        state.update(accounts)
        return {"slot": parent, "accounts": state}, None

    def tape_keys(self, plan, state=None):
        """The keys a tape read uses (what an RPC fallback read may seed)."""
        from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
        keys = [plan["pool"], plan["mint"], plan["fee_config"], plan["quote_vault"]]
        program = (((state or {}).get("accounts") or {}).get(plan["mint"]) or {}).get("owner")
        if program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            keys.append(plan["base_vaults"][program])
        return keys

    # -- verdict: the binding -----------------------------------------------------------------
    def verdict(self, plan, state, candidate):
        from sniper.execution import pump_fees
        from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
        from sniper.execution.pumpswap_buy import expected_base_out, is_pump_migrated_pool, mint_supply_and_decimals, parse_pool_account
        from sniper.services.memecoin_slot_signal import IMPACT_RATIO_MAX, IMPACT_RATIO_MIN
        accounts = state.get("accounts") or {}
        pool_acct = accounts.get(plan["pool"])
        if not isinstance(pool_acct, dict) or pool_acct.get("owner") != AMM_PROGRAM:
            return self._refuse("pool_not_amm")
        try:
            facts = parse_pool_account((pool_acct.get("data") or [None])[0])
        except (ValueError, TypeError):
            return self._refuse("pool_unreadable")
        if facts["base_mint"] != plan["mint"] or facts["quote_mint"] != WSOL_MINT or not is_pump_migrated_pool(facts):
            return self._refuse("pool_identity")
        mint_acct = accounts.get(plan["mint"])
        program = (mint_acct or {}).get("owner")
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            return self._refuse("mint_unreadable")
        try:
            supply = mint_supply_and_decimals((mint_acct.get("data") or [None])[0])
        except (ValueError, TypeError):
            return self._refuse("mint_unreadable")
        if (facts["pool_base_token_account"] != plan["base_vaults"][program]
                or facts["pool_quote_token_account"] != plan["quote_vault"]):
            return self._refuse("vault_identity")
        base_reserve = _token_amount(accounts.get(plan["base_vaults"][program]), program=program, mint=plan["mint"])
        quote_reserve = _token_amount(accounts.get(plan["quote_vault"]), program=TOKEN_PROGRAM, mint=WSOL_MINT)
        if not base_reserve or not quote_reserve:
            return self._refuse("reserves_unread")
        if program not in plan["writable_holders"]:
            return self._refuse("holder_unbound")
        holder = plan["holders"][program]
        held = _token_amount(accounts.get(holder), program=program, mint=plan["mint"], owner=plan["owner"])
        if held is None:
            return self._refuse("holder_unreadable")
        # (2) PRE-NESS, the hard gate (Alpha's condition 2), and FRESHNESS (Gamma LOW-1): a read older than
        # READ_MAX_LAG_SLOTS before the frame, or with no context slot, misses state the source will meet.
        from sniper.settings import memecoin as cfg
        read_slot, frame_slot = state.get("slot"), plan.get("frame_slot")
        if (type(read_slot) is not int or type(frame_slot) is not int
                or frame_slot - read_slot > int(cfg.MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS)):
            return self._refuse("read_stale")
        if read_slot >= frame_slot:
            if held != 0 or self._holder_named_elsewhere(candidate, holder):
                return self._refuse("read_not_before_source")
        # (3) post = pre + sol_in through the fee math; the fee tier fails closed.
        fee_acct = accounts.get(plan["fee_config"])
        try:
            config = pump_fees.parse_fee_config((fee_acct or {}).get("data", [None])[0])
        except (ValueError, TypeError):
            return self._refuse("fee_tier_unknown")
        bps = pump_fees.amm_fee_bps(config, facts, base_supply=supply["base_supply"],
                                    base_reserve=base_reserve, quote_reserve=quote_reserve)
        if bps is None or len(bps) != 3:
            return self._refuse("fee_tier_unknown")
        lp_bps, protocol_bps, creator_bps = (int(b) for b in bps)
        total_bps = lp_bps + protocol_bps + creator_bps
        base_out = expected_base_out(plan["sol_in"], base_reserve, quote_reserve, facts["virtual_quote"], total_bps)
        if base_out <= 0:
            return self._refuse("model_empty")
        ratio = base_reserve / (base_reserve - base_out) if base_reserve > base_out else None
        if ratio is None or not IMPACT_RATIO_MIN <= ratio <= IMPACT_RATIO_MAX:
            return self._refuse("impact_implausible")
        net = int(plan["sol_in"]) * 10_000 // (10_000 + total_bps) - 1
        lp_fee = -(-net * lp_bps // 10_000)
        protocol_fee = -(-net * protocol_bps // 10_000)
        creator_fee = -(-net * creator_bps // 10_000)
        fill = dict(sol_in=int(plan["sol_in"]), quote_amount_in=net, lp_fee=lp_fee, protocol_fee=protocol_fee,
                    coin_creator_fee=creator_fee, base_out=base_out, base_reserve=base_reserve,
                    quote_reserve=quote_reserve, virtual_quote=int(facts["virtual_quote"]),
                    lp_bps=lp_bps, protocol_bps=protocol_bps, creator_bps=creator_bps, held=held,
                    coin_creator=facts["coin_creator"], token_program=program, decimals=supply["base_decimals"],
                    read_slot=read_slot, holder=holder, impact_ratio=ratio)
        return fill, None

    def _holder_named_elsewhere(self, candidate, holder):
        """True when an instruction other than the router's swap names the owner's coin account (an ATA
        create of that very account excepted): it could move the coin in the same transaction."""
        from sniper.services.memecoin_rabbit_signal import ATA_PROGRAM
        for program, accounts, data in candidate.get("instructions") or ():
            if program == self.router_program or holder not in accounts:
                continue
            if program == ATA_PROGRAM and bytes(data) in (b"", b"\x00", b"\x01") and tuple(accounts[1:2]) == (holder,):
                continue
            return True
        return False

    # -- build: the source as the lane already reads a routed PumpSwap buy ---------------------
    def build(self, candidate, plan, state, fill, *, source_user_id, handle, signature_basis, feed, read_ms,
              recv_epoch, head=None, head_mono=None, now_mono):
        from datetime import datetime, timezone
        from sniper.execution.pump_curve_buy import WSOL_MINT
        from sniper.services.memecoin_rabbit_signal import (
            PRE_EXECUTION_PROOF_VERSION, PROVENANCE, VALUATION_BASIS, _slot_delta)
        from sniper.services.memecoin_slot_signal import SlotBuy, _amm
        from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
        recv_utc = datetime.fromtimestamp(recv_epoch, tz=timezone.utc).isoformat()
        line = encode_buy_event(buy_event_fields(plan, fill, timestamp=int(recv_epoch)))
        fields = _amm(anchor_event_bytes(line), None, any_user=True)
        proof = dict(schema_version=1, proof_version=PRE_EXECUTION_PROOF_VERSION, network_id="solana",
                     tx_hash=candidate["signature"], block_number=candidate["slot"], mint=plan["mint"],
                     beneficiary=plan["owner"], payer=plan["owner"], source_user_id=source_user_id,
                     acquired_atomic=str(fill["base_out"]), payment_atomic=str(fill["sol_in"]),
                     payment_mint=WSOL_MINT, payment_basis="pre_execution_estimate", program_id=AMM_PROGRAM,
                     signature_basis=signature_basis, route=self.route, read_slot=fill["read_slot"])
        fields = dict(fields, mint=plan["mint"])
        with self._lock:
            self._fires[candidate["signature"]] = dict(plan=plan, fill=fill)
            while len(self._fires) > JOIN_MAX:
                self._fires.popitem(last=False)
            self.stats["fills"] += 1
        return SlotBuy(source_user_id=source_user_id, handle=handle, owner=plan["owner"],
                       signature=candidate["signature"], slot=candidate["slot"], token_program=None,
                       trade_event_log=line, recv_monotonic=candidate["recv_monotonic"], recv_utc=recv_utc,
                       head_at_recv=head, head_at_recv_mono=head_mono, quote_mint=WSOL_MINT,
                       source_initiation=proof, signal_feed=feed, proof_ready_monotonic=now_mono,
                       proof_provenance=PROVENANCE, decoded_monotonic=now_mono, source_authorized=True,
                       rabbit={"signature_basis": signature_basis, "route": self.route, "read_ms": read_ms,
                               "read_slot": fill["read_slot"], "frame_slot": candidate["slot"],
                               "slot_status": "pre_execution", "valuation_basis": VALUATION_BASIS,
                               "generation": candidate.get("generation"), "holder_atomic": fill["held"],
                               "read_minus_frame_slot": _slot_delta(fill["read_slot"], candidate["slot"]),
                               "sol_in": fill["sol_in"], "input_mint": USDC_MINT,
                               "fee_bps": (fill["lp_bps"], fill["protocol_bps"], fill["creator_bps"]),
                               "impact_ratio": fill["impact_ratio"]},
                       **fields)

    # -- after landing: the modelled fill against the executed BuyEvent ------------------------
    def observe_processed(self, signature, event_line):
        """Join one fire to its processed PumpSwap BuyEvent (see the module docstring): page once when
        the fee tier differs from what the pool charged, or when the model on the executed inputs misses
        the executed out by more than MODEL_TOLERANCE_BPS. Returns the join's numbers, or None."""
        from sniper.execution.pumpswap_buy import expected_base_out, parse_buy_event, total_fee_bps
        with self._lock:
            fire = self._fires.pop(signature, None)
        if fire is None:
            return None
        try:
            ev = parse_buy_event(event_line)
        except Exception:
            return None
        fill = fire["fill"]
        executed = int(ev["base_amount_out"])
        spend = self._executed_spend(ev)
        charged = (int(ev["lp_fee_basis_points"]), int(ev["protocol_fee_basis_points"]),
                   int(ev["coin_creator_fee_basis_points"]))
        modelled = (fill["lp_bps"], fill["protocol_bps"], fill["creator_bps"])
        at_exec = (expected_base_out(spend, int(ev["pool_base_token_reserves"]), int(ev["pool_quote_token_reserves"]),
                                     fill["virtual_quote"], total_fee_bps(ev)) if spend is not None else 0)
        miss_bps = abs(at_exec - executed) * 10_000 / max(1, executed)
        drift = (int(ev["pool_base_token_reserves"]) - fill["base_reserve"]) * 10_000 // max(1, fill["base_reserve"])
        reasons = ([] if charged == modelled else ["fee tier %s, the pool charged %s" % (modelled, charged)]) + (
            ["model %d vs executed %d (%.1f bps)" % (at_exec, executed, miss_bps)]
            if miss_bps > MODEL_TOLERANCE_BPS else []) + self._join_reasons(fill, spend)
        with self._lock:
            self.stats["joined"] += 1
            self.stats["drift_base_bps"].append(drift)
            if reasons:
                self.stats["math_mismatch"] += 1
        if reasons:
            text = ("%s pre-execution model mismatch on %s: %s; our read drifted %d bps from the "
                    "executed pre-state." % (self.route, signature, "; ".join(reasons), drift))
            logger.warning("[memecoin] %s", text)
            try:
                self.alert_fn(self.page_title, text)
                with self._lock:
                    self.stats["paged"] += 1
            except Exception:
                logger.exception("[memecoin] dflow amm model page failed")
        return dict(model_at_exec=at_exec, executed=executed, miss_bps=miss_bps, fee_bps_match=charged == modelled,
                    drift_bps=drift, executed_spend=spend)

    def _executed_spend(self, ev):
        """The SOL the executed buy put in, fees included. DFlow's leg is an exact-base-out buy: the
        event's legacy user_quote_amount_in."""
        return int(ev["user_quote_amount_in"])

    def _join_reasons(self, fill, spend):
        """Route-specific reasons to page on a join (none for DFlow: its SOL is read from the frame)."""
        return []


def buy_event_fields(plan, fill, *, timestamp):
    """The PumpSwap BuyEvent the source's buy is EXPECTED to emit, every field parse_buy_event reads.
    Pre-trade pool reserves (as the program logs them); ix_name names the estimate."""
    from sniper.execution.pump_curve_buy import SYSTEM_PROGRAM
    from sniper.services.memecoin_rabbit_signal import PROVENANCE
    with_lp = fill["quote_amount_in"] + fill["lp_fee"]
    return {"timestamp": timestamp, "base_amount_out": fill["base_out"], "max_quote_amount_in": fill["sol_in"],
            "user_base_token_reserves": fill["held"], "user_quote_token_reserves": 0,
            "pool_base_token_reserves": fill["base_reserve"], "pool_quote_token_reserves": fill["quote_reserve"],
            "quote_amount_in": fill["quote_amount_in"], "lp_fee_basis_points": fill["lp_bps"], "lp_fee": fill["lp_fee"],
            "protocol_fee_basis_points": fill["protocol_bps"], "protocol_fee": fill["protocol_fee"],
            "quote_amount_in_with_lp_fee": with_lp,
            "user_quote_amount_in": with_lp + fill["protocol_fee"] + fill["coin_creator_fee"],
            "pool": plan["pool"], "user": plan["owner"], "user_base_token_account": fill["holder"],
            "user_quote_token_account": SYSTEM_PROGRAM, "protocol_fee_recipient": SYSTEM_PROGRAM,
            "protocol_fee_recipient_token_account": SYSTEM_PROGRAM, "coin_creator": fill["coin_creator"],
            "coin_creator_fee_basis_points": fill["creator_bps"], "coin_creator_fee": fill["coin_creator_fee"],
            "track_volume": False, "total_unclaimed_tokens": 0, "total_claimed_tokens": 0, "current_sol_volume": 0,
            "last_update_timestamp": timestamp, "min_base_amount_out": 0, "ix_name": PROVENANCE,
            "cashback_fee_basis_points": 0, "cashback": 0, "buyback_fee_basis_points": 0, "buyback_fee": 0}


def encode_buy_event(e):
    """The `Program data:` line pumpswap_buy.parse_buy_event reads back field for field."""
    from solders.pubkey import Pubkey
    from sniper.execution.pumpswap_buy import _BUY_EVENT_FIELDS, BUY_EVENT_DISCRIMINATOR
    parts = [BUY_EVENT_DISCRIMINATOR]
    for name, kind in _BUY_EVENT_FIELDS:
        v = e[name]
        if kind == "i64":
            parts.append(struct.pack("<q", int(v)))
        elif kind == "u64":
            parts.append(struct.pack("<Q", int(v)))
        elif kind == "pubkey":
            parts.append(bytes(Pubkey.from_string(v)))
        elif kind == "bool":
            parts.append(b"\x01" if v else b"\x00")
        elif kind == "string":
            raw = str(v).encode()
            parts.append(struct.pack("<I", len(raw)) + raw)
        else:
            raise ValueError("unknown BuyEvent field kind %s" % kind)
    return "Program data: " + base64.b64encode(b"".join(parts)).decode()
