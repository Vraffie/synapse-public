"""ADR 023 for the Codex provider: every real codex spawn is journaled, and a restart fences what a crash left behind.

Mirrors the Claude provider tests in test_spawn_journal_runtime.py, using the fake codex executable.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

from synapse.infrastructure.mock_process_identity import ProcessStatus
from synapse.infrastructure.spawn_journal import FenceReport, SpawnJournal
from synapse.providers import codex_cli
from synapse.providers.codex_cli import CodexCliProvider
from test_codex_cli_provider import env, profile, request, run  # noqa: F401  (env is a fixture)


def alive(pid):
    try:
        raw = open(f"/proc/{pid}/stat").read()
    except FileNotFoundError:
        return False
    return raw[raw.rfind(")") + 2] not in "ZX"


def entries(journal):
    return [json.loads((journal._dir / (k + ".json")).read_text()) for k in journal.pending()]


def test_codex_journal_entry_exists_while_the_agent_runs_and_is_gone_after_every_ending(env, tmp_path):  # noqa: F811
    journal = SpawnJournal(tmp_path / "j")
    mid = []

    async def watch(prompt, cancel):
        prov = CodexCliProvider(profile(env), spawn_journal=journal)
        handle = await prov.start(request(env, prompt))
        events = []

        async def consume():
            async for e in prov.stream(handle):
                events.append(e)
        task = asyncio.create_task(consume())
        if cancel:
            await asyncio.sleep(0.6)
            mid.append(entries(journal))
            await prov.cancel(handle)
        await asyncio.wait_for(task, 30)
        return events

    for prompt, cancel in (("ok", False), ("fail", False), ("hang", True)):
        events = asyncio.run(watch(prompt, cancel))
        assert events[-1].kind in ("succeeded", "failed", "cancelled")
        assert journal.pending() == []  # removed once the process was reaped
    assert len(mid) == 1 and len(mid[0]) == 1
    entry = mid[0][0]
    assert isinstance(entry["pid"], int) and entry["state"] != "starting"  # a real pid was recorded mid-run
    # No prompt text, argv, env or credentials in the entry: only the fixed identity keys.
    assert set(entry) == {"v", "state", "pid", "start_ticks", "boot_id"}
    assert "hang" not in json.dumps(entry)


def test_codex_failed_spawn_leaves_no_entry_and_an_unjournalable_spawn_never_happens(env, tmp_path):  # noqa: F811
    journal = SpawnJournal(tmp_path / "j")
    prov = CodexCliProvider(profile(env), spawn_journal=journal)
    os.chmod(env[1], 0o644)  # probe/start already passed; the exec itself now fails
    try:
        handle, ev = asyncio.run(run(prov, request(env, "ok")))
    finally:
        os.chmod(env[1], 0o755)
    assert prov._execution(handle).spawn_failed and journal.pending() == []

    class Broken(SpawnJournal):
        def begin(self, key):
            raise OSError(28, "no space")
    prov = CodexCliProvider(profile(env), spawn_journal=Broken(tmp_path / "j2"))
    handle, ev = asyncio.run(run(prov, request(env, "ok")))
    ex = prov._execution(handle)
    assert ev[-1].kind == "failed" and ex.spawn_failed
    assert ex.process is None  # cannot journal, so nothing was started


def test_codex_entry_is_kept_when_the_stop_status_is_unknown(env, tmp_path, monkeypatch):  # noqa: F811
    journal = SpawnJournal(tmp_path / "j")
    prov = CodexCliProvider(profile(env), spawn_journal=journal)
    real = codex_cli.stop_mock_child_group

    def unknown(process, identity, grace_seconds):
        real(process, identity, grace_seconds=grace_seconds)  # really stop it, but report it as unprovable
        return ProcessStatus.UNKNOWN
    monkeypatch.setattr(codex_cli, "stop_mock_child_group", unknown)
    asyncio.run(run(prov, request(env, "hang"), cancel_after=0.6))
    assert len(journal.pending()) == 1  # left for the next startup's fence
    assert SpawnJournal(tmp_path / "j").fence_all() == FenceReport(1, 0, 0)  # the fence proves it dead
    assert journal.pending() == []


CRASHER = r'''
import asyncio, json, sys, time
sys.path.insert(0, sys.argv[1])
from test_codex_cli_provider import profile, request
from synapse.infrastructure.spawn_journal import SpawnJournal
from synapse.providers.codex_cli import CodexCliProvider
from pathlib import Path
env = (Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]))
journal = SpawnJournal(Path(sys.argv[6]))

async def main():
    prov = CodexCliProvider(profile(env), spawn_journal=journal)
    handle = await prov.start(request(env, "hang"))
    async def consume():
        async for _ in prov.stream(handle):
            pass
    asyncio.create_task(consume())
    while True:
        pids = [json.loads((journal._dir / (k + ".json")).read_text()).get("pid") for k in journal.pending()]
        if pids and isinstance(pids[0], int):
            print(pids[0], flush=True)
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(120)  # the parent SIGKILLs this "daemon" here

asyncio.run(main())
'''


def test_a_crashed_daemons_orphan_codex_agent_is_killed_by_the_fence_at_restart(env, tmp_path):  # noqa: F811
    jdir = tmp_path / "spawn-journal"
    script = tmp_path / "crasher.py"
    script.write_text(CRASHER)
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    daemon = subprocess.Popen([sys.executable, str(script), tests_dir, *map(str, env), str(jdir)],
                              stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    pid = None
    try:
        pid = int(daemon.stdout.readline())
        assert alive(pid)
        daemon.send_signal(signal.SIGKILL)  # the codex child is in its own session and keeps running
        daemon.wait(timeout=10)
        assert alive(pid)
        report = SpawnJournal(jdir).fence_all()  # "restart": a fresh journal object over the same directory
        assert report == FenceReport(0, 1, 0)
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not alive(pid)
        assert SpawnJournal(jdir).pending() == []
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def test_the_runtimes_journal_wrapped_factory_builds_a_codex_provider(env, tmp_path):  # noqa: F811
    # AgentRuntime wraps its provider factory with functools.partial(..., spawn_journal=...). Codex profiles are not
    # routed to the runtime yet (ADR 016), but a Codex factory under that wrapper must construct and journal.
    from synapse.daemon.agent_runtime import AgentRuntime
    from synapse.persistence.migrations import open_database
    journal = SpawnJournal(tmp_path / "j")
    db = open_database(tmp_path / "s.db")
    try:
        rt = AgentRuntime(db, tmp_path / "blobs", provider_factory=CodexCliProvider, spawn_journal=journal)
        prov = rt._factory(profile(env))
    finally:
        db.close()
    assert isinstance(prov, CodexCliProvider) and prov._journal is journal
