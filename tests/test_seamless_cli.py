from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from vemu import cli


class SeamlessCliTests(unittest.TestCase):
    def test_paused_supervisor_notification_for_shortcut_launch(self) -> None:
        with mock.patch.object(
            cli.shutil, "which", return_value="/usr/bin/notify-send"
        ), mock.patch.object(cli.subprocess, "run") as run, mock.patch.object(
            cli.sys.stdout, "isatty", return_value=False
        ):
            cli._notify_paused_supervisors(["vm-other"])
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertIn("vm-other", command[-1])

    def test_paused_supervisor_notification_skipped_in_terminal(self) -> None:
        with mock.patch.object(
            cli.shutil, "which", return_value="/usr/bin/notify-send"
        ), mock.patch.object(cli.subprocess, "run") as run, mock.patch.object(
            cli.sys.stdout, "isatty", return_value=True
        ):
            cli._notify_paused_supervisors(["vm-other"])
        run.assert_not_called()

    def test_paused_supervisor_notification_noop_without_list(self) -> None:
        with mock.patch.object(cli.subprocess, "run") as run:
            cli._notify_paused_supervisors([])
        run.assert_not_called()

    def test_daemon_rollback_forgets_password_in_client_keyring(self) -> None:
        args = SimpleNamespace(
            domain="vm",
            host_only=False,
            force=False,
            forget_password=True,
            json=True,
            uri="qemu:///system",
            socket=None,
            direct=False,
        )

        def call(_args, method, _params):
            if method == "vm.seamless_status":
                return {"state": {"username": "ProUser"}}
            return {"reverted": True, "layer": "0001"}

        with mock.patch.object(
            cli, "_daemon_enabled", return_value=True
        ), mock.patch.object(
            cli, "_daemon_call", side_effect=call
        ), mock.patch.object(
            cli, "delete_rdp_password", return_value=True
        ) as forget:
            self.assertEqual(cli._cmd_seamless_rollback(args), 0)
        forget.assert_called_once_with("vm", "ProUser")

    def test_daemon_delete_with_storage_forgets_password_in_client_keyring(self) -> None:
        args = SimpleNamespace(
            domain="vm",
            delete_storage=True,
            force=False,
            json=True,
            uri="qemu:///system",
            socket=None,
            direct=False,
        )

        def call(_args, method, _params):
            if method == "vm.seamless_status":
                return {"state": {"username": "ProUser"}}
            return {"domain": "vm", "deleted": True}

        with mock.patch.object(
            cli, "_daemon_enabled", return_value=True
        ), mock.patch.object(
            cli, "_daemon_call", side_effect=call
        ), mock.patch.object(
            cli, "delete_rdp_password", return_value=True
        ) as forget:
            self.assertEqual(cli._cmd_delete_vm(args), 0)
        forget.assert_called_once_with("vm", "ProUser")


if __name__ == "__main__":
    unittest.main()
