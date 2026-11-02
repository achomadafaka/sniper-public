# Office recovery after a planned funded deploy — approval draft

Status: SPEC ONLY. No implementation, key provisioning, policy activation, service restart, or `.env` change is authorized by this document. Operator yes is required before code.

Owner Delta; Alpha reviews the operator decision. Suggested implementation reviewers: Zeta (failure and concurrency boundaries), Codex (Office runtime/sandbox), Gamma (measurement labels).

## Decision requested

Prefer **normal pause, then verified fast recovery**. Keep the existing `funded_heartbeat` trip and Office termination behavior. A valid planned-deploy record can shorten the subsequent 900-second healthy wait only after the funded runner has actually recovered. It cannot make a stale heartbeat healthy.

The alternative, suppressing Office pause during an outage, creates a blind interval: an expected stopped runner and a failed restart can look identical until the deadline. It is not proposed in v1. A plain SHA-256 checksum detects corruption but does not authenticate a marker; anyone who can replace the payload can replace its checksum. Use HMAC-SHA256 with a separate host key, or retain current behavior if that trust boundary cannot be established.

Proposed bounds for operator approval: one non-extendable **300-second** window, armed immediately before the verified old-runner stop, and **5 seconds** of consecutive ordinary healthy telemetry plus at least two advancing funded-heartbeat timestamps before fast recovery. These are proposal values, not measured guarantees or current policy. The existing cron handoff can wait 135 seconds, followed by up to 60 seconds of candidate verification; hence a window shorter than 195 seconds can expire on an ordinary supported path. Expiry is safe: normal 900-second recovery applies.

## Current source contract

- `scripts/office_host_monitor.py:health_sample` reads the funded heartbeat and marks invalid authentication, unknown timestamps, or future timestamps as telemetry errors. It preserves actual heartbeat age.
- `scripts/office_watchdog.py:PilotGuard.check` trips `funded_heartbeat` after two breaches sampled at least two seconds apart, at age over five seconds. Ordinary recovery records `healthy_since` and becomes eligible after 900 seconds. CPU, IO, memory, telemetry, roster, resource and containment checks are separate.
- `start_or_resume` and automatic resume both enforce the ordinary readiness contract. A deployment exception must be explicit in both paths; do not fake an earlier `healthy_since` or set the ordinary 900-second flag early.
- `scripts/deploy_memecoin_runtime.sh` holds its deployment lock through the cutover, validates before stopping, drains entries/exits, writes `.memecoin_deploy_intent`, stops the exact old runner, launches a pinned release and verifies it. `new_runner_verified` precedes clearing its owned entry gate; successful completion removes the intent. Rollback retains its existing reconciliation and process checks.
- The intent contains only a SHA. It remains an alarm/recovery witness and is **not** sufficient exception authority. No deploy tests, reservation checks, provider checks, signing fences, rollback checks, entry/exit policy or backup defaults change.

## Trust boundary and deployment prerequisite

Add one dedicated host-auth key at a fixed path outside all Office-writable directories, proposed `%h/.config/sniper-host-auth/planned-deploy.key`. Do not reuse funded custody, provider, web, crew, model or `.env` credentials. Deploy and the host guard may read the key; Office agents, supervisor, launcher, children and research VM may not read it or ask a signing oracle to sign arbitrary content.

File mode 0600 alone is insufficient: Office components run under the same host UID and currently see much of the host filesystem read-only. Before enabling the exception, mask the key directory in **every** Office/model/launcher namespace and prove that path, `/proc` root/fd/environ access and inherited descriptors cannot expose it. The marker path is read-only/inaccessible to Office writers. The host signer has no Office-callable tool or endpoint. No key material is copied into telemetry, logs, prompts or child environments.

If this boundary cannot be proved, the feature stays disabled. An arbitrary host compromise with the deploy/guard's own authority is outside this protocol's guarantee; a model-generated marker or an Office-writable JSON file is never authority.

