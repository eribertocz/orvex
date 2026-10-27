import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from orvex.server.cli import LOCAL_HOST, free_port, main, server_config
from orvex.server.identity import load_identity
from orvex.server.instance import home_variable
from orvex.server.versions import Versions

IDENTITY = load_identity()


def test_busy_port_moves_to_the_next_free_one() -> None:
    with socket.socket() as busy:
        busy.bind((LOCAL_HOST, 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert free_port(taken) > taken


def test_local_mode_binds_to_loopback_only() -> None:
    config = server_config(7878, Versions("0.0.0", (), ()), None)
    assert config.host == "127.0.0.1"


def test_file_path_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(home_variable(IDENTITY), str(tmp_path / "instance"))
    file = tmp_path / "notes.txt"
    file.write_text("x")
    assert main([str(file), "--no-browser"]) == 2


def test_version_lists_modules_and_contracts(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    output = capsys.readouterr().out
    assert output.startswith(IDENTITY.name)
    assert "module   server" in output
    assert "contract http-api v1" in output


def test_server_starts_and_answers(tmp_path: Path) -> None:
    port = free_port(20000)
    environment = os.environ | {home_variable(IDENTITY): str(tmp_path / "instance")}
    project = tmp_path / "billing"
    process = subprocess.Popen(
        [sys.executable, "-m", "orvex.server", str(project), "--no-browser", "--port", str(port)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        body = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://{LOCAL_HOST}:{port}/api/v1/health") as reply:
                    body = json.loads(reply.read())
                    request_id = reply.headers["X-Request-Id"]
                break
            except OSError:
                time.sleep(0.1)
        assert body == {"status": "ok"}
        assert project.is_dir()
        assert (tmp_path / "instance" / "server.sqlite").is_file()
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=10)
    log_lines = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert any(line.get("request_id") == request_id for line in log_lines)
