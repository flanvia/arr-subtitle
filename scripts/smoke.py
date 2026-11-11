from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import ssl
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from arr_subtitle.engine import Engine
from arr_subtitle.models import Budget, Settings
from arr_subtitle.network import Response, Session

TITLE = "Fixture Show"
BLOG = "https://release-fixture.blogspot.com/"
SUBTITLE = "1\n00:00:01,000 --> 00:00:02,000\n테스트 자막\n".encode("utf-8")


def require(value, message):
    if not value:
        raise AssertionError(message)


class Fixture:
    def __init__(self, batch=False):
        self.batch = batch
        self.calls = []
        self.numbers = range(1, 13) if batch else (1,)
        if batch:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as bundle:
                for number in self.numbers:
                    bundle.writestr(f"{TITLE} - {number:02}.srt", SUBTITLE)
            self.data = buffer.getvalue()
        else:
            self.data = SUBTITLE

    @property
    def filename(self):
        return f"{TITLE} - 01-12.zip" if self.batch else f"{TITLE} - 01.srt"

    @property
    def post_title(self):
        return f"{TITLE} 01-12화" if self.batch else f"{TITLE} 01화"

    @property
    def html(self):
        return f'<a href="https://release-files.example/{self.filename}">{self.filename}</a>'

    def __call__(self, url, method, body, timeout, limit):
        self.calls.append(url)
        parts = urlsplit(url)
        if parts.hostname == "release-files.example":
            return Response(url, self.data, "application/zip" if self.batch else "text/plain", self.filename)
        if parts.hostname == "api.anissia.net":
            anime = {"animeNo": 123, "subject": TITLE, "originalSubject": TITLE}
            if parts.path.startswith("/anime/list/"):
                data = {"content": [anime], "last": True}
            elif parts.path == "/anime/animeNo/123":
                data = anime
            elif parts.path == "/anime/caption/animeNo/123":
                data = [{"episode": str(number), "website": BLOG + "2026/11/fixture.html", "name": "Fixture"} for number in self.numbers]
            else:
                raise AssertionError(f"Unexpected fixture API path: {url}")
            return Response(url, json.dumps({"code": "ok", "data": data}).encode(), "application/json")
        if parts.hostname == "release-fixture.blogspot.com":
            if parts.path == "/feeds/posts/default":
                data = {"feed": {"openSearch$totalResults": {"$t": "1"}, "entry": [
                    {"title": {"$t": self.post_title}, "content": {"$t": self.html},
                     "link": [{"rel": "alternate", "href": BLOG + "2026/11/fixture.html"}]}]}}
                return Response(url, json.dumps(data).encode(), "application/json")
            if parts.path == "/2026/11/fixture.html":
                return Response(url, f'<h1>{self.post_title}</h1><article>{self.html}</article>'.encode(), "text/html")
        raise AssertionError(f"Unexpected fixture network request: {url}")


def smoke_engine(root):
    for batch in (False, True):
        fixture = Fixture(batch)
        with Session(transport=fixture, interval=0) as session:
            engine = Engine(session, Settings(anilist_enabled=False))
            targets = [{"episode": str(number)} for number in fixture.numbers]
            report = engine.collect({"animeId": 123}, targets, str(root / ("batch" if batch else "direct")), Budget())
            require(report["success"], f"Download failed: {report}")
            require(len(report["episodes"]) == len(targets), "Episode count mismatch")
            for result in report["episodes"]:
                require(Path(result["path"]).read_bytes() == SUBTITLE, "Downloaded bytes mismatch")
            require(sum(urlsplit(url).hostname == "release-files.example" for url in fixture.calls) == 1, "Attachment was fetched more than once")
        print("batch: ok" if batch else "direct: ok")
    fixture = Fixture(True)
    with Session(transport=fixture, interval=0) as session:
        engine = Engine(session, Settings(anilist_enabled=False))
        report = engine.search(TITLE, Budget())
        require(report["complete"] and report["success"], f"Search failed: {report}")
        require(len(report["anime"][0]["subtitles"]) == 12, "Search missed batch subtitles")
        event = threading.Event()
        def cancel_after_candidate(update):
            if update.get("stage") == "found":
                event.set()
        cancelled = engine.search(TITLE, Budget(cancel=event), cancel_after_candidate)
        require(not cancelled["complete"] and not cancelled["success"], "Cancelled search reported success")
        require(cancelled["anime"] and cancelled["anime"][0]["subtitles"], "Cancellation discarded discovered candidates")
    print("search/cancel: ok")


def smoke_barefolder(root):
    from arr_subtitle import storage
    from arr_subtitle.cli import main
    media = root / "media" / TITLE / "Season 1"
    media.mkdir(parents=True)
    for number in range(1, 13):
        (media / f"{TITLE} S01E{number:02}.mkv").touch()
    fixture = Fixture(True)
    original = Session.__init__
    def initialize(instance, *args, **kwargs):
        kwargs.update(transport=fixture, interval=0)
        original(instance, *args, **kwargs)
    config = root / "config.json"
    config.write_text('{"aniListEnabled": false, "blogs": [], "mappings": []}', encoding="utf-8")
    with (patch.object(Session, "__init__", initialize), patch.object(storage, "data_path", return_value=root / "data"),
          contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
        require(main([str(media), "--config", str(config)]) == 0, "Bare folder CLI failed")
    for number in range(1, 13):
        sidecar = media / f"{TITLE} S01E{number:02}.ko.srt"
        require(sidecar.read_bytes() == SUBTITLE, "Bare folder sidecar mismatch")
    print("barefolder: ok")


def smoke_tls(certificate, key):
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(3)
        listener.settimeout(15)
        failures = []
        def serve():
            try:
                for _ in range(3):
                    connection, _ = listener.accept()
                    with connection:
                        try:
                            with server_context.wrap_socket(connection, server_side=True) as secured:
                                secured.sendall(b"ok")
                        except ssl.SSLError:
                            pass
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        def connect(context, hostname):
            with socket.create_connection(listener.getsockname(), timeout=10) as connection:
                with context.wrap_socket(connection, server_hostname=hostname) as secured:
                    return secured.recv(2)
        trusted = ssl.create_default_context(cafile=str(certificate))
        require(connect(trusted, "localhost") == b"ok", "Trusted TLS connection failed")
        for context, hostname in ((trusted, "wrong.example"), (ssl.create_default_context(), "localhost")):
            try:
                connect(context, hostname)
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise AssertionError("Invalid TLS certificate was accepted")
        thread.join(timeout=15)
        require(not thread.is_alive() and not failures, f"TLS fixture server failed: {failures}")
    print(f"tls: ok ({ssl.OPENSSL_VERSION})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tls", nargs=2, metavar=("CERTIFICATE", "KEY"), type=Path)
    arguments = parser.parse_args()
    if arguments.tls:
        smoke_tls(*arguments.tls)
        return 0
    with tempfile.TemporaryDirectory(prefix="arr-subtitle-smoke-") as temporary:
        root = Path(temporary).resolve()
        smoke_engine(root)
        smoke_barefolder(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
