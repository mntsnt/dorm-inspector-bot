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

if __name__ == "__main__":
    unittest.main()
