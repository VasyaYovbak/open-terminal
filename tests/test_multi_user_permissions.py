import asyncio
import os
import stat
import subprocess

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-api-key")

from open_terminal import main
from open_terminal.utils.fs import UserFS


@pytest.mark.parametrize("initial_mode,expected_mode", [
    (0o600, 0o660),
    (0o640, 0o660),
    (0o644, 0o664),
    (0o700, 0o760),
])
def test_multi_user_file_ownership_grants_group_read_write_without_changing_other_bits(
    tmp_path, monkeypatch, initial_mode, expected_mode
):
    path = tmp_path / "file.txt"
    path.write_text("example")
    path.chmod(initial_mode)
    calls = []
    real_run = subprocess.run

    def mock_run(command, **kwargs):
        calls.append(command)
        if command[1] == "chmod":
            return real_run(command[1:], **kwargs)
        assert command[1] == "chown"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("open_terminal.utils.fs.subprocess.run", mock_run)
    asyncio.run(UserFS(username="testuser", home=str(tmp_path))._chown(str(path)))

    assert calls == [
        ["sudo", "chown", "testuser:testuser", str(path)],
        ["sudo", "chmod", "g+rw", str(path)],
    ]
    assert stat.S_IMODE(os.stat(path).st_mode) == expected_mode


def test_single_user_ownership_does_not_change_permissions(tmp_path, monkeypatch):
    path = tmp_path / "private.txt"
    path.write_text("example")
    path.chmod(0o600)

    def unexpected_run(*args, **kwargs):
        pytest.fail("single-user mode must not run chown or chmod")

    monkeypatch.setattr("open_terminal.utils.fs.subprocess.run", unexpected_run)
    asyncio.run(UserFS(home=str(tmp_path))._chown(str(path)))

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("existing_file", [False, True])
def test_multi_user_upload_is_group_readable_and_can_be_read_back(tmp_path, monkeypatch, existing_file):
    fs = UserFS(username="testuser", home=str(tmp_path))
    path = tmp_path / "test.sql"
    if existing_file:
        path.write_text("old contents")
        path.chmod(0o600)

    async def ensure_parents(directory):
        assert directory == str(tmp_path)

    monkeypatch.setattr(fs, "_ensure_parents", ensure_parents)
    monkeypatch.setattr(main, "DOCUMENT_PROCESSOR_URL", "")
    real_run = subprocess.run

    def mock_run(command, **kwargs):
        if command[1] == "chmod":
            return real_run(command[1:], **kwargs)
        assert command[1] == "chown"
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("open_terminal.utils.fs.subprocess.run", mock_run)
    main.app.dependency_overrides[main.get_filesystem] = lambda: fs
    try:
        client = TestClient(main.app, headers={"Authorization": "Bearer " + main.API_KEY})
        response = client.post(
            "/files/upload", params={"directory": str(tmp_path)},
            files={"file": (path.name, b"SELECT 1;\n", "text/plain")},
        )
        assert response.status_code == 200
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        response = client.get("/files/read", params={"path": str(path)})
        assert response.status_code == 200
        assert response.json()["content"] == "SELECT 1;\n"
    finally:
        main.app.dependency_overrides.pop(main.get_filesystem, None)
