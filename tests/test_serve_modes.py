"""The server can listen on a unix socket / inherited fd (systemd socket activation) so no TCP port is needed."""
from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path):
    from apigate.crypto import Vault
    e = dict(os.environ, APIGATE_DATA_DIR=str(tmp_path / "data"), APIGATE_MASTER_KEY=Vault.generate_master(), PYTHONPATH=str(ROOT))
    return e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(path: str, uds: str | None = None, port: int | None = None, tries: int = 100):
    for _ in range(tries):
        try:
            if uds:
                with httpx.Client(transport=httpx.HTTPTransport(uds=uds), timeout=3) as c:
                    return c.get("http://gate/healthz")
            return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=3)
        except (httpx.TransportError, OSError):
            time.sleep(0.1)
    raise AssertionError("server did not come up")


@pytest.mark.skipif(sys.platform == "win32", reason="unix sockets")
def test_uds_mode_permissions_and_health_port(tmp_path):
    sock, port = str(tmp_path / "run" / "g.sock"), _free_port()
    p = subprocess.Popen([sys.executable, "-m", "apigate.cli", "serve", "--uds", sock, "--health-port", str(port)], env=_env(tmp_path), cwd=ROOT)
    try:
        assert _get("/healthz", uds=sock).json() == {"ok": True}
        assert _get("/healthz", port=port).json() == {"ok": True}
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        assert mode == 0o660, oct(mode)                      # never world accessible
    finally:
        p.send_signal(signal.SIGTERM)
        assert p.wait(15) in (0, -signal.SIGTERM)


@pytest.mark.skipif(sys.platform == "win32", reason="unix sockets")
def test_socket_activation_queues_requests_during_restart(tmp_path):
    """Emulates systemd: the parent owns the listening socket; the server is restarted; a request sent in the gap is answered, not refused."""
    sock_path = str(tmp_path / "act.sock")
    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lsock.bind(sock_path)
    lsock.listen(64)
    env = _env(tmp_path)

    wrapper = ("import os,sys; fd=int(sys.argv[1]); os.dup2(fd, 3) if fd != 3 else None; os.set_inheritable(3, True); "
               "os.execv(sys.executable, [sys.executable, '-m', 'apigate.cli', 'serve', '--fd', '3'])")

    def start():          # like systemd: the listening socket arrives as fd 3
        return subprocess.Popen([sys.executable, "-c", wrapper, str(lsock.fileno())], env=env, cwd=ROOT,
                                pass_fds=[lsock.fileno()], stderr=subprocess.DEVNULL)
    p1 = start()
    try:
        assert _get("/healthz", uds=sock_path).json() == {"ok": True}
        p1.send_signal(signal.SIGTERM)
        assert p1.wait(15) in (0, -signal.SIGTERM)
        # server is down now; a client connecting in the gap must be accepted by the kernel and wait
        result = {}

        import threading

        def client():
            with httpx.Client(transport=httpx.HTTPTransport(uds=sock_path), timeout=20) as c:
                result["r"] = c.get("http://gate/healthz")
        t = threading.Thread(target=client, daemon=True)
        t.start()
        time.sleep(1.0)
        assert t.is_alive()                                   # still waiting in the backlog
        p2 = start()
        try:
            t.join(20)
            assert result["r"].status_code == 200
        finally:
            p2.send_signal(signal.SIGTERM)
            p2.wait(15)
    finally:
        if p1.poll() is None:
            p1.kill()
        lsock.close()


def test_client_ip_without_peer_trusts_forwarded_header():
    from types import SimpleNamespace
    from apigate.server.deps import client_ip
    req = SimpleNamespace(client=None, headers={"x-forwarded-for": "203.0.113.9"},
                          app=SimpleNamespace(state=SimpleNamespace(gw=SimpleNamespace(settings=SimpleNamespace(proxies=set())))))
    assert client_ip(req) == "203.0.113.9"


def test_backup_command(tmp_path, monkeypatch, capsys):
    from apigate.cli import main
    from apigate.crypto import Vault
    monkeypatch.setenv("APIGATE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("APIGATE_MASTER_KEY", Vault.generate_master())
    assert main(["init"]) == 0
    assert main(["backup"]) == 0
    files = list((tmp_path / "d" / "backups").glob("gate-*.db"))
    assert len(files) == 1 and files[0].stat().st_size > 0


@pytest.mark.skipif(sys.platform == "win32", reason="unix sockets")
def test_two_inherited_sockets_unix_for_proxy_and_loopback_tcp_for_local_apps(tmp_path):
    """systemd .socket with a unix socket (nginx) AND 127.0.0.1:PORT (other local projects): both reach the same server."""
    sock_path = str(tmp_path / "two.sock")
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    u.bind(sock_path)
    u.listen(64)
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.bind(("127.0.0.1", 0))
    t.listen(64)
    port = t.getsockname()[1]
    wrapper = ("import os,sys; a,b=int(sys.argv[1]),int(sys.argv[2]); os.dup2(a,3); os.dup2(b,4); os.set_inheritable(3,True); os.set_inheritable(4,True); "
               "os.execv(sys.executable,[sys.executable,'-m','apigate.cli','serve','--fd','3','--fd','4'])")
    p = subprocess.Popen([sys.executable, "-c", wrapper, str(u.fileno()), str(t.fileno())], env=_env(tmp_path), cwd=ROOT,
                         pass_fds=[u.fileno(), t.fileno()], stderr=subprocess.DEVNULL)
    try:
        assert _get("/healthz", uds=sock_path).json() == {"ok": True}
        assert _get("/healthz", port=port).json() == {"ok": True}
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(15)
        u.close()
        t.close()


def test_healthcheck_command_exit_codes():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from apigate.cli import main

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert main(["healthcheck", "--port", str(srv.server_port)]) == 0
    finally:
        srv.shutdown()
    assert main(["healthcheck", "--port", str(_free_port()), "--timeout", "1"]) == 1
