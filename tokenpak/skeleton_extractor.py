"""TokenPak skeleton extractor — token-reduced code skeletons for injection.

``extract_skeleton(code, language)`` returns a reduced form of ``code`` in
which function/method bodies are elided while everything that carries coding
signal survives verbatim:

- function / method / class **signatures** (including multi-line ones),
- **type annotations**, **decorators**, and **import** lines,
- **docstrings** (preserved by default — they are often the highest-signal
  tokens for a coding task),
- module/class-level structure (constants, attribute annotations, comments).

Languages
---------
- **Python** — parsed with the stdlib :mod:`ast`; elision is line-based against
  the original source, so formatting and comments outside elided bodies are
  preserved byte-for-byte.
- **TypeScript / JavaScript / Go / Rust** — conservative regex + brace-matching
  fallbacks. Declaration heads are detected dynamically (no enumerations of
  project symbols); bodies are elided only when the matching closing brace is
  found unambiguously. Any ambiguity means the construct is left untouched.

Fail-safe contract
------------------
``extract_skeleton`` **never raises** and never returns corrupted output: on a
parse failure (or any unexpected condition) it returns the original ``code``
unchanged and emits a diagnostic on the ``tokenpak.skeleton`` logger — the same
diagnostic channel the Phase-1 truth patch introduced. Unknown or unsupported
languages are returned unchanged without a warning.

Related (non-duplicate) modules
-------------------------------
:mod:`tokenpak.vault.ast_parser` extracts structural *symbols* for vault
indexing; this module rewrites *source text* for proxy injection. They share
the stdlib ``ast`` approach but serve different surfaces.
"""

from __future__ import annotations

import ast
import logging
import re

_log = logging.getLogger("tokenpak.skeleton")

__all__ = ["extract_skeleton"]

# Canonical language keys and accepted aliases (extension forms included so
# callers may pass either a language name or a file extension).
_LANGUAGE_ALIASES = {
    "python": "python",
    "py": "python",
    ".py": "python",
    "typescript": "typescript",
    "ts": "typescript",
    ".ts": "typescript",
    "tsx": "typescript",
    ".tsx": "typescript",
    "javascript": "javascript",
    "js": "javascript",
    ".js": "javascript",
    "jsx": "javascript",
    ".jsx": "javascript",
    "go": "go",
    "golang": "go",
    ".go": "go",
    "rust": "rust",
    "rs": "rust",
    ".rs": "rust",
}

# Bodies this short are not worth eliding (the "..." placeholder plus the kept
# signature would save nothing and can even grow the text).
_MIN_ELIDE_LINES = 2


def extract_skeleton(code: str, language: str | None = None) -> str:
    """Return a token-reduced skeleton of ``code``: signatures + structure,
    bodies elided.

    Args:
        code: Source text to skeletonize.
        language: Language name or file extension (e.g. ``"python"``, ``".py"``,
            ``"ts"``). ``None`` or an unsupported value returns ``code``
            unchanged.

    Returns:
        The skeletonized source, or ``code`` unchanged when the language is
        unsupported or the source cannot be parsed safely (fail-safe — this
        function never raises).
    """
    if not code:
        return code
    lang = _LANGUAGE_ALIASES.get((language or "").strip().lower())
    if lang is None:
        return code
    try:
        if lang == "python":
            return _python_skeleton(code)
        # typescript / javascript / go / rust share the brace-language path.
        return _brace_language_skeleton(code, lang)
    except Exception as exc:  # pragma: no cover - defensive outer guard
        _log.warning(
            "skeleton extraction failed for language=%s — returning original content (%s)",
            lang,
            exc,
        )
        return code


# ---------------------------------------------------------------------------
# Python — stdlib ast, line-preserving body elision
# ---------------------------------------------------------------------------


