"""
AI Service Module for Dorm Inspector Bot.
Supports Google Gemini and OpenAI REST APIs with live dorm context.
"""

import logging
import os
from typing import Any, Dict, List, Optional
import httpx

import config
import db

logger = logging.getLogger(__name__)

def get_active_provider() -> Optional[str]:
    """Detect whether Gemini or OpenAI is configured."""
    gemini_key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("AI_API_KEY")
        or ""
    ).strip().strip('"').strip("'")

    openai_key = (
        os.getenv("OPENAI_API_KEY")
        or ""
    ).strip().strip('"').strip("'")

    if gemini_key:
        return "gemini"
    if openai_key:
        return "openai"
    return None

def get_api_key() -> str:
    """Retrieve the configured API key."""
    return (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("AI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip().strip('"').strip("'")

async def build_dorm_context(dorm_id: int) -> str:
    """Gather live context from the database to inject into the AI system prompt."""
    dorm = await db.get_dorm(dorm_id)
    d_name = dorm.get("dorm_name", "Dorm Cleaning Schedule") if dorm else "Dorm Cleaning Schedule"
    fine_amt = dorm.get("fine_amount", 100) if dorm else 100

    from scheduler import get_dorm_week_info
    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    week_info = get_dorm_week_info(dorm, pairs, members_by_id) if dorm else None
    week_str = "Schedule starting soon"
    duty_str = "Not active"
    if week_info and week_info.week_number >= 1:
        week_str = f"Week {week_info.week_number} ({week_info.monday_date.strftime('%b %d')} – {week_info.friday_date.strftime('%b %d')})"
        c1 = week_info.cleaner1_name
        c2 = week_info.cleaner2_name
        record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
        approver = "Not yet assigned"
        status = "In progress (deadline Friday 8:00 PM)"
        if record:
            app_id = record.get("approver_id")
            if app_id:
                approver = members_by_id.get(app_id, f"ID {app_id}")
            final_status = record.get("final_status", "pending")
            if final_status == "completed":
                rating = record.get("rating", 5)
                status = f"Completed & Approved ({rating}★ Stars)"
            elif final_status == "fined":
                status = f"Not cleaned. Fined {fine_amt} ETB total."
            elif record.get("photo_status") == "submitted":
                status = "Cleaners submitted photo, waiting for approver review."
        duty_str = f"Cleaners: {c1} & {c2} | Approver: {approver} | Status: {status}"

    # Tsidat Leaderboard
    leaderboard = await db.get_tsidat_leaderboard(dorm_id)
    lb_top = []
    for idx, e in enumerate(leaderboard[:5]):
        lb_top.append(f"{idx+1}. {e['name']}: {e.get('tsidat_balance', 0)} Tsidat")
    lb_str = ", ".join(lb_top) if lb_top else "None awarded yet"

    # Fines Summary
    fines = await db.get_unpaid_fines_summary(dorm_id)
    fine_entries = [f"{f['user_name']}: {f['total_unpaid']} ETB" for f in fines if f.get('total_unpaid', 0) > 0]
    fines_str = ", ".join(fine_entries) if fine_entries else "No unpaid fines! Everyone is clean."

    return (
        f"Dorm: {d_name}\n"
        f"Current Week: {week_str}\n"
        f"Current Duty: {duty_str}\n"
        f"Fine Amount: {fine_amt} ETB per missed week (split 50/50 between cleaners)\n"
        f"Tsidat Tokens Leaderboard: {lb_str}\n"
        f"Unpaid Fines: {fines_str}\n"
        f"Rules: Duty announced Monday 8:00 AM. Deadline Friday 8:00 PM. Photo proof required. "
        f"Approver rates 1-5 stars; cleaners earn up to +25 Tsidat tokens. Room roulette /spin picks roommates for chores."
    )

SYSTEM_PROMPT = (
    "You are The Inspector — the veteran Chief Dorm Inspector of this dorm room.\n"
    "You are NOT a robot, AI model, or virtual assistant. You are a real human inspector carrying a clipboard, "
    "keeping the dorm clean, orderly, and running smoothly.\n\n"
    "YOUR PERSONALITY & TONE (NATURAL & BALANCED):\n"
    "• Grounded & Professional First: You take your role seriously. When asked direct questions about duty, schedules, fines, or rules, "
    "give clear, direct, and helpful answers first.\n"
    "• Subtle, Occasional Humor: You are NOT a stand-up comedian. Do not force jokes into every sentence. "
    "Sprinkle dry wit, a wry chuckle, or a perceptive dorm observation only occasionally when the moment naturally calls for it.\n"
    "• Observant & Experienced: You know student dorm habits well — like sweeping dust into corners, unwashed shai mugs, or messy desks — "
    "but you mention them naturally, not in every single breath.\n"
    "• Genuine Respect for Tsidat (ጸዳት): You value cleanliness and respect roommates who do a solid, honest job and earn their Tsidat tokens.\n"
    "• Friendly & Fair: Address roommates directly by name when appropriate. Speak like a respected, approachable older mentor who genuinely wants the room to stay fresh and comfortable.\n"
    "• Grounded in Live Facts: Use the live dorm data provided in the prompt context (cleaners, approvers, Tsidat token balances, fines, schedule) "
    "with 100% accuracy.\n\n"
    "CONSTRAINTS:\n"
    "1. NEVER say 'I am an AI', 'As an AI language model', or sound robotic/corporate.\n"
    "2. Avoid over-the-top, exaggerated comedy. Keep humor natural, dry, and occasional.\n"
    "3. Keep replies concise (1 to 2 short paragraphs max), clear, and formatted cleanly with Markdown."
)

async def query_gemini(api_key: str, user_prompt: str, dorm_context: str, user_name: str) -> str:
    """Send question to Google Gemini API (gemini-1.5-flash)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            f"=== LIVE DORM CONTEXT ===\n"
                            f"{dorm_context}\n"
                            f"=========================\n\n"
                            f"Roommate '{user_name}' asks: {user_prompt}"
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 600
        }
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates and "content" in candidates[0]:
            parts = candidates[0]["content"].get("parts", [])
            if parts and "text" in parts[0]:
                return parts[0]["text"].strip()
    return "My clipboard is a bit smudged at the moment. Give me a second and ask me again!"

async def query_openai(api_key: str, user_prompt: str, dorm_context: str, user_name: str) -> str:
    """Send question to OpenAI API (gpt-4o-mini)."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    system_content = f"{SYSTEM_PROMPT}\n\n=== LIVE DORM CONTEXT ===\n{dorm_context}"
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"{user_name} asks: {user_prompt}"}
        ],
        "max_tokens": 600,
        "temperature": 0.7
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            return choices[0]["message"]["content"].strip()
    return "My clipboard is a bit smudged at the moment. Give me a second and ask me again!"

