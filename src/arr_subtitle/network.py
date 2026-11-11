from __future__ import annotations

import concurrent.futures
import email.message
import http.client
import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from .models import Budget, CollectorError


def public_address(address):
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified and not getattr(ip, "sixtofour", None) and not getattr(ip, "teredo", None)


def validate_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Unsafe destination URL")
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None or parts.port not in (None, 80, 443):
            raise ValueError("Unsafe destination URL")
        host = parts.hostname.encode("idna").decode("ascii")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not host or any(not label or len(label) > 63 for label in host.rstrip(".").split(".")):
                raise ValueError("Invalid destination hostname")
        else:
            if not public_address(str(address)):
                raise ValueError("Non-public destination")
    except (UnicodeError, TypeError) as error:
        raise ValueError("Unsafe destination URL") from error
    return parts


def redact(url):
    try:
        parts = validate_url(url)
    except ValueError:
        return "invalid-url"
    if parts.hostname in ("blog.naver.com", "m.blog.naver.com"):
        query = parse_qs(parts.query)
        blog, post = query.get("blogId", [""])[0], query.get("logNo", [""])[0]
        if blog and all(c.isascii() and (c.isalnum() or c in "_-") for c in blog) and post.isascii() and post.isdigit():
            return f"https://blog.naver.com/{blog}/{post}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def json_data(data):
    value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")))
    def check(item, depth=0):
        if depth > 32:
            raise ValueError("JSON exceeds depth limit")
        if isinstance(item, dict):
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
    check(value)
    return value


@dataclass(frozen=True)
class Response:
    url: str
    data: bytes
    content_type: str = ""
    filename: str | None = None

    @property
    def text(self):
        for encoding in ("utf-8-sig", "cp949"):
            try:
                return self.data.decode(encoding)
            except UnicodeDecodeError:
                pass
        raise ValueError("Unsupported response encoding")


