from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vemu.image_metadata import (
    ImageMetadataStore,
    SolverLookup,
    lookup_image_type,
    releasever_for,
    repos_from_manifest,
    search_patterns,
    target_key,
)
from vemu.errors import ValidationError


def manifest_payload() -> str:
    return json.dumps(
        {
            "version": "2",
            "pipelines": [],
            "sources": {
                "org.osbuild.librepo": {
                    "options": {
                        "mirrors": {
                            "one": {
                                "url": "https://example.test/fedora/",
                                "type": "baseurl",
                            },
                            "two": {
                                "url": "https://example.test/metalink",
                                "type": "metalink",
                            },
                        }
                    },
                    "items": {},
                }
            },
        }
    )


class CacheLayoutTests(unittest.TestCase):
    def test_target_key_is_stable_and_repo_sensitive(self) -> None:
        first = target_key("fedora-45", "x86_64", ["https://a", "https://b"])
        second = target_key("fedora-45", "x86_64", ["https://b", "https://a"])
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, target_key("fedora-45", "x86_64", ["https://c"])
        )

    def test_comma_separated_search_patterns(self) -> None:
        self.assertEqual(
            search_patterns(" inkscape, gimp ,, *kde* "),
            ["*inkscape*", "*gimp*", "*kde*"],
        )

    def test_release_and_image_type(self) -> None:
        self.assertEqual(releasever_for("fedora-45"), "45")
        self.assertEqual(releasever_for("almalinux-10.2"), "10.2")
        self.assertEqual(lookup_image_type("fedora-45"), "generic-qcow2")
        self.assertEqual(lookup_image_type("almalinux-10.2"), "qcow2")
        with self.assertRaises(ValidationError):
            lookup_image_type("debian-13")


class MetadataStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ImageMetadataStore(Path(self.tmp.name) / "meta.db")

    def test_catalog_round_trip_and_ttl(self) -> None:
        entries = [
            {
                "distro": "fedora-45",
                "arch": "x86_64",
                "image_type": "generic-qcow2",
            }
        ]
        with mock.patch("vemu.image_metadata.time.time", return_value=1000.0):
            self.store.catalog_put("host", "89", entries)
        with mock.patch("vemu.image_metadata.time.time", return_value=1010.0):
            self.assertEqual(
                self.store.catalog_get("host", "89", ttl=100), entries
            )
        with mock.patch(
            "vemu.image_metadata.time.time", return_value=2000.0
        ):
            self.assertIsNone(self.store.catalog_get("host", "89", ttl=100))
            self.assertEqual(
                self.store.catalog_get_stale("host", "89"), entries
            )

    def test_cache_round_trip_and_clear(self) -> None:
        with mock.patch("vemu.image_metadata.time.time", return_value=100.0):
            self.store.cache_put("packages", "target", "q", [{"name": "x"}])
        payload = self.store.cache_get("packages", "target", "q")
        self.assertEqual(payload[0], [{"name": "x"}])
        self.store.clear_target("target")
        self.assertIsNone(self.store.cache_get("packages", "target", "q"))


