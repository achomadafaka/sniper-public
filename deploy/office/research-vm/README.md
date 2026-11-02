# Office research VM: offline definition only

Default disabled. No downloader, guest boot, provider/RPC proxy or snapshot producer is installed by this slice.
Missing/unapproved plan, image, quarantine review, snapshots or declared network policy refuses. Non-offline network policies refuse until a separately reviewed host-enforced provider proxy exists; the guest gets no NIC and QEMU has no host network namespace.

The operator approved Debian's `debian-13-nocloud-amd64-20261026-2643.qcow2` (409731072 bytes); Alpha downloaded and verified it. Source: https://cloud.debian.org/images/cloud/trixie/20261026-2643/ . SHA-512 is pinned in `plan.json.template`. Never modify the base: QEMU `-snapshot` creates a fresh temporary qcow2 overlay per run, stored in a 256 MiB private tmpfs. The base and each input are mounted read-only. QEMU option contract: https://qemu-project.gitlab.io/qemu/system/invocation.html .

Before activation Alpha supplies a SHA-256 pinned plan under `data/office-control`, a Gamma-reviewed quarantine manifest, and immutable archive/daily tape COPIES. No live tape, funded ledger, evaluation.db, grade/outcome tables, credentials, repository, crew socket or host bus is exposed. The automated filename/table checks supplement the quarantine review; they cannot prove a dataset lacks disguised labels. Each input is at most 64 MiB, at most 16 files/256 MiB total. Increase these only with an explicit measured host budget.

The export staging path must be a dedicated bounded filesystem of at most 32 MiB, writable by the VM. Ordinary directories on the host's large filesystem refuse: a guest cannot be allowed to fill the host disk. Provisioning that filesystem is an Alpha installation prerequisite, not done here. Export only AFTER the guest stops, via `export_file`; only regular single-link non-executable UTF-8 `.txt` or `.json` files up to 1 MiB are accepted. Every host output gets a random name, mode 0400, SHA-256 and `untrusted_vm_export` label. Nothing is executed, imported, promoted to production or inserted into evaluation memory.

Initial ceiling: 1 vCPU, 1024 MiB guest RAM, 256 MiB overlay, 32 MiB exports. A future runtime must enforce CPUQuota=100%, MemoryMax=1536M, TasksMax=64, idle IO, Nice=19 and a bounded lifetime inside office.slice before any boot. These fit within the approved 4 GiB/2-core Office ceiling but have NOT been calibrated by a guest boot; no VM startup or resource claim follows from the definition.

Runnable checks: `nice -n19 python3 -m pytest -n 2 tests/office/test_research_vm.py`.

The executor uses `pinned_command`: image and input hashes are read through held FDs, the export directory is also held by FD, and `verify()` refuses changed inode identity or metadata before launch. Bwrap receives `--ro-bind-fd` / `--bind-fd`, never a reopened input filename. A concurrent unlink may make bwrap refuse; it must never substitute the replacement bytes. Sources remain operator-owned read-only copies; this does not defend against hostile host processes rewriting an inode after launch. Use the bounded executor, not the path-based `command()` inspection helper. No guest boot is authorized by the source checks.
