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
        self.p1, self.p2 = _free_port(), _free_port()
        saved_origins = list(server.ALLOWED_ORIGINS)
        self.addCleanup(lambda: server.ALLOWED_ORIGINS.__setitem__(slice(None), saved_origins))
        patches = [
            mock.patch.object(server, "PORT", server.PORT),
            mock.patch.object(server, "APP_URL", server.APP_URL),
            mock.patch.object(server, "_candidate_ports", lambda: [self.p1, self.p2]),
            mock.patch.object(server, "_ensure_runtime_dirs"),
            mock.patch.object(server, "_setup_logging"),
            mock.patch("webbrowser.open"),
            mock.patch("threading.Timer"),
            mock.patch.object(server.uvicorn, "Server"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.browser_open, self.timer, self.uvicorn_server = mocks[5], mocks[6], mocks[7]
        self.served = []
        self.uvicorn_server.return_value.run.side_effect = \
            lambda sockets: self.served.extend(s.getsockname() for s in sockets)

    def _hold(self, port, version_body):
        holder = _Holder(port, version_body)
        self.addCleanup(holder.close)

    def test_free_port_hands_bound_socket_to_uvicorn(self):
        self.assertEqual(server.run_server(open_browser=True), 0)
        self.assertEqual(self.served, [("127.0.0.1", self.p1)])
        self.timer.assert_called_once()
        (sock,) = self.uvicorn_server.return_value.run.call_args.kwargs["sockets"]
        self.assertEqual(sock.fileno(), -1)  # closed once the server returns

    def test_foreign_listener_is_skipped_for_the_next_port(self):
        self._hold(self.p1, None)  # e.g. `python -m http.server` — 404 on /version
        self.assertEqual(server.run_server(open_browser=True), 0)
        self.assertEqual(self.served, [("127.0.0.1", self.p2)])
        url = "http://localhost:{}".format(self.p2)
        self.assertEqual(server.APP_URL, url)
        self.assertIn(url, server.ALLOWED_ORIGINS)
        self.assertIn("http://127.0.0.1:{}".format(self.p2), server.ALLOWED_ORIGINS)
        self.assertNotIn("http://localhost:{}".format(self.p1), server.ALLOWED_ORIGINS)
        self.assertEqual(self.timer.call_args.args[0], 1.5)

    def test_foreign_json_without_version_is_not_mistaken_for_fetchforge(self):
        self._hold(self.p1, {"status": "ok"})
        self.assertEqual(server.run_server(open_browser=True), 0)
        self.assertEqual(self.served, [("127.0.0.1", self.p2)])
        self.browser_open.assert_not_called()

    def test_running_fetchforge_is_reused(self):
        self._hold(self.p1, {"version": "9.9.9"})
        self.assertEqual(server.run_server(open_browser=True), 0)
        self.browser_open.assert_called_once_with("http://localhost:{}".format(self.p1))
        self.uvicorn_server.assert_not_called()

    def test_fetchforge_on_a_fallback_port_is_found_and_reused(self):
        self._hold(self.p1, None)
        self._hold(self.p2, {"version": "9.9.9"})
        self.assertEqual(server.run_server(open_browser=True), 0)
        self.browser_open.assert_called_once_with("http://localhost:{}".format(self.p2))
        self.uvicorn_server.assert_not_called()

    def test_all_ports_foreign_fails_without_opening_browser(self):
        self._hold(self.p1, None)
        self._hold(self.p2, None)
        with self.assertLogs("fetchforge", "ERROR") as logs:
            self.assertEqual(server.run_server(open_browser=True), 1)
        self.assertIn("all in use", logs.output[-1])
        self.browser_open.assert_not_called()
        self.timer.assert_not_called()
        self.uvicorn_server.assert_not_called()

    def test_bind_refuses_a_live_listener(self):
        self._hold(self.p1, None)
        self.assertIsNone(server._bind_listen_socket(self.p1))


if __name__ == "__main__":
    unittest.main()
