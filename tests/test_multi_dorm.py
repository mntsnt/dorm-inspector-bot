"""
Multi-Dorm & Backward Compatibility Verification Tests
Tests:
1. Default dorm (-1002378440774, topic 2031) is automatically seeded and active on startup.
2. Roommates from the default dorm can /start and access status/schedule immediately.
3. Placeholder slot (Bilisuma) can be claimed via /claim or /start.
4. Non-members receive polite onboarding guidance to use /setup.
5. Independent multi-tenant operation: A newly onboarded group runs separately from the default dorm.
"""

import asyncio
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure root directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handlers.commands import check_member_permission, start_command, claim_command
from scheduler import get_dorm_week_info

class TestMultiDormPlatform(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create isolated test database
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.tmp_dir.name) / "test_dorm.db"
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    async def test_default_dorm_seeding(self):
        """Verify default dorm is seeded with 8 members, 4 pairs, active status, topic 2031."""
        await db.seed_default_dorm()
        
        dorm = await db.get_dorm(config.DEFAULT_GROUP_ID)
        self.assertIsNotNone(dorm)
        self.assertEqual(dorm["dorm_id"], -1002378440774)
        self.assertEqual(dorm["topic_id"], 2031)
        self.assertEqual(dorm["status"], "active")
        self.assertEqual(dorm["fine_amount"], 100)

        members = await db.get_dorm_members(config.DEFAULT_GROUP_ID)
        self.assertEqual(len(members), 8)
        names = {m["name"] for m in members}
        self.assertIn("Mintesnot", names)
        self.assertIn("Nahom", names)
        self.assertIn("Dawit", names)
        self.assertIn("Bilisuma", names)

        pairs = await db.get_dorm_pairs(config.DEFAULT_GROUP_ID)
        self.assertEqual(len(pairs), 4)

        # Verify the 4 fixed pairs match members.json exactly
        member_lookup = {m["user_id"]: m["name"] for m in members}
        pair_names = [
            (member_lookup[p["cleaner1_id"]], member_lookup[p["cleaner2_id"]])
            for p in pairs
        ]
        self.assertEqual(pair_names[0], ("Bilisuma", "Nahom"))
        self.assertEqual(pair_names[1], ("Hassan", "Mintesnot"))
        self.assertEqual(pair_names[2], ("Dawit", "Zeftret"))
        self.assertEqual(pair_names[3], ("Ahmed", "Yonatan"))

    async def test_existing_member_permission_and_start(self):
        """Pre-configured roommates (e.g. Nahom 1758752115, Bilisuma 7220854785) are recognized and can /start."""
        await db.seed_default_dorm()

        # Nahom is in members.json
        nahom_id = 1758752115
        has_perm = await check_member_permission(nahom_id)
        self.assertTrue(has_perm)

        # Bilisuma with real ID 7220854785 is recognized directly without needing /claim
        bilisuma_id = 7220854785
        has_perm_b = await check_member_permission(bilisuma_id)
        self.assertTrue(has_perm_b)

        # Mock /start message
        update = MagicMock()
        update.effective_user.id = nahom_id
        update.effective_user.first_name = "Nahom"
        update.effective_user.username = "nahom_t"
        update.effective_chat.type = "private"
        update.effective_chat.id = nahom_id
        update.effective_message.reply_text = AsyncMock()
        update.effective_message.reply_photo = AsyncMock()

        context = MagicMock()
        context.args = []

        await start_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply_call = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Nahom", reply_call)

    async def test_non_member_rejection_and_guidance(self):
        """An unknown user is not recognized and receives /setup onboarding guidance."""
        await db.seed_default_dorm()

        unknown_id = 999999999
        has_perm = await check_member_permission(unknown_id)
        self.assertFalse(has_perm)

        update = MagicMock()
        update.effective_user.id = unknown_id
        update.effective_user.first_name = "ExternalStudent"
        update.effective_user.username = "ext_student"
        update.effective_chat.type = "private"
        update.effective_chat.id = unknown_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = []

        await start_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("/setup", reply)

    async def test_claim_command_for_bilisuma(self):
        """A placeholder member slot can be claimed using /claim <Name>."""
        # Temporarily set placeholder ID for test
        config.update_member_id("Bilisuma", 111111111)
        await db.seed_default_dorm()
        await db.update_member_id_in_db(old_id=7220854785, new_id=111111111)

        bilisuma_real_id = 7220854785
        update = MagicMock()
        update.effective_user.id = bilisuma_real_id
        update.effective_user.first_name = "Bilisuma"
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["Bilisuma"]

        try:
            await claim_command(update, context)
            self.assertTrue(update.effective_message.reply_text.called)
            reply = update.effective_message.reply_text.call_args[0][0]
            self.assertIn("Welcome **Bilisuma**", reply)

            # Check DB was updated
            db_member = await db.get_dorm_member_by_name(config.DEFAULT_GROUP_ID, "Bilisuma")
            self.assertEqual(db_member["user_id"], bilisuma_real_id)
        finally:
            # Restore real ID for subsequent tests
            config.update_member_id("Bilisuma", 7220854785)

    async def test_independent_multi_dorm_scheduling(self):
        """A second dorm created via onboarding operates independently from the default dorm."""
        await db.seed_default_dorm()

        # Onboard Dorm 2
        dorm2_id = -1009999999999
        await db.create_or_update_dorm(
            dorm_id=dorm2_id,
            admin_id=555555,
            dorm_name="Hawassa University Block 4 Room 12",
            fine_amount=150,
            start_date="2026-11-23"
        )
        await db.add_dorm_member(dorm2_id, 555555, "Abebe", is_admin=1)
        await db.add_dorm_member(dorm2_id, 666666, "Kebede")
        await db.set_dorm_pairs(dorm2_id, [(555555, 666666)])

        # Verify active dorms
        active = await db.get_all_active_dorms()
        self.assertEqual(len(active), 2)

        # Schedule check for Dorm 2
        d2 = await db.get_dorm(dorm2_id)
        d2_pairs = await db.get_dorm_pairs(dorm2_id)
        d2_members = {555555: "Abebe", 666666: "Kebede"}
        week_info = get_dorm_week_info(d2, d2_pairs, d2_members, target_date=date(2026, 11, 23))
        self.assertIsNotNone(week_info)
        self.assertEqual(week_info.cleaner1_name, "Abebe")
        self.assertEqual(week_info.cleaner2_name, "Kebede")

if __name__ == "__main__":
    unittest.main()
