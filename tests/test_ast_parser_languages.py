from nce.ast_parser import parse_file


def test_java_parsing():
    java_code = """
    public class Calculator {
        public int add(int a, int b) {
            return a + b;
        }
    }
    """
    chunks = list(parse_file(java_code, "java"))
    assert len(chunks) >= 2

    # Verify class declaration is captured
    class_chunk = next((c for c in chunks if c.node_type == "class"), None)
    assert class_chunk is not None
    assert class_chunk.name == "Calculator"

    # Verify method declaration is captured
    method_chunk = next((c for c in chunks if c.node_type == "function"), None)
    assert method_chunk is not None
    assert method_chunk.name == "add"


def test_elixir_parsing_dynamic():
    elixir_code = """
    defmodule MathEngine do
        def add(a, b) do
            a + b
        end
    end
    """
    chunks = list(parse_file(elixir_code, "elixir"))
    assert len(chunks) == 2

    # Verify module is captured as a class
    mod_chunk = chunks[0]
    assert mod_chunk.node_type == "class"
    assert mod_chunk.name == "MathEngine"
    assert mod_chunk.start_line == 2

    # Verify function is captured as a function
    fun_chunk = chunks[1]
    assert fun_chunk.node_type == "function"
    assert fun_chunk.name == "add"
    assert fun_chunk.start_line == 3


def test_zig_parsing():
    zig_code = """
    fn add(a: i32, b: i32) i32 {
        return a + b;
    }
    """
    chunks = list(parse_file(zig_code, "zig"))
    assert len(chunks) == 1

    chunk = chunks[0]
    assert chunk.node_type == "class"  # Zig's Decl maps to class since it's a generic declaration
    assert chunk.name == "add"
    assert chunk.start_line == 2


def test_fallback_unsupported():
    code = "hello world\nline 2\nline 3"
    chunks = list(parse_file(code, "nonexistent_lang"))
    assert len(chunks) == 1
    assert chunks[0].node_type == "block"
    assert chunks[0].name.startswith("<lines_")


def test_semantic_fallback_chunking():
    # 1. Paragraph boundary split test under normal conditions
    code = (
        "Paragraph 1 - Line 1\n"
        "Paragraph 1 - Line 2\n"
        "\n"
        "Paragraph 2 - Line 1\n"
        "Paragraph 2 - Line 2\n"
        "\n"
        "Paragraph 3 - Line 1"
    )
    chunks = list(parse_file(code, "nonexistent_lang"))
    assert len(chunks) == 1
    assert chunks[0].node_type == "block"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 7
    assert "Paragraph 2" in chunks[0].code_string

    # 2. Budget enforcement test with small limits
    from nce import ast_parser

    old_chars = ast_parser._FALLBACK_CHUNK_CHARS
    old_lines = ast_parser._FALLBACK_CHUNK_LINES
    try:
        ast_parser._FALLBACK_CHUNK_CHARS = 50  # Small char budget
        ast_parser._FALLBACK_CHUNK_LINES = 10  # Line budget

        para_code = "Para one is here.\n\nPara two is here.\n\nPara three is here."
        # Trace of para_code line lengths (+1 for newline):
        # 1. "Para one is here." -> 18 chars, 1 line
        # 2. "" -> 1 char, 1 line
        # 3. "Para two is here." -> 18 chars, 1 line
        # 4. "" -> 1 char, 1 line
        # Total so far: 38 chars, 4 lines
        # 5. "Para three is here." -> 20 chars, 1 line.
        # Adding #5 would exceed 50 chars.
        # Thus, first chunk is lines 1-4, second chunk is line 5.

        chunks = list(parse_file(para_code, "nonexistent_lang"))
        assert len(chunks) == 2

        # Verify Chunk 1
        assert chunks[0].start_line == 1
        assert chunks[0].end_line == 4
        assert chunks[0].code_string == "Para one is here.\n\nPara two is here.\n"

        # Verify Chunk 2
        assert chunks[1].start_line == 5
        assert chunks[1].end_line == 5
        assert chunks[1].code_string == "Para three is here."

    finally:
        ast_parser._FALLBACK_CHUNK_CHARS = old_chars
        ast_parser._FALLBACK_CHUNK_LINES = old_lines
