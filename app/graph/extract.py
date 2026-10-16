"""Code extraction behind a replaceable adapter (architecture: Graphify sits behind an adapter).

`BuiltinExtractor` parses Python with `ast` and TypeScript/JavaScript with conservative regexes. It is honest about what
it knows: structure it read directly is `extracted`; a call matched to a symbol by name only is `inferred`, and a name
that matches several symbols is `ambiguous` (one edge per candidate, none silently chosen). It never runs repository code.
A Graphify-based adapter can implement the same `Extractor` protocol and replace this one.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Protocol

EXTRACTOR_NAME = "builtin-ast-regex"
EXTRACTOR_VERSION = "1"
OUTPUT_SCHEMA_VERSION = "1"

LANGUAGES = {".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript"}
#: Seen but not parsed: reported as `unsupported_language` coverage, never silently dropped.
KNOWN_UNPARSED = {".go": "go", ".java": "java", ".rb": "ruby", ".rs": "rust", ".cs": "csharp", ".kt": "kotlin", ".php": "php"}


@dataclass
class Symbol:
    qualified_name: str
    name: str
    kind: str  # function | method | class
    start_line: int
    end_line: int
    parent: str | None = None  # qualified name of the containing class


@dataclass
class RawEdge:
    """An edge as read from one file, before names are resolved across files."""

    kind: str  # calls | imports | inherits
    source: str  # qualified name of the calling/importing symbol, or "" for the file itself
    target_name: str  # simple or dotted name as written
    line: int


@dataclass
class FileResult:
    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[RawEdge] = field(default_factory=list)
    partial: bool = False  # parse failed or was cut short: coverage gap, not an error


class Extractor(Protocol):
    name: str
    version: str

    def extract(self, path: str, text: str) -> FileResult | None: ...


# ---------------------------------------------------------------------------------------------------- python
class _PyVisitor(ast.NodeVisitor):
    def __init__(self, res: FileResult):
        self.res = res
        self.stack: list[str] = []
        self.fn: list[str] = []

    def _qual(self, name: str) -> str:
        return ".".join([*self.stack, name]) if self.stack else name

    def visit_ClassDef(self, n: ast.ClassDef):
        q = self._qual(n.name)
        self.res.symbols.append(Symbol(q, n.name, "class", n.lineno, n.end_lineno or n.lineno, ".".join(self.stack) or None))
        for b in n.bases:
            nm = _dotted(b)
            if nm:
                self.res.edges.append(RawEdge("inherits", q, nm, n.lineno))
        self.stack.append(n.name)
        self.generic_visit(n)
        self.stack.pop()

    def _func(self, n):
        q = self._qual(n.name)
        kind = "method" if self.stack and self.res.symbols and any(s.qualified_name == ".".join(self.stack) and s.kind == "class" for s in self.res.symbols) else "function"
        self.res.symbols.append(Symbol(q, n.name, kind, n.lineno, n.end_lineno or n.lineno, ".".join(self.stack) or None))
        self.stack.append(n.name)
        self.fn.append(q)
        self.generic_visit(n)
        self.fn.pop()
        self.stack.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_Call(self, n: ast.Call):
        nm = _dotted(n.func)
        if nm and self.fn:
            self.res.edges.append(RawEdge("calls", self.fn[-1], nm, n.lineno))
        self.generic_visit(n)

    def visit_Import(self, n: ast.Import):
        for a in n.names:
            self.res.edges.append(RawEdge("imports", "", a.name, n.lineno))

    def visit_ImportFrom(self, n: ast.ImportFrom):
        mod = ("." * (n.level or 0)) + (n.module or "")
        for a in n.names:
            self.res.edges.append(RawEdge("imports", "", f"{mod}:{a.name}", n.lineno))


def _dotted(n: ast.AST) -> str | None:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        base = _dotted(n.value)
        return f"{base}.{n.attr}" if base else n.attr
    return None


def _extract_python(path: str, text: str) -> FileResult:
    res = FileResult(path, "python")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        res.partial = True
        return res
    _PyVisitor(res).visit(tree)
    return res


# ---------------------------------------------------------------------------------------------- typescript / js
_TS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[(<]")
_TS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>")
_TS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$.]*))?")
_TS_METHOD = re.compile(r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?::\s*[^{]+)?\{\s*$")
_TS_IMPORT = re.compile(r"""^\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""")
_TS_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "await", "super", "import", "require", "constructor"}


def _brace_end(lines: list[str], start: int) -> int:
    """Line index of the brace that closes the block opened at/after `start` (0-based). Cut short = last line."""
    depth, seen = 0, False
    for i in range(start, len(lines)):
        for ch in _strip_strings(lines[i]):
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
        if seen and depth <= 0:
            return i
        if not seen and i > start + 3 and not lines[i].rstrip().endswith(("(", ",", "=>")):
            return i  # expression-bodied arrow: ends within a few lines
    return len(lines) - 1


def _strip_strings(line: str) -> str:
    line = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`', '""', line)
    return re.sub(r"//.*$", "", line)


def _extract_ts(path: str, text: str, language: str) -> FileResult:
    res = FileResult(path, language)
    lines = text.splitlines()
    scopes: list[tuple[int, str]] = []  # (end line index, qualified name) of enclosing function/class
    cls: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        scopes = [s for s in scopes if s[0] >= i]
        cls = [c for c in cls if c[0] >= i]
        m_imp = _TS_IMPORT.match(line)
        if m_imp:
            res.edges.append(RawEdge("imports", "", m_imp.group(1), i + 1))
            continue
        m = _TS_CLASS.match(line)
        if m:
            end = _brace_end(lines, i)
            q = m.group(1)
            res.symbols.append(Symbol(q, q, "class", i + 1, end + 1))
            if m.group(2):
                res.edges.append(RawEdge("inherits", q, m.group(2), i + 1))
            cls.append((end, q))
            scopes.append((end, q))
            continue
        m = _TS_FUNC.match(line) or _TS_ARROW.match(line)
        kind = "function"
        if not m and cls:
            m = _TS_METHOD.match(line)
            kind = "method"
            if m and m.group(1) in _KEYWORDS:
                m = None
        if m:
            name = m.group(1)
            end = _brace_end(lines, i)
            q = f"{cls[-1][1]}.{name}" if kind == "method" and cls else name
            res.symbols.append(Symbol(q, name, kind, i + 1, end + 1, cls[-1][1] if kind == "method" and cls else None))
            scopes.append((end, q))
            continue
        if scopes:
            owner = scopes[-1][1]
            body = _strip_strings(line)
            for c in _TS_CALL.finditer(body):
                nm = c.group(1)
                if nm.split(".")[0] in _KEYWORDS or nm in _KEYWORDS:
                    continue
                res.edges.append(RawEdge("calls", owner, nm, i + 1))
    return res


class BuiltinExtractor:
    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION

    def extract(self, path: str, text: str) -> FileResult | None:
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        lang = LANGUAGES.get(ext)
        if lang is None:
            return None
        if lang == "python":
            return _extract_python(path, text)
        return _extract_ts(path, text, lang)