Activation is an explicit crew/operator-approved guard-policy/config revision. Missing key/config/marker means existing behavior. No dynamic env knob or untrusted request can opt in. Initial scope is ordinary code-only cutovers: policy activations, provider staging and any early-stop path not covered by this lifecycle retain normal 900-second recovery. Covering those paths needs a separate reviewed amendment, not a marker written after the runner already stopped.

## Record protocol

One fixed, bounded file, proposed `data/memecoin_deploy_hold.json`, is written by the deployment helper using same-directory temp + file fsync + atomic rename + directory fsync. Reader opens without following symlinks, enforces a regular file, expected owner/mode, exact schema and a 4 KiB maximum. Read/parse/authentication failures mean **no exception**, not an empty successful record. They do not themselves pause a genuinely healthy Office.

Canonical JSON payload, excluding the MAC:

- protocol/version and key identifier;
- random deploy nonce, fixed state-root identity, old and target full release SHAs;
- issuer UID, deploy PID and process-birth ticks, boot ID, and the approved deployment-script path/digest;
- pre-stop old-runner PID/birth identity, heartbeat timestamp, `auth_ok=true` and verification timestamp;
- issued and expires-at readings from Linux `CLOCK_BOOTTIME`, with `expires - issued <= 300 seconds`;
- diagnostic host/corrected UTC and clock provenance (not expiration authority);
- phase `armed`, `completed` or `aborted`;
- on completion: actual resulting SHA, runner PID/birth ticks, verification time and completion kind `target_healthy` or `rollback_healthy`.

HMAC covers every payload field and canonical bytes. Reject unknown fields/versions, malformed/non-finite/bool-as-number values, bad release identity, future issuance, lifetime above the approved maximum, wrong boot or a bad MAC. Compare MACs in constant time. Logs show only typed status, nonce/digest identifiers and release/process provenance, never the key.

Expiry uses boottime so NTP, corrected-clock adjustment and suspend cannot stretch a signed window. Existing host-time heartbeat checks remain unchanged; a skewed/future heartbeat is unknown and cannot qualify recovery.

## Issuer lifecycle

1. Complete the existing gate, entry drain and required exit drain while the old runner is healthy. Do **not** issue a marker before the long test gate or before an unbounded drain.
2. Immediately before the exact old-runner stop, under the existing deploy serialization, issue `armed` only if the old identity and fresh authenticated heartbeat are proven. Bind the initial nonce, boot, deadline, old SHA and target SHA. Store enough prior-healthy provenance for the guard to associate the first outage with this window.
3. Run the existing cutover unchanged. No heartbeat-age substitution and no exemption from funded readiness checks. Any failure follows the existing rollback path.
4. Publish `completed` only after exact runner PID/birth/path, expected pinned SHA, actual authenticated fresh heartbeat, provider readiness and existing reconciliation have passed, the deploy-owned entry hold has cleared, success is established and `.memecoin_deploy_intent` is absent. Bind the verified resulting runner. A verified rollback may publish `rollback_healthy` for its actual restored SHA; an uncertain rollback may not.
5. Failure after arming writes `aborted` best effort. Crash/SIGKILL, missing completion or any failed marker write leaves normal Office recovery in effect. This optional protocol must not stall, fail or reroute funded execution; it fails by losing the Office shortcut, with a typed diagnostic.

No phase may change the nonce, issuer, boot, release pair, issuance or deadline. No renewal within the same cutover. The existing deploy lock prevents concurrent issuers; conflicting records still invalidate the shortcut. An `armed` marker requires the live issuer PID with matching birth identity and approved script identity. The guard must accept it before the first heartbeat breach while its own baseline is still ready; a missed acceptance simply loses the shortcut. No synchronous acknowledgement wait is added to the deploy. A completed marker may outlive the successfully exited issuer, but still needs its original accepted window and live verified resulting runner.

## Guard state machine

The guard remains outside `office.slice` and continues real telemetry and containment checks.