def _python_skeleton(code: str) -> str:
    """Elide function/method bodies via ``ast``; keep everything else verbatim.

    Docstrings are preserved by default. Elided statements are replaced with a
    single ``...`` placeholder at body indentation, so the output remains valid
    Python. On a parse failure the original source is returned unchanged and a
    diagnostic is logged.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        _log.warning(
            "skeleton extraction parse failure (python) — returning original content (%s)",
            exc,
        )
        return code

    lines = code.splitlines(keepends=True)
    # (start_line, end_line, placeholder_indent) ranges to elide, 1-based inclusive.
    elide: list[tuple[int, int, str]] = []

    def _collect(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _elide_function_body(node)
            elif isinstance(node, ast.ClassDef):
                # Keep class-level structure (attributes, annotations,
                # assignments); recurse so methods/nested classes are handled.
                _collect(node.body)
            # Any other statement (imports, assignments, if/try/with blocks at
            # module level, ...) is structure: kept verbatim.

    def _elide_function_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        body = node.body
        first = body[0]
        # One-liner (`def f(): return 1`) — nothing worth eliding.
        if first.lineno == node.lineno:
            return
        keep_through = None  # last line of a leading docstring to preserve
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            keep_through = first.end_lineno or first.lineno
            remainder = body[1:]
        else:
            remainder = body
        if not remainder:
            return  # docstring-only body: already minimal and valid
        start = remainder[0].lineno
        end = max((stmt.end_lineno or stmt.lineno) for stmt in remainder)
        if keep_through is not None:
            start = max(start, keep_through + 1)
        if end - start + 1 < _MIN_ELIDE_LINES and keep_through is None:
            return  # tiny body: elision would not reduce tokens
        if start > end:
            return
        indent = _leading_whitespace(lines[start - 1])
        elide.append((start, end, indent))
        # Recurse no further: nested defs vanish with the elided body.

    _collect(tree.body)

    if not elide:
        return code

    elide.sort()
    out: list[str] = []
    idx = 0  # 0-based cursor into lines
    for start, end, indent in elide:
        out.extend(lines[idx : start - 1])
        placeholder = f"{indent}...\n"
        # Preserve "no trailing newline" if the elided range ended the file.
        if end >= len(lines) and lines and not lines[-1].endswith("\n"):
            placeholder = placeholder.rstrip("\n")
        out.append(placeholder)
        idx = end
    out.extend(lines[idx:])
    return "".join(out)


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# ---------------------------------------------------------------------------
# TypeScript / JavaScript / Go / Rust — conservative brace-matching fallback
# ---------------------------------------------------------------------------

# Declaration-head detectors. These are structural (keywords of the language
# grammar), built per language at import time — not enumerations of project
# symbols. A head match alone never mutates anything: the body is elided only
# when its braces match unambiguously.
_DECL_HEAD_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "go": [
        # func name(...) / func (recv T) name(...)
        re.compile(r"^\s*func\b"),
    ],
    "rust": [
        # [pub [(...)]] [const|async|unsafe|extern "..."]* fn name
        re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:(?:const|async|unsafe|extern\s+\"[^\"]*\")\s+)*fn\s+\w+"
        ),
    ],
    "javascript": [
        # [export [default]] [async] function [*] name(
        re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\b"),
        # [export] const/let/var name [: Type] = ... => {   (block-bodied arrows;
        # anchored on the `=> {` opener so param/return annotations are tolerated)
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+.*=>\s*\{\s*$"),
    ],
}
# TypeScript shares the JS heads (annotations live inside the parens/after them,
# which the head regexes do not constrain).
_DECL_HEAD_PATTERNS["typescript"] = _DECL_HEAD_PATTERNS["javascript"]

# Class-method heads (JS/TS only) — matched only INSIDE a tracked `class { }`
# region to avoid confusing control statements (`if (...) {`) with methods.
_JS_METHOD_HEAD = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|async\s+|get\s+|set\s+)*"
    r"[\w$]+\s*(?:<[^<>]*>)?\s*\("
)
_JS_CONTROL_KEYWORDS = frozenset(
    {"if", "for", "while", "switch", "catch", "return", "do", "else", "new", "typeof", "await"}
)
_JS_CLASS_HEAD = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\b")

_LINE_COMMENT = "//"
_ELISION_MARKER = "// ..."


def _brace_language_skeleton(code: str, lang: str) -> str:
    """Conservative body elision for brace-delimited languages.

    For each declaration head, find its opening ``{`` and the matching ``}``
    (string/comment-aware, line-based). If the interior spans enough lines,
    replace it with a single elision-marker comment at body indentation.
    Constructs whose braces cannot be matched unambiguously are left untouched;
    if the overall scan finds nothing safe to elide, the original is returned.
    """
    lines = code.splitlines(keepends=True)
    counts = _brace_counts(lines, lang)
    if counts is None:
        _log.warning(
            "skeleton extraction parse failure (%s: unbalanced or ambiguous braces) — "
            "returning original content",
            lang,
        )
        return code

    heads = list(_DECL_HEAD_PATTERNS[lang])
    class_regions = _find_class_regions(lines, counts) if lang in ("javascript", "typescript") else []

    elide: list[tuple[int, int, str]] = []  # 0-based [start, end) interior ranges
    claimed_until = -1
    for i, line in enumerate(lines):
        if i <= claimed_until:
            continue  # inside a body already scheduled for elision
        stripped = line.strip()
        if not stripped or stripped.startswith(_LINE_COMMENT):
            continue
        is_head = any(p.match(line) for p in heads)
        if not is_head and _in_regions(i, class_regions) and _JS_METHOD_HEAD.match(line):
            word = re.match(r"\s*([\w$]+)", line)
            if word and word.group(1) not in _JS_CONTROL_KEYWORDS:
                is_head = True
        if not is_head:
            continue
        body = _match_brace_body(lines, counts, i)
        if body is None:
            continue  # interface/type/abstract signature, one-liner, or ambiguous
        open_line, close_line = body
        if close_line - open_line - 1 < _MIN_ELIDE_LINES:
            continue
        indent = _interior_indent(lines, open_line, close_line)
        elide.append((open_line + 1, close_line, indent))
        claimed_until = close_line

    if not elide:
        return code

    out: list[str] = []
    idx = 0
    for start, end, indent in elide:
        out.extend(lines[idx:start])
        out.append(f"{indent}{_ELISION_MARKER}\n")
        idx = end
    out.extend(lines[idx:])
    return "".join(out)


def _brace_counts(lines: list[str], lang: str) -> list[tuple[int, int, int]] | None:
    """Per-line ``(opens, closes, first_open_col)`` with strings/comments
    stripped. Returns ``None`` when the file-level braces do not balance
    (the conservative bail-out signal)."""
    counts: list[tuple[int, int, int]] = []
    state: str | None = None  # None | "comment" | "backtick"
    total = 0
    for line in lines:
        cleaned, state = _strip_strings_and_comments(line, lang, state)
        opens = cleaned.count("{")
        closes = cleaned.count("}")
        first_open = cleaned.find("{")
        counts.append((opens, closes, first_open))
        total += opens - closes
        if total < 0:
            return None
    if total != 0 or state is not None:
        return None
    return counts


def _strip_strings_and_comments(line: str, lang: str, state: str | None) -> tuple[str, str | None]:
    """Blank out string literals and comments so brace counting sees only code.

    Handles ``//`` line comments, ``/* */`` block comments (multi-line, via the
    ``"comment"`` carry-state), double-quoted strings, and backtick strings
    (multi-line, via the ``"backtick"`` carry-state — JS/TS template literals,
    Go raw strings). Rust/Go single quotes (char literals, lifetimes) are
    intentionally NOT treated as string delimiters — a brace inside a char
    literal is vanishingly rare, while Rust lifetimes (``&'a str``) would
    desynchronize a naive quote-tracker.
    """
    out: list[str] = []
    i = 0
    n = len(line)
    quote_chars = "\"'" if lang in ("javascript", "typescript") else '"'
    backtick_langs = ("javascript", "typescript", "go")
    while i < n:
        ch = line[i]
        if state == "comment":
            if ch == "*" and i + 1 < n and line[i + 1] == "/":
                state = None
                i += 2
            else:
                i += 1
            continue
        if state == "backtick":
            if ch == "\\" and lang != "go":  # no escapes in Go raw strings
                i += 2
                continue
            if ch == "`":
                state = None
            i += 1
            continue
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break  # rest of line is a comment
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            state = "comment"
            i += 2
            continue
        if ch == "`" and lang in backtick_langs:
            state = "backtick"
            i += 1
            continue
        if ch in quote_chars:
            quote = ch
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), state


def _match_brace_body(
    lines: list[str], counts: list[tuple[int, int, int]], head_line: int
) -> tuple[int, int] | None:
    """From a declaration head at ``head_line`` (0-based), locate the opening
    ``{`` (on the head line or within the next two lines — multi-line
    signatures) and its matching ``}``. Returns 0-based ``(open_line,
    close_line)`` or ``None`` when no body opens (signature-only declaration)
    or matching is ambiguous."""
    open_line = None
    for j in range(head_line, min(head_line + 3, len(lines))):
        opens, closes, first_open = counts[j]
        if first_open >= 0:
            open_line = j
            break
        stripped = lines[j].strip()
        # A `;` before any `{` means a signature-only declaration (interface
        # member, Rust trait fn, Go func type, `declare function`).
        if stripped.endswith(";"):
            return None
    if open_line is None:
        return None
    depth = 0
    for j in range(open_line, len(lines)):
        opens, closes, _ = counts[j]
        depth += opens - closes
        if depth <= 0:
            if depth < 0:
                return None  # closes ran past opens within a line mix — ambiguous
            return (open_line, j) if j > open_line else None  # same-line body: skip
    return None


def _find_class_regions(
    lines: list[str], counts: list[tuple[int, int, int]]
) -> list[tuple[int, int]]:
    """0-based (start, end) line ranges of ``class { ... }`` bodies (JS/TS)."""
    regions: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if _JS_CLASS_HEAD.match(line):
            body = _match_brace_body(lines, counts, i)
            if body is not None:
                regions.append((body[0], body[1]))
    return regions


def _in_regions(line_no: int, regions: list[tuple[int, int]]) -> bool:
    return any(start < line_no < end for start, end in regions)


def _interior_indent(lines: list[str], open_line: int, close_line: int) -> str:
    """Indentation for the elision marker: first interior line's indent, else
    the closing line's indent plus one step."""
    for j in range(open_line + 1, close_line):
        if lines[j].strip():
            return _leading_whitespace(lines[j])
    return _leading_whitespace(lines[close_line]) + "    "
