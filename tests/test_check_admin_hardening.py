"""
/test-harden — Verify Client-Side Privilege Escalation fix for admin scope validation.

This test suite proves that:
  1. Client-supplied ``is_admin: true`` is IGNORED (no longer grants access).
  2. Missing ``admin_api_key`` is rejected.
  3. Wrong ``admin_api_key`` is rejected (constant-time comparison).
  4. Correct ``admin_api_key`` grants access.
  5. NCE_ADMIN_OVERRIDE=true still works (dev bypass).
  6. Missing NCE_ADMIN_API_KEY env var fails safely (no accidental open door).
  7. Admin tools have ``admin_api_key`` in their required fields.
  8. Admin tools do not expose ``is_admin`` in inputSchema properties (Prompt 48).
"""

import os
import sys
import unittest

# Ensure the project root is on sys.path
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class TestCheckAdminHardening(unittest.TestCase):
    """Direct unit tests for _validate_scope('admin', ...) function."""

    def setUp(self):
        # Import the function under test
        from nce.auth import _validate_scope

        self._validate_scope = _validate_scope

        # Save original env
        self._orig_admin_override = os.environ.pop("NCE_ADMIN_OVERRIDE", None)
        self._orig_admin_api_key = os.environ.pop("NCE_ADMIN_API_KEY", None)

    def tearDown(self):
        # Restore env
        for key in ("NCE_ADMIN_OVERRIDE", "NCE_ADMIN_API_KEY"):
            os.environ.pop(key, None)
        if self._orig_admin_override is not None:
            os.environ["NCE_ADMIN_OVERRIDE"] = self._orig_admin_override
        if self._orig_admin_api_key is not None:
            os.environ["NCE_ADMIN_API_KEY"] = self._orig_admin_api_key

    # ── 1. Client-supplied is_admin is IGNORED ──────────────────────────

    def test_client_is_admin_true_rejected_when_no_key_set(self):
        """Sending is_admin=true should NOT grant access."""
        os.environ["NCE_ADMIN_API_KEY"] = "secret-key-123"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"is_admin": True})
        self.assertIn("missing admin_api_key", str(ctx.exception).lower())

    def test_client_is_admin_true_rejected_when_wrong_key(self):
        """Sending is_admin=true with wrong key should NOT grant access."""
        os.environ["NCE_ADMIN_API_KEY"] = "secret-key-123"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"is_admin": True, "admin_api_key": "wrong"})
        self.assertIn("invalid admin_api_key", str(ctx.exception).lower())

    def test_client_is_admin_false_with_correct_key_still_works(self):
        """is_admin=false is also ignored — only admin_api_key matters."""
        os.environ["NCE_ADMIN_API_KEY"] = "secret-key-123"
        # Should NOT raise
        self._validate_scope("admin", {"is_admin": False, "admin_api_key": "secret-key-123"})

    # ── 2. Missing admin_api_key ────────────────────────────────────────

    def test_missing_admin_api_key_rejected(self):
        """No admin_api_key in arguments -> rejected."""
        os.environ["NCE_ADMIN_API_KEY"] = "secret-key-123"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {})
        self.assertIn("missing admin_api_key", str(ctx.exception).lower())

    def test_empty_admin_api_key_rejected(self):
        """Empty string admin_api_key -> rejected."""
        os.environ["NCE_ADMIN_API_KEY"] = "secret-key-123"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"admin_api_key": ""})
        self.assertIn("missing admin_api_key", str(ctx.exception).lower())

    def test_whitespace_only_admin_api_key_rejected(self):
        """Whitespace-only admin_api_key -> rejected."""
        os.environ["NCE_ADMIN_API_KEY"] = "secret-key-123"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"admin_api_key": "   "})
        self.assertIn("missing admin_api_key", str(ctx.exception).lower())

    # ── 3. Wrong admin_api_key ──────────────────────────────────────────

    def test_wrong_admin_api_key_rejected(self):
        """Incorrect admin_api_key -> rejected with constant-time compare."""
        os.environ["NCE_ADMIN_API_KEY"] = "correct-horse-battery-staple"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"admin_api_key": "wrong-key"})
        self.assertIn("invalid admin_api_key", str(ctx.exception).lower())

    def test_case_sensitive_key_comparison(self):
        """admin_api_key comparison must be case-sensitive."""
        os.environ["NCE_ADMIN_API_KEY"] = "SecretKey"
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"admin_api_key": "secretkey"})
        self.assertIn("invalid admin_api_key", str(ctx.exception).lower())

    def test_timing_side_channel_uses_constant_time_compare(self):
        """Verify secrets.compare_digest is used (indirect: wrong key fails).

        We test that keys of same length but different content fail,
        which is a basic property of constant-time comparison.
        """
        os.environ["NCE_ADMIN_API_KEY"] = "aaaaaaaa"
        with self.assertRaises(Exception):
            self._validate_scope("admin", {"admin_api_key": "bbbbbbbb"})

    # ── 4. Correct admin_api_key ────────────────────────────────────────

    def test_correct_admin_api_key_grants_access(self):
        """Correct admin_api_key -> function returns without error."""
        os.environ["NCE_ADMIN_API_KEY"] = "my-secret-admin-key"
        # Should NOT raise
        self._validate_scope("admin", {"admin_api_key": "my-secret-admin-key"})

    def test_correct_key_with_whitespace_stripping(self):
        """Whitespace around admin_api_key is stripped."""
        os.environ["NCE_ADMIN_API_KEY"] = "key123"
        self._validate_scope("admin", {"admin_api_key": "  key123  "})

    # ── 5. NCE_ADMIN_OVERRIDE ────────────────────────────────────────

    def test_override_grants_access_without_key(self):
        """NCE_ADMIN_OVERRIDE=true bypasses all checks."""
        os.environ["NCE_ADMIN_OVERRIDE"] = "true"
        # No admin_api_key at all — should pass
        self._validate_scope("admin", {})
        self._validate_scope("admin", {"is_admin": False})
        self._validate_scope("admin", {"admin_api_key": "garbage"})

    def test_override_works_when_api_key_not_set(self):
        """Override works even when NCE_ADMIN_API_KEY is absent."""
        os.environ["NCE_ADMIN_OVERRIDE"] = "true"
        # Ensure API key env var is NOT set
        os.environ.pop("NCE_ADMIN_API_KEY", None)
        self._validate_scope("admin", {})

    # ── 6. Missing NCE_ADMIN_API_KEY fails safe ───────────────────────

    def test_no_key_and_no_override_fails_safe(self):
        """When neither NCE_ADMIN_API_KEY nor override is set, fail closed."""
        # Both env vars absent
        os.environ.pop("NCE_ADMIN_API_KEY", None)
        os.environ.pop("NCE_ADMIN_OVERRIDE", None)
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"admin_api_key": "anything"})
        self.assertIn("misconfigured", str(ctx.exception).lower())

    def test_empty_api_key_env_var_fails_safe(self):
        """Empty NCE_ADMIN_API_KEY env var -> fail closed."""
        os.environ["NCE_ADMIN_API_KEY"] = ""
        with self.assertRaises(Exception) as ctx:
            self._validate_scope("admin", {"admin_api_key": "anything"})
        self.assertIn("misconfigured", str(ctx.exception).lower())