- **No valid record:** exact current behavior. Bad/stale/forged records never reset or shorten recovery.
- **Valid armed record while ready:** remember this bounded deployment context durably. Office still pauses on the normal heartbeat trip. If Office was already paused, or the heartbeat/telemetry failure began before issuance, the record cannot retroactively reclassify it.
- **Paused during an accepted window:** retain actual `reason=funded_heartbeat`, actual pause time and actual heartbeat age. Track a separate `planned_deploy` attribution; it is not a healthy sample. Any non-heartbeat failure, malformed latch, failed termination/unknown containment, issuer death before completion, abort, identity mismatch, replaced conflicting record or expiry permanently disqualifies the shortcut for that pause.
- **Completed in time:** require the same accepted nonce and fixed deadline, absent deploy intent, the signed actual resulting SHA, one matching live runner identity, and ordinary current health. Gather the proposed five-second healthy sequence and two distinct advancing heartbeat writes from that same process; any failed/stale/future/unknown sample disqualifies it. A failed restart, wrong release, PID reuse or `auth_ok != true` never qualifies.
- **Consume once:** normal automatic/manual readiness paths may resume Office with `recovery_basis=planned_deploy_verified`, the real healthy duration and completion provenance. Keep `healthy_since` truthful; the ordinary 900-second eligibility field remains false until 900 seconds actually pass. Persist consumption before exposing ready state. Subsequent heartbeat failure receives the full normal pause/recovery policy.
- **Disqualified:** the currently paused Office follows ordinary 900-second recovery. Invalid markers do not manufacture or reset the ordinary healthy clock; actual health failures keep the existing reset behavior. A later valid deployment cannot rescue an older unrelated pause.

Persist accepted context, a per-boot monotonic issuance/consumption watermark and the current eligibility/consumption result in the guard-owned bounded state. This makes restart and replay conservative: unknown/lost state disables the shortcut; replay of an earlier nonce/issuance cannot grant it again. The guard's single-writer ownership and atomic writes serialize consumption. No mark-as-ready is allowed before the durable consume record.

The marker is a single latest-record slot, not one file per deploy. The guard does not unlink it (avoids deleting a newer concurrently replaced record). It becomes inert after consumption/expiry and is overwritten by the next issuer. Guard state is fixed-size; no unbounded nonce history. Diagnostic logs use existing rotation. Existing deployment intent retention/alarming is unchanged.

## Edge cases / expected behavior

| Case | Required result |
| --- | --- |
| No marker, missing key, disabled profile | Current pause + ordinary recovery. |
| Bare hash, forged MAC, Office-written marker, invalid owner/symlink | No exception; preserve ordinary guard decisions. |
| Marker from a prior boot, expired/future marker, overwritten nonce | No exception; never refresh its deadline. |
| Old heartbeat already failed before arm, Office already paused | No retroactive shortcut. |
| Deploy crashes before/after old stop | Pause normally; no live issuer/completion means no shortcut. |
| Rollback returns healthy old release | Shortcut only with signed verified rollback completion and matching current process. |
| Wrong release, reused PID, multiple runners, intent still present | No shortcut. |
| New runner fails, auth/telemetry unknown, pressure/roster/caps fail | Full existing pause and recovery. |
| UTC/NTP/offset skew or suspend | Boottime expiry does not extend; ordinary telemetry uncertainty still refuses readiness. |
| Guard restart during outage or after consuming | Persisted context/consumption gates apply; missing/uncertain state disables fast recovery. |
| Complete record replay after normal later failure | Watermark/nonce cannot be consumed again; full recovery. |
| New deploy starts as guard reads old completion | Different fixed nonce cannot extend/replace accepted authority; guard never unlinks a newer marker. |
| Writer cannot persist marker | Funded deploy follows existing behavior; Office shortcut is unavailable. |

## Measurement and required offline proof before any activation

