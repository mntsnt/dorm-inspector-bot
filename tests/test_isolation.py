"""
Strict Multi-Tenant Isolation & Anti-Leakage Test Suite
Validates that every dorm's data (members, schedules, fines, history, photos, callbacks)
remains 100% strictly isolated and never leaks to another dorm.
"""

import asyncio
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import handlers.commands as commands
import handlers.admin as admin
import handlers.cleaning as cleaning
import handlers.approval as approval
import handlers.setup as setup

class TestTenantIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.tmp_dir.name) / "test_isolation.db"
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        await db.init_db()

        # Seed Default Dorm (Dorm A)
        await db.seed_default_dorm()
        self.dorm_a_id = config.DEFAULT_GROUP_ID  # -1002378440774
        self.dorm_a_member_id = 1758752115  # Nahom

        # Create External Dorm (Dorm B)
        self.dorm_b_id = -1009999888777
        self.dorm_b_admin_id = 777001
        self.dorm_b_member_id = 777002

        await db.create_or_update_dorm(
            dorm_id=self.dorm_b_id,
            admin_id=self.dorm_b_admin_id,
            dorm_name="Hawassa Block 3 Room 4",
            fine_amount=200,
            start_date="2026-11-23"
        )
        await db.add_dorm_member(self.dorm_b_id, self.dorm_b_admin_id, "AdminB", is_admin=1)
        await db.add_dorm_member(self.dorm_b_id, self.dorm_b_member_id, "MemberB")
        await db.set_dorm_pairs(self.dorm_b_id, [(self.dorm_b_admin_id, self.dorm_b_member_id)])

    async def asyncTearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    async def test_dorm_b_member_cannot_view_dorm_a_status(self):
        """A member of Dorm B cannot view Dorm A's status."""
        text, kb = await commands.get_status_content(user_id=self.dorm_b_member_id, dorm_id=self.dorm_a_id)
        self.assertIn("not a member of this dorm", text)

    async def test_dorm_b_member_cannot_view_dorm_a_schedule(self):
        """A member of Dorm B cannot view Dorm A's schedule."""
        text, kb = await commands.get_schedule_content(dorm_id=self.dorm_a_id, user_id=self.dorm_b_member_id)
        self.assertIn("not a member of this dorm", text)

    async def test_dorm_b_member_cannot_view_dorm_a_fines(self):
        """A member of Dorm B cannot view Dorm A's fines."""
        text, kb = await commands.get_fines_content(dorm_id=self.dorm_a_id, user_id=self.dorm_b_member_id)
        self.assertIn("not a member of this dorm", text)

    async def test_dorm_b_member_cannot_view_dorm_a_history(self):
        """A member of Dorm B cannot view Dorm A's history."""
        text, kb = await commands.get_history_content(dorm_id=self.dorm_a_id, user_id=self.dorm_b_member_id)
        self.assertIn("not a member of this dorm", text)

    async def test_dorm_b_members_list_does_not_leak_dorm_a_names(self):
        """Dorm B's members list never leaks Dorm A's member names (Bilisuma, Mintesnot, Nahom, etc)."""
        # Create empty Dorm C
        dorm_c_id = -1005555555555
        await db.create_or_update_dorm(
            dorm_id=dorm_c_id,
            admin_id=999111,
            dorm_name="Empty Dorm C",
            start_date="2026-11-23"
        )
        await db.add_dorm_member(dorm_c_id, 999111, "AdminC", is_admin=1)

        conn_cnt, total_cnt, conn_list, pend_list = await db.get_connection_status(dorm_c_id)
        # Should only have AdminC, not the 8 members from Dorm A
        self.assertEqual(total_cnt, 1)
        names = [m["name"] for m in conn_list + pend_list]
        self.assertNotIn("Bilisuma", names)
        self.assertNotIn("Mintesnot", names)
        self.assertNotIn("Nahom", names)

    async def test_dorm_b_admin_cannot_markpaid_dorm_a_members(self):
        """Admin of Dorm B cannot clear fines for Dorm A members."""
        update = MagicMock()
        update.effective_user.id = self.dorm_b_admin_id
        update.effective_chat.type = "group"
        update.effective_chat.id = self.dorm_b_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["Nahom"]  # Nahom is in Dorm A, not Dorm B

        await admin.markpaid_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("not found in this dorm", reply)
        # Verify Dorm A names are not leaked in the 'Members: ...' error list
        self.assertNotIn("Bilisuma", reply)
        self.assertNotIn("Mintesnot", reply)

    async def test_dorm_b_member_cannot_trigger_dorm_a_callbacks(self):
        """A user from Dorm B clicking a button with Dorm A callback data is blocked."""
        # Cleaned button callback
        update = MagicMock()
        update.callback_query.data = f"cleaned_{self.dorm_a_id}_2026-11-23"
        update.callback_query.from_user.id = self.dorm_b_member_id
        update.callback_query.answer = AsyncMock()

        context = MagicMock()

        await cleaning.cleaned_button_callback(update, context)
        self.assertTrue(update.callback_query.answer.called)
        answer_call = update.callback_query.answer.call_args
        self.assertIn("not a member of this dorm", answer_call[0][0])

    async def test_dorm_b_member_cannot_approve_dorm_a_photo(self):
        """A user from Dorm B clicking approve on Dorm A's photo is blocked."""
        update = MagicMock()
        update.callback_query.data = f"approve_{self.dorm_a_id}_2026-11-23"
        update.callback_query.from_user.id = self.dorm_b_member_id
        update.callback_query.answer = AsyncMock()

        context = MagicMock()

        await approval.approve_photo_callback(update, context)
        self.assertTrue(update.callback_query.answer.called)
        answer_call = update.callback_query.answer.call_args
        self.assertIn("not a member of this dorm", answer_call[0][0])

    async def test_unauthorized_user_cannot_view_setup_members_list(self):
        """A stranger cannot click setup_members callback to inspect roommates."""
        stranger_id = 999888777
        update = MagicMock()
        update.callback_query.data = f"setup_members_{self.dorm_a_id}"
        update.callback_query.from_user.id = stranger_id
        update.callback_query.answer = AsyncMock()

        context = MagicMock()

        await setup.setup_members_callback(update, context)
        self.assertTrue(update.callback_query.answer.called)
        answer_call = update.callback_query.answer.call_args
        self.assertIn("Only members of this dorm", answer_call[0][0])

    async def test_cannot_join_second_active_dorm_via_db(self):
        """A user already belonging to an active dorm cannot be added to another active dorm."""
        success, msg = await db.add_dorm_member(self.dorm_a_id, self.dorm_b_member_id, "MemberB")
        self.assertFalse(success)
        self.assertIn("already a member of Hawassa Block 3 Room 4", msg)

        # Verify user is NOT in Dorm A members
        dorm_a_members = await db.get_dorm_members(self.dorm_a_id)
        dorm_a_uids = [m["user_id"] for m in dorm_a_members]
        self.assertNotIn(self.dorm_b_member_id, dorm_a_uids)

    async def test_cannot_join_second_active_dorm_via_deeplink(self):
        """A member of Dorm A trying to deep-link join Dorm B is blocked with informative message."""
        update = MagicMock()
        update.effective_user.id = self.dorm_a_member_id
        update.effective_user.first_name = "Nahom"
        update.effective_chat.type = "private"
        update.effective_chat.id = self.dorm_a_member_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = [f"join_{abs(self.dorm_b_id)}"]

        await commands.start_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Cannot join", reply)
        self.assertIn("/leave", reply)

        # Ensure user was not added to Dorm B
        dorm_b_member = await db.get_dorm_member(self.dorm_b_id, self.dorm_a_member_id)
        self.assertIsNone(dorm_b_member)

    async def test_cannot_setup_second_dorm_while_active_in_dorm(self):
        """A member of Dorm B cannot create/setup another dorm group."""
        dorm_d_id = -1004443332221
        update = MagicMock()
        update.effective_user.id = self.dorm_b_member_id
        update.effective_user.first_name = "MemberB"
        update.effective_chat.type = "group"
        update.effective_chat.id = dorm_d_id
        update.effective_chat.title = "Dorm Delta"
        update.effective_message.reply_text = AsyncMock()
        update.effective_message.message_thread_id = None

        context = MagicMock()

        await setup.setup_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Cannot set up a new dorm", reply)
        self.assertIn("/leave", reply)

        # Dorm D was not created
        dorm_d = await db.get_dorm(dorm_d_id)
        self.assertIsNone(dorm_d)

    async def test_leave_dorm_cleanly_when_not_on_duty(self):
        """A member can cleanly leave their dorm when not scheduled on duty this week."""
        # Add a 3rd member to Dorm B who is not on duty
        new_uid = 777003
        success, _ = await db.add_dorm_member(self.dorm_b_id, new_uid, "StudentC")
        self.assertTrue(success)

        update = MagicMock()
        update.effective_user.id = new_uid
        update.effective_user.first_name = "StudentC"
        update.effective_chat.type = "private"
        update.effective_chat.id = new_uid
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()

        await commands.leave_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("successfully left", reply)

        # Verify no longer a member
        m = await db.get_dorm_member(self.dorm_b_id, new_uid)
        self.assertIsNone(m)

        # Now this user is free to join another dorm
        success_join, _ = await db.add_dorm_member(self.dorm_a_id, new_uid, "StudentC")
        self.assertTrue(success_join)

    async def test_leave_dorm_blocked_when_on_duty(self):
        """A member scheduled on duty this week is blocked from leaving until completed or swapped."""
        # Create an active pending week in Dorm B with dorm_b_member_id on duty
        await db.get_or_create_week(
            dorm_id=self.dorm_b_id,
            week_start="2026-11-23",
            week_number=1,
            cleaner1_id=self.dorm_b_admin_id,
            cleaner2_id=self.dorm_b_member_id,
            eligible_approver_ids=[self.dorm_b_admin_id]
        )

        update = MagicMock()
        update.effective_user.id = self.dorm_b_member_id
        update.effective_chat.type = "private"
        update.effective_chat.id = self.dorm_b_member_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()

        await commands.leave_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("cannot leave while scheduled on duty", reply)

        # Still a member
        m = await db.get_dorm_member(self.dorm_b_id, self.dorm_b_member_id)
        self.assertIsNotNone(m)

    async def test_stranger_cannot_spin_or_view_members_in_group(self):
        """A stranger running /members or /spin in Dorm B's group chat is rejected."""
        stranger_id = 999000111
        update = MagicMock()
        update.effective_user.id = stranger_id
        update.effective_chat.type = "group"
        update.effective_chat.id = self.dorm_b_id
        update.effective_message.reply_text = AsyncMock()

        context = MagicMock()

        # Members command
        await commands.members_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("not a member of this dorm", reply)

        # Spin command
        update.effective_message.reply_text.reset_mock()
        context.args = ["clean trash"]
        await commands.spin_command(update, context)
        self.assertTrue(update.effective_message.reply_text.called)
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("not a member of this dorm", reply)

    async def test_callback_parsing_invalid_does_not_leak_default_dorm(self):
        """Invalid callback queries return (0, '') and never fall back to default dorm."""
        d_id, w_start = approval._parse_callback_dorm_and_week("approve_notanumber_2026-11-23")
        self.assertEqual(d_id, 0)
        self.assertEqual(w_start, "")

        d_id2, w_start2 = cleaning._parse_callback_dorm_and_week("cleaned_badid_2026-11-23")
        self.assertEqual(d_id2, 0)
        self.assertEqual(w_start2, "")

if __name__ == "__main__":
    unittest.main()
