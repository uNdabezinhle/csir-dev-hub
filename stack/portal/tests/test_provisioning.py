import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient


_sandbox = tempfile.TemporaryDirectory()
_config = Path(_sandbox.name) / "config"
_portal = Path(__file__).resolve().parents[1]
shutil.copytree(_portal / "config", _config)
if not (_config / "roles.json").exists():
    shutil.copyfile(_portal.parents[1] / "identity" / "roles.json", _config / "roles.json")
os.environ["PORTAL_CONFIG_DIR"] = str(_config)
os.environ["PORTAL_DATA_DIR"] = str(Path(_sandbox.name) / "data")

from app import blueprints, browser_security, coder, db, execution_records, main, provisioning, reclaim, sdlc_gate, sdlc_identity, sdlc_store
from app.engine_broker import app as broker
from app.extensions.licence import LicenseBroker
from app.extensions.scanner import ImageScanner


def tearDownModule():
    db._conn.close()
    _sandbox.cleanup()


def pilot_receipt(commit="a" * 40):
    totals = {"vcpu_hours": "1.00", "memory_gib_hours": "2.00"}
    summary = {"dataset_version": "workspace-usage-v1", "totals": totals,
               "projects": [{"project_id": "demo-alpha", **totals}]}
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    return {"schema_version": 1, "source_commit": commit, "inputs": {
        "workspace-usage-v1.csv": "1" * 64, "expected-summary.json": "2" * 64, "summarize_usage.py": "3" * 64,
    }, "summary_sha256": hashlib.sha256(encoded).hexdigest(), "summary": summary}


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        db.init()
        self.licence = LicenseBroker()
        self.licence.init()
        with db.tx() as conn:
            for table in ("licence_seats", "workspace_requests", "workspaces", "user_quotas", "projects"):
                conn.execute(f"DELETE FROM {table}")
            conn.executemany("INSERT INTO user_quotas VALUES (?,1,2,2,'')", [("alice",), ("bob",)])
        self.user = {"username": "alice", "roles": ["engineer"]}

    def reserve(self, name, user=None, project=None, request_id=None):
        return provisioning.reserve(user or self.user, name, "ubuntu-server", "dev", 2, 2, project, request_id)

    def test_concurrent_requests_reserve_user_quota_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(self.reserve, ["first", "second"]))
        self.assertEqual(sum(not result for result in outcomes), 1)
        self.assertEqual(len(provisioning.pending()), 1)

    def test_pending_request_counts_against_project_quota(self):
        project = {"id": 7, "name": "Project", "max_workspaces": 1, "max_cpu": 0, "max_mem_gb": 0}
        self.assertEqual(self.reserve("first", project=project), [])
        self.assertIn("project", self.reserve("second", {"username": "bob"}, project)[0])

    def attach_seat(self, name):
        with db.tx() as conn:
            seat = conn.execute(
                "INSERT INTO licence_seats(pool,kind,checked_out_at) VALUES ('test','workspace',?)",
                (time.time(),),
            ).lastrowid
            conn.execute("UPDATE workspace_requests SET seat_id=? WHERE owner='alice' AND name=?", (seat, name))
        return seat

    def test_failed_submission_releases_quota_and_seat(self):
        self.assertEqual(self.reserve("failed"), [])
        seat = self.attach_seat("failed")
        provisioning.cancel("alice", "failed")
        self.assertIsNotNone(db.one("SELECT returned_at FROM licence_seats WHERE id=?", (seat,))["returned_at"])
        self.assertEqual(self.reserve("retry"), [])

    def test_reconciliation_restores_project_and_seat_after_lost_response(self):
        project = {"id": 7, "name": "Project", "max_workspaces": 1, "max_cpu": 0, "max_mem_gb": 0}
        self.assertEqual(provisioning.reserve(self.user, "recovered", "ubuntu-server", "dev", 2, 2,
                                              project, "original", {"blueprint_id": 42}), [])
        seat = self.attach_seat("recovered")
        provisioning.submitted("alice", "recovered", "original")
        self.licence.reconcile({})
        self.assertIsNone(db.one("SELECT returned_at FROM licence_seats WHERE id=?", (seat,))["returned_at"])
        live = [{"id": "ws-1", "name": "recovered", "owner_name": "alice", "template_name": "ubuntu-server",
                 "latest_build": {"id": "build-1"}}]
        with db._lock:
            db._conn.close()
            db._conn = db.connect()
        provisioning.recover(provisioning.pending(), live, time.time(), lambda _: {"devhub_request_id": "different"})
        self.assertEqual(len(provisioning.pending()), 1)
        provisioning.recover(provisioning.pending(), live, time.time(), lambda _: {"devhub_request_id": "original"})
        provisioning.recover(provisioning.pending(), live, time.time(), lambda _: {})
        self.assertEqual(provisioning.pending(), [])
        self.assertEqual(db.one("SELECT project_id FROM workspaces WHERE id='ws-1'")["project_id"], 7)
        self.assertEqual(db.one("SELECT workspace_id FROM licence_seats WHERE id=?", (seat,))["workspace_id"], "ws-1")
        self.assertEqual(db.one("SELECT workspace_id FROM provisioning_evidence WHERE request_id='original'")["workspace_id"], "ws-1")

    def test_submitted_request_survives_grace_and_stale_unsubmitted_snapshot(self):
        self.assertEqual(self.reserve("missing"), [])
        pending = provisioning.pending()
        provisioning.submitted("alice", "missing")
        observed_at = pending[0]["created_at"] + provisioning.RECOVERY_GRACE_SECONDS + 1
        provisioning.recover(pending, [], observed_at, lambda _: {})
        provisioning.recover(provisioning.pending(), [], observed_at, lambda _: {})
        self.assertEqual(len(provisioning.pending()), 1)

    def test_expired_unsubmitted_request_cannot_submit_replacement(self):
        self.assertEqual(self.reserve("expired", request_id="old"), [])
        pending = provisioning.pending()
        observed_at = pending[0]["created_at"] + provisioning.RECOVERY_GRACE_SECONDS + 1
        provisioning.recover(pending, [], observed_at, lambda _: {})
        self.assertEqual(self.reserve("expired", request_id="new"), [])
        with self.assertRaises(RuntimeError):
            provisioning.submitted("alice", "expired", "old")
        provisioning.cancel("alice", "expired", request_id="old")
        self.assertEqual(provisioning.pending()[0]["request_id"], "new")

    def test_failed_delete_retains_quota_and_repairs_prior_deletion_flag(self):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO workspaces(id,owner,name,cpu,mem_gb,deleted_at) VALUES ('ws','alice','example',2,2,1)"
            )
        workspace = {"id": "ws", "name": "example", "owner_name": "alice",
                     "latest_build": {"status": "failed", "transition": "delete"}}
        with patch.object(coder, "workspaces", return_value=[workspace]):
            main.reconcile()
        self.assertIsNone(db.one("SELECT deleted_at FROM workspaces WHERE id='ws'")["deleted_at"])
        self.assertTrue(self.reserve("replacement"))
        with patch.object(coder, "workspaces", return_value=[]):
            main.reconcile()
        self.assertEqual(self.reserve("replacement"), [])

    def test_route_releases_quota_for_rejection_but_keeps_it_for_unknown_outcome(self):
        user = {**self.user, "email": "alice@example.test", "full_name": "Alice", "auth_source": "oidc"}
        item = {"template": "ubuntu-server"}
        with (
            patch.object(main, "need", return_value=user),
            patch.object(main, "catalog_items", return_value=[item]),
            patch.object(main, "allowed_profiles", return_value=["dev"]),
            patch.object(main, "flash"),
            patch.object(main, "ip", return_value=""),
            patch.object(coder, "ensure_user"),
            patch.object(coder, "template_params", return_value=[]),
            patch.object(coder, "workspaces", return_value=[]),
            patch.object(coder, "create_workspace") as create,
        ):
            create.side_effect = coder.CoderError("rejected", 422)
            response = main.create_ws(None, "ubuntu-server", "example", 2, 2, "dev", "", "24.04", "full")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(provisioning.pending(), [])
            create.side_effect = coder.CoderError("upstream error", 502)
            response = main.create_ws(None, "ubuntu-server", "example", 2, 2, "dev", "", "24.04", "full")
            self.assertEqual(response.status_code, 303)
            self.assertGreater(provisioning.pending()[0]["submitted"], 0)


