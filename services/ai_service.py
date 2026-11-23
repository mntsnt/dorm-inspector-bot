"""
AI Service Module for Dorm Inspector Bot.
Supports Google Gemini and OpenAI REST APIs with live dorm context.
"""

import logging
import os
import re
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
        or os.getenv("GEMINI_KEY")
        or os.getenv("GOOGLE_GENAI_KEY")
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
        or os.getenv("GEMINI_KEY")
        or os.getenv("GOOGLE_GENAI_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip().strip('"').strip("'")

async def build_dorm_context(dorm_id: int, user_id: Optional[int] = None) -> str:
    """Gather live context from the database to inject into the AI system prompt, including user role."""
    dorm = await db.get_dorm(dorm_id)
    d_name = dorm.get("dorm_name", "Dorm Cleaning Schedule") if dorm else "Dorm Cleaning Schedule"
    fine_amt = dorm.get("fine_amount", 100) if dorm else 100

    from scheduler import get_dorm_week_info
    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    # Ensure members from config fallback are included if DB hasn't populated them
    if user_id and user_id not in members_by_id:
        cfg_m = getattr(config, "MEMBERS_BY_ID", {}).get(user_id)
        if cfg_m:
            members_by_id[user_id] = cfg_m.name

    week_info = get_dorm_week_info(dorm, pairs, members_by_id) if dorm else None
    week_str = "Schedule starting soon"
    duty_str = "Not active"
    c1 = "Cleaner 1"
    c2 = "Cleaner 2"
    record = None
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

    # All Registered Roommates
    all_roommates_str = ", ".join(m["name"] for m in members) if members else "None"

    # Today's Date and Upcoming Schedule (Next 3 weeks)
    today = config.get_current_date()
    today_str = today.strftime("%A, %b %d, %Y")

    from datetime import timedelta
    upcoming_lines = []
    if dorm and pairs:
        current_monday = config.get_monday_of_week(today)
        for i in range(1, 4):
            fut_monday = current_monday + timedelta(weeks=i)
            fw = get_dorm_week_info(dorm, pairs, members_by_id, target_date=fut_monday)
            if fw:
                upcoming_lines.append(f"Week {fw.week_number} ({fw.monday_date.strftime('%b %d')}): {fw.cleaner1_name} & {fw.cleaner2_name}")
    upcoming_str = "; ".join(upcoming_lines) if upcoming_lines else "Schedule starting soon"

    # Resolve speaking user's role context
    user_context = ""
    if user_id:
        user_name = members_by_id.get(user_id, "Roommate")
        dorm_admin_id = dorm.get("admin_id") if dorm else getattr(config, "ADMIN_ID", 0)
        is_admin = (user_id == dorm_admin_id or user_id == getattr(config, "ADMIN_ID", 0))

        if week_info and week_info.week_number >= 1:
            c1_id = week_info.cleaner1_id
            c2_id = week_info.cleaner2_id
            app_id = record.get("approver_id") if record else None

            if user_id == c1_id:
                user_context = (
                    f"\n=== SPEAKING USER'S ROLE THIS WEEK ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: CLEANER ON DUTY\n"
                    f"Partner: {c2}\n"
                    f"Duty Status: YOU ARE ON CLEANING DUTY THIS WEEK with {c2}. Deadline is Friday 8:00 PM. Submit photo proof before deadline.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Instruction: Remind them to complete cleaning with their partner {c2} and submit proof before Friday 8:00 PM."
                )
            elif user_id == c2_id:
                user_context = (
                    f"\n=== SPEAKING USER'S ROLE THIS WEEK ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: CLEANER ON DUTY\n"
                    f"Partner: {c1}\n"
                    f"Duty Status: YOU ARE ON CLEANING DUTY THIS WEEK with {c1}. Deadline is Friday 8:00 PM. Submit photo proof before deadline.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Instruction: Remind them to complete cleaning with their partner {c1} and submit proof before Friday 8:00 PM."
                )
            elif app_id and user_id == app_id:
                user_context = (
                    f"\n=== SPEAKING USER'S ROLE THIS WEEK ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: APPROVER (INSPECTOR)\n"
                    f"Duty Status: YOU DO NOT CLEAN THE ROOM! You are the designated approver. Your job is to inspect and rate {c1} & {c2}'s cleaning once photos are submitted.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Instruction: NEVER tell this user to clean. Remind them to inspect and rate."
                )
            elif user_id in members_by_id or config.is_member(user_id):
                user_context = (
                    f"\n=== SPEAKING USER'S ROLE THIS WEEK ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: OFF-DUTY ROOMMATE\n"
                    f"Duty Status: OFF DUTY. You do NOT have cleaning duty this week! Cleaners on duty are {c1} & {c2}.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Instruction: NEVER tell this user to clean. Acknowledge they are off duty this week."
                )
            elif is_admin:
                user_context = (
                    f"\n=== SPEAKING USER'S ROLE THIS WEEK ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: DORM ADMIN\n"
                    f"Duty Status: Managing the dorm cleaning schedule.\n"
                    f"Instruction: Treat as dorm manager."
                )
        elif is_admin:
            user_context = (
                f"\n=== SPEAKING USER'S ROLE THIS WEEK ===\n"
                f"User: {user_name} (ID: {user_id})\n"
                f"Role: DORM ADMIN\n"
                f"Duty Status: Managing dorm.\n"
                f"Instruction: Treat as dorm manager."
            )

    dorm_code = dorm.get("dorm_code") or "DORM-101" if dorm else "DORM-101"
    dash_url = f"{getattr(config, 'DASHBOARD_URL', 'https://dorm-inspector-bot-1.app.aletcloud.com')}?code={dorm_code}"

    return (
        f"Dorm: {d_name}\n"
        f"Dorm Code: {dorm_code}\n"
        f"Website: {dash_url}\n"
        f"Today's Date: {today_str}\n"
        f"All Roommates: {all_roommates_str}\n"
        f"Current Week: {week_str}\n"
        f"Current Duty: {duty_str}\n"
        f"Upcoming Weeks: {upcoming_str}\n"
        f"Fine Amount: {fine_amt} ETB per missed week (split 50/50 between cleaners)\n"
        f"Tsidat Tokens Leaderboard: {lb_str}\n"
        f"Unpaid Fines: {fines_str}\n"
        f"Rules: Duty announced Monday 8:00 AM. Deadline Friday 8:00 PM. Photo proof required. "
        f"Approver rates 1-5 stars; cleaners earn up to +25 Tsidat tokens. Room roulette /spin picks roommates for chores. "
        f"/swap <name> allows a cleaner to swap duty with a roommate for that week only."
        f"{user_context}"
    )

def sanitize_ai_response(text: str) -> str:
    """Ensure response looks strictly human: strip all emojis, repeated symbols, and markdown noise."""
    if not text:
        return ""

    # Strip emoji Unicode ranges
    emoji_pattern = re.compile(
        "["
        "\U00010000-\U0010ffff"  # Supplemental symbols, pictographs, emojis
        "\u2600-\u27bf"          # Misc symbols & dingbats
        "\u2300-\u23ff"          # Misc technical
        "\u2b50-\u2b55"          # Stars & shapes
        "\ufe00-\ufe0f"          # Variation selectors
        "\u200d"                 # Zero-width joiner
        "]+",
        flags=re.UNICODE
    )
    cleaned = emoji_pattern.sub("", text)

    # Remove markdown bold/italic/code clutter to look natural and human
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")

    # Collapse multiple punctuation symbols (e.g. "!!" -> "!", "??" -> "?", "..." -> ".")
    cleaned = re.sub(r"!{2,}", "!", cleaned)
    cleaned = re.sub(r"\?{2,}", "?", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = re.sub(r"={2,}", "", cleaned)
    cleaned = re.sub(r"\*{2,}", "", cleaned)

    # Collapse extra whitespace
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return cleaned.strip()

SYSTEM_PROMPT = (
    "You are The Inspector, the university dorm inspector.\n"
    "You are a real human writing a message in a Telegram dorm group chat or private DM. You are NOT an AI, bot, or virtual assistant.\n\n"
    "STRICT RULES:\n"
    "1. NO EMOJIS: Never use any emojis under any circumstances. Zero emojis allowed.\n"
    "2. HUMAN-LIKE AND NATURAL: Write normally like a real person sending a message. No bot greetings, bullet points, headers, or corporate phrasing.\n"
    "3. NO MULTIPLE SYMBOLS: Never repeat punctuation marks (no '!!', '???', '...'). Do not use asterisks, bold tags, or markdown styling.\n"
    "4. STRICT BREVITY: Answer in 1 or 2 short sentences only (under 25 words). Never write multiple lines or paragraphs.\n"
    "5. ROLE-AWARE CONTEXT (CRITICAL):\n"
    "- If the speaking user is a CLEANER on duty: Remind them of their cleaning duty and Friday 8:00 PM deadline, or answer their questions about duty/swap/fines.\n"
    "- If the speaking user is the APPROVER: Acknowledge they are the approver/inspector this week. They DO NOT clean! Never tell an approver to clean.\n"
    "- If the speaking user is an OFF-DUTY ROOMMATE: Acknowledge they are off-duty while the assigned cleaners clean. Never tell an off-duty roommate to clean the room!\n"
    "- If the speaking user asks 'am I cleaning?' or 'is it my turn?': Clearly answer yes or no based on their role.\n"
    "6. BUSY INSPECTOR TONE: You are on rounds. You speak directly, bluntly, and with dry wit.\n"
    "7. ACCURATE FACTS: Use the dorm context accurately for names, duty, tokens, and fines."
)

def extract_gemini_text(data: Any) -> str:
    """Extract generated text from Interactions API, outputs, or generateContent candidates."""
    if not isinstance(data, dict):
        return ""

    # Direct output_text field
    if data.get("output_text"):
        return str(data["output_text"]).strip()

    # Modern Interactions API steps schema
    steps = data.get("steps") or []
    for step in reversed(steps):
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        if step_type in ("model_output", "output", None):
            if step.get("text"):
                return str(step["text"]).strip()
            content = step.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            elif isinstance(content, list):
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("text"):
                        return str(part["text"]).strip()
                    elif isinstance(part, str) and part.strip():
                        return part.strip()
            parts = step.get("parts")
            if isinstance(parts, list):
                for part in reversed(parts):
                    if isinstance(part, dict) and part.get("text"):
                        return str(part["text"]).strip()

    # Outputs field variant
    outputs = data.get("outputs") or []
    for out in reversed(outputs):
        if isinstance(out, dict) and out.get("text"):
            return str(out["text"]).strip()
        elif isinstance(out, str) and out.strip():
            return out.strip()

    # Legacy generateContent candidates schema
    candidates = data.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content", {})
        if isinstance(content, dict):
            parts = content.get("parts", [])
            if parts and isinstance(parts[0], dict) and parts[0].get("text"):
                return str(parts[0]["text"]).strip()

    return ""

def fallback_inspector_reply(
    user_prompt: str,
    dorm_context: str,
    user_id: Optional[int] = None,
    user_name: str = "Roommate"
) -> str:
    """
    Intelligent, zero-breakage in-character fallback response based on live dorm context and user role.
    Guarantees the bot always answers accurately without emojis or ugly tracebacks.
    """
    p_lower = user_prompt.lower()

    # Extract key lines from dorm_context
    context_lines = {}
    for line in dorm_context.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            context_lines[k.strip()] = v.strip()

    current_duty = context_lines.get("Current Duty", "")
    unpaid_fines = context_lines.get("Unpaid Fines", "No unpaid fines. Everyone is clean.")
    tokens = context_lines.get("Tsidat Tokens Leaderboard", "None awarded yet.")
    roommates = context_lines.get("All Roommates", "Roommates")
    user_role = context_lines.get("Role", "UNKNOWN")
    partner = context_lines.get("Partner", "")
    dorm_code = context_lines.get("Dorm Code", "DORM-101")
    website = context_lines.get("Website", f"{getattr(config, 'DASHBOARD_URL', 'https://dorm-inspector-bot-1.app.aletcloud.com')}?code={dorm_code}")

    # Extract cleaner names from Current Duty line if available
    c1 = "Cleaner 1"
    c2 = "Cleaner 2"
    if "Cleaners:" in current_duty:
        try:
            cleaners_part = current_duty.split("Cleaners:")[1].split("|")[0].strip()
            if "&" in cleaners_part:
                c1, c2 = [c.strip() for c in cleaners_part.split("&", 1)]
        except Exception:
            pass

    # 1. Question about user's own cleaning duty ("am i cleaning", "is it my turn", "do i have duty", etc.)
    if any(q in p_lower for q in (
        "am i cleaning", "is it my turn", "do i have duty", "my duty", "am i on duty",
        "do i clean", "am i supposed to clean", "is it me", "should i clean"
    )):
        if user_role == "CLEANER ON DUTY":
            p_text = f" with {partner}" if partner else ""
            return f"Yes, you are on cleaning duty{p_text} this week. Submit photo proof before Friday 8:00 PM."
        elif user_role == "APPROVER (INSPECTOR)":
            return "No, you are the approver this week, not a cleaner. Your job is to inspect and rate."
        elif user_role == "OFF-DUTY ROOMMATE":
            return f"No, you are off duty this week. {c1} and {c2} are cleaning."
        elif user_role == "DORM ADMIN":
            return "You are the dorm admin. You are off duty from cleaning this week."
        elif current_duty:
            return f"{current_duty}. Deadline is Friday 8:00 PM."

    # 2. Question about approver
    if "approv" in p_lower:
        if user_role == "APPROVER (INSPECTOR)":
            return "You are the approver this week. Review the photo submission and rate 1-5 stars."
        if "Approver:" in current_duty:
            app_part = current_duty.split("Approver:")[1].split("|")[0].strip()
            return f"Approver this week is {app_part}. Inspection review is Friday 8:00 PM."
        return "Approver is assigned on Monday. Inspection deadline is Friday 8:00 PM."

    # 3. Question about who is cleaning / duty / schedule
    if any(k in p_lower for k in ("clean", "duty", "schedule", "turn", "who is", "who's", "roster")) and "fine" not in p_lower:
        if user_role == "CLEANER ON DUTY":
            p_text = f"with {partner} " if partner else ""
            return f"You are cleaning {p_text}this week. Submit proof before Friday 8:00 PM."
        elif user_role == "APPROVER (INSPECTOR)":
            return f"{c1} and {c2} are cleaning this week. You are the approver inspecting their work."
        elif user_role == "OFF-DUTY ROOMMATE":
            return f"{c1} and {c2} are cleaning this week. You are off duty."
        if current_duty:
            return f"{current_duty}. Deadline is Friday 8:00 PM."
        return "Duty starts Monday 8:00 AM. Check the schedule before Friday 8:00 PM."

    # 4. Question about fines
    if "fine" in p_lower or "penalty" in p_lower or "debt" in p_lower:
        return f"Fine status: {unpaid_fines}."

    # 5. Question about tokens / Tsidat
    if any(k in p_lower for k in ("token", "tsidat", "coin", "score", "lead", "point")):
        return f"Tsidat leaderboard: {tokens}."

    # 6. Question about swapping
    if "swap" in p_lower:
        if user_role == "CLEANER ON DUTY":
            return "Use /swap <roommate_name> to exchange your cleaning duty for this week."
        return "Cleaners can use /swap <roommate_name> to exchange duty for this week."

    # 7. Question about spinning / roulette
    if "spin" in p_lower or "roulette" in p_lower:
        return "Use /spin <reason> to spin the room roulette."

    # 8. Question about who lives here / roommates
    if any(k in p_lower for k in ("roommate", "member", "who live", "everyone")):
        return f"Room roster: {roommates}."

    # 9. Question about dorm code / website / dashboard
    if any(k in p_lower for k in ("code", "website", "dashboard", "site", "link", "url", "portal")):
        return f"Dorm code is {dorm_code}. View live status at {website}."

    # 10. General / greeting / fallback based on user role
    if user_role == "CLEANER ON DUTY":
        p_text = f" with {partner}" if partner else ""
        return f"You are on duty{p_text} this week. Make sure proof is submitted before Friday 8:00 PM."
    elif user_role == "APPROVER (INSPECTOR)":
        return "You are the approver this week. Submit your rating once the cleaners upload photos."
    elif user_role == "OFF-DUTY ROOMMATE":
        return f"You are off duty this week. Enjoy your week while {c1} and {c2} handle cleaning."
    elif user_role == "DORM ADMIN":
        return "Inspection system is active. All dorm records are up to date."

    return "I am on rounds. Check the schedule for your assigned cleaning week."

async def query_gemini(api_key: str, user_prompt: str, dorm_context: str, user_name: str) -> str:
    """Send question to Google Gemini API using Interactions API with fallback to generateContent."""
    clean_key = api_key.strip().strip('"').strip("'").replace(" ", "").replace("\r", "").replace("\n", "")

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": clean_key,
    }

    prompt_text = (
        f"{SYSTEM_PROMPT}\n\n"
        f"=== LIVE DORM CONTEXT ===\n"
        f"{dorm_context}\n"
        f"=========================\n\n"
        f"Roommate '{user_name}' asks: {user_prompt}"
    )

    interactions_models = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]

    generate_models = [
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
    ]

    last_resp = None

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Primary: Google Interactions API (POST /v1beta/interactions)
        interactions_url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={clean_key}"
        for model in interactions_models:
            payload = {
                "model": model,
                "input": prompt_text,
            }
            try:
                resp = await client.post(interactions_url, headers=headers, json=payload)
                if resp.status_code == 200:
                    text = extract_gemini_text(resp.json())
                    if text:
                        return text
                last_resp = resp
                err_text = ""
                try:
                    err_text = resp.text.lower()
                except Exception:
                    pass

                # If model is deprecated (400) or not found (404), continue to next model
                if resp.status_code in (400, 404) and any(
                    k in err_text for k in ("no longer available", "not found", "deprecated", "model", "unsupported")
                ):
                    logger.warning("Interactions model %s unavailable (%s: %s). Trying next fallback...", model, resp.status_code, err_text[:100])
                    continue
                elif resp.status_code == 404:
                    # Endpoint itself not found on this host, try generateContent endpoint
                    break
                else:
                    break
            except Exception as e:
                logger.warning("Attempt with Interactions model %s failed: %s", model, e)

        # 2. Fallback: Google generateContent API (POST /v1beta/models/{model}:generateContent)
        # Omit temperature to prevent HTTP 400 validation errors on Gemini 3.x / thinking models
        generate_payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "maxOutputTokens": 80,
            },
        }

        for model in generate_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={clean_key}"
            try:
                resp = await client.post(url, headers=headers, json=generate_payload)
                if resp.status_code == 200:
                    text = extract_gemini_text(resp.json())
                    if text:
                        return text
                last_resp = resp
                err_text = ""
                try:
                    err_text = resp.text.lower()
                except Exception:
                    pass

                if resp.status_code in (400, 404, 429):
                    logger.warning("generateContent model %s unavailable (%s: %s). Trying next fallback...", model, resp.status_code, err_text[:100])
                    continue
                else:
                    break
            except Exception as e:
                logger.warning("Attempt with generateContent model %s failed: %s", model, e)

        if last_resp is not None and last_resp.status_code not in (200, 400, 404):
            last_resp.raise_for_status()

    return ""

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
        "max_tokens": 60,
        "temperature": 0.7
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            return choices[0]["message"]["content"].strip()
    return ""

