"""Verify the staging runner fails closed without touching Docker or GitHub."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("staging_deploy", Path(__file__).with_name("sdlc-staging-deploy.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class StagingRunnerTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        config = root / "staging.yml"
        config.touch()
        self.digest = "sha256:" + "a" * 64
        self.commit = "b" * 40
        self.image = "ghcr.io/example/devhub/portal@" + self.digest
        stack.enter_context(patch.dict(os.environ, {
            "DEVHUB_GATE_URL": "https://gate.example.test", "DEVHUB_STAGING_COMPOSE_FILE": str(config),
            "DEVHUB_STAGING_RELEASE": "1", "DEVHUB_STAGING_DIGEST": self.digest, "DEVHUB_STAGING_ROLLBACK": "false",
            "GITHUB_REPOSITORY": "example/devhub", "GITHUB_ACTOR": "runner", "GHCR_TOKEN": "fixture",
        }))
        self.commands = []
        stack.enter_context(patch.object(runner, "github_token", return_value="test"))
        self.gate = stack.enter_context(patch.object(runner, "request_json"))
        stack.enter_context(patch.object(runner.subprocess, "check_output", side_effect=self.output))
        stack.enter_context(patch.object(runner.subprocess, "run", side_effect=lambda command, **_: self.commands.append(command)))

    def output(self, command, **_):
        if "config" in command:
            return json.dumps({"services": {"portal": {"image": self.image, "healthcheck": {"test": ["CMD", "healthcheck"]}}}})
        if "{{json .RepoDigests}}" in command:
            return json.dumps([self.image])
        if "{{json .Config.Labels}}" in command:
            return json.dumps({"org.opencontainers.image.revision": self.commit})
        if "ps" in command:
            return "test-container"
        return self.image

    def test_gate_outage_stops_before_deployment(self):
        self.gate.side_effect = RuntimeError("gate unavailable")
        with self.assertRaisesRegex(RuntimeError, "gate unavailable"):
            runner.main()
        self.assertFalse(any("up" in command for command in self.commands))

    def test_wrong_source_label_stops_before_consume(self):
        self.gate.return_value = {"image": self.image, "commit": "c" * 40, "permit": "test"}
        with self.assertRaisesRegex(ValueError, "revision label"):
            runner.main()
        self.assertEqual(self.gate.call_count, 1)
        self.assertFalse(any("up" in command for command in self.commands))

    def test_deploy_consumes_permit_and_reports_outcome(self):
        self.gate.side_effect = [{"image": self.image, "commit": self.commit, "permit": "test"},
                                 {"image": self.image, "commit": self.commit}, {"outcome": "succeeded"}]
        runner.main()
        self.assertEqual([call.args[0].rsplit("/", 1)[-1] for call in self.gate.call_args_list],
                         ["permit", "consume", "outcome"])
        self.assertTrue(any("up" in command for command in self.commands))


if __name__ == "__main__":
    unittest.main()