class BrokerTests(unittest.TestCase):
    def test_only_supported_operations_are_exposed(self):
        with TestClient(broker) as client:
            self.assertEqual(client.post("/containers/create", json={}).status_code, 404)
            self.assertEqual(client.post("/virsh", json={"args": ["unsupported"]}).status_code, 422)
            self.assertEqual(client.post("/attach-guacd", json={"network": "bridge"}).status_code, 422)
            self.assertEqual(client.post("/sync-secrets", json={"username": "alice", "values": {"../key": "x"}}).status_code, 422)


class ControlTests(unittest.TestCase):
    def test_pinned_image_requires_matching_local_image_and_active_approval(self):
        db.init()
        row = db.one("SELECT data,published FROM catalog WHERE template='ubuntu-server-pilot'")
        self.assertEqual(row["published"], 0)
        pilot = json.loads(row["data"])
        scanner = main.ext("scanner")
        with patch.object(scanner, "image_id", return_value="sha256:moved") as image:
            self.assertIn("pinned image ID", main.catalog_image_blockers(pilot)[0])
            image.return_value = pilot["pinned_image_id"]
            with patch.object(scanner, "approval", return_value=None) as approval:
                self.assertIn("publisher approval", main.catalog_image_blockers(pilot)[0])
                approval.return_value = {"decision": "approved"}
                with patch.object(scanner, "gate", return_value=[]) as gate:
                    self.assertEqual(main.catalog_image_blockers(pilot), [])
                    gate.assert_called_once_with(pilot["image"])

    def test_execution_record_validation_and_comparison(self):
        record = pilot_receipt()
        self.assertEqual(execution_records.parse(json.dumps(record).encode(), "a" * 40), record)
        for invalid in (
            pilot_receipt("b" * 40),
            {**record, "summary_sha256": "0" * 64},
            {**record, "summary": {**record["summary"], "totals": {"vcpu_hours": "0.00", "memory_gib_hours": "2.00"}}},
        ):
            with self.assertRaises(ValueError):
                execution_records.parse(json.dumps(invalid).encode(), "a" * 40)
        with self.assertRaises(ValueError):
            execution_records.parse(b"x" * (execution_records.MAX_BYTES + 1), "a" * 40)
        first = {"owner": "alpha", "execution": record}
        self.assertEqual(execution_records.comparison([first, first])["state"], "waiting")
        second = {"owner": "beta", "execution": record}
        self.assertEqual(execution_records.comparison([first, second])["state"], "match")
        other = {**record, "inputs": {**record["inputs"], "workspace-usage-v1.csv": "4" * 64}}
        self.assertEqual(execution_records.comparison([first, {"owner": "beta", "execution": other}])["differences"], ["inputs"])

    def test_blueprint_source_requires_full_commit_and_credential_free_https(self):
        repository = "https://git.example.test/research/pilot"
        self.assertEqual(blueprints.parse_source(repository, "A" * 40), (repository, "a" * 40))
        for url, revision in [(repository, "main"), ("http://git.example.test/pilot", "a" * 40),
                              ("https://user:password@git.example.test/pilot", "a" * 40)]:
            with self.subTest(url=url, revision=revision), self.assertRaises(ValueError):
                blueprints.parse_source(url, revision)

    def test_project_blueprint_launch_evidence_and_request_isolation(self):
        db.init()
        main.ext("licence").init()
        admin = {"username": "blueprint-admin", "full_name": "Blueprint Admin", "auth_source": "local",
                 "email": "admin@example.test", "roles": ["hub-admin"]}
        outsider = {**admin, "username": "outsider", "roles": ["engineer"]}
        with db.tx() as conn:
            pid = conn.execute("INSERT INTO projects(name,owner) VALUES (?,?)", ("Research pilot", admin["username"])).lastrowid
        client = TestClient(main.app)
        token = client.get("/api/csrf").json()["token"]
        headers = {"X-CSRF-Token": token}
        path = f"/projects/{pid}/blueprints"
        spec = {"name": "Pilot", "repository": "https://git.example.test/research/pilot", "revision": "a" * 40,
                "template": "ubuntu-server", "profile": "dev", "cpu": 2, "memory": 4}
        template_params = [{"name": name} for name in ("cpu", "memory", "network_profile", "devhub_request_id")]
        workspace = {"id": "blueprint-test-ws", "name": "research-ws", "owner_name": admin["username"],
                     "template_name": "ubuntu-server", "latest_build": {"id": "build", "template_version_id": "pinned"}}
        scanner = main.ext("scanner")
        with ExitStack() as mocks:
            actor = mocks.enter_context(patch.object(main.auth, "current_user", return_value=admin))
            image = mocks.enter_context(patch.object(scanner, "image_id", return_value="sha256:original"))
            mocks.enter_context(patch.object(scanner, "gate", return_value=[]))
            mocks.enter_context(patch.object(coder, "template_by_name", return_value={"active_version_id": "pinned"}))
            mocks.enter_context(patch.object(coder, "version_params", return_value=template_params))
            mocks.enter_context(patch.object(coder, "ensure_user"))
            mocks.enter_context(patch.object(coder, "workspaces", return_value=[]))
            create = mocks.enter_context(patch.object(coder, "create_workspace", return_value=workspace))
            result = client.post(path, data=spec, headers=headers, follow_redirects=False)
            self.assertEqual(result.status_code, 303)
            bid = int(result.headers["location"].split("=")[-1])
            self.assertIn("Shareable blueprint link", client.get(path).text)
            launch = {"name": "research-ws", "template": "kali", "blueprint_id": bid, "cpu": 999}
            image.return_value = "sha256:changed"
            self.assertEqual(client.post("/workspaces", data=launch, headers=headers, follow_redirects=False).status_code, 303)
            create.assert_not_called()
            image.return_value = "sha256:original"
            self.assertEqual(client.post("/workspaces", data=launch, headers=headers, follow_redirects=False).status_code, 303)
            self.assertEqual(create.call_args.kwargs["template_version_id"], "pinned")
            self.assertEqual(create.call_args.args[1], "ubuntu-server")
            self.assertEqual(create.call_args.args[3]["cpu"], 2)
            evidence_path = f"/api/projects/{pid}/evidence/{workspace['id']}"
            with db.tx() as conn:
                conn.execute("UPDATE workspaces SET deleted_at=? WHERE id=?", (time.time(), workspace["id"]))
            evidence = client.get(evidence_path).json()
            self.assertEqual(evidence["recipe"]["revision"], "a" * 40)
            self.assertEqual(evidence["coder_confirmation"]["build_id"], "build")

            upload_path = f"/projects/{pid}/workspaces/{workspace['id']}/execution-record"
            receipt = pilot_receipt()
            files = {"receipt": ("execution-record.json", json.dumps(receipt), "application/json")}
            self.assertEqual(client.post(upload_path, files=files).status_code, 403)
            self.assertEqual(client.post(upload_path, files=files, headers=headers, follow_redirects=False).status_code, 303)
            self.assertEqual(client.post(upload_path, files=files, data={"csrf_token": token},
                                         follow_redirects=False).status_code, 303)
            changed = {**receipt, "inputs": {**receipt["inputs"], "summarize_usage.py": "4" * 64}}
            self.assertEqual(client.post(upload_path, files={"receipt": ("record.json", json.dumps(changed))},
                                         headers=headers, follow_redirects=False).status_code, 303)
            self.assertEqual(client.get(evidence_path + "/execution").json(), receipt)
            self.assertIn("Waiting for a record from a second participant", client.get(path).text)
            with db.tx() as conn:
                conn.execute("INSERT INTO project_members VALUES (?,?)", (pid, "other-member"))
            actor.return_value = {**outsider, "username": "other-member"}
            self.assertEqual(client.post(upload_path, files=files, headers=headers).status_code, 403)
            actor.return_value = admin

            submission = {"kind": "dataset", "subject": "Pilot dataset", "detail": "Approved version needed"}
            self.assertEqual(client.post(f"/projects/{pid}/requests", data=submission, headers=headers,
                                         follow_redirects=False).status_code, 303)
            request_id = db.one("SELECT id FROM project_requests WHERE project_id=?", (pid,))["id"]
            review = f"/projects/{pid}/requests/{request_id}/review"
            actor.return_value = outsider
            self.assertEqual(client.get(evidence_path).status_code, 404)
            self.assertEqual(client.get(evidence_path + "/execution").status_code, 404)
            self.assertEqual(client.post(f"/projects/{pid}/requests", data=submission, headers=headers).status_code, 404)
            self.assertEqual(client.post(review, data={"decision": "reviewed", "note": "Recorded"}, headers=headers).status_code, 403)
            actor.return_value = admin
            review_body = {"decision": "reviewed", "note": "Recorded"}
            self.assertEqual(client.post(review, data=review_body, headers=headers, follow_redirects=False).status_code, 303)
            self.assertEqual(client.post(review, data=review_body, headers=headers).status_code, 409)

    def test_all_portal_screens_render_with_shared_shell(self):
        db.init()
        main.ext("licence").init()
        scanner = main.ext("scanner")
        scanner.init()
        scan_id = scanner.ingest("test-image", {"Results": []}, image_id="test-id")
        billing = main.ext("billing")
        admin = {"username": "ui-admin", "full_name": "UI Admin", "auth_source": "local", "roles": ["hub-admin"]}
        routes = [
            "/", "/projects", "/secrets", "/admin/workspaces", "/admin/users", "/admin/images",
            f"/admin/images/scans/{scan_id}", "/admin/quotas", "/admin/licences", "/admin/reclaim",
            "/admin/finops", "/admin/billing", "/admin/kvm", "/admin/audit", "/admin/health",
        ]
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(main.auth, "current_user", return_value=admin))
            for method in ("workspaces", "templates", "users"):
                mocks.enter_context(patch.object(coder, method, return_value=[]))
            mocks.enter_context(patch.object(main.engine, "call", return_value=[]))
            mocks.enter_context(patch.object(main, "health_checks", return_value=[]))
            mocks.enter_context(patch.object(billing, "usage", return_value=([], billing.rates(), False)))
            mocks.enter_context(patch.object(main.ext("kvm"), "capabilities", return_value={"status": "disabled"}))
            client = TestClient(main.app)
            for route in routes:
                with self.subTest(route=route):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('id="main-content"', response.text)
                    self.assertIn('content="width=device-width, initial-scale=1"', response.text)
        engineer = {**admin, "roles": ["engineer"]}
        with patch.object(main.auth, "current_user", return_value=engineer), patch.object(coder, "workspaces", return_value=[]):
            response = client.get("/")
            self.assertNotIn('href="/admin/users"', response.text)
            self.assertIn('class="card template-card"', response.text)
            self.assertIn('method="post" action="/workspaces"', response.text)
            self.assertEqual(client.get("/admin/users").status_code, 403)

    def test_login_rejects_missing_and_invalid_csrf_tokens(self):
        client = TestClient(main.app)
        creds = {"username": "alice", "password": "example"}
        with patch.object(main.auth, "local_login", return_value=None) as login, patch.object(main.audit, "log"):
            self.assertEqual(client.post("/login", data=creds).status_code, 403)
            login.assert_not_called()
            form = client.get("/login")
            token = client.get("/api/csrf").json()["token"]
            self.assertIn(f'name="csrf_token" value="{token}"', form.text)
            self.assertEqual(client.post("/login", data={**creds, "csrf_token": "invalid"}).status_code, 403)
            accepted = client.post("/login", data={**creds, "csrf_token": token}, follow_redirects=False)
            self.assertEqual(accepted.status_code, 303)
            login.assert_called_once()

    def test_secure_cookie_on_public_host_and_lan_http_cookie(self):
        probe = FastAPI()
        probe.add_api_route("/api/csrf", main.api_csrf)
        with (
            patch.object(browser_security, "PORTAL_PUBLIC_URL", "https://devhub.example"),
            patch.object(browser_security, "PORTAL_LOCAL_URL", "http://localhost:8088"),
        ):
            wrapper = browser_security.PortalSessions(probe, secret_key="test")
        public = TestClient(wrapper, base_url="http://devhub.example").get("/api/csrf")
        lan = TestClient(wrapper, base_url="http://localhost:8088").get("/api/csrf")
        self.assertIn("; secure", public.headers.get_list("set-cookie")[-1].lower())
        self.assertNotIn("; secure", lan.headers.get_list("set-cookie")[-1].lower())

    def test_unverifiable_image_cannot_use_previous_approval(self):
        scanner = ImageScanner()
        with (
            patch.object(scanner, "scan", return_value={"status": "done", "image_id": "old", "critical": 0}),
            patch.object(scanner, "image_id", return_value=None),
            patch("app.extensions.scanner.policy", return_value={"enabled": True}),
        ):
            self.assertIn("could not be verified", scanner.gate("example")[0])

    def test_delete_request_does_not_mark_ledger_deleted(self):
        workspace = {"id": "workspace", "owner_name": "alice", "name": "workspace"}
        plan = [{"ws": workspace, "action": "delete", "reason": "idle"}]
        with patch.object(coder, "build") as build, patch.object(reclaim.audit, "log"), patch.object(db, "tx") as tx:
            self.assertEqual(reclaim.apply(plan), plan)
            build.assert_called_once_with("workspace", "delete")
            tx.assert_not_called()

    def test_reconciliation_reports_failure_and_last_success(self):
        with patch.object(main, "_reconcile", return_value=[]):
            self.assertEqual(main.reconcile(), [])
        last = main.RECONCILE_LAST_SUCCESS._value.get()
        self.assertGreater(last, 0)
        with patch.object(main, "_reconcile", side_effect=RuntimeError("Coder unavailable")):
            with self.assertRaises(RuntimeError):
                main.reconcile()
        self.assertEqual(main.RECONCILE_OK._value.get(), 0)
        self.assertEqual(main.RECONCILE_LAST_SUCCESS._value.get(), last)