No live latency or funded reliability improvement is claimed. Keep actual Office pause/resume windows, actual process-start and first model-call times separate. A shortcut does not prove model work resumed; require actual call/turn evidence. Do not fill paused time with zero outcomes or fabricate 900 seconds of healthy history.

Future implementation must leave RED/GREEN checks for valid planned recovery, each rejection row above, forged checksum-only records, key masking in every child namespace, descriptor/proc escape attempts, PID/boot/nonce replay, fixed expiry under clock movement, successful and failed rollback, failed marker writes, two guard iterations racing consumption, unrelated failure during a valid hold, and original no-marker behavior byte-equivalent. No paid API, funded order, real stop/restart or production DB mutation in tests.

Proposed touch surface only after operator yes: deploy lifecycle helper/call sites; Office monitor/guard and their approved config; all affected sandbox launch units/preflights; focused deploy/watchdog/monitor/sandbox regression tests. No funded entry/exit implementation, signing code, policy, model prompt, paper scoring or ledger schema changes.

## Rollback and approval

Rollback disables the approved exception profile first; guard immediately uses the existing pause/900-second recovery path. Stale records and key files grant no authority under old code. Existing funded deployment rollback remains independent. Do not use this feature to clear operator pauses or change a funded kill switch.

Operator approval is needed for: (1) normal pause + verified fast recovery versus suppression, (2) HMAC key isolation/provisioning boundary, (3) 300-second hard window and five-second recovery proposal, (4) whether verified rollback is eligible. Until then, spec only; no code or host configuration writes.

## Approved amendment — 2026-10-29, Alpha delegated operator authority

Operator approved normal pause + verified fast recovery, fixed 300 seconds and five healthy seconds plus two advancing funded beats. Scope now includes planned Office stops, including a stopped watchdog/monitor whose exact formerly-ready latch becomes stale. This supersedes the SPEC ONLY status for **offline implementation**, not host activation or key creation. Alpha approved the asymmetric amendment: root-owned Ed25519 private key, public verifier only in Office, root-owned immutable signer, operator-run sudo only, no passwordless/polkit/bus signing endpoint. Provisioning steps must reach Alpha before any key exists.

The old same-UID HMAC design is superseded. The child launcher needs host `/proc` to validate supervisor peers; a path mask alone cannot prove that `/proc/<host-pid>/root` cannot bypass it. The guard never receives a private key. Ed25519 signatures cover exact canonical payload bytes; OpenSSL public verification uses a one-second timeout and at most 4096 bytes. Bad/missing authentication confers no shortcut. No private/test key is generated by the offline suite; its positive crypto witness is RFC8032 public test vector 2.

### Planned Office stop attribution

The root issuer must independently capture the exact fresh ready guard bytes and host/process/release/health observations BEFORE the stop, under stop serialization, and sign the unchanged deadline/nonce. On restart a completed Office-stop record may be accepted without having sampled its armed phase ONLY if the old latch is still byte-equivalent to that signed pre-stop ready latch. It can be stale; it cannot be paused, missing, edited, malformed or an operator pause. Completion is for the target release only; rollback recovery remains excluded pending separate approval. It never authorizes a new epoch, roster, capital or prompt change.

The offline machine consumes only authenticated records plus trusted host observations; marker claims cannot be used as those observations. It does not change `PilotGuard`, `external_guard`, the monitor, deploy/repin helpers, services, or automatic/manual recovery. In particular, it cannot clear the currently live guard_stale latch. A future runtime adapter must inspect valid planned authority before the normal loop labels a stale ready latch malformed, preserve real pause/healthy_since/900-second fields, and persist consume before publishing a new ready latch. No blanket `guard_stale` exemption is allowed.

### Offline implementation and remaining activation gates

`sniper/office/planned_recovery.py` supplies strict signed-envelope reads/public verification, a bounded pure transition function, fixed deadline and immutable completion identity, per-boot issuance watermark, and a durable nonblocking single-writer Store. `advance` and `Store.run` require an already-authenticated record from `read_record`; they are protocol internals, never an untrusted RPC/API. Explicit host initialization is required; missing/corrupt state is not initialized implicitly. Consumption is persisted/fsynced before returning ready. Concurrent consumers obtain at most one ready result. No startup, admission, broker or fill path imports this module.

