"""Task 6 — build script contract for the pinned TPU sidecar artifact.

Exercises ``scripts/build_memecoin_tpu_sidecar.sh`` with a FAKE docker (no real compile): a 40-hex
source SHA is required, the artifact is SHA-keyed and path-confined, the exported binary is mode 0555,
the manifest carries the exact { path, sha256, source_sha, cargo_lock_sha256, target,
build_image_digest } fields, an existing artifact is never overwritten, and no binary lands in the
repo tree.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_memecoin_tpu_sidecar.sh"
CTX = REPO_ROOT / "rust" / "solana-tpu-sidecar"
GOOD_SHA = "a" * 40
BASE_IMAGE_DIGEST = "sha256:c1e5f19e773b7878c3f7a805dd00a495e747acbdc76fb2337a4ebf0418896b33"


@pytest.fixture
def fake_docker(tmp_path):
    p = tmp_path / "fake_docker.sh"
    p.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$0.argv\"\n"
        "dest=\"\"; prev=\"\"\n"
        "for a in \"$@\"; do\n"
        "  if [ \"$prev\" = \"-o\" ]; then dest=\"${a#type=local,dest=}\"; fi\n"
        "  prev=\"$a\"\n"
        "done\n"
        "[ -n \"$dest\" ] || { echo 'fake docker: no dest' >&2; exit 9; }\n"
        "printf 'FAKE-TPU-BINARY' > \"$dest/memecoin-tpu-sidecar\"\n"
        "echo 'fake docker build ok'\n"
    )
    p.chmod(0o755)
    return str(p)


def _run(release_root, artifact_root, sha, *, docker):
    env = dict(os.environ, DOCKER=docker)
    return subprocess.run(
        ["bash", str(SCRIPT), str(release_root), str(artifact_root), sha],
        capture_output=True, text=True, env=env)


def test_refuses_short_sha(tmp_path, fake_docker):
    art = tmp_path / "art"
    res = _run(REPO_ROOT, art, "abc123", docker=fake_docker)
    assert res.returncode != 0
    assert "40-hex" in (res.stderr + res.stdout)
    assert not art.exists()                              # nothing produced for a bad sha


def test_builds_binary_mode_0555_and_full_metadata(tmp_path, fake_docker):
    art = tmp_path / "art"
    res = _run(REPO_ROOT, art, GOOD_SHA, docker=fake_docker)
    assert res.returncode == 0, res.stderr
    binp = art / GOOD_SHA / "memecoin-tpu-sidecar"
    assert binp.is_file()
    assert (binp.stat().st_mode & 0o777) == 0o555       # owner/group/other read+exec, no write
    meta = json.loads((art / GOOD_SHA / "metadata.json").read_text())
    assert set(meta) == {"path", "sha256", "source_sha", "cargo_lock_sha256", "target",
                         "build_image_digest"}
    assert meta["source_sha"] == GOOD_SHA
    assert meta["target"] == "x86_64-unknown-linux-gnu"
    assert meta["build_image_digest"] == BASE_IMAGE_DIGEST
    assert meta["path"] == str(binp)
    assert meta["sha256"] == hashlib.sha256(b"FAKE-TPU-BINARY").hexdigest()
    assert meta["cargo_lock_sha256"] == hashlib.sha256(
        (CTX / "Cargo.lock").read_bytes()).hexdigest()


def test_artifact_is_path_confined_to_sha_dir(tmp_path, fake_docker):
    art = tmp_path / "art"
    _run(REPO_ROOT, art, GOOD_SHA, docker=fake_docker)
    produced = {p for p in art.rglob("*") if p.is_file()}
    # every produced file lives under art/<sha>/
    assert produced and all(str(p).startswith(str(art / GOOD_SHA)) for p in produced)


def test_existing_artifact_is_never_overwritten(tmp_path, fake_docker):
    art = tmp_path / "art"
    (art / GOOD_SHA).mkdir(parents=True)
    binp = art / GOOD_SHA / "memecoin-tpu-sidecar"
    binp.write_bytes(b"OLD-DEPLOYED-BINARY")
    # a fake docker that would CLOBBER if ever called (asserts the script skips the build)
    clobber = tmp_path / "clobber_docker.sh"
    clobber.write_text("#!/usr/bin/env bash\necho SHOULD-NOT-RUN >&2\nexit 1\n")
    clobber.chmod(0o755)
    res = _run(REPO_ROOT, art, GOOD_SHA, docker=str(clobber))
    assert res.returncode == 0
    assert binp.read_bytes() == b"OLD-DEPLOYED-BINARY"   # untouched
    assert "SHOULD-NOT-RUN" not in res.stderr            # docker never invoked


def test_no_binary_is_written_into_the_repo_tree(tmp_path, fake_docker):
    art = tmp_path / "art"
    _run(REPO_ROOT, art, GOOD_SHA, docker=fake_docker)
    # the artifact is exported to the external artifact-root, never committed under rust/
    stray = list((REPO_ROOT / "rust").rglob("memecoin-tpu-sidecar"))
    assert stray == []


def test_build_budget_is_two(tmp_path, fake_docker):
    result = _run(REPO_ROOT, tmp_path / "art", GOOD_SHA, docker=fake_docker)
    assert result.returncode == 0
    assert "CARGO_BUILD_JOBS=2" in Path(fake_docker + ".argv").read_text().splitlines()