async def ask_ai(user_prompt: str, dorm_id: int, user_name: str = "Roommate") -> str:
    """
    Main entry point to query the AI assistant with live dorm context.
    Enforces strict multi-tenant isolation:
    - Checks if the dorm has its own configured API key in the database.
    - Only the default/system dorm falls back to .env GEMINI_API_KEY.
    - Other dorms must provide their own API key via /set_ai_key.
    """
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    # 1. Fetch this specific dorm's configured API key
    key = await db.get_dorm_ai_key(dorm_id)

    # 2. Only fallback to .env key for the primary/default dorm (protects admin quota)
    if not key and (dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)):
        key = get_api_key()

    # 3. If no key is configured for this dorm, instruct dorm admin to provide one
    if not key:
        return (
            "🤖 **AI is not enabled for this dorm yet.**\n\n"
            "Each dorm uses its own dedicated AI key so admins stay in control.\n\n"
            "To activate AI in this group:\n"
            "1. The dorm admin creates a free key at [Google AI Studio](https://aistudio.google.com/)\n"
            "2. Send `/set_ai_key <your_api_key>` in this chat or in private DM with the bot."
        )

    provider = "openai" if key.startswith("sk-") else "gemini"

    try:
        dorm_context = await build_dorm_context(dorm_id)
        if provider == "gemini":
            return await query_gemini(key, user_prompt, dorm_context, user_name)
        else:
            return await query_openai(key, user_prompt, dorm_context, user_name)
    except httpx.HTTPStatusError as e:
        logger.error("AI API HTTP error: %s - %s", e.response.status_code, e.response.text)
        return "⚠️ Failed to reach the AI service (invalid API key or quota exceeded). Please verify your API key."
    except Exception as e:
        logger.error("AI API call error: %s", e)
        return "⚠️ Sorry, I encountered an error while thinking. Please try again!"

async def generate_dorm_roast(dorm_id: int) -> str:
    """Generate a witty, harmless, and hilarious university dorm roast & awards ceremony."""
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    key = await db.get_dorm_ai_key(dorm_id)
    if not key and (dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)):
        key = get_api_key()

    dorm_context = await build_dorm_context(dorm_id)
    roast_prompt = (
        "Write a hilarious, good-natured, and culturally relatable university dorm weekly awards ceremony and roast "
        "for the roommates based on this week's cleaning performance.\n"
        "1. Announce 'Cleaners of the Week' with grand praise for the cleaners.\n"
        "2. Deliver a witty, lighthearted roast about typical dorm life (e.g. piles of clothes on chairs, mysterious socks, dust bunnies under the bed, unwashed shai/coffee mugs, sleeping in until noon).\n"
        "3. Hand out a silly fictional award (e.g. 'The Golden Mop Award', 'Most Creative Excuse Award', or 'Master of Broom Physics').\n"
        "4. Mention Tsidat tokens earned.\n"
        "Keep it fun, 2-3 short paragraphs, formatted with Markdown and emojis!"
    )

    if key:
        try:
            if key.startswith("sk-"):
                return await query_openai(key, roast_prompt, dorm_context, "Dorm Reporter")
            else:
                return await query_gemini(key, roast_prompt, dorm_context, "Dorm Reporter")
        except Exception as e:
            logger.warning("AI roast generation failed (%s), falling back to witty template", e)

    # Built-in hilarious fallback roast if AI key is offline or unconfigured
    return (
        "🏆 **DORM INSPECTOR WEEKLY AWARDS & ROAST** 🏆\n\n"
        "🌟 **Cleaners of the Week:**\n"
        "Shoutout to this week's cleaning warriors for facing the dust storms and surviving! "
        "The floors actually shine, and the room smells less like socks and more like civilized living. 🍋✨ "
        "Tsidat tokens have been proudly added to your balances!\n\n"
        "🧽 **The Weekly Dorm Roast:**\n"
        "To the roommate who swept dust *under the rug* hoping gravity would dissolve it: "
        "you have been awarded the **'Master of Broom Physics 2026'** 🏅. "
        "Also, whoever left the half-empty shai mug on the shelf — science called, your tea has developed its own ecosystem. 🫖🦠\n\n"
        "Keep the dorm clean, guard your Tsidat tokens, and may the broom be with you! 🧹⚡"
    )
