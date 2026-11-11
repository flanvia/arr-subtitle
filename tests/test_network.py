import concurrent.futures
import socket
import ssl
import threading
import unittest
import zlib
from unittest.mock import Mock, patch

from arr_subtitle.models import Budget, Cancelled, CollectorError
from arr_subtitle.network import Response, Session, public_address, redact, validate_url


_SOCKET = socket.socket


class NetworkTests(unittest.TestCase):
    def socket_session(self, serve, *, memory_limit=128 * 1024 * 1024, context=None):
        with patch("socket.socket", _SOCKET):
            client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.settimeout(2)
        sock = Mock(wraps=client)
        sock.connect = Mock()
        errors = []

        def run():
            try:
                serve(server)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                errors.append(error)
            finally:
                server.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def finish():
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(finish)
        socket_patch = patch("arr_subtitle.network.socket.socket", return_value=sock)
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        if context is not None:
            context_patch = patch("arr_subtitle.network.ssl.create_default_context", return_value=context)
            context_patch.start()
            self.addCleanup(context_patch.stop)
        session = Session(interval=0, memory_limit=memory_limit)
        session._resolve = lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        self.addCleanup(session.close)
        return session, client

    def test_rejects_non_public_destinations(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "::ffff:127.0.0.1", "2002:0808:0808::"):
            self.assertFalse(public_address(address), address)
        for url in ("file:///etc/passwd", "http://127.0.0.1", "http://user:pw@example.com", "http://example.com:8080", "https://example.com\r\nHeader: value"):
            with self.assertRaises(ValueError, msg=url):
                validate_url(url)

    def test_shares_inflight_request(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def transport(url, method, body, timeout, limit):
            calls.append(url)
            started.set()
            self.assertTrue(release.wait(2))
            return Response(url, b"ok")
        with Session(transport, interval=0) as session, concurrent.futures.ThreadPoolExecutor(2) as pool:
            one = pool.submit(session.get, "https://example.com/a", Budget())
            self.assertTrue(started.wait(2))
            two = pool.submit(session.get, "https://example.com/a", Budget())
            release.set()
            self.assertEqual(one.result(), two.result())
            self.assertEqual(len(calls), 1)
            self.assertEqual(session.counts["example.com"], 1)

    def test_scope_applies_to_shared_and_cached_result(self):
        with Session(lambda url, *args: Response("https://other.example.com/", b"ok"), interval=0) as session:
            session.get("https://example.com/", Budget())
            with self.assertRaises(CollectorError):
                session.get("https://example.com/", Budget(), scope=lambda url: url.startswith("https://example.com/"))

    def test_response_limit_and_cancellation(self):
        with Session(lambda url, *args: Response(url, b"1234"), interval=0) as session:
            with self.assertRaises(ValueError):
                session.get("https://example.com/", Budget(), limit=3)
            event = threading.Event()
            event.set()
            with self.assertRaises(Cancelled):
                session.get("https://example.com/", Budget(cancel=event))

    def test_redacts_query_and_preserves_naver_identity(self):
        self.assertEqual("https://example.com/a", redact("https://example.com/a?token=secret"))
        self.assertEqual("https://blog.naver.com/author/123", redact("https://blog.naver.com/PostView.naver?blogId=author&logNo=123"))

    def test_header_deadline_interrupts_continuous_progress(self):
        interrupted = threading.Event()

        def serve(server):
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            while not interrupted.wait(0.01):
                server.sendall(b"a")

        session, _ = self.socket_session(serve)
        self.addCleanup(interrupted.set)
        budget = Budget(0.15)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(session.get, "http://example.com/", budget)
            try:
                with self.assertRaises(Cancelled):
                    future.result(timeout=1)
            finally:
                interrupted.set()
        self.assertFalse(session._pending)
        self.assertEqual(session._active_bytes, 0)

    def test_cancellation_interrupts_stalled_body_after_connection_close_header(self):
        body_started = threading.Event()

        def serve(server):
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 100\r\n\r\nx")
            body_started.set()
            self.assertEqual(server.recv(1), b"")

        session, _ = self.socket_session(serve)
        budget = Budget()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(session.get, "http://example.com/", budget)
            self.assertTrue(body_started.wait(1))
            budget.cancel.set()
            with self.assertRaises(Cancelled):
                future.result(timeout=1)
        self.assertEqual(session._active_bytes, 0)
        self.assertFalse(session._cache)

    def test_cancellation_interrupts_tls_handshake(self):
        handshake_started = threading.Event()
        context = Mock()

        def serve(server):
            self.assertEqual(server.recv(1), b"")

        session, client = self.socket_session(serve, context=context)
        tls = Mock(wraps=client)

        def handshake():
            handshake_started.set()
            client.recv(1)

        tls.do_handshake = handshake
        context.wrap_socket.return_value = tls
        budget = Budget()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(session.get, "https://example.com/", budget)
            self.assertTrue(handshake_started.wait(1))
            budget.cancel.set()
            with self.assertRaises(Cancelled):
                future.result(timeout=1)
        self.assertEqual(context.wrap_socket.call_args.kwargs, {"server_hostname": "example.com", "do_handshake_on_connect": False})

    def test_tls_certificate_failure_is_not_retried_without_verification(self):
        context = Mock()

        def serve(server):
            self.assertEqual(server.recv(1), b"")

        session, client = self.socket_session(serve, context=context)
        tls = Mock(wraps=client)
        tls.do_handshake = Mock(side_effect=ssl.SSLCertVerificationError("Invalid certificate"))
        context.wrap_socket.return_value = tls
        with self.assertRaisesRegex(CollectorError, "HTTP request failed"):
            session.get("https://example.com/", Budget())
        tls.do_handshake.assert_called_once()
        self.assertFalse(session._cache)

    def test_https_uses_verified_context_and_original_hostname(self):
        context = Mock()

        def serve(server):
            request = server.recv(4096)
            self.assertIn(b"Host: example.com\r\n", request)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        session, client = self.socket_session(serve, context=context)
        tls = Mock(wraps=client)
        tls.do_handshake = Mock()
        context.wrap_socket.return_value = tls
        self.assertEqual(session.get("https://example.com/", Budget()).data, b"ok")
        tls.do_handshake.assert_called_once()
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "example.com")

    def test_rejects_truncated_content_length_without_caching(self):
        def serve(server):
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\npartial")

        session, _ = self.socket_session(serve)
        with self.assertRaisesRegex(CollectorError, "Truncated HTTP response"):
            session.get("http://example.com/", Budget())
        self.assertFalse(session._cache)
        self.assertFalse(session._pending)
        self.assertEqual(session._active_bytes, 0)

    def test_accepts_complete_content_length_and_close_delimited_body(self):
        for headers in (b"Content-Length: 2\r\n", b"Connection: close\r\n"):
            with self.subTest(headers=headers):
                def serve(server):
                    server.recv(4096)
                    server.sendall(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\nok")

                session, _ = self.socket_session(serve)
                response = session.get("http://example.com/", Budget())
                self.assertEqual(response.data, b"ok")
                self.assertEqual(session._retained, 2)
                self.assertEqual(session._active_bytes, 0)

    def test_memory_budget_includes_response_copy(self):
        def serve(server):
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 60\r\n\r\n" + b"x" * 60)

        session, _ = self.socket_session(serve, memory_limit=100)
        with self.assertRaisesRegex(CollectorError, "session memory limit"):
            session.get("http://example.com/", Budget())
        self.assertEqual(session._active_bytes, 0)
        self.assertFalse(session._cache)

    def test_active_response_stays_reserved_until_cache_transfer(self):
        first_ready, release_first = threading.Event(), threading.Event()
        original_response = Response
        def response(*args, **kwargs):
            result = original_response(*args, **kwargs)
            first_ready.set()
            self.assertTrue(release_first.wait(2))
            return result

        def serve(server):
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n" + b"x" * 40)

        session, _ = self.socket_session(serve, memory_limit=100)
        with patch("arr_subtitle.network.Response", side_effect=response), concurrent.futures.ThreadPoolExecutor(1) as pool:
            first = pool.submit(session.get, "http://example.com/", Budget())
            try:
                self.assertTrue(first_ready.wait(1))
                self.assertEqual(session._active_bytes, 80)
                with self.assertRaises(CollectorError):
                    session._reserve(30)
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=1).data, b"x" * 40)
        self.assertEqual(session._active_bytes, 0)
        self.assertEqual(session._retained, 40)
        session._reserve(60)
        self.assertEqual(session._retained + session._active_bytes, 100)
        session._reserve(1)
        self.assertEqual(session._retained, 0)
        self.assertEqual(session._active_bytes, 61)
        session._active_bytes = 0

    def test_completed_body_remains_charged_before_cache_transfer(self):
        ready, release = threading.Event(), threading.Event()

        def serve(server):
            server.recv(4096)
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n" + b"x" * 40)

        session, _ = self.socket_session(serve, memory_limit=100)
        send = session._send

        def pause(*args):
            response = send(*args)
            ready.set()
            self.assertTrue(release.wait(2))
            return response

        session._send = pause
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(session.get, "http://example.com/", Budget())
            try:
                self.assertTrue(ready.wait(1))
                self.assertEqual(session._active_bytes, 40)
                session._reserve(60)
                with self.assertRaises(CollectorError):
                    session._reserve(1)
            finally:
                release.set()
            future.result(timeout=1)
        self.assertEqual(session._retained + session._active_bytes, 100)
        self.assertEqual(session._retained, 40)
        session._active_bytes -= 60

    def test_compressed_response_uses_bounded_expansion_chunks(self):
        data = b"x" * 100000
        compressed = zlib.compress(data)

        def serve(server):
            server.recv(4096)
            headers = b"HTTP/1.1 200 OK\r\nContent-Encoding: deflate\r\nContent-Length: " + str(len(compressed)).encode() + b"\r\n\r\n"
            server.sendall(headers + compressed)

        session, _ = self.socket_session(serve, memory_limit=250000)
        reservations = []
        reserve = session._reserve

        def record(size):
            reserve(size)
            reservations.append((size, session._retained + session._active_bytes))

        session._reserve = record
        self.assertEqual(session.get("http://example.com/", Budget()).data, data)
        self.assertTrue(all(total <= session.memory_limit for _, total in reservations))
        self.assertTrue(all(size <= 65536 for size, _ in reservations[:-1]))
        self.assertEqual(session._active_bytes, 0)
