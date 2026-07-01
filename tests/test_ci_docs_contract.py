import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_docs_name_real_noninteractive_commands() -> None:
    ci_doc = (ROOT / "docs" / "ci.md").read_text()

    assert "python -m tokenpak --version" in ci_doc
    assert "python -m tokenpak status --json --no-meme" in ci_doc
    assert 'TOKENPAK_HOME="${RUNNER_TEMP:-/tmp}/tokenpak-ci"' in ci_doc
    assert "There is no `tokenpak ci` command" in ci_doc

    for line in ci_doc.splitlines():
        assert not re.match(r"\s*tokenpak\s+ci\b", line)


def test_ci_docs_are_linked_from_primary_indexes() -> None:
    readme = (ROOT / "README.md").read_text()
    docs_index = (ROOT / "docs" / "index.md").read_text()

    assert "[CI guide](docs/ci.md)" in readme
    assert "[Using TokenPak in CI](ci.md)" in docs_index
