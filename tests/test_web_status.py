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
        """GET / serves the single-page HTML status dashboard."""
        response = await self.client.get("/")
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
        """GET /api/dorms returns list of active dorms with cleaners and duty status."""
        response = await self.client.get("/api/dorms")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

        default_dorm = next((d for d in data if d["dorm_id"] == config.DEFAULT_GROUP_ID), None)
        self.assertIsNotNone(default_dorm)
        self.assertEqual(default_dorm["member_count"], 8)
        self.assertIn(default_dorm["cleaner1"], ("Bilisuma", "Nahom"))
        self.assertIn(default_dorm["cleaner2"], ("Bilisuma", "Nahom"))

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

    async def test_dorm_detail_not_found(self):
        """GET /api/dorms/{invalid_id} returns 404."""
        response = await self.client.get("/api/dorms/-999999999999")
        self.assertEqual(response.status_code, 404)

if __name__ == "__main__":
    unittest.main()