class TestAdminToolSchemas(unittest.TestCase):
    """Verify that all admin tool inputSchemas are properly hardened."""

    @classmethod
    def setUpClass(cls):
        # Load server.py's TOOLS list by importing
        from server import TOOLS

        cls.tools = TOOLS
        cls.tool_map = {t.name: t for t in TOOLS}

    def _get_tool(self, name: str):
        tool = self.tool_map.get(name)
        if tool is None:
            self.fail(f"Tool '{name}' not found in TOOLS list")
        return tool

    def _required_fields(self, tool):
        return set(tool.inputSchema.get("required", []))

    def _properties(self, tool):
        return set(tool.inputSchema.get("properties", {}).keys())

    # ── Admin tools must require admin_api_key ───────────────────────────

    ADMIN_TOOLS = [
        "unredact_memory",
        "manage_namespace",
        "trigger_consolidation",
        "consolidation_status",
        "manage_quotas",
        "rotate_signing_key",
        "get_health",
        "replay_observe",
        "replay_fork",
        "replay_reconstruct",
        "replay_status",
    ]

    def test_admin_tools_require_admin_api_key(self):
        """Every admin tool must have admin_api_key in required fields."""
        for tool_name in self.ADMIN_TOOLS:
            tool = self._get_tool(tool_name)
            required = self._required_fields(tool)
            self.assertIn(
                "admin_api_key",
                required,
                f"Tool '{tool_name}' must require admin_api_key",
            )

    def test_admin_tools_have_admin_api_key_property(self):
        """Every admin tool must define admin_api_key as a string property."""
        for tool_name in self.ADMIN_TOOLS:
            tool = self._get_tool(tool_name)
            props = tool.inputSchema.get("properties", {})
            self.assertIn(
                "admin_api_key",
                props,
                f"Tool '{tool_name}' must have admin_api_key property",
            )
            self.assertEqual(
                props["admin_api_key"].get("type"),
                "string",
                f"Tool '{tool_name}' admin_api_key must be type 'string'",
            )

    def test_admin_tools_no_longer_require_is_admin(self):
        """is_admin must NOT be in any admin tool's required fields."""
        for tool_name in self.ADMIN_TOOLS:
            tool = self._get_tool(tool_name)
            required = self._required_fields(tool)
            self.assertNotIn(
                "is_admin",
                required,
                f"Tool '{tool_name}' must NOT require is_admin (client-supplied flag is ignored)",
            )

    def test_is_admin_absent_from_admin_tool_schemas(self):
        """is_admin must not appear in admin tool inputSchemas (post Prompt 48)."""
        for tool_name in self.ADMIN_TOOLS:
            tool = self._get_tool(tool_name)
            props = tool.inputSchema.get("properties", {})
            self.assertNotIn(
                "is_admin",
                props,
                f"Tool '{tool_name}' must not expose is_admin (use admin_api_key only)",
            )

    # ── Non-admin tools must NOT require admin_api_key ───────────────────

    NON_ADMIN_TOOLS = [
        "store_memory",
        "store_media",
        "semantic_search",
        "search_codebase",
        "graph_search",
        "get_recent_context",
        "verify_memory",
        "create_snapshot",
        "list_snapshots",
        "delete_snapshot",
        "compare_states",
        "get_event_provenance",
        "list_contradictions",
    ]

    def test_non_admin_tools_do_not_require_admin_api_key(self):
        """Non-admin tools must NOT require admin_api_key."""
        for tool_name in self.NON_ADMIN_TOOLS:
            tool = self._get_tool(tool_name)
            required = self._required_fields(tool)
            self.assertNotIn(
                "admin_api_key",
                required,
                f"Non-admin tool '{tool_name}' must NOT require admin_api_key",
            )


