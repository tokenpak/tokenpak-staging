"""Extraction-quality tests for ``tokenpak.skeleton_extractor``.

Acceptance (p2-skeleton-extractor-implementation):

- per supported language (Python, TypeScript, JavaScript, Go, Rust):
  signatures / annotations / decorators / imports preserved, bodies elided,
  docstrings (and doc comments) preserved by default;
- parse-failure path returns the original content unchanged and emits the
  diagnostic signal on the ``tokenpak.skeleton`` logger;
- savings are MEASURED, never asserted as a marketing percentage — the
  benchmark below records a conservative measured floor on representative
  body-heavy code.
"""

from __future__ import annotations

import ast
import logging

import pytest

from tokenpak.skeleton_extractor import extract_skeleton

# ---------------------------------------------------------------------------
# Python (stdlib ast path)
# ---------------------------------------------------------------------------

PY_SOURCE = '''\
"""Module docstring stays."""
import os
from typing import Any

MAX_RETRIES = 3


@retry(times=MAX_RETRIES)
def fetch(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Fetch *url* and decode JSON."""
    backoff = 0.1
    for attempt in range(MAX_RETRIES):
        result = _attempt(url, timeout)
        if result is not None:
            return result
        backoff *= 2
    raise TimeoutError(url)


class Client:
    """Client docstring stays."""

    base_url: str = "https://example.invalid"

    def __init__(self, base_url: str | None = None) -> None:
        resolved = base_url or self.base_url
        self.base_url = resolved.rstrip("/")
        self.session = os.environ.get("SESSION", "")

    async def get(self, path: str) -> dict[str, Any]:
        """Async GET against the client base URL."""
        url = f"{self.base_url}/{path}"
        data = await afetch(url)
        return data


def tiny(): return 1
'''


def test_python_signatures_decorators_imports_preserved():
    out = extract_skeleton(PY_SOURCE, "python")
    assert '"""Module docstring stays."""' in out
    assert "import os" in out
    assert "from typing import Any" in out
    assert "MAX_RETRIES = 3" in out
    assert "@retry(times=MAX_RETRIES)" in out
    assert "def fetch(url: str, *, timeout: float = 5.0) -> dict[str, Any]:" in out
    assert "class Client:" in out
    assert 'base_url: str = "https://example.invalid"' in out
    assert "def __init__(self, base_url: str | None = None) -> None:" in out
    assert "async def get(self, path: str) -> dict[str, Any]:" in out


def test_python_bodies_elided_docstrings_preserved():
    out = extract_skeleton(PY_SOURCE, "python")
    # docstrings preserved by default
    assert '"""Fetch *url* and decode JSON."""' in out
    assert '"""Client docstring stays."""' in out
    assert '"""Async GET against the client base URL."""' in out
    # bodies elided
    assert "backoff = 0.1" not in out
    assert "raise TimeoutError(url)" not in out
    assert "self.session" not in out
    assert "await afetch(url)" not in out
    assert "..." in out


def test_python_skeleton_is_valid_python():
    out = extract_skeleton(PY_SOURCE, "python")
    ast.parse(out)  # must not raise


def test_python_one_liner_def_untouched():
    out = extract_skeleton(PY_SOURCE, "python")
    assert "def tiny(): return 1" in out


def test_python_language_aliases():
    body_heavy = "def f(a):\n    x = a + 1\n    y = x * 2\n    return y\n"
    for alias in ("python", "py", ".py", "Python", " PY "):
        out = extract_skeleton(body_heavy, alias)
        assert "def f(a):" in out
        assert "x = a + 1" not in out


def test_python_parse_failure_returns_original_and_logs(caplog):
    broken = "def broken(:\n    pass\n"
    with caplog.at_level(logging.WARNING, logger="tokenpak.skeleton"):
        out = extract_skeleton(broken, "python")
    assert out == broken
    assert any("parse failure" in r.message for r in caplog.records)


