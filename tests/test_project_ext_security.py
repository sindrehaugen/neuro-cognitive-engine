"""MPXJ sidecar command allowlist and shell-metacharacter guards."""

from __future__ import annotations

from nce.extractors import project_ext


def test_parse_mpxj_argv_rejects_shell_metacharacters():
    assert project_ext._parse_mpxj_argv("java -jar mpxj.jar; rm -rf /") is None


def test_parse_mpxj_argv_rejects_disallowed_binary(monkeypatch):
    monkeypatch.delenv("NCE_MPXJ_ALLOWED_BINARIES", raising=False)
    assert project_ext._parse_mpxj_argv("curl http://evil") is None


def test_parse_mpxj_argv_accepts_allowlisted_java(monkeypatch):
    monkeypatch.delenv("NCE_MPXJ_ALLOWED_BINARIES", raising=False)
    argv = project_ext._parse_mpxj_argv('java -jar "/opt/mpxj/cli.jar"')
    assert argv is not None
    assert argv[0] == "java"
    assert "-jar" in argv
    assert any("cli.jar" in part for part in argv)


def test_parse_mpxj_argv_honors_custom_allowlist(monkeypatch):
    monkeypatch.setenv("NCE_MPXJ_ALLOWED_BINARIES", "custom-mpp-tool")
    argv = project_ext._parse_mpxj_argv("custom-mpp-tool --json")
    assert argv == ["custom-mpp-tool", "--json"]
