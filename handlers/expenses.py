"""
Dorm Expense Splitter Handler
Splits shared dorm purchases (soap, Omo detergent, mop, broom, water refill, bread)
evenly across roommates and tracks reimbursements.
"""

import logging
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Split a shared dorm expense evenly across roommates."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    dorm = await db.get_dorm(dorm_id)
    if not dorm and not is_group:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
            dorm = await db.get_dorm(dorm_id)

    if not dorm:
        await message.reply_text("🧼 No active dorm found. Send `/setup` to initialize.", parse_mode=ParseMode.MARKDOWN)
        return

    target_dorm_id = dorm["dorm_id"]

    if not context.args or len(context.args) < 2:
        await message.reply_text(
            "Usage: `/split <amount> <description>`\n"
            "Example: `/split 160 Omo detergent and sponge`\n"
            "Or: `/split 200 Drinking water refill`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    amount_str = context.args[0].replace("ETB", "").replace("etb", "").strip()
    try:
        total_amount = float(amount_str)
        if total_amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply_text("⚠️ Please enter a valid positive number for the amount. Example: `/split 160 Omo detergent`")
        return

    description = " ".join(context.args[1:]).strip()
    member = await db.get_dorm_member(target_dorm_id, user.id)
    payer_name = member["name"] if member else (user.first_name or "Roommate")

    exp_data = await db.create_expense(
        dorm_id=target_dorm_id,
        payer_id=user.id,
        payer_name=payer_name,
        total_amount=total_amount,
        description=description
    )

    expense_id = exp_data["expense_id"]
    split_amt = exp_data["split_amount"]
    count = exp_data["count"]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"💳 Settle My Share ({split_amt} ETB)", callback_data=f"settle_exp_{expense_id}"),
        ]
    ])

    announcement = (
        f"💸 **DORM EXPENSE RECORDED** 💸\n\n"
        f"Item: **{description}**\n"
        f"Purchased by: **{payer_name}**\n"
        f"Total Cost: **{total_amount:.2f} ETB**\n\n"
        f"👉 **Each roommate owes: {split_amt:.2f} ETB** (split across {count} roommates)\n\n"
        f"Reimburse **{payer_name}** and tap below to mark your share as paid!"
    )

    await message.reply_text(announcement, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def expenses_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show active dorm expenses and unpaid debts."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    dorm = await db.get_dorm(dorm_id)
    if not dorm and not is_group:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
            dorm = await db.get_dorm(dorm_id)

    if not dorm:
        await message.reply_text("🧼 No active dorm found. Send `/setup` to initialize.")
        return

    target_dorm_id = dorm["dorm_id"]
    expenses = await db.get_active_expenses(target_dorm_id)

    if not expenses:
        await message.reply_text(
            f"🎉 **{dorm['dorm_name']} has no active unsettled expenses!**\n\n"
            "All shared purchases have been settled. Use `/split <amount> <item>` to record a new one.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = [f"📊 **{dorm['dorm_name']} — Active Shared Expenses:**\n"]
    for exp in expenses:
        lines.append(
            f"• **{exp['description']}** (Total: {exp['total_amount']:.2f} ETB | Bought by {exp['payer_name']})\n"
            f"  Each owes: `{exp['split_amount']:.2f} ETB`"
        )
        unpaid = [s['debtor_name'] for s in exp.get('splits', []) if s.get('is_paid') == 0]
        if unpaid:
            lines.append(f"  Waiting on: {', '.join(unpaid)}")
        else:
            lines.append("  Status: Fully settled! ✅")
        lines.append("")

    lines.append("💡 *Tip:* Send `/settle` or tap 'Settle My Share' on an expense message to clear your balance.")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def settle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear user's unpaid expense shares in the dorm."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    dorm = await db.get_dorm(dorm_id)
    if not dorm and not is_group:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
            dorm = await db.get_dorm(dorm_id)

    if not dorm:
        await message.reply_text("🧼 No active dorm found.")
        return

    target_dorm_id = dorm["dorm_id"]
    count = await db.settle_debtor_split(target_dorm_id, debtor_id=user.id)
    member = await db.get_dorm_member(target_dorm_id, user.id)
    name = member["name"] if member else user.first_name

    if count > 0:
        await message.reply_text(
            f"✅ **{name}**, you marked {count} shared expense item(s) as reimbursed!\n"
            "Thanks for keeping your dorm accounts square! 🤝",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(
            f"You don't have any pending expense reimbursements in **{dorm['dorm_name']}**! You're all clear. 🎉",
            parse_mode=ParseMode.MARKDOWN
        )

async def settle_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle click on 'Settle My Share' inline button."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = query.from_user
    parts = query.data.split("_")
    if len(parts) < 3:
        return

    try:
        expense_id = int(parts[2])
    except ValueError:
        return

    # Find expense dorm
    dorm_id = query.message.chat_id if query.message else getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    settled = await db.settle_debtor_split(dorm_id, debtor_id=user.id, expense_id=expense_id)

    member = await db.get_dorm_member(dorm_id, user.id)
    name = member["name"] if member else user.first_name

    if settled > 0:
        await query.answer(f"✅ Marked your share as paid, {name}!", show_alert=True)
    else:
        await query.answer("You are already settled or were the buyer for this expense!", show_alert=True)

def register_expense_handlers(application: Application) -> None:
    """Attach expense splitting command and callback handlers."""
    application.add_handler(CommandHandler("split", split_command))
    application.add_handler(CommandHandler("expense", split_command))
    application.add_handler(CommandHandler("expenses", expenses_command))
    application.add_handler(CommandHandler("settle", settle_command))
    application.add_handler(CallbackQueryHandler(settle_callback_handler, pattern=r"^settle_exp_\d+"))
