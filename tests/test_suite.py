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
        Verify rotation starting next Monday, November 23, 2026:
        - Before start (e.g. Thu Nov 19): week_number <= 0 (Duty not started yet)
        - Week 1 (Nov 23 - Nov 27): Pair 1 (Bilisuma & Nahom) [Announced Mon Nov 23, Cleaners Fri Nov 27]
        - Week 2 (Nov 30 - Dec 04): Pair 2 (Hassan & Mintesnot)
        - Week 3 (Dec 07 - Dec 11): Pair 3 (Dawit & Zeftret)
        - Week 4 (Dec 14 - Dec 18): Pair 4 (Ahmed & Yonatan)
        - Week 5 (Dec 21 - Dec 25): Pair 1 (Bilisuma & Nahom) - cycles back!
        """
        start = config.START_DATE_OBJ
        self.assertEqual(start, date(2026, 11, 23))
        self.assertEqual(start.weekday(), 0, "2026-11-23 is a Monday")
        self.assertEqual(config.START_BASE_MONDAY, date(2026, 11, 23))

        # Before start date: Thursday Nov 19 - cleaner pair is Pair 1 (Bilisuma & Nahom)
        w_before = config.get_week_info(date(2026, 11, 19))
        self.assertLessEqual(w_before.week_number, 0, "Nov 19 is before duty starts")
        self.assertEqual(w_before.cleaner1.name, "Bilisuma")
        self.assertEqual(w_before.cleaner2.name, "Nahom")

        # Week 1 (Monday Nov 23) - Announced Monday Nov 23 with Pair 1, cleaning Friday Nov 27
        w1 = config.get_week_info(date(2026, 11, 23))
        self.assertEqual(w1.week_number, 1)
        self.assertEqual(w1.cleaner1.name, "Bilisuma")
        self.assertEqual(w1.cleaner2.name, "Nahom")
        self.assertEqual(w1.week_start, "2026-11-23")
        self.assertEqual(w1.friday_date, date(2026, 11, 27))

        # Mid-week of week 1 (Wednesday Nov 25)
        w1_wed = config.get_week_info(date(2026, 11, 25))
        self.assertEqual(w1_wed.week_number, 1)
        self.assertEqual(w1_wed.cleaner1.name, "Bilisuma")

        # Friday of week 1 (Friday Nov 27) - cleaning day
        w1_fri = config.get_week_info(date(2026, 11, 27))
        self.assertEqual(w1_fri.week_number, 1)
        self.assertEqual(w1_fri.friday_date, date(2026, 11, 27))

        # Week 2 (Monday Nov 30) - Hassan & Mintesnot
        w2 = config.get_week_info(date(2026, 11, 30))
        self.assertEqual(w2.week_number, 2)
        self.assertEqual(w2.cleaner1.name, "Hassan")
        self.assertEqual(w2.cleaner2.name, "Mintesnot")
        self.assertEqual(w2.week_start, "2026-11-30")
        self.assertEqual(w2.friday_date, date(2026, 12, 4))

        # Week 3 (Monday Dec 7) - Dawit & Zeftret
        w3 = config.get_week_info(date(2026, 12, 7))
        self.assertEqual(w3.week_number, 3)
        self.assertEqual(w3.cleaner1.name, "Dawit")
        self.assertEqual(w3.cleaner2.name, "Zeftret")

        # Week 4 (Monday Dec 14) - Last pair in cycle: Ahmed & Yonatan
        w4 = config.get_week_info(date(2026, 12, 14))
        self.assertEqual(w4.week_number, 4)
        self.assertEqual(w4.cleaner1.name, "Ahmed")
        self.assertEqual(w4.cleaner2.name, "Yonatan")

        # Week 5 (Monday Dec 21 - cycle back to pair 1)
        w5 = config.get_week_info(date(2026, 12, 21))
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
        self.assertTrue(config.is_member(1359460823), "Hasan's real Telegram ID")
        self.assertFalse(config.is_member(999999999))

        m = config.get_member(111111111)
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "Bilisuma")

        # Hasan by ID
        m_hasan = config.get_member(1359460823)
        self.assertIsNotNone(m_hasan)
        self.assertEqual(m_hasan.name, "Hassan")

        # Case-insensitive name lookup
        m_name = config.get_member_by_name("bilisuma")
        self.assertIsNotNone(m_name)
        self.assertEqual(m_name.user_id, 111111111)

        # Lookup with single 's' "Hasan" alias
        m_hasan_alias = config.get_member_by_name("hasan")
        self.assertIsNotNone(m_hasan_alias)
        self.assertEqual(m_hasan_alias.user_id, 1359460823)

        m_name_upper = config.get_member_by_name("NAHOM")
        self.assertIsNotNone(m_name_upper)
        self.assertEqual(m_name_upper.name, "Nahom")

        self.assertIsNone(config.get_member_by_name("GhostResident"))

    async def test_update_member_id_and_db_propagation(self):
        """Test dynamic updating of member user_id in config and db."""
        await db.init_db()

        # Temporarily update Bilisuma's placeholder ID
        orig_id = 111111111
        temp_id = 999111999

        # Record a test fine with orig_id
        await db.create_fines_for_week(
            week_start="2026-11-23",
            cleaner1_id=orig_id,
            cleaner1_name="Bilisuma",
            cleaner2_id=1758752115,
            cleaner2_name="Nahom",
            fine_each=50
        )
        self.assertEqual(await db.get_user_unpaid_total(orig_id), 50)

        # Update ID dynamically
        success = config.update_member_id("Bilisuma", temp_id)
        self.assertTrue(success)
        self.assertEqual(config.get_member(temp_id).name, "Bilisuma")

        # Propagate to database
        await db.update_member_id_in_db(old_id=orig_id, new_id=temp_id)
        self.assertEqual(await db.get_user_unpaid_total(orig_id), 0)
        self.assertEqual(await db.get_user_unpaid_total(temp_id), 50)

        # Revert back to original ID
        config.update_member_id("Bilisuma", orig_id)
        self.assertEqual(config.get_member(orig_id).name, "Bilisuma")

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

    async def test_interactive_menu_keyboards_and_navigation(self):
        """Verify interactive start buttons, back/forward navigation, and content generators."""
        try:
            import telegram
        except ImportError:
            self.skipTest("python-telegram-bot is not installed in the local environment")

        await db.init_db()
        from handlers import commands

        # 1. Main menu welcome text mentions the Nov 23 announcement and cleaners
        menu_text = commands.get_main_menu_text("Nahom")
        self.assertIn("Nahom", menu_text)
        self.assertIn("Bilisuma & Nahom", menu_text)

        # 2. Main menu inline keyboard has 6 functional buttons
        main_kb = commands.get_main_menu_inline_keyboard()
        button_callbacks = [btn.callback_data for row in main_kb.inline_keyboard for btn in row]
        self.assertIn("menu_status", button_callbacks)
        self.assertIn("menu_schedule", button_callbacks)
        self.assertIn("menu_done", button_callbacks)
        self.assertIn("menu_fines", button_callbacks)
        self.assertIn("menu_history", button_callbacks)
        self.assertIn("menu_help", button_callbacks)

        # 3. Mobile reply keyboard has quick jump options
        reply_kb = commands.get_reply_keyboard()
        reply_texts = [btn.text if hasattr(btn, "text") else btn for row in reply_kb.keyboard for btn in row]
        self.assertIn("Status", reply_texts)
        self.assertIn("Schedule", reply_texts)
        self.assertIn("Fines", reply_texts)
        self.assertIn("Main Menu", reply_texts)

        # 4. Status view has Forward (Schedule) and Back (Menu)
        status_text, status_kb = await commands.get_status_content(111111111)
        status_callbacks = [btn.callback_data for row in status_kb.inline_keyboard for btn in row]
        self.assertIn("menu_schedule", status_callbacks, "Forward to schedule")
        self.assertIn("menu_home", status_callbacks, "Back to main menu")

        # 5. Schedule view has Prev (Status), Next (Fines), and Back (Menu)
        sched_text, sched_kb = commands.get_schedule_content()
        sched_callbacks = [btn.callback_data for row in sched_kb.inline_keyboard for btn in row]
        self.assertIn("menu_status", sched_callbacks, "Prev back to status")
        self.assertIn("menu_fines", sched_callbacks, "Next forward to fines")
        self.assertIn("menu_home", sched_callbacks, "Back to main menu")

        # 6. Fines view has Prev (Schedule), Next (History), and Back (Menu)
        fines_text, fines_kb = await commands.get_fines_content()
        fines_callbacks = [btn.callback_data for row in fines_kb.inline_keyboard for btn in row]
        self.assertIn("menu_schedule", fines_callbacks)
        self.assertIn("menu_history", fines_callbacks)
        self.assertIn("menu_home", fines_callbacks)

        # 7. History view has Prev (Fines), Next (Help), and Back (Menu)
        hist_text, hist_kb = await commands.get_history_content()
        hist_callbacks = [btn.callback_data for row in hist_kb.inline_keyboard for btn in row]
        self.assertIn("menu_fines", hist_callbacks)
        self.assertIn("menu_help", hist_callbacks)
        self.assertIn("menu_home", hist_callbacks)

        # 8. Help view has Prev (History), Next (Status), and Back (Menu)
        help_text, help_kb = commands.get_help_content()
        help_callbacks = [btn.callback_data for row in help_kb.inline_keyboard for btn in row]
        self.assertIn("menu_history", help_callbacks)
        self.assertIn("menu_status", help_callbacks)
        self.assertIn("menu_home", help_callbacks)

if __name__ == "__main__":
    unittest.main()
