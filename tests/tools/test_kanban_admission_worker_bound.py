"""worker_memory_max_bytes public alias (plan rev2 §5.5, §9).

``tools/process_registry._worker_memory_max_bytes`` is the per-worker
MemoryMax the admission headroom floor sizes against. The BUILD adds a public
alias with NO behavior change; these tests pin identity with the private
function and the safe-bound arithmetic (min of cgroup limit, phys/2, 4 GiB).
"""

from __future__ import annotations

from pathlib import Path


def test_public_alias_is_the_private_function():
    from tools import process_registry as pr

    assert pr.worker_memory_max_bytes is pr._worker_memory_max_bytes


def test_worker_bound_capped_at_4gib(monkeypatch, tmp_path):
    """A huge host still bounds each worker at 4 GiB."""
    from tools import process_registry as pr

    monkeypatch.setattr(Path, "read_text", lambda self, **k: "0::/\n")
    monkeypatch.setattr(
        Path, "stat", lambda self: type("S", (), {"st_mtime_ns": 0})(), raising=False)
    # /sys/fs/cgroup/<relative>/memory.max read is patched via read_text: the
    # FIRST read is /proc/self/cgroup ("0::/"), the second is memory.max.
    reads = iter(["0::/\n", str(64 * 1024**3)])

    def fake_read(self, encoding=None, **k):
        return next(reads)

    monkeypatch.setattr(Path, "read_text", fake_read)
    monkeypatch.setattr(
        pr.os, "sysconf", lambda name: {
            "SC_PHYS_PAGES": 1024**2, "SC_PAGE_SIZE": 4096}[name])
    assert pr.worker_memory_max_bytes() == 4 * 1024**3


def test_worker_bound_half_of_small_physical(monkeypatch):
    """A 1 GiB host: phys/2 = 512 MiB wins over the 4 GiB cap."""
    from tools import process_registry as pr

    monkeypatch.setattr(
        Path, "read_text",
        lambda self, **k: (_ for _ in ()).throw(OSError("no cgroup")))
    monkeypatch.setattr(
        pr.os, "sysconf", lambda name: {
            "SC_PHYS_PAGES": 1024 * 1024 // 4, "SC_PAGE_SIZE": 4096}[name])
    # phys = 1 GiB -> half = 512 MiB.
    assert pr.worker_memory_max_bytes() == 512 * 1024**2


def test_worker_bound_default_without_system_data(monkeypatch):
    """No cgroup, no sysconf -> the 1 GiB default."""
    from tools import process_registry as pr

    monkeypatch.setattr(
        Path, "read_text",
        lambda self, **k: (_ for _ in ()).throw(OSError("no cgroup")))
    monkeypatch.setattr(
        pr.os, "sysconf", lambda name: (_ for _ in ()).throw(OSError("no sysconf")))
    assert pr.worker_memory_max_bytes() == 1 * 1024**3


def test_admission_module_caches_worker_bound(monkeypatch):
    """The admission module resolves the bound once per process (plan §9)."""
    from hermes_cli import kanban_admission as ka

    calls = {"n": 0}
    real = ka._resolve_worker_bound

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(ka, "_resolve_worker_bound", counting)
    assert ka._worker_bound_cached() == ka._worker_bound_cached()
    assert calls["n"] == 1
