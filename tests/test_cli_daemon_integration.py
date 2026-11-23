"""Exercise the operator CLI against the actual daemon over a Unix socket."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


def test_agentctl_health_and_replay_against_real_daemon(tmp_path):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        db.execute(
            "INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) "
            "VALUES('cli-event','system','system','test',1,'{}','now')"
        )
    daemon = subprocess.Popen(
        [sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path),
         '--migrations', str(ROOT / 'migrations')],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    def cli(*args):
        return subprocess.run(
            [sys.executable, '-m', 'synapse.cli', '--state-dir', str(tmp_path), *args],
            capture_output=True, text=True, timeout=5,
        )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                raise AssertionError(f'daemon exited: {daemon.stderr.read().decode()}')
            if (tmp_path / 'agentd.sock').exists() and (tmp_path / 'operator.token').exists():
                health = cli('health')
                if health.returncode == 0:
                    break
            time.sleep(0.02)
        else:
            raise AssertionError('daemon did not become ready')
        assert json.loads(health.stdout) == {'status': 'ok'}
        assert health.stderr == ''
        events = cli('events', '--after', '0', '--limit', '1')
        assert events.returncode == 0, events.stderr
        page = json.loads(events.stdout)['events']
        assert len(page) == 1 and page[0]['id'] == 'cli-event'
        after = cli('events', '--after', str(page[0]['sequence']))
        assert after.returncode == 0, after.stderr
        assert json.loads(after.stdout)['events'] == []
        token = (tmp_path / 'operator.token').read_text()
        assert all(token not in result.stdout + result.stderr for result in (health, events, after))
    finally:
        if daemon.poll() is None:
            daemon.send_signal(signal.SIGTERM)
        daemon.communicate(timeout=8)
    assert daemon.returncode == 0
    assert not (tmp_path / 'agentd.sock').exists()
