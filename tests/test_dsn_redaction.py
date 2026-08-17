"""Tests for DSN / URI credential redaction in logs (Item 9 observability)."""

from nce.config import redact_dsn, redact_secrets_in_text


def test_redact_dsn_postgresql() -> None:
    dsn = "postgresql://mcp_user:secret_pass@db.example.com:5432/memory_meta"
    out = redact_dsn(dsn)
    assert "secret_pass" not in out
    assert "mcp_user:***@" in out
    assert "db.example.com" in out


def test_redact_dsn_redis_password_only() -> None:
    dsn = "redis://:myredissecret@redis.internal:6379/0"
    out = redact_dsn(dsn)
    assert "myredissecret" not in out
    assert ":***@" in out


def test_redact_dsn_no_password_unchanged() -> None:
    dsn = "redis://localhost:6379/0"
    assert redact_dsn(dsn) == dsn


def test_redact_secrets_in_text_embedded() -> None:
    raw = (
        "Connection refused: could not parse URI "
        "postgresql://app:SUPER_SECRET@10.0.0.5:5432/prod — check firewall"
    )
    out = redact_secrets_in_text(raw)
    assert "SUPER_SECRET" not in out
    assert "app:***@" in out


def test_redact_secrets_in_text_redis_inline() -> None:
    raw = "Error connecting redis://:abc123xyz@cache:6379"
    out = redact_secrets_in_text(raw)
    assert "abc123xyz" not in out
    assert "redis://:***@" in out


def test_redact_secrets_in_text_mongodb_srv() -> None:
    raw = "fail mongodb+srv://user:weakpass@cluster0.abcd.mongodb.net/mydb"
    out = redact_secrets_in_text(raw)
    assert "weakpass" not in out
    assert "user:***@" in out
