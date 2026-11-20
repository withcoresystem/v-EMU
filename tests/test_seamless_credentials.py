from __future__ import annotations

import unittest
from unittest import mock

from vemu.errors import VemuError
from vemu.seamless import credentials


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str) -> None:
        self.values[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        key = (service, account)
        if key not in self.values:
            raise KeyError(account)
        del self.values[key]


class CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = FakeKeyring()
        patcher = mock.patch.object(
            credentials, "_load_keyring", return_value=self.keyring
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_account_is_domain_scoped(self) -> None:
        self.assertEqual(
            credentials.account("W11", "ProUser"), "W11|ProUser"
        )

    def test_save_get_delete(self) -> None:
        self.assertTrue(
            credentials.save_password("W11", "ProUser", "secret")
        )
        self.assertEqual(
            credentials.get_password("W11", "ProUser"), "secret"
        )
        self.assertIsNone(
            credentials.get_password("Other", "ProUser")
        )
        self.assertTrue(
            credentials.delete_password("W11", "ProUser")
        )
        self.assertIsNone(
            credentials.get_password("W11", "ProUser")
        )

    def test_empty_password_rejected(self) -> None:
        with self.assertRaises(VemuError):
            credentials.save_password("W11", "ProUser", "")

    def test_delete_missing_is_false(self) -> None:
        self.assertFalse(
            credentials.delete_password("W11", "ProUser")
        )

    def test_delete_falls_through_chainer_backends(self) -> None:
        empty = FakeKeyring()
        stored = FakeKeyring()
        stored.set_password(
            credentials.SERVICE,
            credentials.account("W11", "ProUser"),
            "secret",
        )

        class FakeChainer(FakeKeyring):
            def __init__(self) -> None:
                super().__init__()
                self.backends = [empty, stored]

        self.keyring = FakeChainer()
        with mock.patch.object(
            credentials, "_load_keyring", return_value=self.keyring
        ):
            self.assertTrue(
                credentials.delete_password("W11", "ProUser")
            )
            self.assertIsNone(
                stored.get_password(
                    credentials.SERVICE,
                    credentials.account("W11", "ProUser"),
                )
            )

    def test_unavailable_keyring(self) -> None:
        with mock.patch.object(
            credentials,
            "_load_keyring",
            side_effect=VemuError("missing"),
        ):
            self.assertFalse(credentials.available())
            with self.assertRaises(VemuError):
                credentials.get_password("W11", "ProUser")
            self.assertFalse(
                credentials.delete_password("W11", "ProUser")
            )

    def test_backend_failure_is_wrapped(self) -> None:
        def fail(*_args, **_kwargs):
            raise RuntimeError("locked")

        self.keyring.set_password = fail
        with self.assertRaises(VemuError) as caught:
            credentials.save_password("W11", "ProUser", "secret")
        self.assertIn("keyring", str(caught.exception))
