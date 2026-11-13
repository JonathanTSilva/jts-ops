import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jts_ops.brand import (
    FOOTER_END,
    FOOTER_START,
    HEADER_END,
    HEADER_START,
    BrandError,
    brand_clone,
    brand_hash,
    button_svg,
    clean_description,
    footer_block,
    header_block,
    header_svg,
    rewrite_readme,
)
from jts_ops.cli import main

DESC = "🧰 Standardize the scripting environment to improve the maintainability of your scripts"
LONG_DESC = (
    "Standardize the scripting environment to improve the maintainability, readability and "
    "efficiency of your scripts across every machine, shell and operating system you use daily"
)
NS = "{http://www.w3.org/2000/svg}"


def covering_rects(svg: str, width: int, height: int) -> list[ET.Element]:
    root = ET.fromstring(svg)
    return [
        r
        for r in root.iter(f"{NS}rect")
        if float(r.get("width", "0")) >= width - 1 and float(r.get("height", "0")) >= height - 1
    ]


def use_extent(svg: str) -> float:
    return max(float(x) for x in re.findall(r'<use [^>]*\bx="([\d.]+)"', svg))


def test_same_input_same_output():
    for theme in ("light", "dark"):
        assert header_svg("scriptize", DESC, theme) == header_svg("scriptize", DESC, theme)
        assert button_svg(theme) == button_svg(theme)


def test_unknown_theme_rejected():
    with pytest.raises(BrandError, match="theme"):
        header_svg("scriptize", DESC, "sepia")
    with pytest.raises(BrandError, match="theme"):
        button_svg("sepia")


def test_themes_differ_only_in_colours():
    light = header_svg("scriptize", DESC, "light")
    dark = header_svg("scriptize", DESC, "dark")
    assert light != dark
    assert "#1f2328" in light and "#4A5BD4" in light
    assert "#e6edf3" in dark and "#6272EA" in dark
    swapped = dark.replace("#e6edf3", "#1f2328").replace("#6272EA", "#4A5BD4")
    assert swapped == light  # same geometry, glyphs and opacities


def test_header_is_transparent():
    for theme in ("light", "dark"):
        svg = header_svg("scriptize", LONG_DESC, theme)
        assert covering_rects(svg, 1280, 160) == []
        for background in ("#F7F8FC", "#0D1117", "#DDE0EE", "#21262D"):
            assert background.lower() not in svg.lower()
        assert 'rx="12"' not in svg  # no rounded panel
        assert "clip-path" not in svg
        root = ET.fromstring(svg)
        assert root.get("width") == "1280" and root.get("height") == "160"


def test_button_canvas_is_transparent_but_pill_is_accent():
    for theme, accent in (("light", "#4A5BD4"), ("dark", "#6272EA")):
        svg = button_svg(theme)
        root = ET.fromstring(svg)
        pill = next(root.iter(f"{NS}rect"))
        assert pill.get("fill") == accent
        assert float(pill.get("rx", "0")) >= 19  # fully rounded: the canvas corners stay empty
        fills = [r.get("fill") for r in root.iter(f"{NS}rect")]
        assert fills.count(accent) == 1  # nothing else is filled behind the pill


def test_description_without_emoji():
    assert clean_description(DESC).startswith("Standardize")
    assert "🧰" not in header_svg("scriptize", DESC, "light")
    assert "<title" in header_svg("scriptize", DESC, "light")


def test_long_description_wraps_and_stays_left_of_the_circuit():
    one = header_svg("scriptize", "Short", "light")
    wrapped = header_svg("scriptize", LONG_DESC, "light")
    ys_one = set(re.findall(r'<use [^>]*\by="([\d.]+)"', one))
    ys_wrapped = set(re.findall(r'<use [^>]*\by="([\d.]+)"', wrapped))
    assert len(ys_wrapped) == len(ys_one) + 1  # name line plus two description lines
    assert use_extent(wrapped) < 880


def test_long_name_shrinks_or_wraps_within_the_text_column():
    name = "research-masters-dissertation"
    svg = header_svg(name, LONG_DESC, "dark")
    assert use_extent(svg) < 880
    huge = "-".join(["segment"] * 14)
    svg = header_svg(huge, "Short", "dark")
    assert use_extent(svg) < 880
    ys = set(re.findall(r'<use [^>]*\by="([\d.]+)"', svg))
    assert len(ys) >= 3  # the name wrapped at hyphens, plus the description


def test_hash_changes_with_the_title():
    h = brand_hash("scriptize", DESC)
    assert h == hashlib.sha256(("scriptize" + clean_description(DESC)).encode()).hexdigest()[:8]
    assert re.fullmatch(r"[0-9a-f]{8}", h)
    assert brand_hash("scriptize", DESC + "!") != h
    assert brand_hash("scriptize2", DESC) != h


def test_footer_block():
    block = footer_block()
    assert block.startswith(FOOTER_START) and block.endswith(FOOTER_END)
    assert 'align="right"' in block
    assert 'href="https://www.jontobias.com"' in block
    assert (
        "https://raw.githubusercontent.com/JonathanTSilva/.github/main/assets/brand/"
        "jontobias-dark.svg" in block
    )
    assert "jontobias-light.svg" in block