class ManifestRepoTests(unittest.TestCase):
    def test_baseurl_and_metalink_repositories(self) -> None:
        result = mock.Mock(
            returncode=0, stdout=manifest_payload(), stderr=""
        )
        with mock.patch(
            "vemu.image_metadata.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ):
            repos = repos_from_manifest("fedora-45", "x86_64")
        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]["baseurl"], ["https://example.test/fedora/"])
        self.assertEqual(repos[1]["metalink"], "https://example.test/metalink")

    def test_extra_repository_is_added(self) -> None:
        result = mock.Mock(
            returncode=0, stdout=manifest_payload(), stderr=""
        )
        with mock.patch(
            "vemu.image_metadata.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ):
            repos = repos_from_manifest(
                "fedora-45",
                "x86_64",
                extra_repos=["https://example.test/extra/"],
            )
        self.assertEqual(repos[-1]["baseurl"], ["https://example.test/extra/"])

    def test_manifest_failure_is_reported(self) -> None:
        result = mock.Mock(returncode=1, stdout="", stderr="no repo")
        with mock.patch(
            "vemu.image_metadata.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ):
            with self.assertRaisesRegex(ValidationError, "no repo"):
                repos_from_manifest("fedora-45", "x86_64")


class SolverLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ImageMetadataStore(Path(self.tmp.name) / "meta.db")
        self.lookup = SolverLookup(
            "fedora-45",
            "x86_64",
            [{"id": "fedora", "baseurl": ["https://example.test/"]}],
            store=self.store,
            cache_root_dir=Path(self.tmp.name) / "solver",
            solver="/usr/bin/osbuild-depsolve-dnf",
        )

    def test_search_normalizes_and_caches(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "name": "inkscape",
                        "epoch": 0,
                        "version": "1.4.4",
                        "release": "5.fc45",
                        "arch": "x86_64",
                        "repo_id": "fedora",
                        "summary": "Vector editor",
                    }
                ]
            ),
            stderr="",
        )
        with mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ) as run:
            first = self.lookup.search_packages("ink")
            second = self.lookup.search_packages("ink")
        self.assertEqual(first, second)
        self.assertEqual(first[0]["version"], "1.4.4-5.fc45")
        self.assertEqual(run.call_count, 1)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["command"], "search")
        self.assertEqual(
            request["arguments"]["search"]["packages"], ["*ink*"]
        )

    def test_multiple_terms_are_combined_and_deduplicated(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "name": "inkscape",
                        "version": "1.4.4",
                        "release": "5.fc45",
                        "arch": "x86_64",
                        "repo_id": "fedora",
                    },
                    {
                        "name": "inkscape",
                        "version": "1.4.4",
                        "release": "5.fc45",
                        "arch": "x86_64",
                        "repo_id": "fedora",
                    },
                    {
                        "name": "gimp",
                        "version": "3.0.0",
                        "release": "1.fc45",
                        "arch": "x86_64",
                        "repo_id": "fedora",
                    },
                ]
            ),
            stderr="",
        )
        with mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ) as run:
            rows = self.lookup.search_packages("ink, gimp")
        self.assertEqual([row["name"] for row in rows], ["gimp", "inkscape"])
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(
            request["arguments"]["search"]["packages"],
            ["*ink*", "*gimp*"],
        )

    def test_group_list_parses_and_caches(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=(
                "ID Name Installed\n"
                "kde-desktop KDE no\n"
                "office Office/Productivity no\n"
            ),
            stderr="",
        )
        with mock.patch(
            "vemu.image_metadata.shutil.which",
            return_value="/usr/bin/dnf",
        ), mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ) as run:
            groups = self.lookup.group_list()
            again = self.lookup.group_list()
        self.assertEqual(groups, again)
        self.assertEqual(groups[0]["id"], "kde-desktop")
        self.assertEqual(groups[0]["name"], "KDE")
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertNotIn("--hidden", command)

    def test_hidden_groups_use_a_separate_command_and_cache(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=(
                "ID Name Installed\n"
                "kde-desktop KDE no\n"
                "core Core yes\n"
            ),
            stderr="",
        )
        with mock.patch(
            "vemu.image_metadata.shutil.which",
            return_value="/usr/bin/dnf",
        ), mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ) as run:
            visible = self.lookup.group_list()
            hidden = self.lookup.group_list(include_hidden=True)
            again = self.lookup.group_list(include_hidden=True)
        self.assertEqual(hidden, again)
        self.assertEqual(len(hidden), 2)
        self.assertEqual(len(visible), 2)
        self.assertEqual(run.call_count, 2)
        self.assertIn("--hidden", run.call_args.args[0])

    def test_group_validation_returns_package_count(self) -> None:
        result = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {"packages": [{"name": "a"}, {"name": "b"}]}
            ),
            stderr="",
        )
        with mock.patch(
            "vemu.image_metadata.subprocess.run", return_value=result
        ) as run:
            first = self.lookup.validate_group("kde-desktop")
            second = self.lookup.validate_group("kde-desktop")
        self.assertEqual(first, second)
        self.assertEqual(first["count"], 2)
        self.assertEqual(run.call_count, 1)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(
            request["arguments"]["transactions"][0]["package-specs"],
            ["@kde-desktop"],
        )


class AdapterCatalogCacheTests(unittest.TestCase):
    def test_cached_catalog_skips_image_builder(self) -> None:
        from vemu.imagebuilder import RealImageBuilderAdapter

        with tempfile.TemporaryDirectory() as tmp:
            store = ImageMetadataStore(Path(tmp) / "meta.db")
            store.catalog_put(
                "host",
                "89.0.0",
                [
                    {
                        "distro": "fedora-45",
                        "arch": "x86_64",
                        "image_type": "generic-qcow2",
                    }
                ],
            )
            adapter = RealImageBuilderAdapter(metadata=store)
            with mock.patch(
                "vemu.imagebuilder.shutil.which",
                return_value="/usr/bin/image-builder",
            ), mock.patch.object(
                RealImageBuilderAdapter,
                "_version",
                return_value="89.0.0",
            ), mock.patch(
                "vemu.imagebuilder.host_cache_id", return_value="host"
            ), mock.patch(
                "vemu.imagebuilder.subprocess.run"
            ) as run:
                entries = adapter.catalog()
        self.assertEqual(entries[0]["image_type"], "generic-qcow2")
        run.assert_not_called()


class CatalogRelevanceTests(unittest.TestCase):
    def test_relevant_image_types(self) -> None:
        from vemu.ui.osbuild_catalog import is_vemu_image_type

        self.assertTrue(is_vemu_image_type("generic-qcow2"))
        self.assertTrue(is_vemu_image_type("qcow2"))
        self.assertTrue(is_vemu_image_type("minimal-installer"))
        self.assertTrue(is_vemu_image_type("server-qcow2"))
        self.assertFalse(is_vemu_image_type("ami"))
        self.assertFalse(is_vemu_image_type("edge-raw-image"))
        self.assertFalse(is_vemu_image_type("oci"))


if __name__ == "__main__":
    unittest.main()
