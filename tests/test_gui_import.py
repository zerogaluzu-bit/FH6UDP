"""Smoke test: GUI module imports and builds without starting mainloop."""

from __future__ import annotations

import unittest


class GuiImportTests(unittest.TestCase):
    def test_gui_module_imports(self) -> None:
        import udp_listener_gui

        self.assertTrue(hasattr(udp_listener_gui, "ListenerApp"))
        self.assertTrue(hasattr(udp_listener_gui, "main"))


if __name__ == "__main__":
    unittest.main()