def blocks(name: str = "scriptize", desc: str = DESC) -> tuple[str, str]:
    return header_block(name, desc, brand_hash(name, desc)), footer_block()


def test_markers_inserted_at_top_and_end():
    header, footer = blocks()
    out = rewrite_readme("# Title\n\nBody text.\n", header, footer)
    assert out.startswith(HEADER_START)
    assert out.endswith(FOOTER_END + "\n")
    assert "# Title\n\nBody text." in out


def test_markers_replaced_in_place_and_rest_untouched():
    header, footer = blocks()
    body = "intro\n\n## Section\n\ntext with <!-- other --> comment\n"
    text = (
        f"top\n{HEADER_START}\nold\n{HEADER_END}\n{body}\n{FOOTER_START}\nold\n{FOOTER_END}\nend\n"
    )
    out = rewrite_readme(text, header, footer)
    assert out == f"top\n{header}\n{body}\n{footer}\nend\n"
    assert "old" not in out
    assert rewrite_readme(out, header, footer) == out  # idempotent


def test_markers_in_prose_inline_code_and_fences_are_ignored():
    header, footer = blocks()
    body = (
        f"Put {HEADER_START} and {HEADER_END} around the header.\n\n"
        f"Inline: `{FOOTER_START}` / `{FOOTER_END}`.\n\n"
        f"```md\n{HEADER_START}\nold\n{HEADER_END}\n```\n\n"
        f"~~~\n{FOOTER_START}\nold\n{FOOTER_END}\n~~~\n\n"
        f"````\n```\n{HEADER_START}\n````\n"
    )
    out = rewrite_readme(body, header, footer)
    assert out == f"{header}\n\n{body.rstrip()}\n\n{footer}\n"
    assert rewrite_readme(out, header, footer) == out  # idempotent


def test_real_marker_lines_win_over_mentions_and_fences():
    header, footer = blocks()
    text = (
        f"```\n{HEADER_START}\nx\n{HEADER_END}\n```\n"
        f"Use {HEADER_START} here.\n\n"
        f"  {HEADER_START}  \nold\n\t{HEADER_END}\n\nbody\n\n{FOOTER_START}\nold\n{FOOTER_END}\n"
    )
    out = rewrite_readme(text, header, footer)
    assert out == (
        f"```\n{HEADER_START}\nx\n{HEADER_END}\n```\n"
        f"Use {HEADER_START} here.\n\n  {header}\n\nbody\n\n{footer}\n"
    )
    assert rewrite_readme(out, header, footer) == out  # idempotent


def test_placeholder_block_is_replaced():
    header, footer = blocks()
    placeholder = header_block("x", "y", "PLACEHOLDER")
    out = rewrite_readme(f"{placeholder}\n\n# T\n\n{footer}\n", header, footer)
    assert "PLACEHOLDER" not in out
    assert brand_hash("scriptize", DESC) in out


def test_only_one_block_present():
    header, footer = blocks()
    out = rewrite_readme(f"{HEADER_START}\nold\n{HEADER_END}\n\nbody\n", header, footer)
    assert out.startswith(header) and out.endswith(footer + "\n")
    out = rewrite_readme(f"body\n\n{FOOTER_START}\nold\n{FOOTER_END}\n", header, footer)
    assert out.startswith(header) and out.endswith(footer + "\n")


def test_broken_marker_pair_is_an_error():
    header, footer = blocks()
    with pytest.raises(BrandError, match="broken"):
        rewrite_readme(f"{HEADER_START}\nno end\n", header, footer)
    with pytest.raises(BrandError, match="broken"):
        rewrite_readme(f"{FOOTER_END}\n{FOOTER_START}\n", header, footer)


def test_header_block_escapes_alt_text():
    block = header_block("w", 'Say "hi" & <wave>', "abcd1234")
    assert 'alt="w: Say &quot;hi&quot; &amp; &lt;wave&gt;"' in block
    assert "header-abcd1234-dark.svg" in block and "header-abcd1234-light.svg" in block


def make_clone(tmp_path: Path, readme: str | None = "# T\n") -> Path:
    clone = tmp_path / "scriptize"
    clone.mkdir()
    if readme is not None:
        (clone / "README.md").write_text(readme, encoding="utf-8")
    return clone


def test_brand_clone_dry_run_writes_nothing(tmp_path: Path):
    clone = make_clone(tmp_path)
    result = brand_clone(clone, "scriptize", DESC, execute=False)
    assert not (clone / "assets").exists()
    assert (clone / "README.md").read_text(encoding="utf-8") == "# T\n"
    assert len(result.written) == 2
    assert any("insert at the top" in n for n in result.readme)


