"""Hygiene checks on the public docs and shipped source: no references to
internal files or folders. Agora's docs live on GitHub (nothing renders on
PyPI), so relative links are fine; the internal-reference ban still holds
for every tracked surface.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DOCS = ["README.md", "USAGE.md", "ARCHITECTURE.md", "MANIFEST.md", "CHANGELOG.md"]

# Internal-only paths and files that must never appear in public text.
# pypi.org/project/... URLs are the one legitimate "project/" spelling.
_INTERNAL = re.compile(
    r"(?<!pypi\.org/)project/|CODING\.md|ROADMAP|BUGS\.md|PRD|~/\.claude|\.claude/"
)

_SOURCE_DIRS = ["agents", "api", "core", "providers", "runners", "static", "config",
                "tests"]

# This file carries the detection pattern itself, so it matches by construction.
_SELF = Path(__file__).resolve()


@pytest.mark.parametrize("doc", PUBLIC_DOCS)
def test_public_docs_reference_no_internal_files(doc):
    text = (ROOT / doc).read_text()
    hits = [
        (i, line)
        for i, line in enumerate(text.splitlines(), 1)
        if _INTERNAL.search(line)
    ]
    assert not hits, (
        f"{doc} references an internal file or folder; state the behavior "
        f"directly instead: {hits}"
    )


def test_shiplock_gate_is_clean():
    """The full shiplock gate (shiplock.toml): docs exist, banned words are
    absent from docs and source, no internal references, the architecture doc
    names every core module, the manifest lists every source file, and the
    USAGE speech-act table covers every ActType member."""
    from shiplock import load_config, run_checks

    report = run_checks(load_config(ROOT))
    assert report.ok, [f.message for f in report.findings]


def test_tracked_source_references_no_internal_files():
    for d in _SOURCE_DIRS:
        for path in sorted((ROOT / d).rglob("*")):
            if path.suffix not in (".py", ".js", ".html", ".css", ".yaml"):
                continue
            if path.resolve() == _SELF:
                continue
            hits = [
                (i, line)
                for i, line in enumerate(path.read_text().splitlines(), 1)
                if _INTERNAL.search(line)
            ]
            assert not hits, (
                f"{path.relative_to(ROOT)} is tracked and references "
                f"an internal file: {hits}"
            )
