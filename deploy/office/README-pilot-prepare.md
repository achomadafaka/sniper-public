# Pilot preparation script

`scripts/office_pilot_activate.sh` prepares files and enables the fixed Office service allowlist. It never starts, stops or restarts a service, runs a canary, seeds an org, initializes a database, copies credentials, edits `.env` or takes a funded action. Canary, seed and supervisor start remain Alpha's manual steps. Telemetry/guard start also remains manual.

The script runs its own process at nice 19 / idle IO. It requires the repository owner, a full commit SHA on local `main`, and a finalized, externally SHA-pinned bundle. No fetch or branch change occurs. Release worktree: `.runtime/office/releases/<sha>`; `.runtime/office/current` is only an entrypoint alias. Model mounts must use the actual content-addressed directory, never that alias.

## Bundle

Pass a directory containing `manifest.json`, `runtime.json`, `runtime.sha256`, `transport.json`, `launcher.json`, `launcher.sha256` and `monitor.json`. The manifest has these exact fields:

```json
{
  "schema": "office-pilot-activation-v1",
  "approved_by": "alpha",
  "main_sha": "<full reviewed main SHA>",
  "repo_root": "/home/bigpapi/Desktop/Sniper",
  "release_root": "/home/bigpapi/Desktop/Sniper/.runtime/office/releases/<same SHA>",
  "guard_manifest_sha256": "<hash of that commit's config/office/host_guard_pilot.json bytes>",
  "office_slice_dropin_sha256": "3db4896d7cb9fbd6e74c5f3b342d0c8209626f66f74b227f7af8caa34542444e",
  "files": {
    "runtime.json": "<SHA256>",
    "runtime.sha256": "<SHA256>",
    "transport.json": "<SHA256>",
    "launcher.json": "<SHA256>",
    "launcher.sha256": "<SHA256>",
    "monitor.json": "<SHA256>"
  }
}
```

`runtime.sha256` contains the runtime JSON SHA256. `launcher.sha256` contains the reviewed launcher JSON's SHA256, not its own hash. The outer `--bundle-sha256` is Alpha's independently approved hash of `manifest.json`; the script never computes an approval for itself. Draft versions and null fields refuse. The preserved Delta scratch drafts are incomplete and cannot be installed directly. Config bytes are copied exactly, without filling unknowns or rewriting paths. The installer statically validates the runtime field set, discovery flags, limits, paths, manager-derived cgroup, transport binding, source config hashes and Alpha-approved prompt bytes against the 9ba7125b7 contract. It never imports or executes the runtime factory. `runtime.sha256` pins the exact runtime JSON bytes; `transport.json` installs at `data/office-control/transport.json`. The supervisor unit mounts the host filesystem read-only and only explicitly binds writable state. Actual mount proof remains a manual runtime gate.

## Commands and outcomes

Use the real finalized bundle and reviewed main SHA in place of placeholders:

```sh
scripts/office_pilot_activate.sh --repo /home/bigpapi/Desktop/Sniper --sha <SHA> --bundle <DIR> --bundle-sha256 <HASH>
scripts/office_pilot_activate.sh --repo /home/bigpapi/Desktop/Sniper --sha <SHA> --bundle <DIR> --bundle-sha256 <HASH> --apply
scripts/office_pilot_activate.sh --repo /home/bigpapi/Desktop/Sniper --sha <SHA> --bundle <DIR> --bundle-sha256 <HASH> --verify
scripts/office_pilot_activate.sh --repo /home/bigpapi/Desktop/Sniper --sha <SHA> --bundle <DIR> --bundle-sha256 <HASH> --rollback --apply
```

- Default is a dry-run. It reads Git/config/manager state and uses a disposable unit-verification directory; it does not install or enable anything. `--rollback` without `--apply` also plans only.
- Exit **0**: valid dry-run, successful rollback, or **preparation** verification. It never grants activation, canary, seed or funded authority.
- Exit **3**: `verification_pending`, with every absent/unverified live check named. Installed files and enabled-not-started units stay prepared. Alpha can run `--verify` after the manual telemetry/guard steps; verification changes nothing.
- Exit **2**: refusal/error. Mismatched bytes, modes, symlinks, loaded unit fragments/drop-ins, non-main commits and altered recovery journals refuse. Operational installation failure rolls back only verified owned artifacts. A tampered or dirty artifact makes rollback itself refuse with `recovery_required`; changed bytes remain untouched for operator recovery.

Only absent or identical files are accepted; there is no automatic upgrade/overwrite mode. Identical pre-existing files keep their bytes, mode and inode. The journal records prior file presence and enable state before mutations. Rollback requires inactive Office services, validates the complete fixed file/unit set before acting, preserves pre-existing artifacts, removes only unchanged files created by this preparation, and never removes data/DBs. Nonempty state directories and recovery metadata remain. Dirty/partial release worktrees are not force-deleted.

State directories use 0700, installed configs 0600 and units 0644. The user manager's `XDG_CONFIG_HOME` unit directory is respected. Unit definition checks and base-cap/guard/health reads do not prove provider identity, child IO pre-exec or actual four-seat runtime behavior; those remain the independently reviewed manual gates.

The existing slice drop-in is mandatory, mode 0644, with the exact approved v2 hash above; the installer never rewrites it. The user manager currently reports `/user.slice/user-1000.slice/user@1000.service`; expected Office cgroup is `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/office.slice`. An absent leaf is `verification_pending`, never verified readiness. No start was used to derive this path.


Supervisor identity uses the exact reviewed unit argv spelling: `<repo>/.runtime/office/current/scripts/office_supervisor.py`, in both runtime and launcher JSON. This is the sole argv identity exception; release/config/model mount roots stay literal content-addressed paths. An absent `current` is planned against the exact release without creating anything during dry-run. Apply creates the release and journals the new alias, then checks the exact target before enabling units; rollback removes only an alias it created. An existing wrong target, non-symlink alias, symlinked parent/target component or non-supervisor argv refuses without replacement. UID/cgroup/PID peer checks remain unchanged.

### Release-local Claude SDK/CLI

Do not install the Office SDK into the shared pyenv. Download the exact wheel with
`pip download --no-deps --only-binary=:all: --require-hashes -r deploy/office/requirements.txt`.
Place it in the private bundle as `claude-sdk.whl`; include the source helper's
`artifact_manifest()` under manifest `claude_sdk` and `runtime_pins(release_root)`
under runtime `claude_sdk`. Existing six config payload pins stay required.

The installer verifies the wheel and unpacks it only under the immutable release's
`vendor/claude_sdk`. Supervisor and Claude-child startup verify SDK version, wheel,
extracted bytes, bundled CLI hash and minimum CLI version. The isolated model child
prepends that vendor directory; the existing read-only `/opt/office` mount covers
it. Shared SDK consumers and the mutable `~/.local/bin/claude` are untouched.
Rollback removes only newly installed, verified vendor bytes.
