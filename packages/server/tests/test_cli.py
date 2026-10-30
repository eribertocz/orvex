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
from orvex.server import cli
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


@pytest.mark.parametrize("value", ["0", "-1", "65536", "abc", "1.5"])
def test_a_port_outside_1_to_65535_is_an_argument_error(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # #144 (decision #182, 3A): port 0 printed an address no server answered on.
    with pytest.raises(SystemExit) as stopped:
        main(["--port", value, "--no-browser"])
    assert stopped.value.code == 2
    assert "from 1 to 65535" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["1", "65535"])
def test_the_ends_of_the_port_range_parse(value: str) -> None:
    assert cli.parser_for(IDENTITY).parse_args(["--port", value]).port == int(value)


def test_the_search_for_a_free_port_stops_at_65535(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    tried: list[int] = []

    class Busy:
        def __init__(self, *arguments: object) -> None:
            pass

        def __enter__(self) -> Busy:
            return self

        def __exit__(self, *arguments: object) -> None:
            pass

        def bind(self, address: tuple[str, int]) -> None:
            tried.append(address[1])
            raise OSError("busy")

    monkeypatch.setattr(socket, "socket", Busy)
    with pytest.raises(OSError, match="no free port from 65530 to 65535"):
        free_port(65530)
    assert tried == list(range(65530, 65536))  # never 65536
    monkeypatch.setenv(home_variable(IDENTITY), str(tmp_path / "instance"))
    assert main([str(tmp_path / "p"), "--no-browser", "--port", "65535"]) == 1
    assert "choose another --port" in capsys.readouterr().err


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


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "86401", "1e400"])
def test_a_stop_grace_that_cannot_be_honored_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    # #166: `nan` and `inf` parse as floats but crashed the grace thread.
    (tmp_path / "project.yaml").write_text("version: 1\nname: Test\ntime_zone: UTC\n")
    assert main(["schedule", str(tmp_path), f"--stop-grace={value}"]) == 2
    assert capsys.readouterr().err.strip() == (
        "--stop-grace must be a number of seconds from 0 to 86400"
    )
    assert not (tmp_path / f".{load_identity().slug}").exists()  # nothing started


@pytest.mark.parametrize("value", ["0", "86400"])
def test_the_stop_grace_bounds_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    seen: list[float] = []

    def schedule(*_arguments: object, stop_grace: float) -> int:
        seen.append(stop_grace)
        return 0

    monkeypatch.setattr(cli, "schedule", schedule)
    assert main(["schedule", str(tmp_path), f"--stop-grace={value}"]) == 0
    assert seen == [float(value)]
