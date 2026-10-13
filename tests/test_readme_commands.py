"""Every ``diaggpsd`` / ``diaggpsd-diag-setup`` command shown in the README must
parse with the real argument parser.

The README once led with ``diaggpsd --diag-port tcp --no-at``; ``--no-at`` is a
``diag_setup`` option, so the first command a reader copied exited with
``unrecognized arguments``. The README sits one level above the tests in the
published layout and elsewhere in the development tree; both are checked.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

import diag_setup
import diaggpsd

_HERE = Path(__file__).resolve()
_CANDIDATES = (
    _HERE.parents[1] / "README.md",                         # published layout
    _HERE.parents[3] / "libs" / "diaggpsd" / "README.md",   # development tree
)


def _readme() -> str:
    for path in _CANDIDATES:
        text = path.read_text() if path.is_file() else ""
        if text.startswith("# diaggpsd\n"):
            return text
    pytest.fail(f"README not found in {[str(p) for p in _CANDIDATES]}")


def _commands(prog: str) -> list[list[str]]:
    blocks = re.findall(r"```bash\n(.*?)```", _readme(), re.S)
    cmds = []
    for line in "\n".join(blocks).splitlines():
        line = line.split("#", 1)[0].strip()
        argv = shlex.split(line) if line else []
        if argv and argv[0] == prog:
            cmds.append(argv[1:])
    return cmds


def test_readme_has_commands_for_both_tools():
    assert len(_commands("diaggpsd")) >= 3
    assert len(_commands("diaggpsd-diag-setup")) >= 1


@pytest.mark.parametrize("argv", _commands("diaggpsd"), ids=" ".join)
def test_diaggpsd_command_parses(argv):
    diaggpsd.build_arg_parser().parse_args(argv)


@pytest.mark.parametrize("argv", _commands("diaggpsd-diag-setup"), ids=" ".join)
def test_diag_setup_command_parses(argv):
    diag_setup.build_arg_parser().parse_args(argv)


# The docs travel too, and ``docs/device-compatibility.md`` carried ten
# ``diaggpsd.py ... --no-at`` table rows after the README was fixed.
# Inline code spans are checked here; a span is cut at the first shell operator
# so a trailing ``| tee`` or ``&&`` does not reach the parser.
_DOCS = _HERE.parents[1] / "docs"


def _doc_commands() -> list[tuple[str, list[str]]]:
    cmds = []
    for path in sorted(_DOCS.glob("*.md")):
        for span in re.findall(r"`([^`\n]*diaggpsd\.py[^`\n]*)`", path.read_text()):
            span = re.split(r"\s(?:\||&&|;|>)\s?", span, maxsplit=1)[0]
            argv = shlex.split(span.split("diaggpsd.py", 1)[1])
            cmds.append((path.name, argv))
    return cmds


def test_docs_have_commands():
    assert len(_doc_commands()) >= 5


@pytest.mark.parametrize("doc,argv", _doc_commands(),
                         ids=lambda v: v if isinstance(v, str) else " ".join(v))
def test_doc_diaggpsd_command_parses(doc, argv):
    diaggpsd.build_arg_parser().parse_args(argv)
