"""
Phase 3.2: AST-Aware Code Parser
Parses source files into function/class chunks using Tree-sitter.
Falls back to a line-based splitter if Tree-sitter bindings are unavailable.
Yields CodeChunk objects consumed by NCEEngine.index_code_file().
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

log = logging.getLogger("nce-ast")

SUPPORTED_LANGUAGES = (
    "python",
    "javascript",
    "typescript",
    "go",
    "rust",
    "java",
    "c",
    "cpp",
    "csharp",
    "ruby",
    "php",
    "swift",
    "kotlin",
    "scala",
    "lua",
)

try:
    import tree_sitter_language_pack

    if hasattr(tree_sitter_language_pack, "manifest_languages"):
        _MANIFEST_LANGUAGES = frozenset(tree_sitter_language_pack.manifest_languages())
    else:
        _MANIFEST_LANGUAGES = frozenset()
except ImportError:
    _MANIFEST_LANGUAGES = frozenset()


def _is_pack_supported(language: str) -> bool:
    """Check if the language pack can provide a parser for this language."""
    if not _MANIFEST_LANGUAGES:
        try:
            from typing import get_args

            from tree_sitter_language_pack import SupportedLanguage

            allowed = frozenset(get_args(SupportedLanguage))
            return language in allowed
        except ImportError:
            return False
    return language in _MANIFEST_LANGUAGES


# Protects against RecursionError on deeply nested auto-generated code (FIX-051)
_MAX_AST_DEPTH = 200

# Line-based fallback chunk size — prevents unbounded embedding payloads for
# large files where Tree-sitter is unavailable or finds no top-level symbols.
_FALLBACK_CHUNK_LINES = 200
_FALLBACK_CHUNK_CHARS = 4000

_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "golang": "go",
    "rs": "rust",
    "c++": "cpp",
    "c#": "csharp",
    "sh": "bash",
    "yml": "yaml",
    "rb": "ruby",
    "pl": "perl",
    "kt": "kotlin",
    "clj": "clojure",
    "ex": "elixir",
}


@dataclass
class CodeChunk:
    node_type: str  # 'function' | 'class' | 'block'
    name: str
    code_string: str
    start_line: int
    end_line: int


# --- Tree-sitter backend (active when packages are installed) ---


def _try_treesitter_parse(raw_code: str, language: str) -> list[CodeChunk] | None:
    """
    Attempt Tree-sitter parse via tree-sitter-language-pack (enterprise bundle).
    Returns None if the pack is missing or the grammar cannot load so the
    caller can fall back gracefully — no hard import-time crash.
    """
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return None

    if not _is_pack_supported(language):
        log.warning("Tree-sitter language pack has no grammar for %r", language)
        return None

    try:
        parser = get_parser(language)  # type: ignore[arg-type]
        # tree-sitter-language-pack >=1.12 ships its own native binding whose
        # ``parse`` takes ``str``; the legacy tree_sitter binding takes bytes.
        try:
            tree = parser.parse(raw_code)
        except TypeError:
            tree = parser.parse(raw_code.encode("utf-8"))  # type: ignore[arg-type]
    except Exception as e:
        log.warning("Tree-sitter language pack failed for %r: %s", language, e)
        return None

    if tree is None:  # new binding's parse() is typed Tree | None
        log.warning("Tree-sitter language pack returned no tree for %r", language)
        return None

    # ---- binding-agnostic node accessors -------------------------------
    # The >=1.12 pack binding exposes node accessors as METHODS
    # (``kind()``, ``child(i)``, ``start_position().row``, byte offsets)
    # while the legacy tree_sitter binding uses PROPERTIES
    # (``type``, ``children``, ``text``, ``start_point[0]``).  This
    # binding has already flipped shape once — normalize both.
    src_bytes = raw_code.encode("utf-8")

    def _ts(value):
        return value() if callable(value) else value

    def _kind(node) -> str:
        attr = getattr(node, "kind", None)
        if attr is None:
            return node.type  # legacy binding
        return _ts(attr)

    def _child_nodes(node) -> list:
        children = getattr(node, "children", None)
        if children is not None and not callable(children):
            return list(children)  # legacy binding
        return [node.child(i) for i in range(_ts(node.child_count))]

    def _text(node) -> str:
        raw = getattr(node, "text", None)
        if raw is not None and not callable(raw):
            return raw.decode(errors="ignore")  # legacy binding
        return src_bytes[_ts(node.start_byte) : _ts(node.end_byte)].decode(errors="ignore")

    def _row(node, which: str) -> int:
        point = getattr(node, f"{which}_point", None)
        if point is not None and not callable(point):
            return point[0]  # legacy binding: (row, column) tuple
        return _ts(getattr(node, f"{which}_position")).row

    # Node types that represent meaningful semantic boundaries
    target_types = {
        "python": {"function_definition", "class_definition"},
        "javascript": {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "class_declaration",
            "method_definition",
        },
        "typescript": {
            "function_declaration",
            "generator_function_declaration",
            "class_declaration",
            "method_definition",
            "interface_declaration",
            "type_alias_declaration",
        },
        "go": {"function_declaration", "method_declaration", "type_declaration"},
        "rust": {
            "function_item",
            "struct_item",
            "enum_item",
            "impl_item",
            "trait_item",
        },
        "java": {
            "class_declaration",
            "interface_declaration",
            "method_declaration",
            "constructor_declaration",
            "enum_declaration",
        },
        "c": {
            "function_definition",
            "struct_specifier",
            "enum_specifier",
        },
        "cpp": {
            "function_definition",
            "class_specifier",
            "struct_specifier",
            "namespace_definition",
        },
        "csharp": {
            "class_declaration",
            "interface_declaration",
            "method_declaration",
            "struct_declaration",
            "constructor_declaration",
            "enum_declaration",
        },
        "ruby": {
            "method",
            "class",
            "module",
        },
        "php": {
            "class_declaration",
            "interface_declaration",
            "method_declaration",
            "function_definition",
        },
        "swift": {
            "class_declaration",
            "struct_declaration",
            "protocol_declaration",
            "function_declaration",
        },
        "kotlin": {
            "class_declaration",
            "object_declaration",
            "function_declaration",
        },
        "scala": {
            "class_definition",
            "object_definition",
            "trait_definition",
            "function_definition",
        },
        "lua": {
            "function_definition",
            "local_function_definition",
        },
        "zig": {
            "Decl",
        },
    }
    targets = target_types.get(language, set())
    lines = raw_code.splitlines()
    chunks: list[CodeChunk] = []

    def _extract_name(node) -> str:
        if _kind(node) in ("call", "macro_definition", "macro_call", "expression"):
            node_children = _child_nodes(node)
            first_child = node_children[0] if node_children else None
            if first_child is not None and _kind(first_child).lower() in ("identifier", "name"):
                keyword = _text(first_child).lower()
                if keyword in (
                    "def",
                    "defmodule",
                    "defmacro",
                    "defn",
                    "fn",
                    "defp",
                    "defstruct",
                    "defimpl",
                    "defprotocol",
                    "func",
                    "function",
                ):
                    for child in node_children:
                        if _kind(child) == "arguments":
                            for gchild in _child_nodes(child):
                                if _kind(gchild).lower() in (
                                    "identifier",
                                    "name",
                                    "alias",
                                    "symbol",
                                ):
                                    return _text(gchild)
                                elif _kind(gchild) in (
                                    "call",
                                    "macro_definition",
                                    "macro_call",
                                    "expression",
                                ):
                                    for ggchild in _child_nodes(gchild):
                                        if _kind(ggchild).lower() in (
                                            "identifier",
                                            "name",
                                            "alias",
                                            "symbol",
                                        ):
                                            return _text(ggchild)

        def _find_id(n) -> str | None:
            if _kind(n).lower() in ("identifier", "name", "alias", "symbol"):
                return _text(n)
            if _kind(n).lower() in ("block", "statement", "body", "compound_statement"):
                return None
            for child in _child_nodes(n):
                res = _find_id(child)
                if res:
                    return res
            return None

        for child in _child_nodes(node):
            if _kind(child).lower() in ("identifier", "name"):
                return _text(child)

        res = _find_id(node)
        return res if res else "<anonymous>"

    def _walk(node, depth: int = 0) -> None:
        if depth > _MAX_AST_DEPTH:
            log.warning(
                "Tree-sitter walk exceeded depth=%d — skipping deeper nodes",
                _MAX_AST_DEPTH,
            )
            return

        kind = _kind(node)
        node_children = _child_nodes(node)

        is_target = False
        if targets:
            is_target = kind in targets
        else:
            # Fallback heuristic for any other language in the 305+ pack:
            # Matches node types that look like definitions/declarations of structural code blocks.
            t = kind.lower()
            is_target = any(
                keyword in t
                for keyword in (
                    "function",
                    "method",
                    "class",
                    "struct",
                    "interface",
                    "module",
                    "definition",
                    "declaration",
                    "procedure",
                    "subroutine",
                )
            )
            if not is_target:
                # Also check if it's a call/macro node with a structural keyword like def/defmodule/fn
                if kind in ("call", "macro_definition", "macro_call", "expression"):
                    first_child = node_children[0] if node_children else None
                    if first_child is not None and _kind(first_child) in ("identifier", "name"):
                        text = _text(first_child).lower()
                        is_target = text in (
                            "def",
                            "defmodule",
                            "defmacro",
                            "defn",
                            "fn",
                            "defp",
                            "defstruct",
                            "defimpl",
                            "defprotocol",
                            "func",
                            "function",
                        )

        if is_target:
            start = _row(node, "start")  # 0-indexed
            end = _row(node, "end")
            code_string = "\n".join(lines[start : end + 1])
            if "function" in kind.lower() or "method" in kind.lower():
                node_type = "function"
            elif kind in ("call", "macro_definition", "macro_call", "expression") and node_children:
                first_child = node_children[0]
                text = _text(first_child).lower() if first_child is not None else ""
                node_type = (
                    "function"
                    if text in ("def", "defn", "fn", "defp", "defmacro", "func", "function")
                    else "class"
                )
            else:
                node_type = "class"
            chunks.append(
                CodeChunk(
                    node_type=node_type,
                    name=_extract_name(node),
                    code_string=code_string,
                    start_line=start + 1,  # 1-indexed for storage
                    end_line=end + 1,
                )
            )
        for child in node_children:
            _walk(child, depth + 1)

    _walk(_ts(tree.root_node))
    return chunks if chunks else None


# --- Public API ---


def _line_chunks(raw_code: str) -> Iterator[CodeChunk]:
    """
    Split raw_code into bounded, semantically-aware line-based chunks.
    Prefers splitting on paragraph boundaries (empty lines) and keeps chunks
    within line and character budgets to preserve RAG retrieval context.
    """
    lines = raw_code.splitlines()
    if not lines:
        return

    max_lines = _FALLBACK_CHUNK_LINES
    max_chars = _FALLBACK_CHUNK_CHARS

    def emit_chunk(start: int, end: int, block_lines: list[str]) -> CodeChunk:
        return CodeChunk(
            node_type="block",
            name=f"<lines_{start}_{end}>",
            code_string="\n".join(block_lines),
            start_line=start,
            end_line=end,
        )

    # Group lines into paragraphs. Empty or whitespace-only lines are treated
    # as standalone single-line paragraphs to maintain exact empty space.
    paragraphs: list[list[str]] = []
    current_para: list[str] = []

    for line in lines:
        if line.strip() == "":
            if current_para:
                paragraphs.append(current_para)
                current_para = []
            paragraphs.append([line])
        else:
            current_para.append(line)

    if current_para:
        paragraphs.append(current_para)

    current_chunk: list[str] = []
    current_start = 1
    current_line_count = 0
    current_char_count = 0

    for para in paragraphs:
        para_lines = len(para)
        para_chars = sum(len(ln) for ln in para) + para_lines  # +1 character per line for newline

        # Check if adding this paragraph would exceed the limits.
        # If so, emit the current chunk first.
        if current_chunk and (
            current_line_count + para_lines > max_lines
            or current_char_count + para_chars > max_chars
        ):
            yield emit_chunk(current_start, current_start + current_line_count - 1, current_chunk)
            current_start = current_start + current_line_count
            current_chunk = []
            current_line_count = 0
            current_char_count = 0

        # If a single paragraph itself exceeds either limit, slice it line-by-line.
        if para_lines > max_lines or para_chars > max_chars:
            for line in para:
                line_len = len(line) + 1
                if current_chunk and (
                    current_line_count + 1 > max_lines or current_char_count + line_len > max_chars
                ):
                    yield emit_chunk(
                        current_start, current_start + current_line_count - 1, current_chunk
                    )
                    current_start = current_start + current_line_count
                    current_chunk = []
                    current_line_count = 0
                    current_char_count = 0

                current_chunk.append(line)
                current_line_count += 1
                current_char_count += line_len
        else:
            # Paragraph fits perfectly, add it.
            current_chunk.extend(para)
            current_line_count += para_lines
            current_char_count += para_chars

    if current_chunk:
        yield emit_chunk(current_start, current_start + current_line_count - 1, current_chunk)


def parse_file(raw_code: str, language: str) -> Iterator[CodeChunk]:
    """
    Parse source code into CodeChunk objects.

    Normalises language aliases (e.g. "py" → "python"), tries Tree-sitter
    for supported languages (dynamically validated against the language pack),
    then falls back to bounded line-based chunks.
    """
    language = _LANGUAGE_ALIASES.get(language.lower(), language.lower())

    is_pack_supported = _is_pack_supported(language)

    if not is_pack_supported and language not in SUPPORTED_LANGUAGES:
        log.warning("Language %r not supported — yielding line-based chunks.", language)
        yield from _line_chunks(raw_code)
        return

    chunks = _try_treesitter_parse(raw_code, language)

    if not chunks:
        # Tree-sitter unavailable or found no top-level symbols — use bounded
        # line-based chunks to avoid huge embedding payloads for large files.
        yield from _line_chunks(raw_code)
        return

    yield from chunks
