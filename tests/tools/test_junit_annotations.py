from pathlib import Path

from tools.ci.junit_annotations import failure_annotations


def test_failures_become_error_annotations(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="a.b" name="test_x">'
        '<failure message="assert 1 == 2">line one\nline 100%</failure></testcase>'
        '<testcase classname="a.b" name="test_ok"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert failure_annotations(report) == [
        "::error title=a.b::test_x::assert 1 == 2%0Aline one%0Aline 100%25"
    ]
