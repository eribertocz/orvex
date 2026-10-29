import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
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
        # A page whose name was made to resolve to 127.0.0.1 (DNS rebinding) is refused (#45).
        rebound = urllib.request.Request(
            f"http://{LOCAL_HOST}:{port}/api/v1/health", headers={"Host": f"rebind.example:{port}"}
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(rebound)
        with refused.value as answer:
            assert answer.code == 400
            assert json.loads(answer.read())["error"]["code"] == "HOST_NOT_ALLOWED"
        assert project.is_dir()
        assert (tmp_path / "instance" / "server.sqlite").is_file()
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=10)
    log_lines = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert any(line.get("request_id") == request_id for line in log_lines)


def started(arguments: list[str], tmp_path: Path) -> subprocess.Popen[str]:
    """The server started as a subprocess with its own temporary folder; returns once it has
    printed its address."""
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    port = free_port(21000)
    environment = os.environ | {
        home_variable(IDENTITY): str(tmp_path / "instance"),
        "TMPDIR": str(temporary),
    }
    with (tmp_path / "stderr.txt").open("w", encoding="utf-8") as errors:  # read by the tests
        process = subprocess.Popen(
            [sys.executable, "-m", "orvex.server", *arguments, "--no-browser", "--port", str(port)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
    assert process.stdout is not None
    line = process.stdout.readline()  # the address line, printed once the project is open
    assert "-> http://" in line, line
    process.stdout.close()
    return process


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize(
    ("number", "returncode"),
    [(signal.SIGTERM, -signal.SIGTERM), (signal.SIGINT, -signal.SIGINT)]
    + ([(signal.SIGHUP, -signal.SIGHUP)] if hasattr(signal, "SIGHUP") else []),
)
def test_the_in_memory_project_is_deleted_however_the_server_stops(
    tmp_path: Path, number: int, returncode: int
) -> None:
    # #43: Ctrl-C deleted the temporary project folder, but SIGTERM (kill, process managers)
    # and SIGHUP (the terminal closed) left it behind.
    process = started([], tmp_path)
    try:
        assert len(list((tmp_path / "temporary").iterdir())) == 1  # the project folder
        process.send_signal(number)
        # It still ends as the signal asked, Ctrl-C included, with no traceback (#151).
        assert process.wait(timeout=10) == returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert list((tmp_path / "temporary").iterdir()) == []
    assert "Traceback" not in (tmp_path / "stderr.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("delay", [0.0, 0.05, 0.1, 0.2])
def test_ctrl_c_during_start_up_stops_cleanly(tmp_path: Path, delay: float) -> None:
    # #151: Ctrl-C right after the address line, while uvicorn may still be starting.
    process = started([], tmp_path)
    try:
        time.sleep(delay)
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=10) == -signal.SIGINT
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert list((tmp_path / "temporary").iterdir()) == []
    assert "Traceback" not in (tmp_path / "stderr.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize(
    "number",
    [signal.SIGINT, signal.SIGTERM, *([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])],
)
def test_a_project_folder_is_never_deleted(tmp_path: Path, number: int) -> None:
    project = tmp_path / "billing"
    project.mkdir()
    (project / "notes.txt").write_text("kept", encoding="utf-8")
    process = started([str(project)], tmp_path)
    try:
        process.send_signal(number)
        assert process.wait(timeout=10) == -number
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert (project / "notes.txt").read_text(encoding="utf-8") == "kept"
