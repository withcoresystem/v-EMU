from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.iso_assets import loader


class FirstbootAutoTests(unittest.TestCase):
    def _tui(self) -> loader.Tui:
        tui = loader.Tui.__new__(loader.Tui)
        tui.script_name = ""
        tui.script_url = ""
        tui.sources = ["usb", "url"]
        tui.retries = 2
        tui.payload = None
        tui.attempts = 0
        return tui

    def test_url_base_and_name_builds_the_payload_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            entry = payload / "site-setup.sh"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            tui = self._tui()
            tui.script_name = "site-setup.sh"
            tui.script_url = "https://deploy.example/firstboot/"
            calls: list[str] = []

            def fetch(url: str, *, interactive: bool = True):
                calls.append(url)
                return payload

            tui.fetch_url = fetch  # type: ignore[method-assign]
            tui.find_entry = lambda value, preferred=None: (entry, 0)
            tui.execute_automatic = lambda value, timeout: True
            with mock.patch.object(loader, "write_log"):
                self.assertTrue(tui.run_automatic())
            self.assertEqual(
                calls, ["https://deploy.example/firstboot/site-setup.sh"]
            )

    def test_url_failure_falls_back_to_named_usb_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            entry = payload / "site-setup.sh"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            tui = self._tui()
            tui.script_name = "site-setup.sh"
            tui.script_url = "https://deploy.example/firstboot/"
            tui.fetch_url = lambda url, interactive=True: None
            tui.auto_usb = lambda name: payload
            tui.find_entry = lambda value, preferred=None: (entry, 0)
            calls: list[str] = []

            def execute(value, timeout):
                calls.append(value.name)
                return True

            tui.execute_automatic = execute
            with mock.patch.object(loader, "write_log"):
                self.assertTrue(tui.run_automatic())
            self.assertEqual(calls, ["site-setup.sh"])

    def test_payload_failure_returns_to_manual_without_usb_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            entry = payload / "run.sh"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            tui = self._tui()
            tui.script_url = "https://deploy.example/firstboot/site-setup.sh"
            tui.fetch_url = lambda url, interactive=True: payload
            tui.find_entry = lambda value, preferred=None: (entry, 0)
            tui.execute_automatic = lambda value, timeout: False
            tui.auto_usb = mock.Mock(side_effect=AssertionError("no USB retry"))
            with mock.patch.object(loader, "write_log"):
                self.assertFalse(tui.run_automatic())
            tui.auto_usb.assert_not_called()

    def test_name_only_uses_usb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            entry = payload / "site-setup.sh"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            tui = self._tui()
            tui.script_name = "site-setup.sh"
            tui.auto_usb = lambda name: payload
            tui.find_entry = lambda value, preferred=None: (entry, 0)
            tui.execute_automatic = lambda value, timeout: True
            with mock.patch.object(loader, "write_log"):
                self.assertTrue(tui.run_automatic())

    def test_find_entry_prefers_the_configured_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            (payload / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (payload / "site.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            tui = self._tui()
            found = tui.find_entry(payload, preferred="site.sh")
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found[0].name, "site.sh")


if __name__ == "__main__":
    unittest.main()
