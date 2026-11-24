"""
AI Service Module for Dorm Inspector Bot.
Supports Google Gemini and OpenAI REST APIs with live dorm context.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx

import config
import db

logger = logging.getLogger(__name__)

def _choose_fresh_reply(
    replies: List[str],
    history: Optional[List[Dict[str, Any]]] = None,
    preferred_index: int = 0
) -> str:
    """Choose a canned reply that has not appeared in the recent assistant history."""
    if not replies:
        return ""

    recent_replies = {
        item.get("content")
        for item in (history or [])[-8:]
        if item.get("role") == "assistant"
    }
    last_reply = next(
        (item.get("content") for item in reversed(history or []) if item.get("role") == "assistant"),
        None
    )
    start = preferred_index % len(replies)
    for offset in range(len(replies)):
        candidate = replies[(start + offset) % len(replies)]
        if candidate not in recent_replies:
            return candidate
    for offset in range(len(replies)):
        candidate = replies[(start + offset) % len(replies)]
        if candidate != last_reply:
            return candidate
    return replies[start]

def resolve_provider_from_key(key: str) -> str:
    """Determine AI provider from key prefix or format."""
    clean = key.strip().strip('"').strip("'")
    if clean.startswith("gsk_"):
        return "groq"
    elif clean.startswith("sk-or-"):
        return "openrouter"
    elif clean.startswith("sk-"):
        return "openai"
    return "gemini"

def get_available_ai_providers(dorm_key: Optional[str] = None, allow_env_fallback: bool = True) -> List[Tuple[str, str]]:
    """
    Returns an ordered list of (provider, api_key) pairs to try in sequence.
    If a dorm has its own configured key, that is tried first.
    If allow_env_fallback is True (for primary dorm), environment keys
    (GEMINI, GROQ, OPENROUTER, OPENAI) are added in order as automatic failovers
    so rate limits (429) or high demand (503) never break live chat.
    """
    providers: List[Tuple[str, str]] = []
    seen_keys = set()

    if dorm_key and dorm_key.strip():
        k = dorm_key.strip().strip('"').strip("'")
        p = resolve_provider_from_key(k)
        providers.append((p, k))
        seen_keys.add(k)

    if allow_env_fallback:
        # 1. Google Gemini
        gemini_k = (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("AI_API_KEY")
            or os.getenv("GEMINI_KEY")
            or os.getenv("GOOGLE_GENAI_KEY")
            or ""
        ).strip().strip('"').strip("'")
        if gemini_k and gemini_k not in seen_keys:
            providers.append(("gemini", gemini_k))
            seen_keys.add(gemini_k)

        # 2. Groq Cloud (Free tier, ultra-fast LLaMA 3.3 / Qwen)
        groq_k = (os.getenv("GROQ_API_KEY") or "").strip().strip('"').strip("'")
        if groq_k and groq_k not in seen_keys:
            providers.append(("groq", groq_k))
            seen_keys.add(groq_k)

        # 3. OpenRouter
        openrouter_k = (os.getenv("OPENROUTER_API_KEY") or "").strip().strip('"').strip("'")
        if openrouter_k and openrouter_k not in seen_keys:
            providers.append(("openrouter", openrouter_k))
            seen_keys.add(openrouter_k)

        # 4. OpenAI
        openai_k = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
        if openai_k and openai_k not in seen_keys:
            providers.append(("openai", openai_k))
            seen_keys.add(openai_k)

    return providers

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
        or os.getenv("GROQ_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
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
    members_uname_by_id = {m["user_id"]: m["username"].lstrip("@") for m in members if m.get("username")}

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
                status = "Cleaners finished cleaning and submitted photo, waiting for approver review."
            elif record.get("done_reported"):
                status = "Cleaners reported cleaning complete, waiting for photo proof."
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

    # All Registered Roommates (Use clean real names in normal conversation)
    all_roommates_str = ", ".join(m["name"] for m in members) if members else "None"

    # Telegram Username Directory (Provided strictly for urgent chat summons/pings)
    uname_parts = [f"{m['name']}: @{m['username'].lstrip('@')}" for m in members if m.get("username")]
    uname_dir_str = ", ".join(uname_parts) if uname_parts else "None"

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
        user_uname = members_uname_by_id.get(user_id)
        user_display = user_name
        uname_tag = f" (@{user_uname})" if user_uname else ""
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
            is_approved_or_completed = bool(
                record and (
                    record.get("final_status") == "completed"
                    or record.get("photo_status") == "approved"
                )
            )
            has_reported_cleaning = bool(
                record and (
                    record.get("done_reported")
                    or record.get("photo_status") in ("submitted", "approved")
                    or record.get("final_status") == "completed"
                )
            )

            if user_id in cleaner_ids:
                other_cleaners = [name for uid, name in zip(cleaner_ids, week_info.cleaner_names) if uid != user_id]
                partner_str = ", ".join(other_cleaners) if other_cleaners else "solo"
                if is_approved_or_completed:
                    app_name = members_by_id.get(app_id, "the approver") if app_id else "the approver"
                    user_context = (
                        f"\n=== SPEAKING USER CONTEXT ===\n"
                        f"User: {user_display} (ID: {user_id})\n"
                        f"Role: CLEANER (DUTY COMPLETED & APPROVED)\n"
                        f"Partner: {partner_str}\n"
                        f"Duty Status: COMPLETED & APPROVED! User already cleaned the dorm this week, submitted photo proof, and was APPROVED by {app_name}. They are DONE for the week! No cleaning, reminders, or proof required.\n"
                        f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                        f"Guideline: Never tell this user their duty is on or that they need to clean. Confirm they have successfully completed their duty and are done for the week.{behavior_notes}"
                    )
                elif record and has_reported_cleaning:
                    photo_submitted = record.get("photo_status") == "submitted"
                    photo_status_text = (
                        "Their photo is submitted and waiting for the approver's review."
                        if photo_submitted
                        else "They still need to send photo proof to the approver."
                    )
                    user_context = (
                        f"\n=== SPEAKING USER CONTEXT ===\n"
                        f"User: {user_display} (ID: {user_id})\n"
                        f"Role: CLEANER (CLEANING REPORTED)\n"
                        f"Partner: {partner_str}\n"
                        f"Duty Status: User already completed the cleaning. {photo_status_text} They must not be asked to clean again.\n"
                        f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                        f"Guideline: Confirm cleaning is done. If photo proof is still missing, ask only for the photo; otherwise tell them to wait for review. Never tell them to clean again.{behavior_notes}"
                    )
                else:
                    user_context = (
                        f"\n=== SPEAKING USER CONTEXT ===\n"
                        f"User: {user_display} (ID: {user_id})\n"
                        f"Role: CLEANER ON DUTY\n"
                        f"Partner: {partner_str}\n"
                        f"Duty Status: YOU ARE ON CLEANING DUTY THIS WEEK with {partner_str}. Deadline is Friday 8:00 PM. Photo proof required.\n"
                        f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                        f"Guideline: If user asks about their cleaning duty or schedule, answer accurately. "
                        f"If user asks general questions or wants to chat (cooking, study, tech, jokes, campus life), answer helpfully with a wide perspective as a friendly dorm mate.{behavior_notes}"
                    )
            elif app_id and user_id == app_id:
                if is_approved_or_completed:
                    user_context = (
                        f"\n=== SPEAKING USER CONTEXT ===\n"
                        f"User: {user_display} (ID: {user_id})\n"
                        f"Role: APPROVER (INSPECTOR)\n"
                        f"Duty Status: YOU ALREADY INSPECTED AND APPROVED THIS WEEK'S CLEANING! Inspection is complete for the week.\n"
                        f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                        f"Guideline: Never tell this user to clean. If they ask general questions, chat warmly with a wide perspective as a friendly mentor.{behavior_notes}"
                    )
                elif has_reported_cleaning:
                    review_status = (
                        "The cleaners submitted photo proof and are waiting for your review."
                        if record and record.get("photo_status") == "submitted"
                        else "The cleaners reported finishing; photo proof is still pending."
                    )
                    user_context = (
                        f"\n=== SPEAKING USER CONTEXT ===\n"
                        f"User: {user_display} (ID: {user_id})\n"
                        f"Role: APPROVER (INSPECTOR)\n"
                        f"Duty Status: YOU DO NOT CLEAN THE ROOM! {review_status}\n"
                        f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                        f"Guideline: Do not say cleaners still need to clean. Ask only for a review when proof is submitted.{behavior_notes}"
                    )
                else:
                    user_context = (
                        f"\n=== SPEAKING USER CONTEXT ===\n"
                        f"User: {user_display} (ID: {user_id})\n"
                        f"Role: APPROVER (INSPECTOR)\n"
                        f"Duty Status: YOU DO NOT CLEAN THE ROOM! You are the designated approver. Your job is to inspect and rate {week_info.cleaner_display}'s cleaning on Friday.\n"
                        f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                        f"Guideline: Never tell this user to clean. If they ask general questions, chat warmly with a wide perspective as a friendly mentor.{behavior_notes}"
                    )
            elif user_id in members_by_id or config.is_member(user_id):
                user_context = (
                    f"\n=== SPEAKING USER CONTEXT ===\n"
                    f"User: {user_display} (ID: {user_id})\n"
                    f"Role: OFF-DUTY ROOMMATE\n"
                    f"Duty Status: OFF DUTY. You do NOT have cleaning duty this week! Cleaners on duty are {week_info.cleaner_display}.\n"
                    f"Admin Status: {'Yes (Dorm Admin)' if is_admin else 'No'}\n"
                    f"Guideline: If user asks whether they clean, confirm they are off duty. "
                    f"If user asks general questions or wants to chat (advice, cooking, campus, humor, study), answer thoroughly and warmly with a wide perspective as a friend.{behavior_notes}"
                )
            elif is_admin:
                user_context = (
                    f"\n=== SPEAKING USER CONTEXT ===\n"
                    f"User: {user_display} (ID: {user_id})\n"
                    f"Role: DORM ADMIN\n"
                    f"Duty Status: Dorm manager.\n"
                    f"Guideline: Treat as dorm manager and helpful friend.{behavior_notes}"
                )
        elif is_admin:
            user_context = (
                f"\n=== SPEAKING USER CONTEXT ===\n"
                f"User: {user_display} (ID: {user_id})\n"
                f"Role: DORM ADMIN\n"
                f"Duty Status: Managing dorm.\n"
                f"Guideline: Treat as dorm manager and helpful friend.{behavior_notes}"
            )

    dorm_code = dorm.get("dorm_code") or "DORM-101" if dorm else "DORM-101"
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    dash_url = f"{dash_base}/?code={dorm_code}"

    return (
        f"Dorm: {d_name}\n"
        f"Dorm Code: {dorm_code}\n"
        f"Website: {dash_url}\n"
        f"Today's Date: {today_str}\n"
        f"All Roommates: {all_roommates_str}\n"
        f"Telegram Username Directory (Use ONLY when urgently summoning someone to look at the chat): {uname_dir_str}\n"
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
    """Ensure response looks strictly human: strip all emojis, and format markdown cleanly for Telegram rendering."""
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

    # Format markdown headers (e.g. '### Step 1' -> '*Step 1*')
    cleaned = re.sub(r"^\s*#{1,6}\s+(.+)$", r"*\1*", cleaned, flags=re.MULTILINE)

    # Format bullet points (* Item or - Item) into clean bullets: '• Item'
    cleaned = re.sub(r"^\s*[-*•]\s+", "• ", cleaned, flags=re.MULTILINE)

    # Collapse multiple punctuation symbols
    cleaned = re.sub(r"!{2,}", "!", cleaned)
    cleaned = re.sub(r"\?{2,}", "?", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = re.sub(r"={2,}", "", cleaned)

    # Check if this is a long text or structured explanation
    is_long_or_structured = ("\n" in cleaned.strip() or len(cleaned.strip()) > 90)

    if is_long_or_structured:
        # Convert double asterisks (**bold**) to single asterisks (*bold*) for Telegram Markdown rendering
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", cleaned)
    else:
        # For short single-line casual remarks, strip markdown bold clutter
        cleaned = cleaned.replace("**", "").replace("__", "")

    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
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
    "6. FORMATTING FOR LONG EXPLANATIONS (STRICTLY NO EMOJIS):\n"
    "   - ZERO EMOJIS: Never use emojis under any circumstances! Keep the text completely emoji-free.\n"
    "   - BEAUTIFULLY FORMATTED FOR LONG TEXTS & EXPLANATIONS: When explaining a topic, breaking down steps, giving advice, sharing recipes, or answering complex questions, make full use of clean formatting: bold headers and key terms (*Step 1*, *Important*), clean bullet points (•), numbered lists (1., 2.), and paragraph breaks. Make long answers structured, easy to read, and organized like a well-structured guide.\n"
    "   - For short 1-2 sentence banter, keep it brief and natural without unnecessary bullet points.\n"
    "7. COMPLETE EXPLANATIONS — NEVER CUT OFF SENTENCES: Walk through explanations thoroughly from start to finish. Never leave a sentence half-finished or cut off in the middle. Always end with a complete sentence and proper punctuation.\n"
    "8. FLEXIBLE RESPONSE LENGTH (MATCH THE CHAT & CONTEXT):\n"
    "   - SHORT & PUNCHY (1-2 sentences): When a roommate sends casual banter, short greetings, jokes, quick check-ins, or simple yes/no questions, keep your reply SHORT and punchy! Never write long lectures for short remarks. Example: 'I hear you, Nahom. Keep that kitchen clean and don't slack off.'\n"
    "   - MEDIUM (2-4 sentences): For normal questions, routine advice, sharing opinions, or chore status checks.\n"
    "   - DETAILED & STRUCTURED (Longer): ONLY when a roommate explicitly asks you to explain something in-depth, break down a topic, write a recipe, or teach a concept. Format it with clean bold headings, bullet points, and steps.\n"
    "9. USE REAL NAMES IN NORMAL CONVERSATION (RESERVE @username ONLY FOR URGENT CHAT SUMMONS):\n"
    "   - DEFAULT TO REAL NAMES: In regular conversation, general questions, explanations, casual banter, routine duty checks, and normal updates, ALWAYS use roommates' real names (e.g. 'Bilisuma and Nahom are cleaning', 'Good morning Mintesnot', 'Hassan is the approver'). NEVER append or spam (@username) in normal talk, as this causes unwanted notification pings!\n"
    "   - ONLY USE @username WHEN URGENTLY SUMMONING THEM TO LOOK AT THE CHAT: Tag a roommate with their @username ONLY when you genuinely need them to receive an urgent notification and look at the chat right now! Specific valid cases:\n"
    "     * Urgently calling out a cleaner whose Friday 8:00 PM deadline is approaching or overdue to look at the chat (e.g. '@nahom_t check the chat and submit cleaning proof now!').\n"
    "     * Alerting the approver that cleaning photos were submitted and they must inspect (e.g. '@hassan_k look at the chat and inspect the proof now').\n"
    "     * Warning someone about impending fines or penalties.\n"
    "     * When another roommate in the chat asks you to ping, summon, or alert someone to look at the chat.\n"
    "   - In all normal chats and routine questions, strictly use their real name."
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
    fine_amount_match = re.search(r"Fine Amount:\s*(\d+)\s*ETB", dorm_context)
    fine_amount = int(fine_amount_match.group(1)) if fine_amount_match else 100
    dorm_code = context_lines.get("Dorm Code", "DORM-101")
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    website = context_lines.get("Website", f"{dash_base}/?code={dorm_code}")

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

    # 0. Urgent summons / callouts / notifications to check chat
    if any(k in p_lower for k in ("ping", "summon", "alert", "call out", "notify", "tell to look", "look at chat", "check chat", "look at the chat")):
        uname_dir = context_lines.get("Telegram Username Directory (Use ONLY when urgently summoning someone to look at the chat)", "")
        # Check if a specific roommate name was targeted
        for entry in uname_dir.split(","):
            if ":" in entry:
                m_name, m_tag = [s.strip() for s in entry.split(":", 1)]
                if m_name.lower() in p_lower:
                    return f"{m_tag} Look at the chat! You are urgently needed here."
        if any(c in p_lower for c in ("cleaner", "cleaners", "duty")):
            c_tags = []
            for c_name in [c1, c2]:
                for entry in uname_dir.split(","):
                    if ":" in entry and c_name.lower() in entry.lower():
                        c_tags.append(entry.split(":", 1)[1].strip())
            if c_tags:
                return f"{' '.join(c_tags)} Look at the chat! Check your cleaning duty and deadline right now."

    is_duty_done = "completed" in current_duty.lower() or "approved" in current_duty.lower() or "COMPLETED" in user_role
    is_cleaning_reported = (
        "CLEANING REPORTED" in user_role
        or "cleaners finished cleaning" in current_duty.lower()
        or "cleaners reported cleaning complete" in current_duty.lower()
    )
    photo_waiting_review = "submitted photo" in current_duty.lower()

    def reported_cleaning_reply() -> str:
        if photo_waiting_review:
            return "You have already cleaned and sent the photo. The approver is reviewing it; you are done cleaning for the week."
        return "You have already cleaned. Please send the required photo proof if you have not; you do not need to clean again."

    def reported_cleaning_status() -> str:
        if photo_waiting_review:
            return f"{cleaners_display} have finished cleaning and submitted photo proof; the approver's review is pending."
        return f"{cleaners_display} have reported the cleaning complete; photo proof is still pending."

    # 1. Question about user's own cleaning duty ("am i cleaning", "is it my turn", "do i have duty", etc.)
    if any(q in p_lower for q in (
        "am i cleaning", "is it my turn", "do i have duty", "my duty", "am i on duty",
        "do i clean", "am i supposed to clean", "is it me", "should i clean", "am i free"
    )):
        name_str = f" {user_name}" if user_name and user_name != "Roommate" else ""
        if user_role.startswith("CLEANER") and is_duty_done:
            return f"You have already completed your cleaning duty this week{name_str}, and it was approved! You are done for the week. Great job!"
        elif user_role.startswith("CLEANER") and is_cleaning_reported:
            return reported_cleaning_reply()
        elif user_role == "CLEANER ON DUTY":
            p_text = f" with {partner}" if partner else ""
            return f"Yes{name_str}, you are on cleaning duty{p_text} this week. Submit photo proof before Friday 8:00 PM."
        elif user_role == "APPROVER (INSPECTOR)":
            if is_duty_done:
                return f"You are the approver this week{name_str}. You have already inspected and approved this week's cleaning."
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
            return (
                f"*Dorm Cleaning System*\n\n"
                f"Listen up {user_name}, here is how the dorm inspection works:\n"
                f"• *Announcement:* Every Monday at 8:00 AM duty is assigned.\n"
                f"• *Execution:* Cleaners deep clean the room and submit photo proof.\n"
                f"• *Deadline:* Friday 8:00 PM sharp.\n"
                f"• *Approver Review:* Inspects and awards 1-5 stars (up to +25 Tsidat tokens).\n"
                f"• *Penalty:* Miss the deadline, and you split a 100 ETB fine.\n\n"
                f"Stay disciplined and keep the floors spotless."
            )
        if user_role.startswith("CLEANER") and is_duty_done:
            return f"This week's cleaning duty has already been completed by {cleaners_display} and approved! You are done for the week."
        elif user_role.startswith("CLEANER") and is_cleaning_reported:
            return reported_cleaning_reply()
        elif user_role == "CLEANER ON DUTY":
            p_text = f"with {partner} " if partner else ""
            return f"You are cleaning {p_text}this week. Submit proof before Friday 8:00 PM."
        elif user_role == "APPROVER (INSPECTOR)":
            if is_duty_done:
                return f"This week's cleaning has already been completed by {cleaners_display.replace('&', 'and')} and you approved it!"
            if is_cleaning_reported:
                return reported_cleaning_status() + " You are the approver."
            return f"{cleaners_display.replace('&', 'and')} are cleaning this week. You are the approver inspecting their work."
        elif user_role == "OFF-DUTY ROOMMATE":
            if is_duty_done:
                return f"This week's cleaning has already been completed by {cleaners_display.replace('&', 'and')} and approved. You are off duty."
            if is_cleaning_reported:
                return reported_cleaning_status() + " You are off duty."
            return f"{cleaners_display.replace('&', 'and')} are cleaning this week. You are off duty."
        if current_duty:
            if is_duty_done:
                return f"{cleaners_display} completed cleaning for this week (Approved)."
            return f"{cleaners_display} are cleaning this week. Deadline is Friday 8:00 PM."
        return f"{cleaners_display} are cleaning this week. Deadline is Friday 8:00 PM."

    # 4. Question about fines
    if "fine" in p_lower or "penalty" in p_lower or "debt" in p_lower:
        if any(e in p_lower for e in ("explain", "how do", "how to", "what is", "why", "rule")):
            return (
                f"*Dorm Fine Policy*\n\n"
                f"Listen up {user_name}: the fine rule in this dorm is simple and non-negotiable:\n"
                f"• *Deadline:* Friday 8:00 PM.\n"
                f"• *Missed Cleaning:* {fine_amount} ETB total fine per missed week.\n"
                f"• *Split:* Shared equally between both cleaners ({fine_amount // 2} ETB each).\n\n"
                f"Submit your photo proof on time, or pay up."
            )
        return f"Fine status: {unpaid_fines}."

    # 5. Question about tokens / Tsidat
    if any(k in p_lower for k in ("token", "tsidat", "coin", "score", "lead", "point")):
        if any(e in p_lower for e in ("explain", "how do", "how to", "what is", "why", "rule", "work")):
            return (
                f"*Tsidat Tokens & Leaderboard*\n\n"
                f"Listen up {user_name}: as your Inspector, I expect everyone in this dorm to be disciplined and knowledgeable about our currency:\n"
                f"• *Earning Tokens:* Cleaners earn up to +25 tokens when their photo proof gets a 5-star rating from the approver.\n"
                f"• *Star Ratings:* 5 stars = 25 tokens, 4 stars = 15 tokens, 3 stars = 5 tokens.\n"
                f"• *Prestige:* Higher tokens climb the leaderboard for dorm prestige and respect.\n\n"
                f"Keep your living space shining and climb to the top!"
            )
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

    # 9b. Question about AI status, why repeating, or if AI is working
    if any(k in p_lower for k in ("is the ai", "is ai", "ai work", "why do you repeat", "saying the same", "same thing", "are you real", "are you a bot", "are you broken", "ai status", "ai broken", "not working")):
        return (
            f"Listen up, {user_name}! Live AI is unavailable for this request, so I am using built-in responses. "
            "Check /ai_status for the provider state, or ask your dorm admin to configure an AI key with /set_ai_key."
        )

    # 10. Greetings & Casual Banter (Friendly Roommate Chat)
    if any(w in p_lower for w in ("hello", "hi", "hey", "sup", "yo", "morning", "evening", "what's up", "wassup")):
        if p_lower.strip() in ("hello inspector", "hi inspector", "hey inspector") or (
            "inspector" in p_lower and not any(k in p_lower for k in (
                "how", "what", "why", "where", "can", "tell", "joke", "food", "cook", "study", "exam", "explain", "help"
            )) and any(p_lower.strip().startswith(g) for g in ("hello", "hi", "hey"))
        ):
            if user_role.startswith("CLEANER") and is_duty_done:
                return f"You completed and passed this week's cleaning duty! Well done {user_name}."
            elif user_role.startswith("CLEANER") and is_cleaning_reported:
                return reported_cleaning_reply()
            elif user_role == "CLEANER ON DUTY":
                p_text = f" with {partner}" if partner else ""
                return f"You are on duty{p_text} this week. Make sure proof is submitted before Friday 8:00 PM."
            elif user_role == "APPROVER (INSPECTOR)":
                if is_duty_done:
                    return "You have already approved this week's cleaning! Inspection is all done."
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
        return _choose_fresh_reply(greetings, history, idx)

    # 11. "How are you" / "What are you doing"
    if any(w in p_lower for w in ("how are you", "how are u", "what are you doing", "what r u doing", "how's it going", "how is it going")):
        check_ins = [
            f"Running a tight ship in this dorm as always, {user_name}! Making sure no one slacks off. How are your classes going?",
            f"On high alert during dorm rounds, {user_name}! Keeping the room organized and making sure everyone does their part. What's up?"
        ]
        return _choose_fresh_reply(check_ins, history, len(user_prompt) % len(check_ins))

    # 12. Jokes & Humor
    if any(w in p_lower for w in ("joke", "funny", "laugh", "humor")):
        jokes = [
            f"Why did the dorm broom get promoted, {user_name}? Because it swept the competition and didn't leave dust under the rug like some roommates!",
            f"Dorm life rule number one: If you can't find a clean plate, a frisbee is officially dinnerware. Don't let me catch you doing that!",
            f"There are two types of roommates, {user_name}: those who clean, and those who treat a clean cup like a luxury. Don't be the second type!"
        ]
        return _choose_fresh_reply(jokes, history, len(user_prompt) % len(jokes))

    # 13. Food & Cooking
    if any(w in p_lower for w in ("food", "eat", "cook", "hungry", "dinner", "lunch", "breakfast", "recipe", "pasta")):
        food_replies = [
            f"Listen up, {user_name}: you cannot live on instant noodles alone. Boil some water, toss in pasta with eggs and garlic or cook whatever ingredients you have in the dorm for a proper meal. And you better wash that pan right after—no leaving crusty pans in the sink under my watch!",
            f"{user_name}, make a proper meal with what you have—pasta, eggs, and garlic are an easy start. Wash the pan when you're done; the sink is not a storage unit.",
            f"Inspector's kitchen order, {user_name}: add something nutritious to those noodles, clean your dishes straight away, and leave the counter ready for the next roommate."
        ]
        return _choose_fresh_reply(food_replies, history)

    # 14. Studying & Exams
    if any(w in p_lower for w in ("study", "exam", "test", "quiz", "homework", "class", "assignment")):
        study_replies = [
            f"Here is your study protocol, {user_name}: put your phone in another room, set a 25-minute Pomodoro focus block, and grind for that exam. If you fail, who am I going to boss around in this dorm? Get to work and ace it!",
            f"{user_name}, pick one topic, study it without your phone for 25 minutes, then take a short break and test what you remember. One focused block at a time.",
            f"Start with the assignment due soonest, {user_name}. Clear your desk, set a timer, and work until it rings before checking messages. You can do this."
        ]
        return _choose_fresh_reply(study_replies, history)

    # 15. Thanks & Appreciation
    if any(w in p_lower for w in ("thank", "thanks", "thx", "appreciate", "good job", "cool")):
        return f"You're welcome {user_name}! Just keep this dorm in top shape and we will have zero issues."

    # 16. Explanation requests
    if any(w in p_lower for w in ("explain", "how do", "how does", "what is", "why is", "tell me about", "teach me", "can you explain")):
        return (
            f"*Dorm Inspector Briefing*\n\n"
            f"Listen up, {user_name}: as your Inspector, I expect everyone in this dorm to be sharp, disciplined, and knowledgeable.\n\n"
            f"• *In-Depth AI Explanations:* To unlock comprehensive AI explanations on '{user_prompt[:40]}', configure an AI key with /set_ai_key.\n"
            f"• *Dorm Inquiries:* Ask me anytime about cleaning duty, /status, /fines, /tsidat, or /schedule.\n\n"
            f"Stay focused, don't leave dirty dishes in the sink, and ask me any specific dorm questions you have!"
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
        return _choose_fresh_reply(q_replies, history, (len(user_prompt) + len(user_name)) % len(q_replies))

    chat_replies = [
        f"I hear you, {user_name}. Keep your corner clean and stay disciplined, and we will get along great. What is new with you?",
        f"Noted, {user_name}! I have my eyes on this dorm, so keep up the good work and do not slack off. What is on your mind?",
        f"Fair point, {user_name}. Just make sure you stay on track and maintain high standards around here. What is next?",
        f"Listen, {user_name}: as your Inspector, I keep this dorm running and I keep you on track. Tell me what you need."
    ]
    return _choose_fresh_reply(
        chat_replies,
        history,
        (len(user_prompt) + sum(ord(c) for c in user_name)) % len(chat_replies)
    )

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

    # Determine dynamic response length guidance based on chat and context
    p_len = len(user_prompt.strip())
    p_lower = user_prompt.lower()
    is_explanation_request = any(w in p_lower for w in ("explain", "how do", "how does", "what is", "why is", "tell me about", "teach me", "break down", "recipe", "write a", "can you explain"))
    is_short_prompt = not is_explanation_request and p_len < 65

    length_guidance = ""
    if is_short_prompt:
        length_guidance = " (Instruction: The user's input is brief. Respond concisely in 1 to 2 punchy, in-character sentences! Do not lecture.)"
    elif is_explanation_request:
        length_guidance = " (Instruction: Provide a thorough, complete explanation.)"

    prompt_text = (
        f"{full_system_context}\n\n"
        f"{history_str}"
        f"Roommate '{user_name}' asks: {user_prompt}{length_guidance}"
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

    curr_user_text = f"{user_name}: {user_prompt}{length_guidance}" if user_name else f"{user_prompt}{length_guidance}"
    if multi_turn_contents and multi_turn_contents[-1]["role"] == "user":
        multi_turn_contents[-1]["parts"][0]["text"] += f"\n{curr_user_text}"
    else:
        multi_turn_contents.append({"role": "user", "parts": [{"text": curr_user_text}]})

    interactions_models = [
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-latest",
    ]

    generate_models = [
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite-preview",
        "gemini-3-flash-preview",
        "gemini-flash-latest",
        "gemini-pro-latest",
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

                # If model is deprecated (400), not found (404), rate-limited (429) or overloaded (503), try next model
                if resp.status_code in (400, 404, 429, 503):
                    logger.warning("Interactions model %s unavailable (%s: %s). Trying next fallback...", model, resp.status_code, err_text[:100])
                    continue
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

                if resp.status_code in (400, 404, 429, 503):
                    logger.warning("generateContent model %s unavailable (%s: %s). Trying next fallback...", model, resp.status_code, err_text[:100])
                    continue
                else:
                    break
            except Exception as e:
                logger.warning("Attempt with generateContent model %s failed: %s", model, e)

        if last_resp is not None and last_resp.status_code not in (200, 400, 404, 503):
            last_resp.raise_for_status()

    return ""

async def query_openai(
    api_key: str,
    user_prompt: str,
    dorm_context: str,
    user_name: str,
    history: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Send question to OpenAI, Groq, or OpenRouter with conversation history."""
    clean_key = api_key.strip().strip('"').strip("'")
    if clean_key.startswith("gsk_"):
        url = "https://api.groq.com/openai/v1/chat/completions"
        models_to_try = [
            "qwen/qwen3.8-27b",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ]
    elif clean_key.startswith("sk-or-"):
        url = "https://openrouter.ai/api/v1/chat/completions"
        models_to_try = ["meta-llama/llama-3.3-70b-instruct:free", "google/gemini-2.0-flash-exp:free"]
    else:
        url = "https://api.openai.com/v1/chat/completions"
        models_to_try = ["gpt-4o-mini"]

    headers = {
        "Authorization": f"Bearer {clean_key}",
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

    p_len = len(user_prompt.strip())
    p_lower = user_prompt.lower()
    is_explanation_request = any(w in p_lower for w in ("explain", "how do", "how does", "what is", "why is", "tell me about", "teach me", "break down", "recipe", "write a", "can you explain"))
    is_short_prompt = not is_explanation_request and p_len < 65

    length_guidance = ""
    if is_short_prompt:
        length_guidance = " (Instruction: The user's input is brief. Respond concisely in 1 to 2 punchy, in-character sentences! Do not lecture.)"
    elif is_explanation_request:
        length_guidance = " (Instruction: Provide a thorough, complete explanation.)"

    messages.append({"role": "user", "content": f"{user_name}: {user_prompt}{length_guidance}"})

    async with httpx.AsyncClient(timeout=15.0) as client:
        last_exc = None
        for model in models_to_try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": 1200,
                "temperature": 0.85,
                "top_p": 0.95
            }
            try:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        return choices[0]["message"]["content"].strip()
                elif resp.status_code in (400, 404, 429, 503):
                    logger.warning("Model %s unavailable or rate limited on %s (%s). Trying fallback...", model, url, resp.status_code)
                    continue
                else:
                    resp.raise_for_status()
            except Exception as e:
                last_exc = e
                logger.warning("Attempt with %s failed: %s", model, e)
        if last_exc:
            raise last_exc
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
    dorm_key = await db.get_dorm_ai_key(dorm_id)

    # 3. Only fallback to .env keys for the primary/default dorm (protects admin quota)
    is_primary_dorm = dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)
    available_providers = get_available_ai_providers(dorm_key=dorm_key, allow_env_fallback=is_primary_dorm)

    dorm_context = await build_dorm_context(dorm_id, user_id=user_id)

    # 4. If no provider is available:
    # - Secondary dorms are instructed to configure their own key (strict isolation)
    # - Primary dorm falls back to accurate in-character live DB context
    if not available_providers:
        if not is_primary_dorm:
            return (
                "AI is not enabled for this dorm yet.\n\n"
                "To activate AI in this group:\n"
                "1. The dorm admin creates a free key at Google AI Studio (https://aistudio.google.com/) or Groq (https://console.groq.com/)\n"
                "2. Send /set_ai_key <your_api_key> in this chat or in private DM with the bot."
            )
        logger.info("No AI providers configured for primary dorm %s, using live context fallback.", dorm_id)
        reply = fallback_inspector_reply(user_prompt, dorm_context, user_id=user_id, user_name=user_name, history=history)
        try:
            await db.add_ai_chat_message(dorm_id, target_chat_id, user_id or 0, user_name, "user", user_prompt)
            await db.add_ai_chat_message(dorm_id, target_chat_id, 0, "The Inspector", "assistant", reply)
        except Exception:
            pass
        return reply

    final_reply = ""
    last_err: Optional[Exception] = None
    for provider, prov_key in available_providers:
        try:
            if provider == "gemini":
                raw_result = await query_gemini(prov_key, user_prompt, dorm_context, user_name, history=history)
            else:
                raw_result = await query_openai(prov_key, user_prompt, dorm_context, user_name, history=history)

            if raw_result and raw_result.strip():
                final_reply = sanitize_ai_response(raw_result)
                logger.info("AI response successfully generated using provider '%s'", provider)
                break
            else:
                logger.warning("Provider '%s' returned empty output, attempting failover...", provider)
        except httpx.HTTPStatusError as e:
            last_err = e
            logger.warning("AI provider '%s' HTTP %s error: %s. Attempting failover...", provider, e.response.status_code, e.response.text[:200])
        except Exception as e:
            last_err = e
            logger.warning("AI provider '%s' failed (%s). Attempting failover...", provider, e)

    if not final_reply:
        logger.warning("All configured AI providers failed (last error: %s), using in-character fallback reply.", last_err)
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
    dorm_key = await db.get_dorm_ai_key(dorm_id)
    is_primary_dorm = dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)
    providers = get_available_ai_providers(dorm_key=dorm_key, allow_env_fallback=is_primary_dorm)

    dorm_context = await build_dorm_context(dorm_id)
    roast_prompt = (
        "Write a short, hilarious, and punchy dorm weekly roast & award for the roommates based on cleaning.\n"
        "1. Praise the cleaners in 1 short sentence.\n"
        "2. Deliver a witty, dry roast about dorm life in 1-2 short sentences.\n"
        "3. Hand out one funny fictional award.\n"
        "Keep it very brief (under 60 words total). At most 1-2 emojis total. No emoji spam or walls of text."
    )

    for provider, prov_key in providers:
        try:
            if provider == "gemini":
                raw = await query_gemini(prov_key, roast_prompt, dorm_context, "Dorm Reporter")
            else:
                raw = await query_openai(prov_key, roast_prompt, dorm_context, "Dorm Reporter")
            if raw and raw.strip():
                return sanitize_ai_response(raw)
        except Exception as e:
            logger.warning("Roast generation on provider '%s' failed (%s). Trying failover...", provider, e)

    # Built-in short fallback roast if AI key is offline or unconfigured
    return (
        "🏆 **Dorm Inspector Weekly Awards**\n\n"
        "Cleaners of the Week: Good job keeping the room civilized. Tsidat tokens awarded.\n\n"
        "Roast: To whoever swept the dust under the bed hoping it would vanish: "
        "you earned the **'Master of Broom Physics'** badge. Less stalling, more sweeping."
    )
