"""
Unit and Integration Test Suite for Dorm Inspector Bot
Verifies pairing validation, rotation math, database persistence, and fine rules.
"""

import asyncio
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db

class TestDormInspectorBot(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Set up a temporary database file for test isolation
        try:
            self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:
            self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_dorm.db"
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        self.temp_dir.cleanup()

    def test_members_validation_success(self):
        """Verify that default 8 members and 4 pairs load cleanly."""
        self.assertEqual(len(config.MEMBERS), 8)
        self.assertEqual(len(config.PAIRS), 4)
        for p in config.PAIRS:
            self.assertEqual(len(p), 2)

    def test_members_validation_missing_in_pair(self):
        """Verify startup failure when a member is missing from pairs."""
        invalid_data = {
            "members": [
                {"user_id": 1, "name": "Alice"},
                {"user_id": 2, "name": "Bob"},
                {"user_id": 3, "name": "Charlie"},
                {"user_id": 4, "name": "David"}
            ],
            "pairs": [
                ["Alice", "Bob"]
                # Charlie and David missing
            ]
        }
        temp_file = Path(self.temp_dir.name) / "invalid_members.json"
        with open(temp_file, "w") as f:
            json.dump(invalid_data, f)

        with self.assertRaises(SystemExit) as ctx:
            config.load_and_validate_members(temp_file)
        self.assertIn("CRITICAL ERROR", str(ctx.exception))

    def test_members_validation_duplicate_in_pair(self):
        """Verify startup failure when a member appears in more than one pair."""
        invalid_data = {
            "members": [
                {"user_id": 1, "name": "Alice"},
                {"user_id": 2, "name": "Bob"},
                {"user_id": 3, "name": "Charlie"},
                {"user_id": 4, "name": "David"}
            ],
            "pairs": [
                ["Alice", "Bob"],
                ["Alice", "Charlie"],  # Alice in 2 pairs
                ["David", "Bob"]
            ]
        }
        temp_file = Path(self.temp_dir.name) / "dup_members.json"
        with open(temp_file, "w") as f:
            json.dump(invalid_data, f)

        with self.assertRaises(SystemExit):
            config.load_and_validate_members(temp_file)

    def test_rotation_schedule_and_cycling(self):
        """
        Verify rotation starting next Monday, November 30, 2026:
        - Nov 27 (Friday setup): week_number <= 0 (Duty not started yet)
        - Week 1 (Nov 30 - Dec 04): Pair 1 (Bilisuma & Nahom) [Announced Mon Nov 30]
        - Week 2 (Dec 07 - Dec 11): Pair 2 (Hassan & Mintesnot)
        - Week 3 (Dec 14 - Dec 18): Pair 3 (Dawit & Zeftret)
        - Week 4 (Dec 21 - Dec 25): Pair 4 (Ahmed & Yonatan)
        - Week 5 (Dec 28 - Jan 01): Pair 1 (Bilisuma & Nahom) - cycles back!
        """
        start = config.START_DATE_OBJ
        self.assertEqual(start, date(2026, 11, 27))
        self.assertEqual(start.weekday(), 4, "2026-11-27 is a Friday")
        self.assertEqual(config.START_BASE_MONDAY, date(2026, 11, 30))

        # Before start date: Friday Nov 27
        w_before = config.get_week_info(start)
        self.assertLessEqual(w_before.week_number, 0, "Friday Nov 27 is before duty starts")

        # Week 1 (Monday Nov 30) - Announced Monday Nov 30 with Pair 1
        w1 = config.get_week_info(date(2026, 11, 30))
        self.assertEqual(w1.week_number, 1)
        self.assertEqual(w1.cleaner1.name, "Bilisuma")
        self.assertEqual(w1.cleaner2.name, "Nahom")
        self.assertEqual(w1.week_start, "2026-11-30")
        self.assertEqual(w1.friday_date, date(2026, 12, 4))

        # Mid-week of week 1 (Wednesday Dec 2)
        w1_wed = config.get_week_info(date(2026, 12, 2))
        self.assertEqual(w1_wed.week_number, 1)
        self.assertEqual(w1_wed.cleaner1.name, "Bilisuma")

        # Week 2 (Monday Dec 7) - Swapped: Hassan & Mintesnot
        w2 = config.get_week_info(date(2026, 12, 7))
        self.assertEqual(w2.week_number, 2)
        self.assertEqual(w2.cleaner1.name, "Hassan")
        self.assertEqual(w2.cleaner2.name, "Mintesnot")
        self.assertEqual(w2.week_start, "2026-12-07")

        # Week 3 (Monday Dec 14) - Swapped: Dawit & Zeftret
        w3 = config.get_week_info(date(2026, 12, 14))
        self.assertEqual(w3.week_number, 3)
        self.assertEqual(w3.cleaner1.name, "Dawit")
        self.assertEqual(w3.cleaner2.name, "Zeftret")

        # Week 4 (Monday Dec 21) - Last pair in cycle: Ahmed & Yonatan
        w4 = config.get_week_info(date(2026, 12, 21))
        self.assertEqual(w4.week_number, 4)
        self.assertEqual(w4.cleaner1.name, "Ahmed")
        self.assertEqual(w4.cleaner2.name, "Yonatan")

        # Week 5 (Monday Dec 28 - cycle back to pair 1)
        w5 = config.get_week_info(date(2026, 12, 28))
        self.assertEqual(w5.week_number, 5)
        self.assertEqual(w5.cleaner1.name, "Bilisuma")
        self.assertEqual(w5.cleaner2.name, "Nahom")

    async def test_db_week_creation_and_approver_selection(self):
        """Test week initialization in DB and approver selection."""
        await db.init_db()

        w_info = config.get_week_info(config.START_BASE_MONDAY)
        eligible = [
            m.user_id for m in config.MEMBERS
            if m.user_id not in (w_info.cleaner1.user_id, w_info.cleaner2.user_id)
        ]
        self.assertEqual(len(eligible), 6, "Must have exactly 6 non-cleaning members as eligible approvers")

        record = await db.get_or_create_week(
            w_info.week_start,
            w_info.week_number,
            w_info.cleaner1.user_id,
            w_info.cleaner2.user_id,
            eligible
        )
        self.assertEqual(record["week_start"], w_info.week_start)
        self.assertIn(record["approver_id"], eligible)
        self.assertNotIn(record["approver_id"], (w_info.cleaner1.user_id, w_info.cleaner2.user_id))

        # Test idempotence: calling again returns the exact same approver
        record2 = await db.get_or_create_week(
            w_info.week_start,
            w_info.week_number,
            w_info.cleaner1.user_id,
            w_info.cleaner2.user_id,
            eligible
        )
        self.assertEqual(record["approver_id"], record2["approver_id"])

    async def test_fines_ledger_and_markpaid(self):
        """Test fine generation (50 ETB each) and marking paid."""
        await db.init_db()

        w_info = config.get_week_info(config.START_BASE_MONDAY)
        c1 = w_info.cleaner1
        c2 = w_info.cleaner2

        # Create fines for week
        await db.create_fines_for_week(
            week_start=w_info.week_start,
            cleaner1_id=c1.user_id,
            cleaner1_name=c1.name,
            cleaner2_id=c2.user_id,
            cleaner2_name=c2.name,
            fine_each=50
        )

        # Verify fines created
        unpaid_c1 = await db.get_user_unpaid_total(c1.user_id)
        unpaid_c2 = await db.get_user_unpaid_total(c2.user_id)
        self.assertEqual(unpaid_c1, 50)
        self.assertEqual(unpaid_c2, 50)

        # Verify idempotency: calling again does not duplicate fines
        await db.create_fines_for_week(
            week_start=w_info.week_start,
            cleaner1_id=c1.user_id,
            cleaner1_name=c1.name,
            cleaner2_id=c2.user_id,
            cleaner2_name=c2.name,
            fine_each=50
        )
        self.assertEqual(await db.get_user_unpaid_total(c1.user_id), 50)

        # Mark paid for cleaner 1
        count = await db.mark_fines_paid(c1.user_id, admin_id=config.ADMIN_ID)
        self.assertEqual(count, 1)
        self.assertEqual(await db.get_user_unpaid_total(c1.user_id), 0)
        self.assertEqual(await db.get_user_unpaid_total(c2.user_id), 50)

    async def test_photo_and_approval_workflow(self):
        """Test photo submission, approval, and rejection state transitions."""
        await db.init_db()

        w_info = config.get_week_info(config.START_BASE_MONDAY)
        c1 = w_info.cleaner1
        c2 = w_info.cleaner2

        week = await db.get_or_create_week(
            w_info.week_start, w_info.week_number, c1.user_id, c2.user_id, [config.MEMBERS[2].user_id]
        )

        # Initial photo status should be 'none'
        self.assertEqual(week["photo_status"], "none")

        # Cleaner submits photo
        await db.submit_photo(w_info.week_start, file_id="photo_12345", user_id=c1.user_id)
        week_after_photo = await db.get_week(w_info.week_start)
        self.assertEqual(week_after_photo["photo_status"], "submitted")
        self.assertEqual(week_after_photo["photo_file_id"], "photo_12345")

        # Approver approves photo
        await db.update_photo_status(w_info.week_start, status="approved", final_status="completed")
        week_approved = await db.get_week(w_info.week_start)
        self.assertEqual(week_approved["photo_status"], "approved")
        self.assertEqual(week_approved["final_status"], "completed")

    async def test_cleaner_friday_check_responses(self):
        """Test recording Friday check responses from cleaners."""
        await db.init_db()
        w_info = config.get_week_info(config.START_BASE_MONDAY)
        c1 = w_info.cleaner1
        c2 = w_info.cleaner2

        await db.get_or_create_week(
            w_info.week_start, w_info.week_number, c1.user_id, c2.user_id, [config.MEMBERS[2].user_id]
        )

        # Cleaner 1 answers yes, Cleaner 2 answers no
        await db.record_cleaner_response(w_info.week_start, c1.user_id, "yes")
        await db.record_cleaner_response(w_info.week_start, c2.user_id, "no")

        week = await db.get_week(w_info.week_start)
        self.assertEqual(week["cleaner1_response"], "yes")
        self.assertEqual(week["cleaner2_response"], "no")

    def test_member_lookup_helpers(self):
        """Verify is_member, get_member, and get_member_by_name."""
        self.assertTrue(config.is_member(111111111))
        self.assertFalse(config.is_member(999999999))

        m = config.get_member(111111111)
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "Bilisuma")

        # Case-insensitive name lookup
        m_name = config.get_member_by_name("bilisuma")
        self.assertIsNotNone(m_name)
        self.assertEqual(m_name.user_id, 111111111)

        m_name_upper = config.get_member_by_name("NAHOM")
        self.assertIsNotNone(m_name_upper)
        self.assertEqual(m_name_upper.name, "Nahom")

        self.assertIsNone(config.get_member_by_name("GhostResident"))

    def test_weekday_math_for_all_days(self):
        """Verify get_monday_of_week correctly identifies Monday for any day of the week."""
        # 2026-11-16 is Monday
        mon = date(2026, 11, 16)
        tue = date(2026, 11, 17)
        fri = date(2026, 11, 20)
        sat = date(2026, 11, 21)
        sun = date(2026, 11, 22)

        for d in (mon, tue, fri, sat, sun):
            calculated_mon = config.get_monday_of_week(d)
            self.assertEqual(calculated_mon, mon, f"Failed for {d.strftime('%A')}")

if __name__ == "__main__":
    unittest.main()
