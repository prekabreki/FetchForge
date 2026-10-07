"""run_server's handling of a port that is free, held by FetchForge, or held by something else."""
import http.server
import json
import socket
import threading
import unittest
from unittest import mock

from fetchforge import server


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Holder:
    """A real HTTP listener on PORT answering /version with `version_body` (None → 404)."""

    def __init__(self, port, version_body):
        body = version_body

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/version" and body is not None:
                    payload = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_error(404)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestPortConflict(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        patches = [
            mock.patch.object(server, "PORT", self.port),
            mock.patch.object(server, "APP_URL", "http://localhost:{}".format(self.port)),
            mock.patch.object(server, "_ensure_runtime_dirs"),
            mock.patch.object(server, "_setup_logging"),
            mock.patch("webbrowser.open"),
            mock.patch("threading.Timer"),
            mock.patch.object(server.uvicorn, "Server"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.browser_open, self.timer, self.uvicorn_server = mocks[4], mocks[5], mocks[6]

    def _hold(self, version_body):
        holder = _Holder(self.port, version_body)
        self.addCleanup(holder.close)

    def test_foreign_listener_fails_without_opening_browser(self):
        self._hold(None)  # e.g. `python -m http.server` — 404 on /version
        with self.assertLogs("fetchforge", "ERROR") as logs:
            rc = server.run_server(open_browser=True)
        self.assertEqual(rc, 1)
        self.assertIn("already in use", logs.output[0])
        self.browser_open.assert_not_called()
        self.timer.assert_not_called()
        self.uvicorn_server.assert_not_called()

    def test_foreign_json_without_version_is_not_mistaken_for_fetchforge(self):
        self._hold({"status": "ok"})
        self.assertEqual(server.run_server(open_browser=True), 1)
        self.browser_open.assert_not_called()

    def test_running_fetchforge_is_reused(self):
        self._hold({"version": "9.9.9"})
        rc = server.run_server(open_browser=True)
        self.assertEqual(rc, 0)
        self.browser_open.assert_called_once_with("http://localhost:{}".format(self.port))
        self.uvicorn_server.assert_not_called()

    def test_free_port_hands_bound_socket_to_uvicorn(self):
        seen = []
        self.uvicorn_server.return_value.run.side_effect = \
            lambda sockets: seen.extend(s.getsockname() for s in sockets)
        rc = server.run_server(open_browser=True)
        self.assertEqual(rc, 0)
        self.timer.assert_called_once()
        self.assertEqual(seen, [("127.0.0.1", self.port)])
        (sock,) = self.uvicorn_server.return_value.run.call_args.kwargs["sockets"]
        self.assertEqual(sock.fileno(), -1)  # closed once the server returns

    def test_bind_refuses_a_live_listener(self):
        self._hold(None)
        self.assertIsNone(server._bind_listen_socket())


if __name__ == "__main__":
    unittest.main()
