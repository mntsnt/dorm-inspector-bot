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
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.assertTrue(config.is_member(7220854785))
        self.assertTrue(config.is_member(1359460823), "Hasan's real Telegram ID")
        self.assertFalse(config.is_member(999999999))

        m = config.get_member(7220854785)
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "Bilisuma")

        # Hasan by ID
        m_hasan = config.get_member(1359460823)
        self.assertIsNotNone(m_hasan)
        self.assertEqual(m_hasan.name, "Hassan")

        # Case-insensitive name lookup
        m_name = config.get_member_by_name("bilisuma")
        self.assertIsNotNone(m_name)
        self.assertEqual(m_name.user_id, 7220854785)

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

        # Temporarily update Bilisuma's ID
        orig_id = 7220854785
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

    async def test_connection_status_and_settings(self):
        """Verify tracking of connected members and idempotent settings."""
        await db.init_db()

        # Default dorm has all 8 confirmed roommates pre-connected from start
        count, total, conn_list, pend_list = await db.get_connection_status()
        self.assertEqual(count, 8)
        self.assertEqual(total, 8)
        self.assertEqual(len(conn_list), 8)
        self.assertEqual(len(pend_list), 0)

        # For a new onboarding dorm, connection status tracks dynamically
        test_dorm_id = -100888999777
        await db.create_or_update_dorm(test_dorm_id, admin_id=9001, dorm_name="New Dorm")
        await db.add_dorm_member(test_dorm_id, user_id=9001, name="Student1", chat_id=None)
        await db.add_dorm_member(test_dorm_id, user_id=9002, name="Student2", chat_id=None)

        d_count, d_total, d_conn, d_pend = await db.get_connection_status(test_dorm_id)
        self.assertEqual(d_count, 0)
        self.assertEqual(d_total, 2)
        self.assertEqual(len(d_pend), 2)

        # Student1 connects
        await db.upsert_user(9001, "Student1", chat_id=9001)
        d_count, d_total, d_conn, d_pend = await db.get_connection_status(test_dorm_id)
        self.assertEqual(d_count, 1)
        self.assertEqual(d_total, 2)
        self.assertEqual(d_conn[0].user_id, 9001)

        # Settings key-value test
        self.assertIsNone(await db.get_setting("test_setting_key"))
        await db.set_setting("test_setting_key", "1")
        self.assertEqual(await db.get_setting("test_setting_key"), "1")

    async def test_multi_tenant_dorm_isolation(self):
        """Verify that multiple dorms operate completely independently without leaking data."""
        await db.init_db()

        # Create Dorm A (4 students, 2 pairs)
        dorm_a_id = -1001111111111
        await db.create_or_update_dorm(
            dorm_id=dorm_a_id,
            admin_id=101,
            dorm_name="Block 1 Room 101",
            fine_amount=50,
            start_date="2026-11-23"
        )
        await db.add_dorm_member(dorm_a_id, user_id=101, name="Abebe", is_admin=1)
        await db.add_dorm_member(dorm_a_id, user_id=102, name="Kebede")
        await db.add_dorm_member(dorm_a_id, user_id=103, name="Almaz")
        await db.add_dorm_member(dorm_a_id, user_id=104, name="Tigist")

        pairs_a = await db.auto_generate_dorm_pairs(dorm_a_id)
        self.assertEqual(len(pairs_a), 2)

        # Create Dorm B (6 students, 3 pairs)
        dorm_b_id = -1002222222222
        await db.create_or_update_dorm(
            dorm_id=dorm_b_id,
            admin_id=201,
            dorm_name="Block 2 Room 202",
            fine_amount=100,
            start_date="2026-11-23"
        )
        for i in range(1, 7):
            await db.add_dorm_member(dorm_b_id, user_id=200 + i, name=f"Student_{i}")

        pairs_b = await db.auto_generate_dorm_pairs(dorm_b_id)
        self.assertEqual(len(pairs_b), 3)

        # Verify members list isolation
        members_a = await db.get_dorm_members(dorm_a_id)
        members_b = await db.get_dorm_members(dorm_b_id)
        self.assertEqual(len(members_a), 4)
        self.assertEqual(len(members_b), 6)

        # Verify fines isolation
        await db.create_fines_for_week(
            week_start="2026-11-23",
            cleaner1_id=101,
            cleaner1_name="Abebe",
            cleaner2_id=102,
            cleaner2_name="Kebede",
            fine_each=25,
            dorm_id=dorm_a_id
        )
        fines_a = await db.get_unpaid_fines_summary(dorm_id=dorm_a_id)
        fines_b = await db.get_unpaid_fines_summary(dorm_id=dorm_b_id)
        self.assertEqual(len(fines_a), 2)
        self.assertEqual(len(fines_b), 0, "Dorm B must not see Dorm A fines")

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

        # 2. Main menu inline keyboard: cleaner has submit cleaning, non-cleaner does not
        cleaner_kb = commands.get_main_menu_inline_keyboard(is_cleaner=True)
        button_callbacks = [btn.callback_data for row in cleaner_kb.inline_keyboard for btn in row]
        self.assertIn("menu_status", button_callbacks)
        self.assertIn("menu_schedule", button_callbacks)
        self.assertIn("menu_done", button_callbacks)
        self.assertIn("menu_fines", button_callbacks)
        self.assertIn("menu_history", button_callbacks)
        self.assertIn("menu_help", button_callbacks)

        non_cleaner_kb = commands.get_main_menu_inline_keyboard(is_cleaner=False)
        non_cleaner_callbacks = [btn.callback_data for row in non_cleaner_kb.inline_keyboard for btn in row]
        self.assertNotIn("menu_done", non_cleaner_callbacks)

        # 3. Mobile reply keyboard has quick jump options and is persistent
        cleaner_reply_kb = commands.get_reply_keyboard(is_cleaner=True)
        self.assertTrue(cleaner_reply_kb.is_persistent)
        cleaner_reply_texts = [btn.text if hasattr(btn, "text") else btn for row in cleaner_reply_kb.keyboard for btn in row]
        self.assertIn("/submit", cleaner_reply_texts)

        reply_kb = commands.get_reply_keyboard(is_cleaner=False)
        self.assertTrue(reply_kb.is_persistent)
        reply_texts = [btn.text if hasattr(btn, "text") else btn for row in reply_kb.keyboard for btn in row]
        self.assertIn("/status", reply_texts)
        self.assertIn("/schedule", reply_texts)
        self.assertIn("/fines", reply_texts)
        self.assertIn("/menu", reply_texts)
        self.assertNotIn("/submit", reply_texts)

        # 4. Status view has Forward (Schedule) and Back (Menu)
        status_text, status_kb = await commands.get_status_content(7220854785)
        status_callbacks = [btn.callback_data for row in status_kb.inline_keyboard for btn in row]
        self.assertIn("menu_schedule", status_callbacks, "Forward to schedule")
        self.assertIn("menu_home", status_callbacks, "Back to main menu")

        # 5. Schedule view has Prev (Status), Next (Fines), and Back (Menu)
        sched_text, sched_kb = await commands.get_schedule_content()
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

    async def test_multi_dorm_scheduler_flow(self):
        """Verify scheduler calculations and isolation across distinct dorms."""
        try:
            import telegram
        except ImportError:
            self.skipTest("python-telegram-bot is not installed in the local environment")

        await db.init_db()
        from scheduler import get_dorm_week_info

        # Create two distinct dorms
        dorm_eng = await db.create_or_update_dorm(
            dorm_id=-1001111111,
            admin_id=901,
            dorm_name="Engineering Dorm",
            fine_amount=60,
            start_date="2026-11-23"
        )
        dorm_med = await db.create_or_update_dorm(
            dorm_id=-1002222222,
            admin_id=905,
            dorm_name="Medical Dorm",
            fine_amount=100,
            start_date="2026-11-30"  # Starts a week later
        )

        # Set up members and pairs for Engineering Dorm (4 members, 2 pairs)
        await db.add_dorm_member(-1001111111, 901, "Eng_Abebe")
        await db.add_dorm_member(-1001111111, 902, "Eng_Kebede")
        await db.add_dorm_member(-1001111111, 903, "Eng_Almaz")
        await db.add_dorm_member(-1001111111, 904, "Eng_Tigist")
        await db.set_dorm_pairs(-1001111111, [(901, 902), (903, 904)])

        # Set up members and pairs for Medical Dorm (4 members, 2 pairs)
        await db.add_dorm_member(-1002222222, 905, "Med_Sara")
        await db.add_dorm_member(-1002222222, 906, "Med_Hanna")
        await db.add_dorm_member(-1002222222, 907, "Med_Blen")
        await db.add_dorm_member(-1002222222, 908, "Med_Eden")
        await db.set_dorm_pairs(-1002222222, [(905, 906), (907, 908)])

        # Test week info calculation on 2026-11-23
        eng_pairs = await db.get_dorm_pairs(-1001111111)
        eng_members = {m["user_id"]: m["name"] for m in await db.get_dorm_members(-1001111111)}
        w_eng = get_dorm_week_info(dorm_eng, eng_pairs, eng_members, target_date=date(2026, 11, 23))

        self.assertIsNotNone(w_eng)
        self.assertEqual(w_eng.week_number, 1)
        self.assertEqual(w_eng.cleaner1_name, "Eng_Abebe")
        self.assertEqual(w_eng.cleaner2_name, "Eng_Kebede")

        # Medical Dorm starts 2026-11-30, so on 2026-11-23 week_number is 0 (not started)
        med_pairs = await db.get_dorm_pairs(-1002222222)
        med_members = {m["user_id"]: m["name"] for m in await db.get_dorm_members(-1002222222)}
        w_med = get_dorm_week_info(dorm_med, med_pairs, med_members, target_date=date(2026, 11, 23))
        self.assertLessEqual(w_med.week_number, 0)

        # On 2026-11-30, Medical Dorm starts Week 1 (Med_Sara & Med_Hanna)
        w_med_start = get_dorm_week_info(dorm_med, med_pairs, med_members, target_date=date(2026, 11, 30))
        self.assertEqual(w_med_start.week_number, 1)
        self.assertEqual(w_med_start.cleaner1_name, "Med_Sara")
        self.assertEqual(w_med_start.cleaner2_name, "Med_Hanna")

        # And Engineering Dorm on 2026-11-30 is on Week 2 (Eng_Almaz & Eng_Tigist)
        w_eng_w2 = get_dorm_week_info(dorm_eng, eng_pairs, eng_members, target_date=date(2026, 11, 30))
        self.assertEqual(w_eng_w2.week_number, 2)
        self.assertEqual(w_eng_w2.cleaner1_name, "Eng_Almaz")
        self.assertEqual(w_eng_w2.cleaner2_name, "Eng_Tigist")

    async def test_tsidat_token_system(self):
        """Verify adding Tsidat tokens, querying balances, and leaderboard ordering."""
        await db.init_db()
        test_dorm_id = -1005555555
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=101, dorm_name="Tsidat Test Dorm")
        await db.add_dorm_member(test_dorm_id, 101, "Member_A")
        await db.add_dorm_member(test_dorm_id, 102, "Member_B")
        await db.add_dorm_member(test_dorm_id, 103, "Member_C")

        # Initial balances should be 0
        bal_a = await db.get_tsidat_balance(test_dorm_id, 101)
        self.assertEqual(bal_a, 0)

        # Award tokens
        new_bal_a = await db.add_tsidat_tokens(test_dorm_id, 101, 25)
        self.assertEqual(new_bal_a, 25)

        new_bal_b = await db.add_tsidat_tokens(test_dorm_id, 102, 15)
        self.assertEqual(new_bal_b, 15)

        # Add more tokens to A
        updated_bal_a = await db.add_tsidat_tokens(test_dorm_id, 101, 5)
        self.assertEqual(updated_bal_a, 30)

        # Verify leaderboard ranking
        leaderboard = await db.get_tsidat_leaderboard(test_dorm_id)
        self.assertEqual(len(leaderboard), 3)
        self.assertEqual(leaderboard[0]["user_id"], 101)
        self.assertEqual(leaderboard[0]["tsidat_balance"], 30)
        self.assertEqual(leaderboard[1]["user_id"], 102)
        self.assertEqual(leaderboard[1]["tsidat_balance"], 15)
        self.assertEqual(leaderboard[2]["user_id"], 103)
        self.assertEqual(leaderboard[2]["tsidat_balance"], 0)

    async def test_five_star_inspection_review_persistence(self):
        """Verify rating (1-5 stars) and review text storage in weeks table and history."""
        await db.init_db()
        test_dorm_id = -1007777777
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=201, dorm_name="Review Test Dorm")
        await db.add_dorm_member(test_dorm_id, 201, "Reviewer")
        await db.add_dorm_member(test_dorm_id, 202, "Cleaner_1")
        await db.add_dorm_member(test_dorm_id, 203, "Cleaner_2")

        week_record = await db.get_or_create_week(
            week_start="2026-11-23",
            week_number=1,
            cleaner1_id=202,
            cleaner2_id=203,
            eligible_approver_ids=[201],
            dorm_id=test_dorm_id
        )
        self.assertIsNotNone(week_record)

        # Update with 5-star rating and review comment
        await db.update_photo_status(
            week_start="2026-11-23",
            status="approved",
            final_status="completed",
            dorm_id=test_dorm_id,
            rating=5,
            review_text="Sparkling clean! 5-star hotel standard. 🌟✨"
        )

        updated = await db.get_week("2026-11-23", dorm_id=test_dorm_id)
        self.assertEqual(updated["final_status"], "completed")
        self.assertEqual(updated["photo_status"], "approved")
        self.assertEqual(updated["rating"], 5)
        self.assertEqual(updated["review_text"], "Sparkling clean! 5-star hotel standard. 🌟✨")

        # History should include the rating
        history = await db.get_history(dorm_id=test_dorm_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["rating"], 5)

    async def test_tsidat_content_generator(self):
        """Verify get_tsidat_content produces formatted markdown with leaderboard."""
        from handlers import commands
        await db.init_db()
        test_dorm_id = -1008888888
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=301, dorm_name="Leaderboard Dorm")
        await db.add_dorm_member(test_dorm_id, 301, "Top_Cleaner")
        await db.add_tsidat_tokens(test_dorm_id, 301, 50)

        content, kb = await commands.get_tsidat_content(dorm_id=test_dorm_id, user_id=301)
        self.assertIn("Tsidat Leaderboard", content)
        self.assertIn("Top_Cleaner", content)
        self.assertIn("50 Tsidat", content)
        self.assertIn("Earn Tsidat tokens", content)

    async def test_spin_command_execution(self):
        """Verify spin_command randomly picks a registered roommate and supports custom chores."""
        from handlers import commands
        from unittest.mock import AsyncMock, MagicMock

        await db.init_db()
        test_dorm_id = -1009999999
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=401, dorm_name="Roulette Dorm")
        await db.add_dorm_member(test_dorm_id, 401, "Spin_User_A")
        await db.add_dorm_member(test_dorm_id, 402, "Spin_User_B")

        # Mock update and context
        update = MagicMock()
        update.effective_user.id = 401
        update.effective_user.first_name = "Spin_User_A"
        update.effective_chat.id = test_dorm_id
        update.effective_chat.type = "supergroup"
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["wash", "dishes"]

        await commands.spin_command(update, context)

        update.effective_message.reply_text.assert_called_once()
        sent_text = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Dorm Roulette", sent_text)
        self.assertIn("wash dishes", sent_text)
        self.assertTrue("Spin_User_A" in sent_text or "Spin_User_B" in sent_text)

        # Verify spin without arguments prompts the spinner to insert a reason
        update.effective_message.reply_text.reset_mock()
        context.args = []
        await commands.spin_command(update, context)
        update.effective_message.reply_text.assert_called_once()
        empty_text = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Please provide a reason or task for the spin", empty_text)

    async def test_ai_service_context_and_detection(self):
        """Verify AI provider detection and dorm context building."""
        from services import ai_service
        import os

        # Provider detection
        orig_gemini = os.environ.get("GEMINI_API_KEY")
        orig_openai = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["GEMINI_API_KEY"] = "AIzaSyFakeKey123"
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertEqual(ai_service.get_active_provider(), "gemini")

            os.environ.pop("GEMINI_API_KEY", None)
            os.environ["OPENAI_API_KEY"] = "sk-fakeKey123"
            self.assertEqual(ai_service.get_active_provider(), "openai")

            os.environ.pop("OPENAI_API_KEY", None)
            self.assertIsNone(ai_service.get_active_provider())
        finally:
            if orig_gemini:
                os.environ["GEMINI_API_KEY"] = orig_gemini
            elif "GEMINI_API_KEY" in os.environ:
                os.environ.pop("GEMINI_API_KEY")
            if orig_openai:
                os.environ["OPENAI_API_KEY"] = orig_openai
            elif "OPENAI_API_KEY" in os.environ:
                os.environ.pop("OPENAI_API_KEY")

        # Context builder
        await db.init_db()
        test_dorm_id = -1004443332
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=501, dorm_name="AI Test Dorm", fine_amount=80)
        await db.add_dorm_member(test_dorm_id, 501, "AI_User_A")
        await db.add_dorm_member(test_dorm_id, 502, "AI_User_B")
        await db.set_dorm_pairs(test_dorm_id, [(501, 502)])

        context_str = await ai_service.build_dorm_context(test_dorm_id)
        self.assertIn("AI Test Dorm", context_str)
        self.assertIn("80 ETB", context_str)
        self.assertIn("Tsidat Tokens", context_str)

    async def test_ai_mention_handler_trigger(self):
        """Verify AI mention handler triggers when bot is tagged and ignores non-mentions."""
        from handlers import ai_chat
        from unittest.mock import AsyncMock, MagicMock, patch

        await db.init_db()
        update = MagicMock()
        update.effective_user.id = 601
        update.effective_user.first_name = "Teshome"
        update.effective_chat.id = -1004443332
        update.effective_chat.type = "supergroup"
        update.effective_message.message_id = 99
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.username = "DormInspectorBot"
        context.bot.send_chat_action = AsyncMock()

        # Case 1: Random message without tag or reply -> Ignored
        update.effective_message.text = "Hey roommates what's up"
        update.effective_message.reply_to_message = None
        await ai_chat.handle_ai_mention(update, context)
        update.effective_message.reply_text.assert_not_called()

        # Case 2: Mentioning @DormInspectorBot -> Triggers AI
        update.effective_message.text = "@DormInspectorBot who is cleaning this week?"
        with patch("handlers.ai_chat.ask_ai", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "Pair 1 is cleaning this week!"
            await ai_chat.handle_ai_mention(update, context)
            mock_ask.assert_called_once()
            update.effective_message.reply_text.assert_called_once()
            self.assertIn("Pair 1 is cleaning", update.effective_message.reply_text.call_args[0][0])

        # Case 3: Mentioning @inspector -> Triggers AI as well
        update.effective_message.reply_text.reset_mock()
        update.effective_message.text = "@inspector who is the approver?"
        with patch("handlers.ai_chat.ask_ai", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "Approver is assigned!"
            await ai_chat.handle_ai_mention(update, context)
            mock_ask.assert_called_once()
            # Verify @inspector was cleaned from prompt
            self.assertEqual(mock_ask.call_args[0][0], "who is the approver?")
            update.effective_message.reply_text.assert_called_once()
            self.assertIn("Approver is assigned", update.effective_message.reply_text.call_args[0][0])

    async def test_per_dorm_ai_key_isolation(self):
        """Verify strict per-dorm AI key isolation (secondary dorms cannot use primary dorm key)."""
        from services import ai_service
        from handlers import admin
        from unittest.mock import AsyncMock, MagicMock, patch
        import os

        await db.init_db()

        # Set a global key simulating user's primary dorm key
        orig_gemini = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "AIzaSyPrimaryAdminKey"

        try:
            primary_dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
            secondary_dorm_id = -100999888777

            await db.create_or_update_dorm(dorm_id=secondary_dorm_id, admin_id=777, dorm_name="Secondary Dorm")

            # 1. Secondary dorm has NO key configured -> Must be refused and instruct them to set a key
            with patch("services.ai_service.query_gemini", new_callable=AsyncMock) as mock_query:
                resp = await ai_service.ask_ai("Hello", dorm_id=secondary_dorm_id)
                self.assertIn("AI is not enabled for this dorm yet", resp)
                mock_query.assert_not_called()

            # 2. Secondary dorm admin configures their own key
            await db.set_dorm_ai_key(secondary_dorm_id, "AIzaSySecondaryKey999")
            saved_key = await db.get_dorm_ai_key(secondary_dorm_id)
            self.assertEqual(saved_key, "AIzaSySecondaryKey999")

            with patch("services.ai_service.query_gemini", new_callable=AsyncMock) as mock_query:
                mock_query.return_value = "Hello from secondary dorm AI!"
                resp = await ai_service.ask_ai("Hello", dorm_id=secondary_dorm_id)
                mock_query.assert_called_once()
                # Verify that it called with the secondary dorm's own key, NOT the primary key!
                self.assertEqual(mock_query.call_args[0][0], "AIzaSySecondaryKey999")
                self.assertEqual(resp, "Hello from secondary dorm AI!")

            # 3. Test non-admin rejection on set_ai_key_command
            update = MagicMock()
            update.effective_user.id = 888  # Non-admin
            update.effective_chat.id = secondary_dorm_id
            update.effective_chat.type = "supergroup"
            update.effective_message.reply_text = AsyncMock()

            context = MagicMock()
            context.args = ["AIzaSyHackerKey"]

            await admin.set_ai_key_command(update, context)
            update.effective_message.reply_text.assert_called_once_with("Only the dorm admin can configure the AI API key.")

        finally:
            if orig_gemini:
                os.environ["GEMINI_API_KEY"] = orig_gemini
            elif "GEMINI_API_KEY" in os.environ:
                os.environ.pop("GEMINI_API_KEY")

    async def test_one_week_duty_swap_isolation(self):
        """Verify peer duty swap is temporary (affects only target week; dorm_pairs rotation untouched)."""
        await db.init_db()
        test_dorm_id = -1003332221
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=1001, dorm_name="Swap Test Dorm")
        await db.add_dorm_member(test_dorm_id, 1001, "Swap_Cleaner_A")
        await db.add_dorm_member(test_dorm_id, 1002, "Swap_Cleaner_B")
        await db.add_dorm_member(test_dorm_id, 1003, "Swap_Roommate_C")
        await db.add_dorm_member(test_dorm_id, 1004, "Swap_Roommate_D")

        # Set persistent rotation pairs
        original_pairs = [(1001, 1002), (1003, 1004)]
        await db.set_dorm_pairs(test_dorm_id, original_pairs)

        # Create Week 1 duty record
        week_1 = await db.get_or_create_week(
            week_start="2026-11-23",
            week_number=1,
            cleaner1_id=1001,
            cleaner2_id=1002,
            eligible_approver_ids=[1003, 1004],
            dorm_id=test_dorm_id
        )
        self.assertEqual(week_1["cleaner1_id"], 1001)
        self.assertEqual(week_1["cleaner2_id"], 1002)

        # Cleaner A (1001) requests swap with Roommate C (1003) for an exam
        swap_id = await db.create_swap_request(test_dorm_id, "2026-11-23", requester_id=1001, target_id=1003)
        self.assertIsNotNone(swap_id)

        # Execute the swap
        success, msg = await db.execute_swap(swap_id)
        self.assertTrue(success)

        # 1. Verify Week 1 cleaners in weeks table are now 1003 & 1002
        updated_w1 = await db.get_week("2026-11-23", dorm_id=test_dorm_id)
        self.assertEqual(updated_w1["cleaner1_id"], 1003)
        self.assertEqual(updated_w1["cleaner2_id"], 1002)

        # 2. CRUCIAL: Verify dorm_pairs is 100% UNTOUCHED (normal rotation pairs continue next cycle!)
        persisted_pairs = await db.get_dorm_pairs(test_dorm_id)
        pair_tuples = [(p["cleaner1_id"], p["cleaner2_id"]) for p in persisted_pairs]
        self.assertEqual(pair_tuples, original_pairs)

        # 3. Verify scheduler for future cycle (Week 5) still pairs Cleaner A (1001) with Cleaner B (1002)
        from scheduler import get_dorm_week_info
        members_map = {1001: "Swap_Cleaner_A", 1002: "Swap_Cleaner_B", 1003: "Swap_Roommate_C", 1004: "Swap_Roommate_D"}
        dorm_data = await db.get_dorm(test_dorm_id)
        w5_info = get_dorm_week_info(dorm_data, persisted_pairs, members_map, target_date=date(2026, 12, 21))
        self.assertEqual(w5_info.cleaner1_name, "Swap_Cleaner_A")
        self.assertEqual(w5_info.cleaner2_name, "Swap_Cleaner_B")

    async def test_dorm_roast_generation(self):
        """Verify generate_dorm_roast produces engaging, witty awards and roast text."""
        from services import ai_service
        await db.init_db()
        test_dorm_id = -1006667778
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=3001, dorm_name="Roast Dorm")
        await db.add_dorm_member(test_dorm_id, 3001, "Roast_User_1")
        await db.add_dorm_member(test_dorm_id, 3002, "Roast_User_2")

        roast_text = await ai_service.generate_dorm_roast(test_dorm_id)
        self.assertIn("Dorm Inspector Weekly Awards", roast_text)
        self.assertIn("Cleaners of the Week", roast_text)
        self.assertIn("Tsidat", roast_text)

    async def test_safe_edit_message_swallows_message_not_modified(self):
        """Verify _safe_edit_message gracefully swallows 'Message is not modified' error."""
        from handlers.approval import _safe_edit_message
        from telegram.error import BadRequest

        mock_query = MagicMock()
        mock_msg = MagicMock()
        mock_msg.photo = []
        mock_query.message = mock_msg
        mock_query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified: specified new message content and reply markup are exactly the same"))

        # Should execute without raising BadRequest
        await _safe_edit_message(mock_query, "Same text")
        mock_query.edit_message_text.assert_awaited_once()

    def test_parse_callback_dorm_and_week_formats(self):
        """Verify callback parser accurately decodes negative supergroup IDs across diverse prefixes."""
        from handlers.approval import _parse_callback_dorm_and_week as app_parse
        from handlers.cleaning import _parse_callback_dorm_and_week as clean_parse

        supergroup_id = -1002378440774
        week = "2026-11-23"

        # 4-part fconfirm
        d_id, w_str = app_parse(f"fconfirm_clean_{supergroup_id}_{week}")
        self.assertEqual(d_id, supergroup_id)
        self.assertEqual(w_str, week)

        d_id2, w_str2 = app_parse(f"fconfirm_notclean_{supergroup_id}_{week}")
        self.assertEqual(d_id2, supergroup_id)
        self.assertEqual(w_str2, week)

        # 3-part approve/reject/rate
        d_id3, w_str3 = app_parse(f"approve_{supergroup_id}_{week}")
        self.assertEqual(d_id3, supergroup_id)
        self.assertEqual(w_str3, week)

        # 4-part fcheck
        d_id4, w_str4 = clean_parse(f"fcheck_yes_{supergroup_id}_{week}")
        self.assertEqual(d_id4, supergroup_id)
        self.assertEqual(w_str4, week)

        # 3-part cleaned
        d_id5, w_str5 = clean_parse(f"cleaned_{supergroup_id}_{week}")
        self.assertEqual(d_id5, supergroup_id)
        self.assertEqual(w_str5, week)

    async def test_handle_cleaner_done_group_chat_redirect(self):
        """Verify cleaner done in group chat directs cleaner to DM with URL button."""
        from handlers.cleaning import handle_cleaner_done

        mock_update = MagicMock()
        mock_msg = MagicMock()
        mock_chat = MagicMock()
        mock_chat.type = "supergroup"
        mock_chat.id = -1002378440774
        mock_msg.chat = mock_chat
        mock_msg.reply_text = AsyncMock()
        mock_update.effective_message = mock_msg

        mock_context = MagicMock()
        mock_bot = MagicMock()
        mock_bot.get_me = AsyncMock(return_value=MagicMock(username="TestInspectorBot"))
        mock_context.bot = mock_bot

        await handle_cleaner_done(mock_update, mock_context, user_id=101)
        mock_msg.reply_text.assert_awaited_once()
        call_args = mock_msg.reply_text.call_args
        self.assertIn("Photo submissions are handled in private chat", call_args[0][0])
        self.assertIsNotNone(call_args[1].get("reply_markup"))

    async def test_schedule_content_date_fallback(self):
        """Verify get_schedule_content renders schedule correctly with fallback date handling."""
        from handlers.commands import get_schedule_content
        await db.init_db()

        test_dorm_id = -100999111
        await db.create_or_update_dorm(dorm_id=test_dorm_id, admin_id=5001, dorm_name="Fallback Dorm", start_date="invalid-date")
        await db.add_dorm_member(test_dorm_id, 5001, "Member_A")
        await db.add_dorm_member(test_dorm_id, 5002, "Member_B")
        await db.set_dorm_pairs(test_dorm_id, [(5001, 5002)])

        text, keyboard = await get_schedule_content(dorm_id=test_dorm_id, user_id=5001)
        self.assertIn("Fallback Dorm", text)
        self.assertIsNotNone(keyboard)

    async def test_gemini_model_fallback_on_deprecation(self):
        """Verify query_gemini falls back when encountering model deprecation errors."""
        from services import ai_service
        import httpx

        mock_responses = [
            # First attempt (e.g. gemini-3.8-flash) returns 400 deprecation
            MagicMock(status_code=400, text='{"error": {"message": "This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use models/gemini-3.8-flash"}}'),
            # Second attempt succeeds
            MagicMock(status_code=200, json=lambda: {
                "candidates": [{"content": {"parts": [{"text": "Floors look clean."}]}}]
            })
        ]

        with patch("httpx.AsyncClient.post", side_effect=mock_responses):
            result = await ai_service.query_gemini("fake-api-key", "How is the room?", "context", "Student")
            self.assertEqual(result, "Floors look clean.")

    def test_sanitize_ai_response_removes_emojis_and_symbol_spam(self):
        """Verify sanitize_ai_response strips all emojis, bold clutter, and repeated symbols."""
        from services.ai_service import sanitize_ai_response

        raw = "🤖 **Hello!** Clean the floor right now!! 🧹✨ Who is next???... Let me check."
        sanitized = sanitize_ai_response(raw)

        # No emojis
        self.assertNotIn("🤖", sanitized)
        self.assertNotIn("🧹", sanitized)
        self.assertNotIn("✨", sanitized)

        # No bold markdown markers
        self.assertNotIn("**", sanitized)

        # No multiple punctuation marks
        self.assertNotIn("!!", sanitized)
        self.assertNotIn("???", sanitized)
        self.assertNotIn("...", sanitized)

        # Clean natural phrasing
        self.assertEqual(sanitized, "Hello! Clean the floor right now! Who is next?. Let me check.")

    async def test_gemini_interactions_api_success(self):
        """Verify query_gemini successfully parses modern Interactions API response."""
        from services import ai_service
        mock_resp = MagicMock(status_code=200, json=lambda: {
            "object": "interaction",
            "model": "gemini-3.8-flash",
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "text": "Cleaners are Bilisuma and Nahom. Floor must be swept."
                }
            ]
        })
        with patch("httpx.AsyncClient.post", return_value=mock_resp):
            result = await ai_service.query_gemini("test-key", "Who cleans?", "context", "Bilisuma")
            self.assertEqual(result, "Cleaners are Bilisuma and Nahom. Floor must be swept.")

    async def test_gemini_interactions_api_content_structure(self):
        """Verify query_gemini parses Interactions API response with content array."""
        from services import ai_service
        mock_resp = MagicMock(status_code=200, json=lambda: {
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {"type": "text", "text": "Approver this week is Hassan."}
                    ]
                }
            ]
        })
        with patch("httpx.AsyncClient.post", return_value=mock_resp):
            result = await ai_service.query_gemini("test-key", "Who is approver?", "context", "Nahom")
            self.assertEqual(result, "Approver this week is Hassan.")

    def test_fallback_inspector_reply_accurate_facts(self):
        """Verify fallback_inspector_reply extracts accurate facts from context without emojis."""
        from services.ai_service import fallback_inspector_reply

        context = (
            "Dorm: Test Dorm\n"
            "Today's Date: Monday, Nov 23, 2026\n"
            "All Roommates: Bilisuma, Nahom, Hassan\n"
            "Current Week: Week 1 (Nov 23 – Nov 27)\n"
            "Current Duty: Cleaners: Bilisuma & Nahom | Approver: Hassan | Status: In progress (deadline Friday 8:00 PM)\n"
            "Unpaid Fines: No unpaid fines! Everyone is clean.\n"
            "Tsidat Tokens Leaderboard: 1. Nahom: 25 Tsidat\n"
        )

        reply1 = fallback_inspector_reply("who is cleaning this week?", context)
        self.assertIn("Bilisuma & Nahom", reply1)
        self.assertIn("Friday 8:00 PM", reply1)

        reply2 = fallback_inspector_reply("who is the approver?", context)
        self.assertIn("Hassan", reply2)

        reply3 = fallback_inspector_reply("any fines?", context)
        self.assertIn("No unpaid fines", reply3)

    async def test_ai_mention_natural_name(self):
        """Verify that typing '@inspector who is cleaning' triggers AI, while bare mention does not."""
        await db.init_db()
        from handlers import ai_chat
        update = MagicMock()
        update.effective_chat.id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        update.effective_chat.type = "supergroup"
        update.effective_user.id = 7220854785  # Bilisuma
        update.effective_user.first_name = "Bilisuma"
        update.effective_message.text = "inspector who is cleaning this week?"
        update.effective_message.message_id = 999
        update.effective_message.reply_to_message = None
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.username = "DormInspectorBot"

        # 1. Bare word "inspector" without @ should NOT trigger in group (user requirement)
        with patch("handlers.ai_chat.ask_ai", new_callable=AsyncMock) as mock_ask:
            await ai_chat.handle_ai_mention(update, context)
            mock_ask.assert_not_called()
            update.effective_message.reply_text.assert_not_called()

        # 2. Explicit @inspector mention MUST trigger AI
        update.effective_message.text = "@inspector who is cleaning this week?"
        with patch("handlers.ai_chat.ask_ai", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "Bilisuma and Nahom are on duty."
            await ai_chat.handle_ai_mention(update, context)
            mock_ask.assert_called_once()
            update.effective_message.reply_text.assert_called_once_with(
                "Bilisuma and Nahom are on duty.",
                reply_to_message_id=999
            )

    async def test_inspector_command_trigger(self):
        """Verify /inspector command triggers ask_ai."""
        await db.init_db()
        from handlers import ai_chat
        update = MagicMock()
        update.effective_chat.id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        update.effective_chat.type = "supergroup"
        update.effective_user.id = 7220854785
        update.effective_user.first_name = "Bilisuma"
        update.effective_message.message_id = 1000
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["who", "is", "approver?"]
        context.bot.username = "DormInspectorBot"

        with patch("handlers.ai_chat.ask_ai", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "Approver this week is Hassan."
            await ai_chat.inspector_command(update, context)
            mock_ask.assert_called_once_with(
                "who is approver?",
                dorm_id=update.effective_chat.id,
                user_name="Bilisuma",
                user_id=7220854785,
                chat_id=update.effective_chat.id
            )
            update.effective_message.reply_text.assert_called_once_with(
                "Approver this week is Hassan.",
                reply_to_message_id=1000
            )

    async def test_ai_dm_attaches_persistent_reply_keyboard(self):
        """Verify that chatting with AI in DM attaches the persistent reply keyboard."""
        await db.init_db()
        await db.seed_default_dorm()
        from handlers import ai_chat
        update = MagicMock()
        update.effective_chat.id = 7220854785
        update.effective_chat.type = "private"
        update.effective_user.id = 7220854785  # Bilisuma
        update.effective_user.first_name = "Bilisuma"
        update.effective_message.text = "who is cleaning?"
        update.effective_message.message_id = 1005
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.username = "DormInspectorBot"

        with patch("handlers.ai_chat.ask_ai", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "You are on cleaning duty with Nahom."
            await ai_chat.handle_ai_mention(update, context)
            mock_ask.assert_called_once()
            update.effective_message.reply_text.assert_called_once()
            call_kwargs = update.effective_message.reply_text.call_args[1]
            self.assertIn("reply_markup", call_kwargs)
            self.assertTrue(call_kwargs["reply_markup"].is_persistent)

    async def test_ai_role_context_resolution(self):
        """Verify build_dorm_context correctly injects role for cleaner, approver, and off-duty roommate."""
        await db.init_db()
        from services import ai_service
        test_dorm_id = -100444555666

        # Setup test dorm with 4 members: 2 cleaners, 1 approver, 1 off-duty
        cleaner1_id = 9101
        cleaner2_id = 9102
        approver_id = 9103
        offduty_id = 9104
        admin_id = 9105

        await db.create_or_update_dorm(
            dorm_id=test_dorm_id,
            admin_id=admin_id,
            dorm_name="Role Test Dorm",
            start_date="2026-11-23"
        )
        await db.add_dorm_member(test_dorm_id, cleaner1_id, "CleanerA")
        await db.add_dorm_member(test_dorm_id, cleaner2_id, "CleanerB")
        await db.add_dorm_member(test_dorm_id, approver_id, "ApproverC")
        await db.add_dorm_member(test_dorm_id, offduty_id, "OffDutyD")
        await db.set_dorm_pairs(test_dorm_id, [(cleaner1_id, cleaner2_id), (approver_id, offduty_id)])

        # Create week record with assigned approver
        await db.get_or_create_week(
            week_start="2026-11-23",
            week_number=1,
            cleaner1_id=cleaner1_id,
            cleaner2_id=cleaner2_id,
            eligible_approver_ids=[approver_id],
            dorm_id=test_dorm_id
        )

        # 1. Cleaner context
        ctx_cleaner = await ai_service.build_dorm_context(test_dorm_id, user_id=cleaner1_id)
        self.assertIn("Role: CLEANER ON DUTY", ctx_cleaner)
        self.assertIn("Partner: CleanerB", ctx_cleaner)
        self.assertIn("YOU ARE ON CLEANING DUTY THIS WEEK", ctx_cleaner)

        # 2. Approver context
        ctx_approver = await ai_service.build_dorm_context(test_dorm_id, user_id=approver_id)
        self.assertIn("Role: APPROVER (INSPECTOR)", ctx_approver)
        self.assertIn("YOU DO NOT CLEAN THE ROOM!", ctx_approver)

        # 3. Off-duty context
        ctx_offduty = await ai_service.build_dorm_context(test_dorm_id, user_id=offduty_id)
        self.assertIn("Role: OFF-DUTY ROOMMATE", ctx_offduty)
        self.assertIn("OFF DUTY. You do NOT have cleaning duty this week!", ctx_offduty)

    def test_fallback_inspector_reply_role_awareness(self):
        """Verify fallback_inspector_reply tailors responses by role and never tells off-duty/approver to clean."""
        from services.ai_service import fallback_inspector_reply

        cleaner_ctx = (
            "Dorm: Test Dorm\n"
            "Current Duty: Cleaners: Alice & Bob | Approver: Charlie | Status: In progress\n"
            "Role: CLEANER ON DUTY\n"
            "Partner: Bob\n"
        )
        approver_ctx = (
            "Dorm: Test Dorm\n"
            "Current Duty: Cleaners: Alice & Bob | Approver: Charlie | Status: In progress\n"
            "Role: APPROVER (INSPECTOR)\n"
        )
        offduty_ctx = (
            "Dorm: Test Dorm\n"
            "Current Duty: Cleaners: Alice & Bob | Approver: Charlie | Status: In progress\n"
            "Role: OFF-DUTY ROOMMATE\n"
        )

        # Asking 'am I cleaning?'
        self.assertIn("Yes, you are on cleaning duty with Bob", fallback_inspector_reply("am i cleaning?", cleaner_ctx))
        self.assertIn("No, you are the approver this week, not a cleaner", fallback_inspector_reply("am i cleaning?", approver_ctx))
        self.assertIn("No, you are off duty this week", fallback_inspector_reply("am i cleaning?", offduty_ctx))

        # Casual greeting
        self.assertIn("You are on duty with Bob this week", fallback_inspector_reply("hello inspector", cleaner_ctx))
        self.assertIn("You are the approver this week", fallback_inspector_reply("hello inspector", approver_ctx))
        self.assertIn("You are off duty this week", fallback_inspector_reply("hello inspector", offduty_ctx))

        # Duty inquiry
        self.assertIn("You are cleaning with Bob this week", fallback_inspector_reply("what is the duty?", cleaner_ctx))
        self.assertIn("Alice and Bob are cleaning this week. You are the approver", fallback_inspector_reply("what is the duty?", approver_ctx))
        self.assertIn("Alice and Bob are cleaning this week. You are off duty", fallback_inspector_reply("what is the duty?", offduty_ctx))

    async def test_dorm_code_db_operations(self):
        """Verify dorm code assignment, lookup, and custom updating."""
        await db.init_db()
        await db.seed_default_dorm()

        # 1. Default dorm should have a secure unguessable code
        active_dorms = await db.get_all_active_dorms()
        self.assertGreaterEqual(len(active_dorms), 1)
        expected_code = active_dorms[0]["dorm_code"]
        self.assertTrue(expected_code.startswith("DORM-"))
        self.assertGreaterEqual(len(expected_code), 10)

        default_dorm = await db.get_dorm_by_code(expected_code)
        self.assertIsNotNone(default_dorm)
        self.assertEqual(default_dorm["dorm_code"], expected_code)

        # 2. Case-insensitive and prefix-insensitive lookup
        lookup_lower = await db.get_dorm_by_code(expected_code.lower())
        self.assertIsNotNone(lookup_lower)
        self.assertEqual(lookup_lower["dorm_id"], default_dorm["dorm_id"])

        bare_code = expected_code.replace("DORM-", "")
        lookup_bare = await db.get_dorm_by_code(bare_code)
        self.assertIsNotNone(lookup_bare)
        self.assertEqual(lookup_bare["dorm_id"], default_dorm["dorm_id"])

        # 3. Create a secondary dorm and verify it receives a code
        sec_id = -100555666777
        sec_dorm = await db.create_or_update_dorm(sec_id, 9999, "Block B Room 12")
        self.assertTrue(sec_dorm["dorm_code"].startswith("DORM-"))

        # 4. Update custom dorm code
        ok, code = await db.set_dorm_code(sec_id, "BLOCK-B12")
        self.assertTrue(ok)
        self.assertEqual(code, "BLOCK-B12")

        found_sec = await db.get_dorm_by_code("block-b12")
        self.assertIsNotNone(found_sec)
        self.assertEqual(found_sec["dorm_id"], sec_id)

    async def test_web_api_dorm_code_endpoint(self):
        """Verify FastAPI /api/dorms/code/{code} endpoint."""
        await db.init_db()
        await db.seed_default_dorm()
        from web import app
        from httpx import AsyncClient, ASGITransport

        active_dorms = await db.get_all_active_dorms()
        code = active_dorms[0]["dorm_code"]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Valid code
            res = await client.get(f"/api/dorms/code/{code}")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["dorm_code"], code)
            self.assertIn("current_week", data)

            # Invalid code
            res404 = await client.get("/api/dorms/code/NONEXISTENT999")
            self.assertEqual(res404.status_code, 404)

    async def test_dorm_code_command(self):
        """Verify /code command replies with dorm code and dashboard link."""
        await db.init_db()
        await db.seed_default_dorm()
        from handlers import commands

        active_dorms = await db.get_all_active_dorms()
        code = active_dorms[0]["dorm_code"]

        update = MagicMock()
        update.effective_chat.id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        update.effective_chat.type = "supergroup"
        update.effective_user.id = 6163528558
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        await commands.dorm_code_command(update, context)

        update.effective_message.reply_text.assert_called_once()
        text_arg = update.effective_message.reply_text.call_args[0][0]
        self.assertIn(code, text_arg)
        self.assertIn(f"?code={code}", text_arg)

    async def test_role_context_aware_buttons_cleaner_vs_non_cleaner(self):
        """Verify Submit Cleaning button only appears for current week's cleaners."""
        await db.init_db()
        await db.seed_default_dorm()
        from handlers import commands

        # Get week 1 cleaners and an off-duty member
        d_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        dorm = await db.get_dorm(d_id)
        pairs = await db.get_dorm_pairs(d_id)
        members = await db.get_dorm_members(d_id)
        members_by_id = {m["user_id"]: m["name"] for m in members}

        from scheduler import get_dorm_week_info
        week_info = get_dorm_week_info(dorm, pairs, members_by_id)
        self.assertIsNotNone(week_info)
        cleaner_id = week_info.cleaner1_id

        # Find off-duty member
        off_duty_id = None
        for m in members:
            if m["user_id"] not in (week_info.cleaner1_id, week_info.cleaner2_id):
                off_duty_id = m["user_id"]
                break
        self.assertIsNotNone(off_duty_id)

        # 1. Cleaner status view must have Submit Cleaning
        _, cleaner_status_kb = await commands.get_status_content(cleaner_id, dorm_id=d_id)
        cleaner_callbacks = [btn.callback_data for row in cleaner_status_kb.inline_keyboard for btn in row]
        self.assertIn("menu_done", cleaner_callbacks)

        # 2. Off-duty status view must NOT have Submit Cleaning
        _, off_duty_status_kb = await commands.get_status_content(off_duty_id, dorm_id=d_id)
        off_duty_callbacks = [btn.callback_data for row in off_duty_status_kb.inline_keyboard for btn in row]
        self.assertNotIn("menu_done", off_duty_callbacks)
        self.assertIn("menu_fines", off_duty_callbacks)

        # 3. get_user_week_role
        is_cleaner_yes, role_cleaner, _ = await commands.get_user_week_role(cleaner_id, dorm_id=d_id)
        self.assertTrue(is_cleaner_yes)
        self.assertEqual(role_cleaner, "CLEANER")

        is_cleaner_no, role_off_duty, _ = await commands.get_user_week_role(off_duty_id, dorm_id=d_id)
        self.assertFalse(is_cleaner_no)
        self.assertNotEqual(role_off_duty, "CLEANER")

    async def test_non_cleaner_menu_done_callback_guard(self):
        """Verify non-cleaner tapping menu_done gets alerted and rejected."""
        await db.init_db()
        await db.seed_default_dorm()
        from handlers import commands

        d_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        members = await db.get_dorm_members(d_id)
        dorm = await db.get_dorm(d_id)
        pairs = await db.get_dorm_pairs(d_id)
        members_by_id = {m["user_id"]: m["name"] for m in members}

        from scheduler import get_dorm_week_info
        week_info = get_dorm_week_info(dorm, pairs, members_by_id)
        off_duty_member = next(m for m in members if m["user_id"] not in (week_info.cleaner1_id, week_info.cleaner2_id))

        update = MagicMock()
        update.callback_query.data = "menu_done"
        update.callback_query.from_user.id = off_duty_member["user_id"]
        update.callback_query.from_user.first_name = off_duty_member["name"]
        update.callback_query.answer = AsyncMock()
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        await commands.menu_callback_handler(update, context)

        update.callback_query.answer.assert_called_once()
        alert_msg = update.callback_query.answer.call_args[0][0]
        self.assertIn("not on cleaning duty", alert_msg.lower())
        self.assertTrue(update.callback_query.answer.call_args[1].get("show_alert"))

    async def test_reply_keyboard_text_handler_submit_guard(self):
        """Verify non-cleaner texting 'Submit Cleaning' is politely rejected and bottom keyboard stays."""
        await db.init_db()
        await db.seed_default_dorm()
        from handlers import commands

        d_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        members = await db.get_dorm_members(d_id)
        dorm = await db.get_dorm(d_id)
        pairs = await db.get_dorm_pairs(d_id)
        members_by_id = {m["user_id"]: m["name"] for m in members}

        from scheduler import get_dorm_week_info
        week_info = get_dorm_week_info(dorm, pairs, members_by_id)
        off_duty_member = next(m for m in members if m["user_id"] not in (week_info.cleaner1_id, week_info.cleaner2_id))

        update = MagicMock()
        update.effective_chat.type = "private"
        update.effective_user.id = off_duty_member["user_id"]
        update.effective_user.first_name = off_duty_member["name"]
        update.effective_message.text = "🧼 Submit Cleaning"
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        await commands.reply_keyboard_text_handler(update, context)

        update.effective_message.reply_text.assert_called_once()
        reply_call = update.effective_message.reply_text.call_args
        self.assertIn("not on cleaning duty", reply_call[0][0].lower())
        reply_markup = reply_call[1].get("reply_markup")
        self.assertIsNotNone(reply_markup)
        self.assertTrue(reply_markup.is_persistent)

    async def test_configurable_cleaners_per_week_1_2_3(self):
        """Verify pair auto-generation and week info with 1, 2, and 3 cleaners per week."""
        await db.init_db()
        test_dorm_id = -999888777
        await db.create_or_update_dorm(
            dorm_id=test_dorm_id,
            dorm_name="Multi-Size Test Dorm",
            admin_id=6163528558,
            dorm_code="DORM-TEST-999",
            cleaners_per_week=1
        )
        # Register 6 members
        members_list = [
            (101, "Member 1"),
            (102, "Member 2"),
            (103, "Member 3"),
            (104, "Member 4"),
            (105, "Member 5"),
            (106, "Member 6"),
        ]
        for uid, name in members_list:
            await db.add_dorm_member(test_dorm_id, uid, name)

        # 1 Cleaner per week
        pairs_1 = await db.auto_generate_dorm_pairs(test_dorm_id)
        self.assertEqual(len(pairs_1), 6)
        for p in pairs_1:
            self.assertGreater(p[0], 0)
            self.assertEqual(p[1], 0)
            self.assertEqual(p[2], 0)
        saved_1 = await db.get_dorm_pairs(test_dorm_id)
        self.assertEqual(len(saved_1), 6)
        self.assertGreater(saved_1[0]["cleaner1_id"], 0)
        self.assertEqual(saved_1[0]["cleaner2_id"], 0)
        self.assertEqual(saved_1[0]["cleaner3_id"], 0)

        # 2 Cleaners per week
        await db.update_dorm_settings(test_dorm_id, cleaners_per_week=2)
        pairs_2 = await db.auto_generate_dorm_pairs(test_dorm_id)
        self.assertEqual(len(pairs_2), 3)
        for p in pairs_2:
            self.assertGreater(p[0], 0)
            self.assertGreater(p[1], 0)
            self.assertEqual(p[2], 0)
        saved_2 = await db.get_dorm_pairs(test_dorm_id)
        self.assertEqual(len(saved_2), 3)
        self.assertGreater(saved_2[0]["cleaner1_id"], 0)
        self.assertGreater(saved_2[0]["cleaner2_id"], 0)
        self.assertEqual(saved_2[0]["cleaner3_id"], 0)

        # 3 Cleaners per week
        await db.update_dorm_settings(test_dorm_id, cleaners_per_week=3)
        pairs_3 = await db.auto_generate_dorm_pairs(test_dorm_id)
        self.assertEqual(len(pairs_3), 2)
        for p in pairs_3:
            self.assertGreater(p[0], 0)
            self.assertGreater(p[1], 0)
            self.assertGreater(p[2], 0)
        saved_3 = await db.get_dorm_pairs(test_dorm_id)
        self.assertEqual(len(saved_3), 2)
        self.assertGreater(saved_3[0]["cleaner1_id"], 0)
        self.assertGreater(saved_3[0]["cleaner2_id"], 0)
        self.assertGreater(saved_3[0]["cleaner3_id"], 0)

        # Check DormWeekInfo with 3 cleaners
        dorm_data = await db.get_dorm(test_dorm_id)
        members_map = {uid: name for uid, name in members_list}
        from scheduler import get_dorm_week_info
        week_info = get_dorm_week_info(dorm_data, saved_3, members_map)
        self.assertEqual(len(week_info.cleaner_ids), 3)
        self.assertEqual(len(week_info.cleaners), 3)
        self.assertIn(" & ", week_info.cleaner_display)

    async def test_dorm_code_lookup_robustness(self):
        """Verify dorm lookup by code handles messy input, quotes, backticks, and fallbacks."""
        await db.init_db()
        await db.seed_default_dorm()
        default_dorm = await db.get_dorm(getattr(config, "DEFAULT_GROUP_ID", -1002378440774))
        self.assertIsNotNone(default_dorm)

        # Direct code
        found1 = await db.get_dorm_by_code(default_dorm["dorm_code"])
        self.assertIsNotNone(found1)
        self.assertEqual(found1["dorm_id"], default_dorm["dorm_id"])

        # With quotes and backticks
        found2 = await db.get_dorm_by_code(f"`'{default_dorm['dorm_code']}'`")
        self.assertIsNotNone(found2)
        self.assertEqual(found2["dorm_id"], default_dorm["dorm_id"])

        # With DORM-101 legacy alias
        found3 = await db.get_dorm_by_code("DORM-101")
        self.assertIsNotNone(found3)
        self.assertEqual(found3["dorm_id"], default_dorm["dorm_id"])

        # With # prefix and lowercase
        found4 = await db.get_dorm_by_code(f"#{default_dorm['dorm_code'].lower()}")
        self.assertIsNotNone(found4)
        self.assertEqual(found4["dorm_id"], default_dorm["dorm_id"])

    async def test_ai_chat_history_db_operations(self):
        """Verify adding, retrieving, and clearing AI conversational memory in db."""
        await db.init_db()
        test_chat_id = -888777666
        test_dorm_id = -888777666

        # Initially empty
        await db.clear_ai_chat_history(test_chat_id)
        history0 = await db.get_recent_ai_chat_history(test_chat_id)
        self.assertEqual(len(history0), 0)

        # Add user message and assistant reply
        await db.add_ai_chat_message(test_dorm_id, test_chat_id, 1001, "Nahom", "user", "What can I cook for dinner?")
        await db.add_ai_chat_message(test_dorm_id, test_chat_id, 0, "The Inspector", "assistant", "Stir-fry noodles or pasta with garlic!")

        history1 = await db.get_recent_ai_chat_history(test_chat_id, limit=5)
        self.assertEqual(len(history1), 2)
        self.assertEqual(history1[0]["role"], "user")
        self.assertEqual(history1[0]["content"], "What can I cook for dinner?")
        self.assertEqual(history1[1]["role"], "assistant")

        # Test user recent topics
        user_topics = await db.get_user_recent_chat_history(1001, limit=5)
        self.assertGreaterEqual(len(user_topics), 1)
        self.assertEqual(user_topics[0]["content"], "What can I cook for dinner?")

        # Clear
        deleted = await db.clear_ai_chat_history(test_chat_id)
        self.assertEqual(deleted, 2)
        history2 = await db.get_recent_ai_chat_history(test_chat_id)
        self.assertEqual(len(history2), 0)

    async def test_ai_wide_perspective_general_questions(self):
        """Verify that asking general questions gives friendly, helpful answers without forcing 'you are off duty'."""
        from services import ai_service
        context = (
            "Dorm: Test Dorm\n"
            "Role: OFF-DUTY ROOMMATE\n"
            "Current Duty: Cleaners: Alice & Bob | Approver: Charlie\n"
        )

        # 1. Joke
        joke_reply = ai_service.fallback_inspector_reply("tell me a joke", context, user_name="Nahom")
        self.assertNotIn("off duty", joke_reply.lower())
        self.assertNotIn("cleaning duty", joke_reply.lower())
        self.assertTrue(any(w in joke_reply.lower() for w in ("rule", "plate", "broom", "cup", "laugh", "frisbee", "roommate")))

        # 2. Food / Cooking
        food_reply = ai_service.fallback_inspector_reply("what should I cook for dinner?", context, user_name="Nahom")
        self.assertNotIn("off duty", food_reply.lower())
        self.assertTrue(any(w in food_reply.lower() for w in ("meal", "pasta", "noodles", "eggs", "cook", "ingredients")))

        # 3. Studying
        study_reply = ai_service.fallback_inspector_reply("how should I study for exams?", context, user_name="Nahom")
        self.assertNotIn("off duty", study_reply.lower())
        self.assertTrue(any(w in study_reply.lower() for w in ("study", "pomodoro", "focus", "exam")))

        # 4. Casual friendly greeting
        greeting_reply = ai_service.fallback_inspector_reply("hey what's up?", context, user_name="Nahom")
        self.assertNotIn("off duty", greeting_reply.lower())
        self.assertIn("Nahom", greeting_reply)

    async def test_ai_chat_history_conversational_memory(self):
        """Verify ask_ai records conversation turns and can access past history."""
        await db.init_db()
        from services import ai_service
        chat_id = -999111222
        dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        await db.clear_ai_chat_history(chat_id)

        # Ensure default dorm exists
        await db.seed_default_dorm()

        # First interaction
        reply1 = await ai_service.ask_ai("tell me a joke", dorm_id=dorm_id, user_name="Bilisuma", user_id=101, chat_id=chat_id)
        self.assertTrue(len(reply1) > 0)

        # History should now have 2 messages (user + assistant)
        history = await db.get_recent_ai_chat_history(chat_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "tell me a joke")
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")

        # Second interaction (follow-up)
        reply2 = await ai_service.ask_ai("what did you say earlier?", dorm_id=dorm_id, user_name="Bilisuma", user_id=101, chat_id=chat_id)
        self.assertTrue(len(reply2) > 0)

        # History now has 4 messages
        history2 = await db.get_recent_ai_chat_history(chat_id)
        self.assertEqual(len(history2), 4)

    async def test_reset_ai_command(self):
        """Verify /reset_ai command clears conversational memory and sends confirmation."""
        await db.init_db()
        from handlers import ai_chat
        chat_id = -777666555
        await db.add_ai_chat_message(chat_id, chat_id, 101, "Nahom", "user", "Remember this message")

        update = MagicMock()
        update.effective_chat.id = chat_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        await ai_chat.reset_ai_command(update, context)

        update.effective_message.reply_text.assert_called_once()
        sent_text = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("memory cleared", sent_text.lower())

        # Verify DB is cleared
        history = await db.get_recent_ai_chat_history(chat_id)
        self.assertEqual(len(history), 0)

if __name__ == "__main__":
    unittest.main()
