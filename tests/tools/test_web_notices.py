"""#47: a web build must carry the license notices of the code bundled into it."""

from pathlib import Path

from tools.licenses.web_notices import NOTICES_FILE, main, problems

MIT = "Permission is hereby granted, free of charge, to any person obtaining a copy of this."
NOTICES = (
    "# Licenses\n\nThe app bundles dependencies which contain the following licenses:\n\n"
    f"## svelte - 5.57.3 (MIT)\n\nCopyright (c) 2016-2025 Svelte Contributors\n\n{MIT}\n\n"
    f"## devalue - 5.9.4 (MIT)\n\nCopyright (c) 2018-19 these people\n\n{MIT}\n"
)
CSS = "/*! tailwindcss v4.3.3 | MIT License | https://tailwindcss.com */\nbody{margin:0}"


def build(root: Path, notices: str | None = NOTICES, css: str | None = CSS) -> Path:
    folder = root / "build"
    (folder / "_app" / "immutable" / "assets").mkdir(parents=True)
    (folder / "index.html").write_text("<html></html>", encoding="utf-8")
    if notices is not None:
        (folder / NOTICES_FILE).write_text(notices, encoding="utf-8")
    if css is not None:
        (folder / "_app" / "immutable" / "assets" / "0.css").write_text(css, encoding="utf-8")
    return folder


def test_a_complete_build_passes(tmp_path: Path) -> None:
    assert problems(build(tmp_path)) == []
    assert main([str(tmp_path / "build")]) == 0


def test_a_missing_notices_file_fails(tmp_path: Path) -> None:
    assert problems(build(tmp_path, notices=None)) == [
        f"{NOTICES_FILE} is missing from {tmp_path / 'build'}"
    ]
    assert main([str(tmp_path / "build")]) == 1


def test_a_section_without_license_text_fails(tmp_path: Path) -> None:
    doctored = NOTICES.replace(f"Copyright (c) 2018-19 these people\n\n{MIT}\n", "")
    assert problems(build(tmp_path, notices=doctored)) == ["devalue 5.9.4 has no license text"]


def test_a_file_listing_no_package_fails(tmp_path: Path) -> None:
    assert problems(build(tmp_path, notices="# Licenses\n")) == [
        f"{NOTICES_FILE} lists no bundled package"
    ]


def test_css_without_the_tailwind_banner_fails(tmp_path: Path) -> None:
    assert problems(build(tmp_path, css="body{margin:0}")) == [
        "the built CSS lost Tailwind's MIT License banner"
    ]


def test_a_build_without_css_needs_no_banner(tmp_path: Path) -> None:
    assert problems(build(tmp_path, css=None)) == []
