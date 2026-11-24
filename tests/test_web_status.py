"""
Unit & Integration Tests for Web Status Dashboard & API Endpoints
Validates that the single-page dashboard and REST API endpoints correctly expose
live bot status, dorm schedules, cleaners, leaderboards, and fines without errors.
"""

from pathlib import Path
import sys
import tempfile
import unittest

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from httpx import ASGITransport, AsyncClient

import config
import db
import web

class TestWebStatusDashboard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Isolate database for tests
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.tmp_dir.name) / "test_web.db"
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        await db.init_db()
        await db.seed_default_dorm()

        self.transport = ASGITransport(app=web.app)
        self.client = AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    async def test_dashboard_html_page_served(self):
        """GET / and /dashboard serve the single-page HTML status dashboard."""
        for path in ("/", "/dashboard"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.headers.get("content-type", ""))
            self.assertIn("Dorm Inspector • Live Status Dashboard", response.text)
            self.assertIn("stat-dorms", response.text)
            self.assertIn("stat-members", response.text)

    async def test_health_check_endpoints(self):
        """GET /health and /api/health return 200 healthy status."""
        for path in ("/health", "/api/health"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "healthy")
            self.assertEqual(data["service"], "dorm-inspector-bot")
            self.assertIn("uptime", data)

    async def test_system_status_api(self):
        """GET /api/status returns live platform metrics."""
        response = await self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "online")
        self.assertIn("stats", data)
        self.assertGreaterEqual(data["stats"]["active_dorms_count"], 1)
        self.assertGreaterEqual(data["stats"]["total_members_count"], 8)
        self.assertIn("database", data)

    async def test_dorms_list_api(self):
        """GET /api/dorms requires admin authorization for privacy, and returns dorms when authorized."""
        # 1. Unauthenticated request is blocked with 403 Forbidden
        unauth_resp = await self.client.get("/api/dorms")
        self.assertEqual(unauth_resp.status_code, 403)
        self.assertIn("Public dorm listing is disabled", unauth_resp.json()["detail"])

        # 2. Authenticated with admin key returns full list
        response = await self.client.get("/api/dorms", headers={"x-admin-key": str(config.ADMIN_ID)})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

        default_dorm = next((d for d in data if d["dorm_id"] == config.DEFAULT_GROUP_ID), None)
        self.assertIsNotNone(default_dorm)
        self.assertEqual(default_dorm["member_count"], 8)
        self.assertIn(default_dorm["cleaner1"], ("Bilisuma", "Nahom"))
        self.assertIn(default_dorm["cleaner2"], ("Bilisuma", "Nahom"))

    async def test_dorm_detail_by_id_and_code(self):
        """GET /api/dorms/{id_or_code} and /api/dorms/code/{code} resolve correctly."""
        # 1. By integer ID
        res_id = await self.client.get(f"/api/dorms/{config.DEFAULT_GROUP_ID}")
        self.assertEqual(res_id.status_code, 200)
        data_id = res_id.json()
        self.assertEqual(data_id["dorm_id"], config.DEFAULT_GROUP_ID)
        dorm_code = data_id.get("dorm_code")
        self.assertIsNotNone(dorm_code)

        # 2. By string dorm code in the universal /api/dorms/{dorm_identifier} endpoint
        res_code = await self.client.get(f"/api/dorms/{dorm_code}")
        self.assertEqual(res_code.status_code, 200)
        self.assertEqual(res_code.json()["dorm_id"], config.DEFAULT_GROUP_ID)

        # 3. By explicit code endpoint /api/dorms/code/{code}
        res_explicit = await self.client.get(f"/api/dorms/code/{dorm_code}")
        self.assertEqual(res_explicit.status_code, 200)
        self.assertEqual(res_explicit.json()["dorm_id"], config.DEFAULT_GROUP_ID)

    async def test_dorm_sub_routes(self):
        """GET /api/dorms/{id_or_code}/schedule, fines, and leaderboard."""
        code_res = await self.client.get(f"/api/dorms/{config.DEFAULT_GROUP_ID}")
        dorm_code = code_res.json()["dorm_code"]

        for identifier in (str(config.DEFAULT_GROUP_ID), dorm_code):
            # Schedule
            sched_res = await self.client.get(f"/api/dorms/{identifier}/schedule")
            self.assertEqual(sched_res.status_code, 200)
            self.assertIsInstance(sched_res.json(), list)
            self.assertGreaterEqual(len(sched_res.json()), 1)

            # Fines
            fines_res = await self.client.get(f"/api/dorms/{identifier}/fines")
            self.assertEqual(fines_res.status_code, 200)
            self.assertIsInstance(fines_res.json(), list)

            # Leaderboard
            lead_res = await self.client.get(f"/api/dorms/{identifier}/leaderboard")
            self.assertEqual(lead_res.status_code, 200)
            self.assertIsInstance(lead_res.json(), list)

    async def test_legacy_redirect_routes(self):
        """GET /fines and /leaderboard redirect cleanly to /."""
        for path in ("/fines", "/leaderboard"):
            res = await self.client.get(path, follow_redirects=False)
            self.assertIn(res.status_code, (301, 302, 307))
            self.assertEqual(res.headers.get("location"), "/")

    async def test_dorm_detail_api(self):
        """GET /api/dorms/{dorm_id} returns detailed schedule, leaderboard, and fines."""
        response = await self.client.get(f"/api/dorms/{config.DEFAULT_GROUP_ID}")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(data["dorm_id"], config.DEFAULT_GROUP_ID)
        self.assertEqual(data["fine_amount"], 100)
        self.assertIn("current_week", data)
        self.assertIn("schedule", data)
        self.assertIn("leaderboard", data)
        self.assertIn("fines", data)
        self.assertIn("history", data)

        # 4 weeks rotation preview check
        self.assertEqual(len(data["schedule"]), 4)
        week1 = data["schedule"][0]
        self.assertEqual(week1["week_number"], 1)
        self.assertTrue(week1["is_current"])

    async def test_default_dorm_api(self):
        """GET /api/dorms/default returns the active default dorm and subroutes."""
        res = await self.client.get("/api/dorms/default")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["dorm_id"], config.DEFAULT_GROUP_ID)
        self.assertIn("dorm_code", data)
        self.assertIn("current_week", data)

        # Sub-routes for default dorm
        sched = await self.client.get("/api/dorms/default/schedule")
        self.assertEqual(sched.status_code, 200)
        self.assertIsInstance(sched.json(), list)

        fines = await self.client.get("/api/dorms/default/fines")
        self.assertEqual(fines.status_code, 200)
        self.assertIsInstance(fines.json(), list)

        lead = await self.client.get("/api/dorms/default/leaderboard")
        self.assertEqual(lead.status_code, 200)
        self.assertIsInstance(lead.json(), list)

    async def test_dashboard_html_has_fallback_logic(self):
        """Verify dashboard frontend contains loadDefaultDorm and no blocking inline modal script."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("loadDefaultDorm", resp.text)
        self.assertIn("saved_dorm_code", resp.text)
        self.assertNotIn("if (!p.get('code') && !p.get('dorm'))", resp.text)

    async def test_dorm_detail_not_found(self):
        """GET /api/dorms/{invalid_id} returns 404."""
        response = await self.client.get("/api/dorms/-999999999999")
        self.assertEqual(response.status_code, 404)
        response2 = await self.client.get("/api/dorms/INVALID-CODE-XYZ")
        self.assertEqual(response2.status_code, 404)

if __name__ == "__main__":
    unittest.main()
