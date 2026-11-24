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

    # Resolve speaking user's role context and behavioral memory
    user_context = ""
    if user_id:
        user_name = members_by_id.get(user_id, "Roommate")
        dorm_admin_id = dorm.get("admin_id") if dorm else getattr(config, "ADMIN_ID", 0)
        is_admin = (user_id == dorm_admin_id or user_id == getattr(config, "ADMIN_ID", 0))

        # Retrieve recent user topics/interactions to understand roommate habits and behavior
        behavior_notes = ""
        try:
            user_past_msgs = await db.get_user_recent_chat_history(user_id, limit=4)
            recent_user_topics = [
                m["content"].strip() for m in user_past_msgs if m.get("content") and len(m["content"].strip()) < 80
            ]
            if recent_user_topics:
                behavior_notes = (
                    f"\nRecent topics from {user_name}: " + " | ".join(f'"{t}"' for t in recent_user_topics) + "\n"
                    f"Behavior Memory: You remember {user_name}'s past conversations and habits. Feel free to reference past topics naturally like a friend who knows them well."
                )
        except Exception:
            pass

        if week_info and week_info.week_number >= 1:
            cleaner_ids = week_info.cleaner_ids
            app_id = record.get("approver_id") if record else None

            if user_id in cleaner_ids:
                other_cleaners = [name for uid, name in zip(cleaner_ids, week_info.cleaner_names) if uid != user_id]
                partner_str = ", ".join(other_cleaners) if other_cleaners else "solo"
                user_context = (
                    f"\n=== SPEAKING USER CONTEXT ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: CLEANER ON DUTY\n"
                    f"Partner: {partner_str}\n"
                    f"Duty Status: YOU ARE ON CLEANING DUTY THIS WEEK with {partner_str}. Deadline is Friday 8:00 PM. Photo proof required.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Guideline: If user asks about their cleaning duty or schedule, answer accurately. "
                    f"If user asks general questions or wants to chat (cooking, study, tech, jokes, campus life), answer helpfully with a wide perspective as a friendly dorm mate.{behavior_notes}"
                )
            elif app_id and user_id == app_id:
                user_context = (
                    f"\n=== SPEAKING USER CONTEXT ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: APPROVER (INSPECTOR)\n"
                    f"Duty Status: YOU DO NOT CLEAN THE ROOM! You are the designated approver. Your job is to inspect and rate {week_info.cleaner_display}'s cleaning on Friday.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Guideline: Never tell this user to clean. If they ask general questions, chat warmly with a wide perspective as a friendly mentor.{behavior_notes}"
                )
            elif user_id in members_by_id or config.is_member(user_id):
                user_context = (
                    f"\n=== SPEAKING USER CONTEXT ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: OFF-DUTY ROOMMATE\n"
                    f"Duty Status: OFF DUTY. You do NOT have cleaning duty this week! Cleaners on duty are {week_info.cleaner_display}.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Guideline: If user asks whether they clean, confirm they are off duty. "
                    f"If user asks general questions or wants to chat (advice, cooking, campus, humor, study), answer thoroughly and warmly with a wide perspective as a friend.{behavior_notes}"
                )
            elif is_admin:
                user_context = (
                    f"\n=== SPEAKING USER CONTEXT ===\n"
                    f"User: {user_name} (ID: {user_id})\n"
                    f"Role: DORM ADMIN\n"
                    f"Duty Status: Dorm manager.\n"
                    f"Guideline: Treat as dorm manager and helpful friend.{behavior_notes}"
                )
        elif is_admin:
            user_context = (
                f"\n=== SPEAKING USER CONTEXT ===\n"
                f"User: {user_name} (ID: {user_id})\n"
                f"Role: DORM ADMIN\n"
                f"Duty Status: Managing dorm.\n"
                f"Guideline: Treat as dorm manager and helpful friend.{behavior_notes}"
            )

    dorm_code = dorm.get("dorm_code") or "DORM-101" if dorm else "DORM-101"
    dash_url = f"{getattr(config, 'DASHBOARD_URL', 'https://inspector.app.aletcloud.com')}?code={dorm_code}"

    return (
        f"Dorm: {d_name}\n"
        f"Dorm Code: {dorm_code}\n"
        f"Website: {dash_url}\n"
        f"Today's Date: {today_str}\n"
        f"All Roommates: {all_roommates_str}\n"
        f"Current Week: {week_str}\n"
        f"Current Duty: {duty_str}\n"
        f"Upcoming Weeks: {upcoming_str}\n"
        f"Fine Amount: {fine_amt} ETB per missed week (split between cleaners)\n"
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

    # Remove markdown bold/italic/code clutter and symbols to look natural and human
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = cleaned.replace("~", "").replace("#", "")

    # Collapse multiple punctuation symbols (e.g. "!!" -> "!", "??" -> "?", "..." -> ".")
    cleaned = re.sub(r"!{2,}", "!", cleaned)
    cleaned = re.sub(r"\?{2,}", "?", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = re.sub(r"={2,}", "", cleaned)
    cleaned = re.sub(r"\*{2,}", "", cleaned)

    # Clean leading bullet symbols to keep sentences flowing naturally
    cleaned = re.sub(r"^\s*[-*•]\s+", "", cleaned, flags=re.MULTILINE)

    # Collapse extra whitespace
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return cleaned.strip()

SYSTEM_PROMPT = (
    "You are The Inspector, the notoriously bossy, sharp-witted, confident, and hilarious university dorm inspector.\n"
    "You live in this dorm with the students and chat with them in their Telegram group and private DMs.\n\n"
    "YOUR DUAL PERSONALITY (BOSSY INSPECTOR + CHATTING ROOMMATE COMPANION — FOLLOW CHAT LIKE CHATGPT):\n"
    "1. FOLLOW THE CONVERSATION NATURALLY: Carry on a real, dynamic multi-turn conversation just like ChatGPT does. Listen carefully to what the roommate actually says, refer back to previous messages in the chat history, and reply directly to their specific topic and ideas.\n"
    "2. ANSWER QUESTIONS DIRECTLY: When a roommate asks any question (about studying, exams, coding, physics, math, cooking, movies, life dilemmas, campus gossip, or everyday advice), ANSWER IT DIRECTLY, intelligently, and helpfully. Do not dodge the question.\n"
    "3. STILL BOSSY, CONFIDENT & IN CHARGE: Keep your signature bossy Inspector attitude! You run this dorm, you command respect, and you hold roommates to high standards. Use sharp, comedic authority ('Listen up', 'Pay attention so I don't repeat myself', 'Rule number one in this dorm', 'Don't test my patience'). You mentor them like a loud, demanding, caring friend who wants them to succeed.\n"
    "4. NO REPETITIVE CANNED FORMULAS: NEVER repeat the same greetings, opening lines, or standard template phrases. Every response must feel fresh, responsive, and unique.\n"
    "5. ONLY MENTION CLEANING WHEN ASKED: You have live dorm facts below for reference. But CRITICAL: DO NOT constantly tell roommates 'you are on duty' or 'you are off duty' unless they explicitly ask about cleaning, who cleans, or the chore schedule! When chatting about other topics, focus 100% on what was asked.\n"
    "6. HUMAN TEXT, LESS SYMBOLS: Write in clean, natural human sentences. Avoid symbol clutter: no asterisks, no hashtag headers, no markdown bullet lists, no symbol spam, and zero emoji spam. Write smooth, complete human sentences like a real person typing on Telegram.\n"
    "7. COMPLETE EXPLANATIONS — NEVER CUT OFF SENTENCES: Walk through explanations thoroughly from start to finish. Never leave a sentence half-finished or cut off in the middle. Always end with a complete sentence and proper punctuation."
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
    user_name: str = "Roommate",
    history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """
    Intelligent, zero-breakage in-character fallback response based on live dorm context and user role.
    Guarantees the bot always answers accurately without emojis or ugly tracebacks.
    Acts as a wise, friendly dorm mate who can chat about anything while maintaining accurate dorm duty facts.
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
    website = context_lines.get("Website", f"{getattr(config, 'DASHBOARD_URL', 'https://inspector.app.aletcloud.com')}?code={dorm_code}")

    # Extract cleaner names from Current Duty line if available
    c1 = "Cleaner 1"
    c2 = "Cleaner 2"
    cleaners_display = "Bilisuma & Nahom"
    if "Cleaners:" in current_duty:
        try:
            cleaners_part = current_duty.split("Cleaners:")[1].split("|")[0].strip()
            cleaners_display = cleaners_part
            if "&" in cleaners_part:
                c_names = [c.strip() for c in cleaners_part.split("&")]
                c1 = c_names[0]
                c2 = c_names[1] if len(c_names) > 1 else ""
        except Exception:
            pass

    # 1. Question about user's own cleaning duty ("am i cleaning", "is it my turn", "do i have duty", etc.)
    if any(q in p_lower for q in (
        "am i cleaning", "is it my turn", "do i have duty", "my duty", "am i on duty",
        "do i clean", "am i supposed to clean", "is it me", "should i clean", "am i free"
    )):
        name_str = f" {user_name}" if user_name and user_name != "Roommate" else ""
        if user_role == "CLEANER ON DUTY":
            p_text = f" with {partner}" if partner else ""
            return f"Yes{name_str}, you are on cleaning duty{p_text} this week. Submit photo proof before Friday 8:00 PM."
        elif user_role == "APPROVER (INSPECTOR)":
            return f"No{name_str}, you are the approver this week, not a cleaner. Your job is to inspect and rate."
        elif user_role == "OFF-DUTY ROOMMATE":
            return f"No{name_str}, you are off duty this week. {cleaners_display} are cleaning."
        elif user_role == "DORM ADMIN":
            return f"You are the dorm admin{name_str}. You are off duty from cleaning this week."
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

    # 3. Question about who is cleaning / duty / schedule / rules
    if any(k in p_lower for k in ("clean", "duty", "schedule", "turn", "who is", "who's", "roster")) and "fine" not in p_lower:
        if any(e in p_lower for e in ("explain", "how do", "how to", "what are", "rules", "system")):
            return f"Here is the cleaning system, {user_name}: duty is announced every Monday at 8:00 AM. Cleaners have until Friday 8:00 PM to deep clean the room and submit photo proof. The designated approver inspects and rates 1-5 stars. Miss the deadline, and you get fined. Simple, disciplined, and spotless."
        if user_role == "CLEANER ON DUTY":
            p_text = f"with {partner} " if partner else ""
            return f"You are cleaning {p_text}this week. Submit proof before Friday 8:00 PM."
        elif user_role == "APPROVER (INSPECTOR)":
            return f"{cleaners_display.replace('&', 'and')} are cleaning this week. You are the approver inspecting their work."
        elif user_role == "OFF-DUTY ROOMMATE":
            return f"{cleaners_display.replace('&', 'and')} are cleaning this week. You are off duty."
        if current_duty:
            return f"{cleaners_display} are cleaning this week. Deadline is Friday 8:00 PM."
        return f"{cleaners_display} are cleaning this week. Deadline is Friday 8:00 PM."

    # 4. Question about fines
    if "fine" in p_lower or "penalty" in p_lower or "debt" in p_lower:
        if any(e in p_lower for e in ("explain", "how do", "how to", "what is", "why", "rule")):
            return f"Listen up {user_name}: the fine rule in this dorm is simple and non-negotiable. If you fail to submit photo proof before Friday 8:00 PM, you and your cleaning partner split a 100 ETB fine (50 ETB each). Keep your floors clean, or pay up."
        return f"Fine status: {unpaid_fines}."

    # 5. Question about tokens / Tsidat
    if any(k in p_lower for k in ("token", "tsidat", "coin", "score", "lead", "point")):
        if any(e in p_lower for e in ("explain", "how do", "how to", "what is", "why", "rule", "work")):
            return f"Listen up {user_name}: Tsidat tokens are our dorm currency for cleanliness and discipline! Cleaners earn up to +25 tokens when their photo proof gets a 5-star rating from the approver. The cleaner the dorm, the higher you climb on the leaderboard."
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

    # 10. Greetings & Casual Banter (Friendly Roommate Chat)
    if any(w in p_lower for w in ("hello", "hi", "hey", "sup", "yo", "morning", "evening", "what's up", "wassup")):
        if p_lower.strip() in ("hello inspector", "hi inspector", "hey inspector") or (
            "inspector" in p_lower and not any(k in p_lower for k in (
                "how", "what", "why", "where", "can", "tell", "joke", "food", "cook", "study", "exam", "explain", "help"
            )) and any(p_lower.strip().startswith(g) for g in ("hello", "hi", "hey"))
        ):
            if user_role == "CLEANER ON DUTY":
                p_text = f" with {partner}" if partner else ""
                return f"You are on duty{p_text} this week. Make sure proof is submitted before Friday 8:00 PM."
            elif user_role == "APPROVER (INSPECTOR)":
                return "You are the approver this week. Submit your rating once the cleaners upload photos."
            elif user_role == "OFF-DUTY ROOMMATE":
                return f"You are off duty this week. Enjoy your week while {c1} and {c2} handle cleaning."

        greetings = [
            f"Listen up {user_name}! Inspector on dorm rounds. Everything clean and in order? What's on your mind?",
            f"Attention {user_name}! The Inspector is listening. What's the situation in the dorm?",
            f"Well look who decided to check in with the Inspector! What do you need, {user_name}?",
            f"Inspector here, {user_name}! Keeping this room civilized. How can I help you today?"
        ]
        idx = (sum(ord(c) for c in user_name) + len(user_prompt)) % len(greetings)
        return greetings[idx]

    # 11. "How are you" / "What are you doing"
    if any(w in p_lower for w in ("how are you", "how are u", "what are you doing", "what r u doing", "how's it going", "how is it going")):
        check_ins = [
            f"Running a tight ship in this dorm as always, {user_name}! Making sure no one slacks off. How are your classes going?",
            f"On high alert during dorm rounds, {user_name}! Keeping the room organized and making sure everyone does their part. What's up?"
        ]
        return check_ins[len(user_prompt) % len(check_ins)]

    # 12. Jokes & Humor
    if any(w in p_lower for w in ("joke", "funny", "laugh", "humor")):
        jokes = [
            f"Why did the dorm broom get promoted, {user_name}? Because it swept the competition and didn't leave dust under the rug like some roommates!",
            f"Dorm life rule number one: If you can't find a clean plate, a frisbee is officially dinnerware. Don't let me catch you doing that!",
            f"There are two types of roommates, {user_name}: those who clean, and those who treat a clean cup like a luxury. Don't be the second type!"
        ]
        return jokes[len(user_prompt) % len(jokes)]

    # 13. Food & Cooking
    if any(w in p_lower for w in ("food", "eat", "cook", "hungry", "dinner", "lunch", "breakfast", "recipe", "pasta")):
        return f"Listen up, {user_name}: you cannot live on instant noodles alone. Boil some water, toss in pasta with eggs and garlic or cook whatever ingredients you have in the dorm for a proper meal. And you better wash that pan right after—no leaving crusty pans in the sink under my watch!"

    # 14. Studying & Exams
    if any(w in p_lower for w in ("study", "exam", "test", "quiz", "homework", "class", "assignment")):
        return f"Here is your study protocol, {user_name}: put your phone in another room, set a 25-minute Pomodoro focus block, and grind for that exam. If you fail, who am I going to boss around in this dorm? Get to work and ace it!"

    # 15. Thanks & Appreciation
    if any(w in p_lower for w in ("thank", "thanks", "thx", "appreciate", "good job", "cool")):
        return f"You're welcome {user_name}! Just keep this dorm in top shape and we will have zero issues."

    # 16. Explanation requests
    if any(w in p_lower for w in ("explain", "how do", "how does", "what is", "why is", "tell me about", "teach me", "can you explain")):
        return (
            f"Listen up, {user_name}: as your Inspector, I expect everyone in this dorm to be sharp, disciplined, and knowledgeable. "
            f"To get full, in-depth AI explanations on complex topics like '{user_prompt[:40]}', make sure your dorm admin has configured an AI key with /set_ai_key. "
            "In the meantime: stay focused, don't leave dirty dishes in the sink, and ask me any specific dorm questions you have!"
        )

    # 17. Multi-turn context awareness (if asking about previous turns)
    if history and any(w in p_lower for w in ("earlier", "before", "you said", "repeat", "what did")):
        last_asst = next((h["content"] for h in reversed(history) if h.get("role") == "assistant"), None)
        if last_asst:
            return f"Earlier we were talking about: {last_asst[:120]}"

    # 18. General friendly conversation fallback (dynamic, conversational, varied)
    if "?" in user_prompt or any(q in p_lower for q in ("can you", "what", "how", "why", "who", "where", "when", "is it", "should i")):
        q_replies = [
            f"Listen up {user_name}. To answer you: keep your standards high, think it through, and make the smart play. What else is on your mind?",
            f"Here is my take {user_name}: do not take shortcuts and handle your business. In this dorm we do things right. What else do you need?",
            f"Good question {user_name}. Stay focused on the prize, do not get distracted, and keep grinding. What is next?"
        ]
        return q_replies[(len(user_prompt) + len(user_name)) % len(q_replies)]

    chat_replies = [
        f"I hear you, {user_name}. Keep your corner clean and stay disciplined, and we will get along great. What is new with you?",
        f"Noted, {user_name}! I have my eyes on this dorm, so keep up the good work and do not slack off. What is on your mind?",
        f"Fair point, {user_name}. Just make sure you stay on track and maintain high standards around here. What is next?",
        f"Listen, {user_name}: as your Inspector, I keep this dorm running and I keep you on track. Tell me what you need."
    ]
    return chat_replies[(len(user_prompt) + sum(ord(c) for c in user_name)) % len(chat_replies)]

async def query_gemini(
    api_key: str,
    user_prompt: str,
    dorm_context: str,
    user_name: str,
    history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Send question to Google Gemini API with conversational memory and wide perspective."""
    clean_key = api_key.strip().strip('"').strip("'").replace(" ", "").replace("\r", "").replace("\n", "")

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": clean_key,
    }

    # Format multi-turn conversation memory for prompt context
    history_str = ""
    if history:
        hist_lines = []
        for h in history[-8:]:
            speaker = h.get("user_name") or ("The Inspector" if h.get("role") == "assistant" else "Roommate")
            c_text = h.get("content", "").strip()
            if c_text:
                hist_lines.append(f"{speaker}: {c_text}")
        if hist_lines:
            history_str = "\n=== RECENT CONVERSATION HISTORY & BEHAVIOR ===\n" + "\n".join(hist_lines) + "\n=============================================\n\n"

    full_system_context = (
        f"{SYSTEM_PROMPT}\n\n"
        f"=== LIVE DORM CONTEXT ===\n"
        f"{dorm_context}\n"
        f"========================="
    )

    prompt_text = (
        f"{full_system_context}\n\n"
        f"{history_str}"
        f"Roommate '{user_name}' asks: {user_prompt}"
    )

    # Format structured multi-turn contents array for generateContent API
    multi_turn_contents: List[Dict[str, Any]] = []
    if history:
        last_role = None
        for h in history[-8:]:
            role = "user" if h.get("role") == "user" else "model"
            txt = h.get("content", "").strip()
            if not txt:
                continue
            if role == "user" and h.get("user_name"):
                txt = f"{h['user_name']}: {txt}"
            if role == last_role and multi_turn_contents:
                multi_turn_contents[-1]["parts"][0]["text"] += f"\n{txt}"
            else:
                multi_turn_contents.append({"role": role, "parts": [{"text": txt}]})
                last_role = role

        # Ensure first turn is from user as required by Gemini specification
        if multi_turn_contents and multi_turn_contents[0]["role"] != "user":
            multi_turn_contents.pop(0)

    curr_user_text = f"{user_name}: {user_prompt}" if user_name else user_prompt
    if multi_turn_contents and multi_turn_contents[-1]["role"] == "user":
        multi_turn_contents[-1]["parts"][0]["text"] += f"\n{curr_user_text}"
    else:
        multi_turn_contents.append({"role": "user", "parts": [{"text": curr_user_text}]})

    interactions_models = [
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
    ]

    generate_models = [
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-2.5-flash",
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
        for model in generate_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={clean_key}"
            # Primary payload with system_instruction and structured multi-turn conversation
            generate_payload = {
                "system_instruction": {
                    "parts": [{"text": full_system_context}]
                },
                "contents": multi_turn_contents,
                "generationConfig": {
                    "maxOutputTokens": 1200,
                    "temperature": 0.85,
                    "topP": 0.95,
                },
            }
            try:
                resp = await client.post(url, headers=headers, json=generate_payload)
                if resp.status_code == 200:
                    text = extract_gemini_text(resp.json())
                    if text:
                        return text

                # If system_instruction is not supported by endpoint, retry with flat prompt_text
                if resp.status_code == 400:
                    flat_payload = {
                        "contents": [{"parts": [{"text": prompt_text}]}],
                        "generationConfig": {
                            "maxOutputTokens": 1200,
                            "temperature": 0.85,
                            "topP": 0.95,
                        },
                    }
                    resp_flat = await client.post(url, headers=headers, json=flat_payload)
                    if resp_flat.status_code == 200:
                        text = extract_gemini_text(resp_flat.json())
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

async def query_openai(
    api_key: str,
    user_prompt: str,
    dorm_context: str,
    user_name: str,
    history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Send question to OpenAI API (gpt-4o-mini) with conversation history."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    system_content = f"{SYSTEM_PROMPT}\n\n=== LIVE DORM CONTEXT ===\n{dorm_context}"
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]

    if history:
        for h in history[-8:]:
            role = "user" if h.get("role") == "user" else "assistant"
            txt = h.get("content", "").strip()
            if not txt:
                continue
            if role == "user" and h.get("user_name"):
                txt = f"{h['user_name']}: {txt}"
            messages.append({"role": role, "content": txt})

    messages.append({"role": "user", "content": f"{user_name}: {user_prompt}"})

    payload = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "max_tokens": 1200,
        "temperature": 0.85,
        "top_p": 0.95
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            return choices[0]["message"]["content"].strip()
    return ""

async def ask_ai(
    user_prompt: str,
    dorm_id: int,
    user_name: str = "Roommate",
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None
) -> str:
    """
    Main entry point to query the AI assistant with live dorm context, behavioral memory, and user role.
    Remembers previous conversation history per chat and records roommate interactions.
    Enforces strict multi-tenant isolation and resilient zero-breakage fallbacks:
    - Checks if the dorm has its own configured API key in the database.
    - Falls back to .env key for the primary/default dorm.
    - If key is missing or API call fails, provides accurate in-character response from live DB context.
    """
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    target_chat_id = chat_id if chat_id is not None else dorm_id

    # 1. Fetch conversational history memory for this specific chat
    history: List[Dict[str, Any]] = []
    try:
        history = await db.get_recent_ai_chat_history(target_chat_id, limit=8)
    except Exception as e:
        logger.warning("Could not retrieve AI chat history: %s", e)

    # 2. Fetch this specific dorm's configured API key
    key = await db.get_dorm_ai_key(dorm_id)

    # 3. Only fallback to .env key for the primary/default dorm (protects admin quota)
    is_primary_dorm = dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)
    if not key and is_primary_dorm:
        key = get_api_key()

    dorm_context = await build_dorm_context(dorm_id, user_id=user_id)

    # 4. If no key is configured:
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
        reply = fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name, history=history)
        try:
            await db.add_ai_chat_message(dorm_id, target_chat_id, user_id or 0, user_name, "user", user_prompt)
            await db.add_ai_chat_message(dorm_id, target_chat_id, 0, "The Inspector", "assistant", reply)
        except Exception:
            pass
        return reply

    provider = "openai" if key.startswith("sk-") else "gemini"

    final_reply = ""
    try:
        if provider == "gemini":
            raw_result = await query_gemini(key, user_prompt, dorm_context, user_name, history=history)
        else:
            raw_result = await query_openai(key, user_prompt, dorm_context, user_name, history=history)

        if raw_result and raw_result.strip():
            final_reply = sanitize_ai_response(raw_result)
        else:
            final_reply = fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name, history=history)
    except httpx.HTTPStatusError as e:
        logger.warning("AI API HTTP error: %s - %s", e.response.status_code, e.response.text[:200])
        final_reply = fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name, history=history)
    except Exception as e:
        logger.warning("AI API call error: %s", e)
        final_reply = fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name, history=history)

    # Persist interaction into chat history memory for future turns
    try:
        await db.add_ai_chat_message(dorm_id, target_chat_id, user_id or 0, user_name, "user", user_prompt)
        await db.add_ai_chat_message(dorm_id, target_chat_id, 0, "The Inspector", "assistant", final_reply)
    except Exception as e:
        logger.warning("Could not persist AI chat history: %s", e)

    return final_reply

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
