import os
import shutil
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

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

from app import blueprints, browser_security, coder, db, main, provisioning, reclaim
from app.engine_broker import app as broker
from app.extensions.licence import LicenseBroker
from app.extensions.scanner import ImageScanner


def tearDownModule():
    db._conn.close()
    _sandbox.cleanup()


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

            submission = {"kind": "dataset", "subject": "Pilot dataset", "detail": "Approved version needed"}
            self.assertEqual(client.post(f"/projects/{pid}/requests", data=submission, headers=headers,
                                         follow_redirects=False).status_code, 303)
            request_id = db.one("SELECT id FROM project_requests WHERE project_id=?", (pid,))["id"]
            review = f"/projects/{pid}/requests/{request_id}/review"
            actor.return_value = outsider
            self.assertEqual(client.get(evidence_path).status_code, 404)
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