class Session:
    def __init__(self, transport=None, interval=1.0, *, memory_limit=128 * 1024 * 1024, request_started=None):
        self.transport = transport
        self.interval = interval
        self.memory_limit = memory_limit
        self.request_started = request_started
        self._lock = threading.RLock()
        self._global = threading.BoundedSemaphore(4)
        self._hosts = {}
        self._starts = {}
        self._pending = {}
        self._cache = OrderedDict()
        self._retained = 0
        self._active_bytes = 0
        self._dns_slots = threading.BoundedSemaphore(4)
        self._counts = {}
        self._received = 0
        self._closed = False

    @property
    def counts(self):
        with self._lock:
            return dict(self._counts)

    @property
    def received_bytes(self):
        with self._lock:
            return self._received

    def close(self):
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._retained = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, url, budget, limit=4 * 1024 * 1024, scope=None):
        return self._fetch(url, "GET", None, budget, limit, scope)

    def post_json(self, url, body, budget):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return self._fetch(url, "POST", data, budget, 4 * 1024 * 1024, None)

    def _fetch(self, url, method, body, budget, limit, scope):
        validate_url(url)
        budget.check()
        if scope is not None and not scope(url):
            raise CollectorError("UnsupportedSource", "URL outside registered blog scope")
        key = (url, method, body, limit)
        with self._lock:
            if self._closed:
                raise RuntimeError("Session is closed")
            for old in list(self._cache):
                if time.monotonic() - self._cache[old][0] >= 30:
                    self._retained -= len(self._cache.pop(old)[1].data)
            if key in self._cache:
                response = self._cache[key][1]
                if scope is not None and not scope(response.url):
                    raise CollectorError("UnsupportedSource", "Cached response outside blog scope")
                return response
            owner = key not in self._pending
            future = self._pending.setdefault(key, concurrent.futures.Future())
        if not owner:
            while True:
                budget.check()
                try:
                    response = future.result(timeout=min(0.1, budget.remaining))
                    if scope is not None and not scope(response.url):
                        raise CollectorError("UnsupportedSource", "Shared response outside blog scope")
                    return response
                except concurrent.futures.TimeoutError:
                    continue
        charged = 0
        try:
            response, charged = self._send(url, method, body, budget, limit, scope)
            budget.check()
            with self._lock:
                if self._closed:
                    raise RuntimeError("Session is closed")
                self._active_bytes -= charged
                charged = 0
                self._cache[key] = (time.monotonic(), response)
                self._retained += len(response.data)
            future.set_result(response)
            return response
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self._lock:
                self._active_bytes -= charged
                self._pending.pop(key, None)

    def _reserve(self, size):
        with self._lock:
            while self._cache and self._retained + self._active_bytes + size > self.memory_limit:
                _, (_, previous) = self._cache.popitem(last=False)
                self._retained -= len(previous.data)
            if self._retained + self._active_bytes + size > self.memory_limit:
                raise CollectorError("SearchIncomplete", "Active responses exceed session memory limit")
            self._active_bytes += size

    @staticmethod
    def _acquire(lock, budget):
        while not lock.acquire(timeout=min(0.1, budget.remaining)):
            budget.check()
        try:
            budget.check()
        except BaseException:
            lock.release()
            raise

    def _send(self, url, method, body, budget, limit, scope):
        for _ in range(6):
            parts = validate_url(url)
            if scope is not None and not scope(url):
                raise CollectorError("UnsupportedSource", "Redirect outside registered blog scope")
            with self._lock:
                host_lock = self._hosts.setdefault(parts.hostname, threading.Lock())
            self._acquire(host_lock, budget)
            try:
                with self._lock:
                    delay = self.interval - (time.monotonic() - self._starts.get(parts.hostname, -float("inf")))
                if delay > 0:
                    budget.wait(delay)
                self._acquire(self._global, budget)
                try:
                    with self._lock:
                        self._starts[parts.hostname] = time.monotonic()
                        self._counts[parts.hostname] = self._counts.get(parts.hostname, 0) + 1
                    if self.request_started:
                        self.request_started(redact(url))
                    if self.transport is not None:
                        response = self.transport(url, method, body, min(10, budget.remaining), limit)
                        if not isinstance(response, Response) or not isinstance(response.data, bytes):
                            raise ValueError("Invalid transport response")
                        validate_url(response.url)
                        if scope is not None and not scope(response.url):
                            raise CollectorError("UnsupportedSource", "Response outside registered blog scope")
                        if len(response.data) > limit:
                            raise ValueError("Response exceeds size limit")
                        budget.check()
                        self._reserve(len(response.data))
                        with self._lock:
                            self._received += len(response.data)
                        return response, len(response.data)
                    response, location, status, charged = self._request(url, method, body, budget, limit)
                    if location is None:
                        return response, charged
                finally:
                    self._global.release()
            finally:
                host_lock.release()
            url = urljoin(url, location)
            if status == 303 or status in (301, 302) and method == "POST":
                method, body = "GET", None
        raise CollectorError("SearchIncomplete", "Redirect limit reached")

    def _request(self, url, method, body, budget, limit):
        budget = budget.child(10)
        parts = validate_url(url)
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = self._resolve(host, port, budget)
        if not addresses or any(not public_address(item[4][0]) for item in addresses):
            raise CollectorError("UnsupportedSource", "DNS returned non-public destination")
        timeout = min(10, budget.remaining)
        context = ssl.create_default_context() if parts.scheme == "https" else None
        connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context) if context is not None else http.client.HTTPConnection(host, port, timeout=timeout)
        sockets, socket_lock = [], threading.Lock()
        finished = threading.Event()

        def register(sock):
            with socket_lock:
                sockets.append(sock)
                if budget.cancelled:
                    sock.close()
                    budget.check()

        def interrupt():
            while not finished.wait(min(0.05, budget.remaining)):
                if budget.cancelled:
                    with socket_lock:
                        for sock in sockets:
                            try:
                                sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        # Wake every reader before closing potentially shared descriptors.
                        for sock in sockets:
                            sock.close()
                    return

        def connect():
            last = None
            for family, socktype, proto, _, address in addresses:
                budget.check()
                sock = socket.socket(family, socktype, proto)
                try:
                    register(sock)
                    sock.settimeout(min(timeout, budget.remaining))
                    sock.connect(address)
                    if context is not None:
                        # Register the TLS socket before a blocking handshake can start.
                        sock = context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                        register(sock)
                        sock.do_handshake()
                    budget.check()
                    connection.sock = sock
                    return
                except (OSError, ValueError) as error:
                    sock.close()
                    budget.check()
                    if not isinstance(error, OSError):
                        raise
                    last = error
            raise OSError("Public destination connection failed") from last

        connection.connect = connect
        watcher = threading.Thread(target=interrupt, daemon=True)
        watcher.start()
        charged = 0
        handed_off = False
        result = None
        try:
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            headers = {"User-Agent": "ArrSubtitle/0.2", "Accept-Encoding": "gzip, deflate"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            result = connection.getresponse()
            if result.status in (301, 302, 303, 307, 308):
                location = result.getheader("Location")
                if not location:
                    raise CollectorError("SearchIncomplete", "Redirect has no location")
                budget.check()
                return None, location, result.status, 0
            if not 200 <= result.status < 300:
                raise CollectorError("SearchIncomplete", f"HTTP {result.status} request failed")
            encoding = result.getheader("Content-Encoding", "").lower()
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding in ("gzip", "deflate") else None
            if encoding and encoding not in ("gzip", "deflate", "identity"):
                raise ValueError("Unsupported HTTP content encoding")
            data = bytearray()
            transferred = 0
            while True:
                budget.check()
                if connection.sock:
                    connection.sock.settimeout(min(10, budget.remaining))
                chunk = result.read1(32768)
                if not chunk:
                    break
                transferred += len(chunk)
                with self._lock:
                    self._received += len(chunk)
                if transferred > limit:
                    raise ValueError("Response exceeds size limit")
                while chunk:
                    budget.check()
                    expanded = decoder.decompress(chunk, min(32768, limit + 1 - len(data))) if decoder else chunk
                    size = len(expanded)
                    self._reserve(size * 2)
                    charged += size * 2
                    data.extend(expanded)
                    del expanded
                    with self._lock:
                        self._active_bytes -= size
                        charged -= size
                    if len(data) > limit:
                        raise ValueError("Expanded response exceeds size limit")
                    chunk = decoder.unconsumed_tail if decoder else b""
            budget.check()
            if result.length not in (None, 0):
                raise CollectorError("SearchIncomplete", "Truncated HTTP response")
            if decoder and not decoder.eof:
                raise ValueError("Truncated compressed response")
            message = email.message.Message()
            message["Content-Disposition"] = result.getheader("Content-Disposition", "")
            filename = message.get_filename()
            if filename and (len(filename) > 1024 or any(ord(c) < 32 for c in filename)):
                raise ValueError("Invalid response filename")
            self._reserve(len(data))
            charged += len(data)
            response = Response(url, bytes(data), result.getheader("Content-Type", ""), filename)
            size = len(data)
            data.clear()
            with self._lock:
                self._active_bytes -= size
                charged -= size
            budget.check()
            # Keep the immutable body charged until _fetch transfers it to the cache.
            handed_off = True
            return response, None, result.status, charged
        except (OSError, http.client.HTTPException, ValueError) as error:
            budget.check()
            if isinstance(error, ValueError):
                raise
            raise CollectorError("SearchIncomplete", "HTTP request failed") from error
        finally:
            finished.set()
            watcher.join()
            try:
                if result is not None:
                    result.close()
                connection.close()
            finally:
                for sock in sockets:
                    sock.close()
                with self._lock:
                    if not handed_off:
                        self._active_bytes -= charged

    def _resolve(self, host, port, budget):
        self._acquire(self._dns_slots, budget)
        result = queue.Queue(maxsize=1)
        def resolve():
            try:
                result.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
            except Exception as error:
                result.put((False, error))
            finally:
                self._dns_slots.release()
        threading.Thread(target=resolve, daemon=True).start()
        while True:
            budget.check()
            try:
                ok, value = result.get(timeout=min(0.1, budget.remaining))
            except queue.Empty:
                continue
            if not ok:
                raise CollectorError("SearchIncomplete", "DNS lookup failed") from value
            return value
