import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-api-key")

from open_terminal import main


@pytest.fixture
def client():
    return TestClient(main.app, headers={"Authorization": "Bearer " + main.API_KEY})


@pytest.fixture
def processor():
    class Handler(BaseHTTPRequestHandler):
        requests = []
        status = 200
        body = json.dumps({"status": "done", "markdown": "first\nsecond\nthird\n"}).encode()

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.requests.append((self.path, self.headers, body))
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/convert", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_binary_file_sent_to_processor_and_line_range_preserved(client, processor, tmp_path, monkeypatch):
    url, handler = processor
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", url)
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4\n\xffbinary")

    response = client.get("/files/read", params={"path": str(path), "start_line": 2, "end_line": 2})

    assert response.status_code == 200
    assert response.json() == {"path": str(path), "content": "second\n", "total_lines": 3}
    request_path, headers, body = handler.requests[0]
    assert request_path == "/convert"
    assert headers["Content-Type"].startswith("multipart/form-data;")
    assert b'name="file"; filename="sample.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert path.read_bytes() in body


def test_text_is_read_locally_even_when_processor_is_configured(client, processor, tmp_path, monkeypatch):
    url, handler = processor
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", url)
    path = tmp_path / "text.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")

    response = client.get("/files/read", params={"path": str(path)})

    assert response.status_code == 200
    assert response.json()["content"] == "one\ntwo\n"
    assert handler.requests == []


def test_unauthenticated_request_does_not_send_a_file(processor, tmp_path, monkeypatch):
    url, handler = processor
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", url)
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF\xff")

    response = TestClient(main.app).get("/files/read", params={"path": str(path)})

    assert response.status_code == 401
    assert handler.requests == []


def test_images_are_sent_to_processor_when_configured(client, processor, tmp_path, monkeypatch):
    url, handler = processor
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", url)
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = client.get("/files/read", params={"path": str(path)})

    assert response.status_code == 200
    assert response.json()["content"] == "first\nsecond\nthird\n"
    assert b"Content-Type: image/png" in handler.requests[0][2]


def test_external_response_temporary_files_are_cleaned(client, processor, tmp_path, monkeypatch):
    url, _ = processor
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", url)
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF\xff")
    created = []
    original = main.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        fd, name = original(*args, **kwargs)
        created.append(name)
        return fd, name

    monkeypatch.setattr(main.tempfile, "mkstemp", tracked_mkstemp)
    response = client.get("/files/read", params={"path": str(path)})

    assert response.status_code == 200
    assert len(created) >= 2
    assert all(not os.path.exists(name) for name in created)


@pytest.mark.parametrize("status,body", [
    (500, b"failure"),
    (200, b"not json"),
    (200, b'{"status":"pending","markdown":"unfinished"}'),
    (200, b'{"status":"done","markdown":null}'),
    (200, b'[]'),
])
def test_processor_errors_return_502_without_fallback(client, processor, tmp_path, monkeypatch, status, body):
    url, handler = processor
    handler.status, handler.body = status, body
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", url)
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = client.get("/files/read", params={"path": str(path)})

    assert response.status_code == 502
    assert response.json()["detail"]


def test_unreachable_processor_returns_502(client, tmp_path, monkeypatch):
    path = tmp_path / "document.bin"
    path.write_bytes(b"\xffbinary")
    with socket.socket() as reserved_port:
        reserved_port.bind(("127.0.0.1", 0))
        port = reserved_port.getsockname()[1]
        monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", f"http://127.0.0.1:{port}/")
        response = client.get("/files/read", params={"path": str(path)})

    assert response.status_code == 502
    assert "Document processor request failed" in response.json()["detail"]


def test_unconfigured_processor_keeps_builtin_extractors_and_images(client, tmp_path, monkeypatch):
    from docx import Document

    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", "")
    document = tmp_path / "sample.docx"
    doc = Document()
    doc.add_paragraph("Built-in extraction")
    doc.save(document)
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = client.get("/files/read", params={"path": str(document)})
    assert response.status_code == 200
    assert "Built-in extraction" in response.json()["content"]
    response = client.get("/files/read", params={"path": str(image)})
    assert response.status_code == 200
    assert response.content == image.read_bytes()
    assert response.headers["content-type"] == "image/png"


@pytest.mark.parametrize("environment_value,expected", [
    (None, "https://config.example/process"),
    (" https://env.example/process ", "https://env.example/process"),
    ("", ""),
])
def test_config_and_environment_precedence(tmp_path, environment_value, expected):
    config = tmp_path / "config.toml"
    config.write_text('document_processor_url = "https://config.example/process"\n')
    env = os.environ.copy()
    env.pop("OPEN_TERMINAL_DOCUMENT_PROCESSOR_URL", None)
    if environment_value is not None:
        env["OPEN_TERMINAL_DOCUMENT_PROCESSOR_URL"] = environment_value
    script = (
        "import sys; from open_terminal import config; "
        "config.init(sys.argv[1], isolated=True); "
        "from open_terminal.env import DOCUMENT_PROCESSOR_URL; print(DOCUMENT_PROCESSOR_URL)"
    )

    result = subprocess.run([sys.executable, "-c", script, str(config)], env=env, text=True, capture_output=True, check=True)

    assert result.stdout.strip() == expected