def test_python_idempotent():
    once = extract_skeleton(PY_SOURCE, "python")
    twice = extract_skeleton(once, "python")
    assert twice == once


# ---------------------------------------------------------------------------
# TypeScript / JavaScript (conservative brace fallback)
# ---------------------------------------------------------------------------

TS_SOURCE = '''\
import { Thing } from "./thing";

/** Doc comment stays. */
export async function fetchData(url: string): Promise<Thing> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`bad ${res.status}`);
  }
  return res.json();
}

export const compute = (a: number, b: number): number => {
  const x = a * 2;
  const y = b + x;
  return x + y;
};

interface Opts {
  retries: number;
  log(msg: string): void;
}

export class Service {
  private count = 0;

  handle(req: Request): Response {
    this.count += 1;
    const body = `{ "n": ${this.count} }`;
    return new Response(body);
  }
}
'''


def test_typescript_signatures_and_structure_preserved():
    out = extract_skeleton(TS_SOURCE, "typescript")
    assert 'import { Thing } from "./thing";' in out
    assert "/** Doc comment stays. */" in out
    assert "export async function fetchData(url: string): Promise<Thing> {" in out
    assert "export const compute = (a: number, b: number): number => {" in out
    # interfaces / type-level structure untouched
    assert "interface Opts {" in out
    assert "retries: number;" in out
    assert "log(msg: string): void;" in out
    assert "export class Service {" in out
    assert "private count = 0;" in out
    assert "handle(req: Request): Response {" in out


def test_typescript_bodies_elided():
    out = extract_skeleton(TS_SOURCE, "ts")
    assert "const res = await fetch(url);" not in out
    assert "const x = a * 2;" not in out
    assert "this.count += 1;" not in out
    assert "// ..." in out
    # braces stay balanced
    assert out.count("{") == out.count("}")


JS_SOURCE = '''\
const fs = require("fs");

function readAll(dir) {
  const out = [];
  for (const f of fs.readdirSync(dir)) {
    out.push(f);
  }
  return out;
}

const sum = (xs) => {
  let t = 0;
  xs.forEach((x) => { t += x; });
  return t;
};
'''


def test_javascript_function_and_arrow_bodies_elided():
    out = extract_skeleton(JS_SOURCE, "javascript")
    assert 'const fs = require("fs");' in out
    assert "function readAll(dir) {" in out
    assert "const sum = (xs) => {" in out
    assert "out.push(f);" not in out
    assert "let t = 0;" not in out
    assert "// ..." in out


def test_javascript_template_literal_braces_do_not_corrupt():
    src = (
        "const tpl = `\n"
        '  { "a": } } { {\n'
        "`;\n"
        "\n"
        "function realWork(n) {\n"
        "  const a = n + 1;\n"
        "  const b = a * 2;\n"
        "  return b;\n"
        "}\n"
    )
    out = extract_skeleton(src, "js")
    # template literal content survives verbatim (braces inside it ignored)
    assert '{ "a": } } { {' in out
    # the real function body is still elided
    assert "const a = n + 1;" not in out
    assert "// ..." in out


def test_javascript_control_flow_not_mistaken_for_method():
    # `if (...) {` at top level must never be elided as a "method".
    src = (
        "if (process.env.DEBUG) {\n"
        "  console.log('a');\n"
        "  console.log('b');\n"
        "  console.log('c');\n"
        "}\n"
    )
    assert extract_skeleton(src, "js") == src


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

GO_SOURCE = '''\
package main

import "fmt"

type Server struct {
	Port int
}

// Run starts the server.
func (s *Server) Run() error {
	addr := fmt.Sprintf(":%d", s.Port)
	if addr == "" {
		return nil
	}
	fmt.Println(addr)
	return nil
}

func main() {
	s := &Server{Port: 8080}
	_ = s.Run()
}
'''


