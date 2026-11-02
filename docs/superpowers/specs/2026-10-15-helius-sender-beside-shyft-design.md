# Helius Sender beside the Shyft infra provider (design, 2026-10-15)

Owner: Beta. Ask: Alpha 2026-10-15 ~20:3xZ, after the slot-S read showed that landing in S is decided
by delivery and priority, not by a few ms of decision time (brain finding 15f1b94d). Operator YES for
the Sender A/B test stands from 2026-10-01 (tip 5,000 lamports, SWQoS-only, pre-registration
`docs/audits/2026-10-01-sender-swqos-ab-preregistration.md`).

## Today

- `MEMECOIN_SOLANA_INFRA_PROVIDER=shyft`. Under Shyft, `memecoin_submission.validate_submission_config`
  REFUSES `MEMECOIN_SLOT_LANE_SENDER_AB=1` and `MEMECOIN_SLOT_LANE_SENDER_ALL=ab|all` at startup
  ("conflicts with Shyft-only submission"), and `submission_backends.build_solana_backend` builds the
  Sender provider only `if not shyft`. That exclusion came in with the Shyft migration (006a11fcd,
  2026-10-12), which restored Nozomi beside Shyft but not Sender.
- The Shyft graph: `solana:custody_rpc` (Shyft funded RPC), the TPU sidecar (`auto_free`), and Nozomi
  when its knobs are on. Nozomi went off on 2026-10-15 20:14Z.
- The slot lane's arm (`_sender_arm`, rule `sha256-attempt-odd-v1`) runs only when the executor reports
  `sender_ab_ready`, which is true only when the transport holds a `solana:sender` provider.

## Change

1. **Registration knob** `MEMECOIN_SOLANA_SENDER_WITH_SHYFT` (default `1`). With Shyft and this knob on,
   `validate_submission_config` accepts `MEMECOIN_SLOT_LANE_SENDER_AB=1` (and `SENDER_ALL`), and
   `build_solana_backend` appends `SenderProvider` after custody and TPU, exactly as the non-Shyft
   graph does today. With the knob `0`, today's refusal stands. Kill switch for the test:
   `MEMECOIN_SLOT_LANE_SENDER_AB=0` (existing), which removes the provider and the arm together.
2. **Same bytes, never instead.** The transport sends one signed payload to every provider. Sender
   posts only a payload that carries exactly one Helius tip of at most 10,000 lamports
   (`payload_tip`); an arm A payload (no tip) is `NOT_SENT` there, so arm A is byte- and
   path-identical to today. `guard_refusal` keeps refusing, before signing, any tip the arm did not ask for.
3. **Tip:** fixed `TIP_LAMPORTS = 5,000` on arm B only, to one Helius tip account chosen by the
   attempt hash. No dynamic tip.
4. **Rate.** Keyless Sender allows 1 request per second per IP per region. The slot lane's
   re-sends (150 / 350 ms) go to `solana:tpu` only (`SlotResender` calls `submit_one("solana:tpu")`), so
   Sender gets ONE post per attempt, the first dispatch. Two arm-B sends inside one second can still
   draw a 429 on the second; that post is refused and the other providers carry the same bytes. No
   per-attempt budget is added: one post per attempt is already the minimum, and a budget would only
   drop the post we would otherwise try. The warm ping (`GET /ping` every
   `MEMECOIN_SOLANA_WARM_INTERVAL_S`, 5 s, on the warm thread, never the fill path) stays as today in
   the non-Shyft graph; if the first read shows 429s, the ping is the first suspect.
5. **Outcomes.** As every other provider (`solana_fast_submit._classify_rpc`): a 429 or any other
   non-200 answer, a timeout or a transport loss is AMBIGUOUS for Sender's attempt (`http_429`),
   never an explicit refusal, because a rate-limited request may still have been forwarded. It never
   changes an aggregate that custody or TPU accepted. When Sender is the only carrier (no TPU sidecar,
   custody preflight refused), a Sender accept makes the aggregate ACCEPTED by `solana:sender` and the
   reservation is held until the receipt; a Sender 429 makes it AMBIGUOUS (`submitted_unknown`).
   The read counts 429s from `sender_ab_send.sender_http_status`. The provider's per-identity outcome
   map and lock already exist; no new module state.
6. **Custody identity gating.** Under Shyft with `allow_unavailable_custody`, every provider is
   wrapped in the custody identity gate, so a custody identity failure also gates Sender, as it gates
   Nozomi today.

## Edge cases

- Money path: entry submission. Cost: 5,000 lamports per LANDED arm-B transaction (a failed
  transaction pays no tip). No change to clip, min-out, priority or arm A.
- Speed: the Sender POST runs on its own transport worker, appended LAST, after custody and TPU
  dispatch. Measured before deploy: the first-dispatch time to `solana:tpu` with and without the
  provider in the graph (p50/p90), so "nothing slows the fill" is a number.
- Packet: the tip adds one SystemProgram transfer. A build over the 1,232-byte packet falls back to
  arm A's untipped build (`tip_over_packet`, existing).
- Endpoint: `http://fra-sender.helius-rpc.com/fast?swqos_only=true`, plain HTTP, keyless: no secret on
  the wire; the signed transaction is public once sent.
- Absent / malformed knob: anything but `0|false|off|no` is on (default on); `SENDER_AB` stays the
  operator's switch.
- Rollback: `MEMECOIN_SLOT_LANE_SENDER_AB=0` and restart, or `MEMECOIN_SOLANA_SENDER_WITH_SHYFT=0`
  to restore the startup refusal.

## The check that stays

A test that builds the REAL Shyft backend (`build_solana_backend` with the Shyft env and a fake HTTP
seam) with `SENDER_AB=1`, runs the slot executor's `slot_execute_fn` on an arm-B tipped message, and
asserts that `solana:sender` received exactly the signed bytes that custody and TPU received (sha256
match, one post), and that an arm-A message never reaches Sender. It fails if the provider is not
registered under Shyft or if the Shyft refusal returns.

## First read

Arm B against arm A on the slot lane: landing k (L - S) and in-block index, fill rate, and Sender's
own answer (`sender_ab_send`: state, `sender_http_status`, the count of 429s), in two cohorts split
at the deploy of the 5M fallback bid if the operator sets it.

## If keyless is too tight

Helius Sender with an API key allows 50 requests per second per key per region, uses no credits, and
works on the free plan (Helius docs, read 2026-10-15). It needs a dedicated key, never the shared
research key; creating the account or key is the operator's action. The paid alternative with its
own limit is Nozomi (5 rps per key by default, minimum tip 0.001 SOL, about 200x Sender's tip); it was
turned off on 2026-10-15.