async def ask_ai(user_prompt: str, dorm_id: int, user_name: str = "Roommate", user_id: Optional[int] = None) -> str:
    """
    Main entry point to query the AI assistant with live dorm context and user role.
    Enforces strict multi-tenant isolation and resilient zero-breakage fallbacks:
    - Checks if the dorm has its own configured API key in the database.
    - Falls back to .env key for the primary/default dorm.
    - If key is missing or API call fails, provides accurate in-character response from live DB context.
    """
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    # 1. Fetch this specific dorm's configured API key
    key = await db.get_dorm_ai_key(dorm_id)

    # 2. Only fallback to .env key for the primary/default dorm (protects admin quota)
    is_primary_dorm = dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)
    if not key and is_primary_dorm:
        key = get_api_key()

    dorm_context = await build_dorm_context(dorm_id, user_id=user_id)

    # 3. If no key is configured:
    # - Secondary dorms are instructed to configure their own key (strict isolation)
    # - Primary dorm falls back to accurate in-character live DB context
    if not key:
        if not is_primary_dorm:
            return (
                "AI is not enabled for this dorm yet.\n\n"
                "To activate AI in this group:\n"
                "1. The dorm admin creates a free key at Google AI Studio (https://aistudio.google.com/)\n"
                "2. Send /set_ai_key <your_api_key> in this chat or in private DM with the bot."
            )
        logger.info("No AI key configured for primary dorm %s, using live context fallback.", dorm_id)
        return fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name)

    provider = "openai" if key.startswith("sk-") else "gemini"

    try:
        if provider == "gemini":
            raw_result = await query_gemini(key, user_prompt, dorm_context, user_name)
        else:
            raw_result = await query_openai(key, user_prompt, dorm_context, user_name)

        if raw_result and raw_result.strip():
            return sanitize_ai_response(raw_result)
        return fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name)
    except httpx.HTTPStatusError as e:
        logger.warning("AI API HTTP error: %s - %s", e.response.status_code, e.response.text[:200])
        return fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name)
    except Exception as e:
        logger.warning("AI API call error: %s", e)
        return fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name)

