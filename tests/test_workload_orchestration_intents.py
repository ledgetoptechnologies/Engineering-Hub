"""Tests for the source-only workload intent proposal."""

import unittest
from collections import UserDict
from dataclasses import FrozenInstanceError, asdict
import builtins
import importlib
from pathlib import Path
import socket
import sys
import urllib.request
from unittest.mock import patch

from engineering_hub.work_queue import WorkTarget
from hub_workload_orchestration import (
    ProviderQueueObservation,
    ViewerProcessingIntent,
    observe_provider_info,
)


class ViewerProcessingIntentTests(unittest.TestCase):
    def setUp(self):
        self.code_target = WorkTarget(
            task_id="00000000-0000-4000-8000-000000000001",
            source_commit="a" * 40,
            artifact_digest="sha256:" + "b" * 64,
        )

    def make_intent(self, **changes):
        values = {
            "code_target": self.code_target,
            "submission_id": "00000000-0000-4000-8000-000000000002",
            "viewer_project_id": "viewer:sample-1",
            "dataset_id": "dataset.sample_1",
            "provider_id": "node-odx:local-1",
            "input_manifest_digest": "sha256:" + "c" * 64,
            "options_digest": "sha256:" + "d" * 64,
        }
        values.update(changes)
        return ViewerProcessingIntent(**values)

    def test_intent_keeps_existing_code_target_and_distinct_data_digests(self):
        intent = self.make_intent()

        self.assertIs(intent.code_target, self.code_target)
        self.assertEqual(intent.project, "3d-viewer")
        self.assertEqual(intent.input_manifest_digest, "sha256:" + "c" * 64)
        self.assertEqual(intent.options_digest, "sha256:" + "d" * 64)
        self.assertEqual(intent.code_target.artifact_digest, "sha256:" + "b" * 64)
        with self.assertRaises((AttributeError, TypeError)):
            intent.dataset_id = "changed"

    def test_opaque_ids_are_preserved_without_normalization(self):
        value = "Viewer.Project:Case_9"

        intent = self.make_intent(viewer_project_id=value)

        self.assertEqual(intent.viewer_project_id, value)

    def test_intent_rejects_noncanonical_or_unsafe_fields(self):
        invalid_values = (
            ("code_target", True),
            ("code_target", {"task_id": self.code_target.task_id}),
            ("submission_id", "00000000-0000-4ABC-8DEF-000000000002"),
            ("submission_id", True),
            ("viewer_project_id", ""),
            ("viewer_project_id", "viewer/../other"),
            ("viewer_project_id", "viewér"),
            ("dataset_id", "bad\nkey"),
            ("provider_id", "http://provider.invalid"),
            ("input_manifest_digest", "sha256:" + "C" * 64),
            ("options_digest", "d" * 64),
            ("project", "viewer"),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.make_intent(**{field: value})

    def test_constructor_rejects_unlisted_fields(self):
        with self.assertRaises(TypeError):
            self.make_intent(untrusted_payload={"data": "must not be admitted"})


class ProviderObservationTests(unittest.TestCase):
    def test_observation_states_distinguish_direct_and_managed_queues(self):
        busy = observe_provider_info({"taskQueueCount": 2}, provider_kind="nodeodx")
        direct_zero = observe_provider_info({"taskQueueCount": 0}, provider_kind="nodeodx")
        managed_zero = observe_provider_info({"taskQueueCount": 0}, provider_kind="clusterodm")

        self.assertIs(type(busy), ProviderQueueObservation)
        self.assertEqual((busy.kind, busy.queue_count, busy.state),
                         ("nodeodx", 2, "queued_work_observed"))
        self.assertEqual((direct_zero.queue_count, direct_zero.state),
                         (0, "no_queued_work_observed"))
        self.assertEqual((managed_zero.queue_count, managed_zero.state),
                         (0, "unknown"))
        self.assertFalse(busy.admission_allowed)
        self.assertFalse(direct_zero.admission_allowed)
        self.assertFalse(managed_zero.admission_allowed)

    def test_invalid_counts_are_unknown_and_raw_metadata_is_not_returned(self):
        invalid_infos = (
            {},
            {"taskQueueCount": None},
            {"taskQueueCount": True},
            {"taskQueueCount": False},
            {"taskQueueCount": -1},
            {"taskQueueCount": "7"},
            {"taskQueueCount": 1.5},
        )
        for provider_kind in ("nodeodx", "clusterodm"):
            for info in invalid_infos:
                with self.subTest(provider_kind=provider_kind, info=info):
                    observation = observe_provider_info(info, provider_kind=provider_kind)
                    self.assertIsNone(observation.queue_count)
                    self.assertEqual(observation.state, "unknown")
                    self.assertFalse(observation.admission_allowed)

        secret = "DO_NOT_ECHO_SECRET"
        observation = observe_provider_info(
            {"taskQueueCount": 1, "datasetBody": secret, "token": secret,
             "providerResponse": secret, "machineId": "machine-private"},
            provider_kind="nodeodx",
        )
        self.assertEqual(set(asdict(observation)), {
            "kind", "queue_count", "state", "admission_allowed"
        })
        self.assertNotIn(secret, repr(observation))
        self.assertNotIn("machine-private", repr(observation))

    def test_provider_kind_and_info_types_are_closed(self):
        class DictSubclass(dict):
            pass

        with self.assertRaises(TypeError):
            observe_provider_info(UserDict({"taskQueueCount": 0}), provider_kind="nodeodx")
        with self.assertRaises(TypeError):
            observe_provider_info(DictSubclass(taskQueueCount=0), provider_kind="nodeodx")
        for provider_kind in ("other", "NodeODX", True, None):
            with self.subTest(provider_kind=provider_kind):
                with self.assertRaises(ValueError):
                    observe_provider_info({}, provider_kind=provider_kind)

    def test_admission_is_fixed_false_and_observation_is_immutable(self):
        observation = observe_provider_info({"taskQueueCount": 0}, provider_kind="nodeodx")

        with self.assertRaises(TypeError):
            ProviderQueueObservation("nodeodx", 0, "no_queued_work_observed",
                                     admission_allowed=True)
        with self.assertRaises(ValueError):
            ProviderQueueObservation("nodeodx", 0, "machine_idle")
        with self.assertRaises(FrozenInstanceError):
            observation.admission_allowed = True

    def test_import_and_construction_do_not_write_or_open_network(self):
        forbidden = AssertionError("unexpected external effect")
        sys.modules.pop("hub_workload_orchestration", None)
        sys.modules.pop("hub_workload_orchestration.intents", None)
        with (
            patch("builtins.open", side_effect=forbidden),
            patch("socket.socket", side_effect=forbidden),
            patch("socket.create_connection", side_effect=forbidden),
            patch("urllib.request.urlopen", side_effect=forbidden),
            patch.object(Path, "open", side_effect=forbidden),
            patch.object(Path, "mkdir", side_effect=forbidden),
            patch.object(Path, "write_text", side_effect=forbidden),
            patch.object(Path, "write_bytes", side_effect=forbidden),
            patch.object(Path, "unlink", side_effect=forbidden),
        ):
            fresh_module = importlib.import_module("hub_workload_orchestration")
            code_target = WorkTarget(
                task_id="00000000-0000-4000-8000-000000000003",
                source_commit="e" * 40,
                artifact_digest="sha256:" + "f" * 64,
            )
            intent_type = fresh_module.ViewerProcessingIntent
            intent_type(
                code_target=code_target,
                submission_id="00000000-0000-4000-8000-000000000004",
                viewer_project_id="viewer:offline",
                dataset_id="dataset.offline",
                provider_id="nodeodx:offline",
                input_manifest_digest="sha256:" + "1" * 64,
                options_digest="sha256:" + "2" * 64,
            )
            fresh_module.observe_provider_info({"taskQueueCount": 0}, provider_kind="nodeodx")


if __name__ == "__main__":
    unittest.main()