def test_brand_clone_writes_files_and_removes_older_headers(tmp_path: Path):
    clone = make_clone(tmp_path)
    folder = clone / "assets" / "brand"
    folder.mkdir(parents=True)
    (folder / "header-deadbeef-light.svg").write_text("old", encoding="utf-8")
    (folder / "header-deadbeef-dark.svg").write_text("old", encoding="utf-8")
    (folder / "logo.svg").write_text("mine", encoding="utf-8")
    brand_clone(clone, "scriptize", DESC, execute=True)
    digest = brand_hash("scriptize", DESC)
    assert sorted(p.name for p in folder.iterdir()) == [
        f"header-{digest}-dark.svg",
        f"header-{digest}-light.svg",
        "logo.svg",
    ]
    for theme in ("light", "dark"):
        assert (folder / f"header-{digest}-{theme}.svg").read_text(encoding="utf-8") == header_svg(
            "scriptize", DESC, theme
        )
    readme = (clone / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(HEADER_START) and f"header-{digest}-light.svg" in readme
    assert "# T" in readme
    # a changed title gives a new file name and removes the previous one
    brand_clone(clone, "scriptize", DESC + " more", execute=True)
    names = {p.name for p in folder.iterdir()}
    assert f"header-{digest}-light.svg" not in names
    assert len([n for n in names if n.startswith("header-")]) == 2
    assert "logo.svg" in names


def test_brand_clone_is_idempotent(tmp_path: Path):
    clone = make_clone(tmp_path)
    brand_clone(clone, "scriptize", DESC, execute=True)
    first = (clone / "README.md").read_text(encoding="utf-8")
    result = brand_clone(clone, "scriptize", DESC, execute=True)
    assert (clone / "README.md").read_text(encoding="utf-8") == first
    assert result.readme == ["README.md: already up to date"]


def test_brand_clone_creates_a_missing_readme_but_not_beside_another_one(tmp_path: Path):
    clone = make_clone(tmp_path, None)
    brand_clone(clone, "scriptize", DESC, execute=True)
    text = (clone / "README.md").read_text(encoding="utf-8")
    assert text.startswith(HEADER_START) and text.rstrip().endswith(FOOTER_END)
    other = tmp_path / "other"
    other.mkdir()
    (other / "README.rst").write_text("rst", encoding="utf-8")
    brand_clone(other, "scriptize", DESC, execute=True)
    assert not (other / "README.md").exists()
    assert (other / "assets" / "brand").is_dir()


def run_cli(args: list[str], fixtures_dir: Path, tmp_path: Path) -> int:
    return main(
        ["brand", *args, "--standard-dir", str(fixtures_dir), "--projects-dir", str(tmp_path)]
    )


def test_cli_dry_run_then_execute(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(fixtures_dir / "inventory.private.yml"))
    clone = tmp_path / "ai-helper"
    clone.mkdir()
    (clone / "README.md").write_text("# helper\n", encoding="utf-8")
    assert run_cli(["ai-helper"], fixtures_dir, tmp_path) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "header-" in out
    assert not (clone / "assets").exists()
    assert run_cli(["ai-helper", "--execute"], fixtures_dir, tmp_path) == 0
    assert len(list((clone / "assets" / "brand").glob("header-*.svg"))) == 2
    text = (clone / "README.md").read_text(encoding="utf-8")
    assert text.startswith(HEADER_START) and "# helper" in text


def test_cli_uses_the_target_name_and_works_for_private_repos(
    fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(fixtures_dir / "inventory.private.yml"))
    clone = tmp_path / "hidden-notes"  # a private inventory entry
    clone.mkdir()
    (clone / "README.md").write_text("# n\n", encoding="utf-8")
    assert run_cli(["hidden-notes", "--execute"], fixtures_dir, tmp_path) == 0
    digest = brand_hash("hidden-notes", "Example private repo")
    assert (clone / "assets" / "brand" / f"header-{digest}-light.svg").is_file()
    assert f"header-{digest}-dark.svg" in (clone / "README.md").read_text(encoding="utf-8")


def test_cli_errors(
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("JTS_PRIVATE_INVENTORY", str(fixtures_dir / "inventory.private.yml"))
    assert run_cli(["not-in-inventory"], fixtures_dir, tmp_path) == 2
    assert "not in the inventory" in capsys.readouterr().err
    assert run_cli(["ai-helper"], fixtures_dir, tmp_path) == 2  # no clone
    assert "no clone" in capsys.readouterr().err
    clone = tmp_path / "ai-helper"
    clone.mkdir()
    (clone / "README.md").write_text(f"{HEADER_START}\nno end\n", encoding="utf-8")
    assert run_cli(["ai-helper", "--execute"], fixtures_dir, tmp_path) == 2
    assert "broken" in capsys.readouterr().err
    assert not (clone / "assets").exists()


def test_fonts_are_packaged_with_their_licence():
    fonts = Path(__file__).parents[1] / "src" / "jts_ops" / "fonts"
    assert (fonts / "Geist-SemiBold.ttf").is_file()
    assert (fonts / "Geist-Regular.ttf").is_file()
    assert "SIL Open Font License" in (fonts / "OFL.txt").read_text(encoding="utf-8")
