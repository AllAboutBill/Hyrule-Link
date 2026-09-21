"""A whole co-op round, end to end, with no browser: two fake games
(tools/fake_snes.py), the real server (uvicorn server.app:app) and
tools/coop_check.js, which links both games with the page's own modules
(web/js/items.js, effects.js, agent.js, hud.js, snes.js) under node.

The harness prints each step; this test only runs it and wants exit 0. Skips
without node 22+ (the harness needs node's global WebSocket). Ports are fixed
so they never meet a real bridge: server 5919, fakes 23174 and 23274 (control
23175 and 23275), never 23074.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = ROOT / "tools" / "coop_check.js"

HOST = "127.0.0.1"
SERVER_PORT = 5919
FAKE_PORTS = (23174, 23274)
HARNESS_TIMEOUT = 90
START_TIMEOUT = 20


def _node_major():
    if not NODE:
        return 0
    try:
        out = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.match(r"v(\d+)", out.strip())
    return int(m.group(1)) if m else 0


NODE_MAJOR = _node_major()


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def _get_json(url):
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def _tail(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


@unittest.skipIf(NODE_MAJOR < 22, f"needs node 22+ for the harness (found {NODE_MAJOR or 'none'})")
class CoopRoundTest(unittest.TestCase):
    def setUp(self):
        busy = [p for p in (SERVER_PORT, *FAKE_PORTS, *(p + 1 for p in FAKE_PORTS)) if not _port_free(p)]
        if busy:
            self.fail(f"port(s) {busy} already in use: a server or fake from another run is "
                      "still up; stop it and run again")
        self.tmp = tempfile.TemporaryDirectory(prefix="hl-coop-", ignore_cleanup_errors=True)
        self.procs = []     # (name, Popen, log path, log file)
        self.addCleanup(self._stop_all)

    def _spawn(self, name, args, env):
        log_path = os.path.join(self.tmp.name, name + ".log")
        log = open(log_path, "w", encoding="utf-8")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(args, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **kw)
        self.procs.append((name, proc, log_path, log))
        return proc

    def _stop_all(self):
        for _name, proc, _path, log in self.procs:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(10)
            except Exception:
                pass
            finally:
                log.close()
        self.procs = []
        self.tmp.cleanup()

    def _logs(self):
        return "\n".join(f"--- {name} ---\n{_tail(path)}" for name, _p, path, _l in self.procs)

    def _wait_up(self, name, proc, url):
        end = time.monotonic() + START_TIMEOUT
        while time.monotonic() < end:
            if proc.poll() is not None:
                self.fail(f"{name} exited with {proc.returncode} before it answered\n{self._logs()}")
            try:
                return _get_json(url)
            except (OSError, ValueError):
                time.sleep(0.2)
        self.fail(f"{name} did not answer {url} within {START_TIMEOUT} s\n{self._logs()}")

    def test_two_players_one_round(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONUNBUFFERED"] = "1"
        env["HYRULELINK_DB"] = os.path.join(self.tmp.name, "hyrulelink.db")
        env["HYRULELINK_OPERATOR_KEY_FILE"] = os.path.join(self.tmp.name, "no-operator.key")

        fakes = []
        for port in FAKE_PORTS:
            proc = self._spawn(f"fake-{port}", [sys.executable, os.path.join("tools", "fake_snes.py"),
                                                "--port", str(port), "--host", HOST], env)
            fakes.append((port, proc))
        server = self._spawn("server", [sys.executable, "-m", "uvicorn", "server.app:app",
                                        "--host", HOST, "--port", str(SERVER_PORT),
                                        "--log-level", "warning"], env)

        health = self._wait_up("server", server, f"http://{HOST}:{SERVER_PORT}/api/health")
        self.assertTrue(health.get("ok"), health)
        for port, proc in fakes:
            doc = self._wait_up(f"fake-{port}", proc, f"http://{HOST}:{port + 1}/")
            self.assertIn("strip", doc)

        started = time.monotonic()
        try:
            done = subprocess.run(
                [NODE, str(HARNESS), "--server", f"http://{HOST}:{SERVER_PORT}",
                 "--a", f"ws://{HOST}:{FAKE_PORTS[0]}", "--b", f"ws://{HOST}:{FAKE_PORTS[1]}"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=HARNESS_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            self.fail(f"coop_check.js ran past {HARNESS_TIMEOUT} s\n{out}\n{self._logs()}")
        took = time.monotonic() - started

        report = f"{done.stdout}{done.stderr}"
        self.assertEqual(done.returncode, 0,
                         f"coop_check.js exited {done.returncode} after {took:.1f} s\n{report}\n{self._logs()}")
        self.assertIn("\nPASS", report)
        for n in range(1, 8):
            self.assertRegex(report, rf"(?m)^step {n} .* ok \(", f"step {n} did not report ok\n{report}")
        for name, proc, _path, _log in self.procs:
            self.assertIsNone(proc.poll(), f"{name} died during the round\n{self._logs()}")


if __name__ == "__main__":
    unittest.main()
