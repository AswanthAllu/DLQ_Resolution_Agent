"""tests/test_shared_platform.py — Verify tool registry."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from unittest.mock import patch


class TestSharedPlatform(unittest.TestCase):

    def test_register_and_list(self):
        import shared_platform
        shared_platform._registry.clear()

        def my_tool(x: int) -> int:
            """A test tool."""
            return x * 2

        shared_platform.register_tool("my_tool", my_tool)
        tools = shared_platform.list_tools()
        names = [t["name"] for t in tools]
        self.assertIn("my_tool", names)

    def test_invoke_known_tool(self):
        import shared_platform
        shared_platform._registry.clear()

        def double(x: int) -> int:
            return x * 2

        shared_platform.register_tool("double", double)
        result = shared_platform.invoke_tool("double", {"x": 5})
        self.assertEqual(result, 10)

    def test_invoke_unknown_tool(self):
        import shared_platform
        result = shared_platform.invoke_tool("nonexistent", {})
        self.assertFalse(result.get("success"))
        self.assertIn("not registered", result.get("error", ""))

    def test_no_remote_call_when_disabled(self):
        with patch("config_loader.platform", return_value={"enabled": False}):
            import shared_platform
            # Should not raise even though no baseUrl is set
            shared_platform.register_tool("safe_tool", lambda: None)


if __name__ == "__main__":
    unittest.main()
