"""The project-file reader (`ConfigFile`): only a bounded regular file, never a data source.

This is the reason of its entry in `tools/checks/data-reader-allowlist.toml` (#261)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from orvex.transform.config_file import MAX_FILE_BYTES, ConfigFile, ConfigProblem


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_config_file_reader_reads_only_a_bounded_regular_file(tmp_path: Path) -> None:
    pipe = tmp_path / "project.yaml"
    os.mkfifo(pipe)
    with pytest.raises(ConfigProblem, match="is not a regular file"):
        ConfigFile(pipe, 1)
    large = tmp_path / "large.yaml"
    large.write_bytes(b"a: " + b"x" * MAX_FILE_BYTES + b"\n")
    with pytest.raises(ConfigProblem, match="larger than"):
        ConfigFile(large, 1)