**Root-owned replay state is also required before activation.** A root-only signing key prevents forgery, but does not prevent rollback of a same-UID writable watermark through a host-process namespace escape. The current Store is an offline fixture, not proof of protected live storage. A future reviewed host adapter must own durable state under root or an independently isolated service identity; a public-verifier sandbox plus a same-UID writable state file is not sufficient. No signing endpoint may be exposed as a solution. If protected state consumption and trusted observations cannot be established, retain existing 900-second/manual behavior. Do not provision the key or install a live recovery unit until this boundary and the signer artifact are reviewed.

Offline witnesses cover valid funded/Office recovery; forged/bare-hash/malformed marker; expiry, future issue and boot mismatch; crash/abort/issuer death; nonce/deadline/phase changes; wrong PID birth/release; existing pause and changed latch; containment/health/intent uncertainty; repeated/future heartbeat; state loss/corruption; disk failure at consume; two independent consumers; and no-marker existing guard regressions. They do NOT prove root filesystem isolation, absence of a signing oracle, live process provenance, or all sandbox `/proc`/fd escape resistance. Those require preactivation tests using a harmless protected canary, not the private key. Existing guard tests are compatibility evidence only because there is no live integration.

### Exact provisioning commands — operator run only, NOT executed

These commands are provided for Alpha review now. They are gated on a reviewed immutable root signer and protected-state adapter, public-key fingerprint approval, and canary namespace proof. No signer install command is supplied until its exact reviewed artifact/SHA exists. Do not install root code from a mutable user checkout or use a user-writable parent path. No sudoers, polkit or user-bus exception may be added.

```sh
sudo install -d -o root -g root -m 0700 /etc/sniper-host-auth
sudo install -d -o root -g root -m 0700 /var/lib/sniper-planned-recovery
sudo install -d -o root -g root -m 0755 /etc/sniper-office-public
sudo /bin/sh -eu -c '
  umask 077
  test ! -e /etc/sniper-host-auth/planned-stop.key
  test ! -L /etc/sniper-host-auth/planned-stop.key
  test ! -e /etc/sniper-office-public/planned-stop.pem
  test ! -L /etc/sniper-office-public/planned-stop.pem
  tmp=$(mktemp /etc/sniper-host-auth/.planned-stop.XXXXXX)
  trap '\''rm -f "$tmp"'\'' EXIT
  /usr/bin/openssl genpkey -algorithm ED25519 -out "$tmp"
  chmod 0600 "$tmp"
  chown root:root "$tmp"
  ln "$tmp" /etc/sniper-host-auth/planned-stop.key
  /usr/bin/openssl pkey -in /etc/sniper-host-auth/planned-stop.key -pubout -out /etc/sniper-office-public/planned-stop.pem
  chmod 0644 /etc/sniper-office-public/planned-stop.pem
  chown root:root /etc/sniper-office-public/planned-stop.pem
  sync -f /etc/sniper-host-auth/planned-stop.key
  sync -f /etc/sniper-host-auth
  sync -f /etc/sniper-office-public/planned-stop.pem
  sync -f /etc/sniper-office-public
'
/usr/bin/openssl pkey -pubin -in /etc/sniper-office-public/planned-stop.pem -outform DER | sha256sum
```

Only the public fingerprint is reported; never cat, copy, log or pass the private key via argv/env/stdio. Root provisioning refuses overwrite; a crash after private creation requires operator inspection, never automatic regeneration or deletion. The operator supplies fresh sudo authentication outside all Office namespaces; no cached authentication or descriptors are passed to Office. Runtime source rollback leaves the shortcut disabled and existing real heartbeat/900-second recovery intact. Key removal/rotation and root-helper installation remain separately reviewed host steps.
