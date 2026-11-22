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
    "You are Dorm Inspector Bot, the smart, helpful, witty, and slightly humorous AI assistant "
    "for university dorm cleaning and roommate life.\n"
    "You have access to live dorm data provided in the prompt context.\n"
    "Your instructions:\n"
    "1. Answer dorm questions accurately based on the provided live dorm context (who is cleaning, who is approver, fines, Tsidat tokens, schedule, etc.).\n"
    "2. Help with cleaning tips, roommate chore resolution, and general advice.\n"
    "3. Keep answers concise (1-3 short paragraphs max), formatted nicely with Markdown, emojis, and bullet points if helpful.\n"
    "4. Be friendly, energetic, and playful about keeping the dorm clean!\n"
)

async def query_gemini(api_key: str, user_prompt: str, dorm_context: str, user_name: str) -> str:
    """Send question to Google Gemini API (gemini-1.5-flash)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    full_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"=== LIVE DORM CONTEXT ===\n"
        f"{dorm_context}\n"
        f"=========================\n\n"
        f"Question from roommate '{user_name}': {user_prompt}"
    )
    payload = {
        "contents": [
            {
                "parts": [{"text": full_prompt}]
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
    return "I couldn't generate a response right now. Please try again in a moment!"

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
            {"role": "user", "content": f"{user_name}: {user_prompt}"}
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
    return "I couldn't generate a response right now. Please try again in a moment!"

async def ask_ai(user_prompt: str, dorm_id: int, user_name: str = "Roommate") -> str:
    """Main entry point to query the AI assistant with live dorm context."""
    provider = get_active_provider()
    key = get_api_key()

    if not provider or not key:
        return (
            "🤖 **AI is not enabled yet!**\n\n"
            "Please configure `GEMINI_API_KEY` (from Google AI Studio) or `OPENAI_API_KEY` in your `.env` file to chat with me."
        )

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