def test_go_signatures_structs_imports_preserved_bodies_elided():
    out = extract_skeleton(GO_SOURCE, "go")
    assert "package main" in out
    assert 'import "fmt"' in out
    # struct definitions are structure, not bodies — untouched
    assert "type Server struct {" in out
    assert "Port int" in out
    # doc comment + signatures preserved
    assert "// Run starts the server." in out
    assert "func (s *Server) Run() error {" in out
    assert "func main() {" in out
    # bodies elided
    assert "addr := fmt.Sprintf" not in out
    assert "s := &Server{Port: 8080}" not in out
    assert "// ..." in out
    assert out.count("{") == out.count("}")


def test_go_unbalanced_braces_fail_safe(caplog):
    broken = "func broken() {\n\tx := 1\n"
    with caplog.at_level(logging.WARNING, logger="tokenpak.skeleton"):
        out = extract_skeleton(broken, "go")
    assert out == broken
    assert any("parse failure" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

RUST_SOURCE = '''\
use std::collections::HashMap;

pub struct Cache {
    map: HashMap<String, String>,
}

impl Cache {
    /// Doc comment stays.
    pub fn get(&self, key: &str) -> Option<&String> {
        let normalized = key.trim().to_lowercase();
        let v = self.map.get(&normalized);
        match v {
            Some(s) => Some(s),
            None => None,
        }
    }

    pub fn put(&mut self, key: String, value: String) -> Option<String> {
        let normalized = key.trim().to_lowercase();
        if normalized.is_empty() {
            return None;
        }
        self.map.insert(normalized, value)
    }

    pub fn new() -> Self {
        let map = HashMap::new();
        let cache = Cache { map };
        cache
    }
}

trait Store {
    fn put(&mut self, key: String, value: String);
}
'''


def test_rust_signatures_structs_uses_preserved_bodies_elided():
    out = extract_skeleton(RUST_SOURCE, "rust")
    assert "use std::collections::HashMap;" in out
    assert "pub struct Cache {" in out
    assert "map: HashMap<String, String>," in out
    assert "impl Cache {" in out
    assert "/// Doc comment stays." in out
    assert "pub fn get(&self, key: &str) -> Option<&String> {" in out
    # body elided
    assert "let v = self.map.get(key);" not in out
    assert "// ..." in out
    # trait signature-only declarations untouched
    assert "fn put(&mut self, key: String, value: String);" in out
    assert out.count("{") == out.count("}")


# ---------------------------------------------------------------------------
# Fail-safe contract / unsupported inputs
# ---------------------------------------------------------------------------


def test_unsupported_or_missing_language_returns_unchanged():
    src = "def f():\n    pass\n"
    assert extract_skeleton(src, None) == src
    assert extract_skeleton(src, "") == src
    assert extract_skeleton(src, "ruby") == src
    assert extract_skeleton("", "python") == ""


def test_extract_skeleton_never_raises_on_garbage():
    garbage = "\x00\x01{{{{`\"'/*\n}}}"
    for lang in ("python", "typescript", "javascript", "go", "rust", "nope", None):
        # contract: never raises, always returns a str
        assert isinstance(extract_skeleton(garbage, lang), str)


# ---------------------------------------------------------------------------
# Measured savings (benchmark — measure, don't assert marketing claims)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "language"),
    [
        (PY_SOURCE, "python"),
        (TS_SOURCE, "typescript"),
        (JS_SOURCE, "javascript"),
        (GO_SOURCE, "go"),
        (RUST_SOURCE, "rust"),
    ],
    ids=["python", "typescript", "javascript", "go", "rust"],
)
def test_measured_reduction_floor_on_representative_code(source, language):
    """Conservative measured floor: on representative body-heavy code the
    skeleton must be at least 25% smaller than the original. This is the
    committed measurement backing any savings statement — no unbacked
    percentage claims are made anywhere in live code."""
    skeleton = extract_skeleton(source, language)
    assert skeleton != source
    reduction = 1 - (len(skeleton) / len(source))
    assert reduction >= 0.25, f"{language}: measured reduction {reduction:.1%} below 25% floor"