async def generate_dorm_roast(dorm_id: int) -> str:
    """Generate a witty, harmless, and hilarious university dorm roast & awards ceremony."""
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    key = await db.get_dorm_ai_key(dorm_id)
    if not key and (dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)):
        key = get_api_key()

    dorm_context = await build_dorm_context(dorm_id)
    roast_prompt = (
        "Write a short, hilarious, and punchy dorm weekly roast & award for the roommates based on cleaning.\n"
        "1. Praise the cleaners in 1 short sentence.\n"
        "2. Deliver a witty, dry roast about dorm life in 1-2 short sentences.\n"
        "3. Hand out one funny fictional award.\n"
        "Keep it very brief (under 60 words total). At most 1-2 emojis total. No emoji spam or walls of text."
    )

    if key:
        try:
            if key.startswith("sk-"):
                return await query_openai(key, roast_prompt, dorm_context, "Dorm Reporter")
            else:
                return await query_gemini(key, roast_prompt, dorm_context, "Dorm Reporter")
        except Exception as e:
            logger.warning("AI roast generation failed (%s), falling back to witty template", e)

    # Built-in short fallback roast if AI key is offline or unconfigured
    return (
        "🏆 **Dorm Inspector Weekly Awards**\n\n"
        "Cleaners of the Week: Good job keeping the room civilized. Tsidat tokens awarded.\n\n"
        "Roast: To whoever swept the dust under the bed hoping it would vanish: "
        "you earned the **'Master of Broom Physics'** badge. Less stalling, more sweeping."
    )