class SdlcTests(unittest.TestCase):
    def setUp(self):
        db.init()
        with db.tx() as conn:
            self.pid = conn.execute("INSERT INTO projects(name,owner) VALUES (?,?)",
                                    (self._testMethodName, "delivery-lead")).lastrowid
            self.other = conn.execute("INSERT INTO projects(name,owner) VALUES (?,?)",
                                      (self._testMethodName + "-other", "other-lead")).lastrowid
            for username, role in (("delivery-qa", "qa"), ("delivery-release", "release")):
                conn.execute("INSERT OR IGNORE INTO users(username,auth_source,roles) VALUES (?,'local','[]')", (username,))
                conn.execute("INSERT INTO project_members VALUES (?,?)", (self.pid, username))
                conn.execute("INSERT INTO sdlc_roles VALUES (?,?,?)", (self.pid, username, role))
        env = patch.dict(os.environ, {
            "DEVHUB_SDLC_ENABLED": "true", "SDLC_PILOT_PROJECT_IDS": str(self.pid),
            "SDLC_GITHUB_REPOSITORY": "example/devhub", "SDLC_GITHUB_REPOSITORY_ID": "123",
            "SDLC_STAGING_PROJECT_ID": str(self.pid), "SDLC_STAGING_IMAGE": "ghcr.io/example/devhub/portal",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(main.app)
        self.headers = {"X-CSRF-Token": self.client.get("/api/csrf").json()["token"]}
        self.prefix = f"/delivery/{self.pid}"
        self.actor = patch.object(main.auth, "current_user", return_value=self.user("delivery-lead"))
        self.current_user = self.actor.start()
        self.addCleanup(self.actor.stop)

    def user(self, name):
        return {"username": name, "full_name": name, "auth_source": "local", "roles": []}

    def post(self, suffix, data):
        return self.client.post(self.prefix + suffix, data=data, headers=self.headers, follow_redirects=False)

    def test_project_work_and_role_isolation(self):
        self.assertEqual(self.client.get(self.prefix).status_code, 200)
        self.assertEqual(self.client.get(f"/delivery/{self.other}").status_code, 404)
        self.assertEqual(self.post("/milestones", {"title": "Pilot", "due_on": "2026-12-08"}).status_code, 303)
        milestone = db.one("SELECT id FROM sdlc_milestones WHERE project_id=?", (self.pid,))["id"]
        self.assertEqual(self.post("/work", {"title": "Requirement", "kind": "requirement", "milestone": milestone}).status_code, 303)
        self.assertEqual(self.post("/work/1/status", {"status": "in-progress"}).status_code, 303)
        self.assertEqual(self.post("/work/1/comments", {"body": "Reviewed the scope."}).status_code, 303)
        board = self.client.get(self.prefix + "?tab=work&view=board&milestone=")
        self.assertEqual(board.status_code, 200)
        self.assertIn("Reviewed the scope.", board.text)
        self.current_user.return_value = self.user("delivery-qa")
        self.assertEqual(self.post("/roles", {"username": "delivery-qa", "role": "lead"}).status_code, 403)
        self.assertEqual(self.client.get(self.prefix + "?tab=people").status_code, 403)
        self.current_user.return_value = self.user("outsider")
        self.assertEqual(self.client.get(self.prefix + "?tab=work").status_code, 404)

    def test_gate_verifies_signed_workflow_identity(self):
        now = int(time.time())
        claims = {"iss": sdlc_gate.ISSUER, "aud": sdlc_gate.AUDIENCE, "iat": now, "nbf": now, "exp": now + 60,
                  "sub": "repo:example/devhub:environment:staging",
                  "repository": "example/devhub", "repository_id": "123", "ref": "refs/heads/main",
                  "environment": "staging", "runner_environment": "self-hosted", "event_name": "workflow_dispatch",
                  "workflow_ref": "example/devhub/.github/workflows/deploy-staging.yml@refs/heads/main",
                  "run_id": "10", "run_attempt": "1", "sha": "a" * 40}
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        url = "/api/sdlc/automation/permit"
        payload = {"release_id": 99999, "digest": "sha256:" + "b" * 64}
        with patch.object(sdlc_gate, "github_keys") as keys:
            keys.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=signing_key.public_key())
            for overrides, expected in (
                ({}, 404),
                ({"aud": "other"}, 401),
                ({"exp": now - 1}, 401),
                ({"sub": "repo:example/devhub:ref:refs/heads/main"}, 403),
                ({"repository_id": "321"}, 403),
                ({"workflow_ref": "other.yml"}, 403),
                ({"environment": "production"}, 403),
                ({"ref": "refs/heads/branch"}, 403),
                ({"runner_environment": "github-hosted"}, 403),
            ):
                with self.subTest(overrides=overrides):
                    token = jwt.encode({**claims, **overrides}, signing_key, algorithm="RS256")
                    response = self.client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
                    self.assertEqual(response.status_code, expected, response.text)

    def test_external_callback_establishes_project_only_session(self):
        issuer = "https://identity.example.test/realms/collaborators"
        client_id = "external-client"
        callback = "/delivery/external/callback"
        external = TestClient(main.app, base_url="https://portal.example.test")
        config = (issuer, client_id, "fixture-secret", "2", str(external.base_url) + callback)
        email = "collaborator@example.test"
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with patch.object(sdlc_identity, "settings", return_value=config):
            invitation = self.post("/invitations", {"email": email, "role": "commenter"})
            self.assertEqual(invitation.status_code, 200, invitation.text)
            code = re.search(r"<code>([A-Za-z0-9_-]{43})</code>", invitation.text).group(1)
            self.current_user.return_value = None
            for valid in (False, True):
                headers = {"X-CSRF-Token": external.get("/api/csrf").json()["token"]}
                start = external.post("/delivery/external", data={"code": code}, headers=headers, follow_redirects=False)
                self.assertEqual(start.status_code, 303, start.text)
                query = parse_qs(urlsplit(start.headers["location"]).query)
                now = int(time.time())
                claims = {"iss": issuer, "aud": client_id, "sub": "fixture-collaborator",
                          "iat": now, "exp": now + 60, "nonce": query["nonce"][0] if valid else "wrong-nonce",
                          "acr": "2", "email": email, "email_verified": True}
                token = jwt.encode(claims, signing_key, algorithm="RS256")
                token_url = issuer + "/protocol/openid-connect/token"
                exchange = httpx.Response(200, json={"id_token": token}, request=httpx.Request("POST", token_url))
                params = {"code": "provider-code", "state": query["state"][0]}
                with patch.object(sdlc_identity.httpx, "post", return_value=exchange), patch.object(sdlc_identity, "jwks") as keys:
                    keys.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=signing_key.public_key())
                    result = external.get(callback, params=params, follow_redirects=False)
                self.assertEqual(result.status_code, 303 if valid else 403, result.text)

            self.assertEqual(result.headers["location"], "/delivery/external/projects")
            self.assertEqual(external.get(callback, params=params).status_code, 403)
            self.assertIn(self._testMethodName, external.get(result.headers["location"]).text)
            self.assertEqual(external.get(self.prefix + "?tab=work").status_code, 200)
            self.assertEqual(external.get(f"/delivery/{self.other}").status_code, 404)
            self.assertEqual(external.get("/", follow_redirects=False).headers["location"], "/login")
            principal = db.one("SELECT subject FROM sdlc_external WHERE project_id=?", (self.pid,))["subject"]
            self.current_user.return_value = self.user("delivery-lead")
            self.assertEqual(self.post(f"/external/{principal}", {"action": "revoke"}).status_code, 303)
            self.current_user.return_value = None
            self.assertEqual(external.get(self.prefix).status_code, 404)

    def test_invitation_identity_expiry_revocation_and_single_use(self):
        issuer = "https://identity.example.test/realms/collaborators"
        code = "test-code"
        with db.tx() as conn:
            conn.execute("INSERT INTO sdlc_invitations VALUES (?,?,?,?,?,NULL,NULL)",
                         (sdlc_identity.digest(code), self.pid, "collaborator@example.test", "commenter", time.time() + 60))
        claims = {"iss": issuer, "sub": "external-id", "email": "COLLABORATOR@example.test", "email_verified": True, "acr": "2"}
        with self.assertRaises(main.HTTPException):
            sdlc_identity.accept_identity({**claims, "acr": "1"}, issuer, "2", sdlc_identity.digest(code))
        with self.assertRaises(main.HTTPException):
            sdlc_identity.accept_identity({**claims, "email": "someone@example.test"}, issuer, "2", sdlc_identity.digest(code))
        principal = sdlc_identity.accept_identity(claims, issuer, "2", sdlc_identity.digest(code))
        self.assertEqual(sdlc_store.external_membership(self.pid, principal)["role"], "commenter")
        self.assertIsNone(sdlc_store.external_membership(self.pid, principal, time.time() + 31 * 86400))
        self.assertIsNone(sdlc_store.external_membership(self.other, principal))
        with self.assertRaises(main.HTTPException):
            sdlc_identity.accept_identity(claims, issuer, "2", sdlc_identity.digest(code))
        self.assertEqual(self.post(f"/external/{principal}", {"action": "revoke"}).status_code, 303)
        self.assertIsNone(sdlc_store.external_membership(self.pid, principal))

    def test_staging_denies_missing_revoked_changed_and_replayed_approval(self):
        self.post("/milestones", {"title": "Release"})
        milestone = db.one("SELECT id FROM sdlc_milestones WHERE project_id=?", (self.pid,))["id"]
        commit, digest = "a" * 40, "sha256:" + "b" * 64
        self.post("/work", {"title": "Test", "kind": "test", "milestone": milestone,
                            "reference": "https://example.test/evidence"})
        self.post("/work/1/status", {"status": "done"})
        self.assertEqual(self.post("/releases", {"title": "Candidate", "milestone": milestone, "commit": commit,
            "digest": digest, "ci_url": "https://github.com/example/devhub/actions/runs/123",
            "evidence": "https://example.test/test-evidence"}).status_code, 303)
        release = db.one("SELECT id FROM sdlc_releases WHERE project_id=?", (self.pid,))["id"]
        review = f"/releases/{release}/review"
        self.assertEqual(self.post(review, {"decision": "approve", "note": "Self approval"}).status_code, 403)
        base = "/api/sdlc/automation"
        payload = {"release_id": release, "digest": digest}
        claims = {"run_id": "12", "run_attempt": "1", "sha": commit}
        with patch.object(sdlc_gate, "authorise", return_value=claims):
            self.assertEqual(self.client.post(base + "/permit", json=payload).status_code, 409)
            for actor in ("delivery-qa", "delivery-release"):
                self.current_user.return_value = self.user(actor)
                self.assertEqual(self.post(review, {"decision": "approve", "note": "Evidence checked"}).status_code, 303)
            self.assertEqual(self.client.post(base + "/permit", json={**payload, "digest": "sha256:" + "c" * 64}).status_code, 409)
            permit = self.client.post(base + "/permit", json=payload).json()["permit"]
            self.current_user.return_value = self.user("delivery-release")
            self.post(review, {"decision": "revoke", "note": "Hold"})
            self.assertEqual(self.client.post(base + "/consume", json={**payload, "permit": permit}).status_code, 409)
            self.post(review, {"decision": "approve", "note": "Cleared"})
            self.current_user.return_value = self.user("delivery-lead")
            self.assertEqual(self.post("/releases", {"title": "Alternate", "milestone": milestone, "commit": commit,
                "digest": digest, "ci_url": "https://github.com/example/devhub/actions/runs/123",
                "evidence": "https://example.test/test-evidence"}).status_code, 303)
            alternate = db.one("SELECT id FROM sdlc_releases WHERE project_id=? AND title='Alternate'", (self.pid,))["id"]
            for actor in ("delivery-qa", "delivery-release"):
                self.current_user.return_value = self.user(actor)
                self.assertEqual(self.post(f"/releases/{alternate}/review", {"decision": "approve", "note": "Evidence checked"}).status_code, 303)
            alternate_payload = {**payload, "release_id": alternate}
            alternate_response = self.client.post(base + "/permit", json=alternate_payload)
            self.assertEqual(alternate_response.status_code, 200, alternate_response.text)
            self.assertEqual(self.client.post(base + "/consume", json={**payload, "permit": permit}).status_code, 200)
            self.assertEqual(self.client.post(base + "/consume", json={**payload, "permit": permit}).status_code, 409)
            self.assertEqual(self.client.post(base + "/consume", json={
                **alternate_payload, "permit": alternate_response.json()["permit"]
            }).status_code, 409)
            self.assertEqual(self.client.post(base + "/permit", json=alternate_payload).status_code, 409)
            self.assertEqual(self.client.post(base + "/outcome", json={**payload, "permit": permit, "outcome": "succeeded"}).status_code, 200)
            claims.update({"run_id": "13", "sha": "c" * 40})
            self.assertEqual(self.client.post(base + "/permit", json=payload).status_code, 409)
            self.assertEqual(self.client.post(base + "/permit", json={**payload, "rollback": True}).status_code, 200)
            with db.tx() as conn:
                conn.execute("UPDATE sdlc_work SET reference_url='https://example.test/new' WHERE project_id=? AND number=1", (self.pid,))
            self.assertEqual(self.client.post(base + "/permit", json={**payload, "rollback": True}).status_code, 409)
        self.assertEqual(self.client.post(base + "/permit", json=payload).status_code, 401)