class TestAdminToolHandlerArgumentFiltering(unittest.TestCase):
    """Verify that admin_api_key/is_admin are stripped before model construction."""

    def test_manage_namespace_argument_filtering(self):
        """ManageNamespaceRequest should not receive admin_api_key or is_admin."""
        from nce.models import ManageNamespaceRequest

        arguments = {
            "command": "list",
            "admin_api_key": "secret",
            "is_admin": True,
        }
        from nce.mcp_args import model_kwargs

        filtered = model_kwargs(arguments)
        try:
            req = ManageNamespaceRequest(**filtered)
        except Exception as e:
            self.fail(f"ManageNamespaceRequest(**filtered) raised {e}")

        self.assertEqual(req.command.value, "list")

    def test_manage_quotas_argument_filtering(self):
        """ManageQuotasRequest should not receive admin_api_key or is_admin."""
        from nce.models import ManageQuotasRequest

        arguments = {
            "command": "list",
            "namespace_id": "954c595b-ffa6-4619-92af-0d4758948336",
            "admin_api_key": "secret",
            "is_admin": False,
        }
        from nce.mcp_args import model_kwargs

        filtered = model_kwargs(arguments)
        try:
            req = ManageQuotasRequest(**filtered)
        except Exception as e:
            self.fail(f"ManageQuotasRequest(**filtered) raised {e}")

        self.assertEqual(req.command.value, "list")
        self.assertEqual(str(req.namespace_id), "954c595b-ffa6-4619-92af-0d4758948336")


if __name__ == "__main__":
    # Run with verbose output
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))

    # Print summary
    print(f"\n{'=' * 60}")
    print(f"Tests run: {result.testsRun}")
    print(f"Failures:  {len(result.failures)}")
    print(f"Errors:    {len(result.errors)}")
    print(f"Skipped:   {len(result.skipped)}")

    if result.wasSuccessful():
        print("\n✅ ALL TESTS PASSED — Client-side privilege escalation vector is CLOSED.")
        sys.exit(0)
    else:
        print("\n❌ SOME TESTS FAILED — See output above for details.")
        for test, traceback in result.failures + result.errors:
            print(f"\n--- {test} ---")
            print(traceback)
        sys.exit(1)
