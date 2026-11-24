"""
Web Status Dashboard & Health Server for Dorm Inspector Bot.
FastAPI-based single-page application and REST API showing real-time bot status,
active dorms, cleaning duties, inspection photo approvals, token leaderboards, and fines.
"""

from datetime import datetime, timezone
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

import config
import db
from scheduler import get_dorm_week_info

logger = logging.getLogger("DormInspectorWeb")

START_TIME = time.time()

app = FastAPI(
    title="Dorm Inspector Bot Status Dashboard",
    description="Real-time multi-tenant monitoring dashboard for Dorm Inspector Bot",
    version="2.0.0",
    docs_url=None,
    redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def on_startup() -> None:
    """Initialize database and seed default dorm if needed on server start."""
    try:
        await db.init_db()
        if getattr(config, "GROUP_ID", 0):
            await db.seed_default_dorm()
    except Exception as e:
        logger.error("Error during web app startup: %s", e)

def get_uptime_seconds() -> int:
    return int(time.time() - START_TIME)

def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", response_class=JSONResponse)
@app.get("/api/health", response_class=JSONResponse)
async def health_check() -> Dict[str, Any]:
    """Lightweight health probe endpoint for Docker / cloud orchestrators."""
    return {
        "status": "healthy",
        "service": "dorm-inspector-bot",
        "uptime": format_uptime(get_uptime_seconds()),
        "uptime_seconds": get_uptime_seconds(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/status", response_class=JSONResponse)
async def get_system_status() -> Dict[str, Any]:
    """Global system status and aggregated platform metrics."""
    active_dorms = await db.get_all_active_dorms()
    
    total_members = 0
    total_fines_etb = 0
    for d in active_dorms:
        d_id = d["dorm_id"]
        members = await db.get_dorm_members(d_id)
        total_members += len(members)
        fines = await db.get_unpaid_fines_summary(d_id)
        total_fines_etb += sum(f.get("total_unpaid", 0) for f in fines)

    return {
        "status": "online",
        "service": "Dorm Inspector Bot",
        "version": "2.0.0",
        "uptime": format_uptime(get_uptime_seconds()),
        "uptime_seconds": get_uptime_seconds(),
        "current_date": config.get_current_date().strftime("%A, %b %d, %Y"),
        "timezone": str(config.TIMEZONE),
        "database": {
            "engine": "SQLite WAL (Concurrent Multi-Tenant)",
            "status": "connected"
        },
        "stats": {
            "active_dorms_count": len(active_dorms),
            "total_members_count": total_members,
            "total_unpaid_fines_etb": total_fines_etb
        }
    }

@app.get("/api/dorms", response_class=JSONResponse)
async def list_active_dorms(request: Request) -> Any:
    """Admin-only endpoint returning summary list of active dorm rooms. Protected for privacy."""
    admin_key = request.headers.get("x-admin-key") or request.query_params.get("admin_key")
    expected_admin = str(getattr(config, "ADMIN_ID", 6163528558))
    if not admin_key or admin_key != expected_admin:
        raise HTTPException(
            status_code=403,
            detail="Public dorm listing is disabled for privacy. Use /api/dorms/code/{dorm_code} with your dorm's code."
        )
    dorms = await db.get_all_active_dorms()
    results = []
    current_date = config.get_current_date()

    for d in dorms:
        d_id = d["dorm_id"]
        pairs = await db.get_dorm_pairs(d_id)
        members = await db.get_dorm_members(d_id)
        members_by_id = {m["user_id"]: m["name"] for m in members}
        
        week_info = get_dorm_week_info(d, pairs, members_by_id, target_date=current_date)
        cleaner1_name = week_info.cleaner1_name if week_info else "TBD"
        cleaner2_name = week_info.cleaner2_name if week_info else "TBD"

        week_record = await db.get_week(week_info.week_start, dorm_id=d_id) if week_info else None
        if not week_info or week_info.week_number < 1:
            duty_status = f"Starts {d.get('start_date', 'Soon')}"
        else:
            duty_status = "In Progress"
            if week_record:
                f_status = week_record.get("final_status", "pending")
                p_status = week_record.get("photo_status", "none")
                if f_status == "completed":
                    cleaned_at = config.format_local_datetime(
                        week_record.get("done_reported_at") or week_record.get("photo_submitted_at")
                    )
                    duty_status = "Cleaned & Approved"
                    if cleaned_at:
                        duty_status += f" • Cleaned at {cleaned_at}"
                elif f_status == "fined":
                    duty_status = "Fined"
                elif p_status == "submitted":
                    cleaned_at = config.format_local_datetime(
                        week_record.get("done_reported_at") or week_record.get("photo_submitted_at")
                    )
                    duty_status = f"Cleaned at {cleaned_at} • Photo Review Pending" if cleaned_at else "Photo Review Pending"
                elif p_status == "approved":
                    duty_status = "Photo Approved"

        results.append({
            "dorm_id": d_id,
            "dorm_name": d["dorm_name"],
            "dorm_code": d.get("dorm_code") or "DORM-101",
            "admin_id": d["admin_id"],
            "fine_amount": d["fine_amount"],
            "start_date": d.get("start_date") or getattr(config, "START_DATE_STR", "2026-11-23"),
            "member_count": len(members),
            "pair_count": len(pairs),
            "current_week": week_info.week_number if week_info else 0,
            "cleaner1": cleaner1_name,
            "cleaner2": cleaner2_name,
            "duty_status": duty_status
        })

    return results

@app.get("/api/dorms/default", response_class=JSONResponse)
async def get_default_dorm() -> Dict[str, Any]:
    """Retrieve detailed live metrics for the default / primary active dorm."""
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    dorm = await db.get_dorm(default_gid)
    if not dorm:
        active_dorms = await db.get_all_active_dorms()
        if active_dorms:
            dorm = active_dorms[0]
    if not dorm:
        dorm = await db.get_dorm_by_code("DEFAULT")
    if not dorm:
        raise HTTPException(status_code=404, detail="No active dorm found in system")
    return await get_dorm_detail(str(dorm["dorm_id"]))

@app.get("/api/dorms/{dorm_identifier}", response_class=JSONResponse)
async def get_dorm_detail(dorm_identifier: str) -> Dict[str, Any]:
    """Detailed live metrics for a specific dorm by ID or dorm code."""
    dorm = None
    clean_id = dorm_identifier.strip()
    if clean_id.lower() in ("default", "active", "main", "primary"):
        default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        dorm = await db.get_dorm(default_gid)
        if not dorm:
            active_dorms = await db.get_all_active_dorms()
            if active_dorms:
                dorm = active_dorms[0]
        if not dorm:
            dorm = await db.get_dorm_by_code("DEFAULT")
    else:
        try:
            dorm_int = int(clean_id)
            dorm = await db.get_dorm(dorm_int)
        except ValueError:
            pass

        if not dorm:
            dorm = await db.get_dorm_by_code(clean_id)

    if not dorm:
        raise HTTPException(status_code=404, detail=f"Dorm '{dorm_identifier}' not found")

    dorm_id = dorm["dorm_id"]
    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    current_date = config.get_current_date()
    week_info = get_dorm_week_info(dorm, pairs, members_by_id, target_date=current_date)
    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id) if week_info else None

    # Current duty status determination
    approver_name = "Not yet assigned"

    if not week_info or week_info.week_number < 1:
        start_d = dorm.get("start_date") or getattr(config, "START_DATE_STR", "Nov 23")
        duty_state = f"Schedule starts Monday, {start_d}"
        status_code = "not_started"
    else:
        duty_state = "In progress (Deadline Friday 8:00 PM)"
        status_code = "in_progress"

        if week_record:
            if week_record.get("approver_id"):
                approver_name = members_by_id.get(week_record["approver_id"], f"ID {week_record['approver_id']}")
            f_status = week_record.get("final_status", "pending")
            p_status = week_record.get("photo_status", "none")
            cleaned_at = config.format_local_datetime(
                week_record.get("done_reported_at") or week_record.get("photo_submitted_at")
            )
            if f_status == "completed":
                rating = week_record.get("rating", 5)
                when = f" • Cleaned at {cleaned_at}" if cleaned_at else ""
                bonus = week_record.get("early_bonus", 0)
                bonus_text = f" • +{bonus} early bonus tokens" if bonus else ""
                duty_state = f"Cleaned & Approved ({rating}★ Stars){when}{bonus_text}"
                status_code = "completed"
            elif f_status == "fined":
                duty_state = f"Missed Deadline (Fined {dorm.get('fine_amount', 100)} ETB)"
                status_code = "fined"
            elif p_status == "submitted":
                duty_state = (
                    f"Cleaned at {cleaned_at} • Photo Submitted (Review Pending)"
                    if cleaned_at else "Photo Submitted (Review Pending)"
                )
                status_code = "submitted"
            elif p_status == "rejected":
                duty_state = (
                    f"Photo Rejected (Needs Recleaning; last cleaned at {cleaned_at})"
                    if cleaned_at else "Photo Rejected (Needs Recleaning)"
                )
                status_code = "rejected"
            elif week_record.get("done_reported"):
                duty_state = (
                    f"Cleaners Reported Cleaned at {cleaned_at} (Photo Awaited)"
                    if cleaned_at else "Cleaners Reported Done (Photo Awaited)"
                )
                status_code = "done_reported"

    # Next 4 weeks schedule preview
    schedule_preview = []
    if pairs:
        current_monday = config.get_monday_of_week(current_date)
        from datetime import timedelta
        for i in range(4):
            tgt_monday = current_monday + timedelta(weeks=i)
            fw = get_dorm_week_info(dorm, pairs, members_by_id, target_date=tgt_monday)
            if fw:
                schedule_preview.append({
                    "week_number": fw.week_number,
                    "week_start": fw.week_start,
                    "monday": fw.monday_date.strftime("%b %d"),
                    "friday": fw.friday_date.strftime("%b %d"),
                    "cleaner1": fw.cleaner1_name,
                    "cleaner2": fw.cleaner2_name,
                    "cleaner3": getattr(fw, "cleaner3_name", ""),
                    "cleaners_display": fw.cleaner_display,
                    "cleaners": fw.cleaner_names,
                    "is_current": (tgt_monday == current_monday)
                })

    leaderboard = await db.get_tsidat_leaderboard(dorm_id)
    fines = await db.get_unpaid_fines_summary(dorm_id)
    history = await db.get_history(limit=6, dorm_id=dorm_id)

    formatted_history = []
    for h in history:
        c1 = members_by_id.get(h["cleaner1_id"], f"ID {h['cleaner1_id']}")
        c2 = members_by_id.get(h["cleaner2_id"], f"ID {h['cleaner2_id']}") if h.get("cleaner2_id") else ""
        c3 = members_by_id.get(h.get("cleaner3_id", 0), f"ID {h.get('cleaner3_id', 0)}") if h.get("cleaner3_id") else ""
        c_names = [n for n in (c1, c2, c3) if n]
        formatted_history.append({
            "week_number": h["week_number"],
            "week_start": h["week_start"],
            "cleaner1": c1,
            "cleaner2": c2,
            "cleaner3": c3,
            "cleaners_display": " & ".join(c_names),
            "final_status": h.get("final_status", "pending"),
            "rating": h.get("rating")
        })

    cleaner_names_list = week_info.cleaner_names if week_info else []
    cleaners_disp = week_info.cleaner_display if week_info else "TBD"

    return {
        "dorm_id": dorm_id,
        "dorm_name": dorm["dorm_name"],
        "dorm_code": dorm.get("dorm_code") or "DORM-101",
        "cleaners_per_week": dorm.get("cleaners_per_week", 2),
        "fine_amount": dorm.get("fine_amount", 100),
        "status": dorm.get("status", "active"),
        "members": [{"user_id": m["user_id"], "name": m["name"], "is_admin": m.get("is_admin", 0)} for m in members],
        "current_week": {
            "week_number": week_info.week_number if week_info else 0,
            "week_start": week_info.week_start if week_info else "",
            "cleaner1": week_info.cleaner1_name if week_info else "TBD",
            "cleaner2": week_info.cleaner2_name if week_info else "",
            "cleaner3": getattr(week_info, "cleaner3_name", "") if week_info else "",
            "cleaners_display": cleaners_disp,
            "cleaners": cleaner_names_list,
            "approver": approver_name,
            "duty_state": duty_state,
            "status_code": status_code,
            "photo_status": week_record.get("photo_status", "none") if week_record else "none",
            "final_status": week_record.get("final_status", "pending") if week_record else "pending",
            "done_reported": bool(week_record.get("done_reported", 0)) if week_record else False,
            "done_reported_at": week_record.get("done_reported_at") if week_record else None,
            "photo_submitted_at": week_record.get("photo_submitted_at") if week_record else None,
            "cleaned_at_display": config.format_local_datetime(
                (week_record.get("done_reported_at") or week_record.get("photo_submitted_at"))
                if week_record else None
            ),
            "early_bonus": week_record.get("early_bonus", 0) if week_record else 0,
            "rating": week_record.get("rating") if week_record else None,
            "review_text": week_record.get("review_text") if week_record else None
        },
        "schedule": schedule_preview,
        "leaderboard": leaderboard,
        "fines": fines,
        "history": formatted_history
    }

@app.get("/api/dorms/code/{dorm_code}", response_class=JSONResponse)
async def get_dorm_by_code_endpoint(dorm_code: str) -> Dict[str, Any]:
    """Detailed live metrics for a dorm looked up by its dorm code."""
    raw = (dorm_code or "").replace("`", "").replace("'", "").replace('"', '').replace("#", "").strip()
    clean_code = raw.upper()
    if not clean_code:
        raise HTTPException(status_code=400, detail="Dorm code cannot be empty")
    dorm = await db.get_dorm_by_code(clean_code)
    if not dorm:
        raise HTTPException(status_code=404, detail=f"Dorm with code '{dorm_code}' not found")
    return await get_dorm_detail(str(dorm["dorm_id"]))

@app.get("/api/dorms/{dorm_identifier}/schedule", response_class=JSONResponse)
async def get_dorm_schedule(dorm_identifier: str) -> List[Dict[str, Any]]:
    """Convenience endpoint returning rotation schedule for a dorm."""
    detail = await get_dorm_detail(dorm_identifier)
    return detail.get("schedule", [])

@app.get("/api/dorms/{dorm_identifier}/fines", response_class=JSONResponse)
async def get_dorm_fines(dorm_identifier: str) -> List[Dict[str, Any]]:
    """Convenience endpoint returning unpaid fines summary for a dorm."""
    detail = await get_dorm_detail(dorm_identifier)
    return detail.get("fines", [])

@app.get("/api/dorms/{dorm_identifier}/leaderboard", response_class=JSONResponse)
async def get_dorm_leaderboard(dorm_identifier: str) -> List[Dict[str, Any]]:
    """Convenience endpoint returning Tsidat token leaderboard for a dorm."""
    detail = await get_dorm_detail(dorm_identifier)
    return detail.get("leaderboard", [])

@app.get("/fines", include_in_schema=False)
async def redirect_fines():
    return RedirectResponse(url="/", status_code=307)

@app.get("/leaderboard", include_in_schema=False)
async def redirect_leaderboard():
    return RedirectResponse(url="/", status_code=307)

# ---------------------------------------------------------------------------
# Single-Page HTML Dashboard UI (Minimal, Fixed Single-Screen Layout)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Dorm Inspector • Live Status Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    * {
      box-sizing: border-box;
    }
    body {
      background-color: #000000;
      color: #ffffff;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
      overflow-x: hidden;
    }
    .panel {
      background-color: #050505;
      border: 1px solid rgba(255, 255, 255, 0.16);
    }
    .panel-subtle {
      background-color: #0c0c0c;
      border: 1px solid rgba(255, 255, 255, 0.10);
    }
    .card-scene {
      perspective: 1200px;
    }
    .mastercard-3d {
      position: relative;
      width: 100%;
      max-width: 440px;
      aspect-ratio: 1.586 / 1;
      background: radial-gradient(circle at 35% 25%, #181818 0%, #070707 70%, #000000 100%);
      border: 1px solid rgba(255, 255, 255, 0.28);
      border-radius: 18px;
      padding: 22px 26px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      box-shadow: 0 25px 50px -15px rgba(0, 0, 0, 0.95), 0 0 1px 1px rgba(255, 255, 255, 0.1);
      transform-style: preserve-3d;
      transition: transform 0.12s ease-out, box-shadow 0.2s ease-out;
      cursor: pointer;
      overflow: hidden;
      user-select: none;
    }
    .mastercard-3d:hover {
      border-color: rgba(255, 255, 255, 0.55);
      box-shadow: 0 30px 60px -15px rgba(255, 255, 255, 0.1), 0 0 25px rgba(255, 255, 255, 0.05);
    }
    .card-glare {
      position: absolute;
      inset: 0;
      border-radius: 18px;
      pointer-events: none;
      background: radial-gradient(circle at 50% 50%, rgba(255, 255, 255, 0.22) 0%, transparent 65%);
      opacity: 0;
      transition: opacity 0.2s ease-out;
    }
    .emv-chip {
      width: 46px;
      height: 36px;
      background: linear-gradient(135deg, #e0e0e0 0%, #888888 50%, #d0d0d0 100%);
      border: 1px solid rgba(255, 255, 255, 0.7);
      border-radius: 7px;
      position: relative;
      overflow: hidden;
      box-shadow: inset 0 1px 2px rgba(255, 255, 255, 0.9), inset 0 -1px 2px rgba(0, 0, 0, 0.6);
    }
    .chip-line-h {
      position: absolute;
      top: 50%;
      left: 0;
      right: 0;
      height: 1px;
      background: rgba(0, 0, 0, 0.45);
    }
    .chip-line-v {
      position: absolute;
      left: 45%;
      top: 0;
      bottom: 0;
      width: 1px;
      background: rgba(0, 0, 0, 0.45);
    }
    .chip-core {
      position: absolute;
      top: 25%;
      left: 22%;
      right: 22%;
      bottom: 25%;
      border: 1px solid rgba(0, 0, 0, 0.45);
      border-radius: 3px;
    }
    ::-webkit-scrollbar { width: 3px; height: 3px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #262626; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #525252; }
  </style>
</head>
<body class="min-h-screen lg:h-screen lg:overflow-hidden flex flex-col justify-between p-2.5 sm:p-4 text-xs antialiased bg-black text-white">

  <!-- Code Entrance Portal Modal (Single Fixed Page Code Input) -->
  <div id="code-portal-modal" class="hidden fixed inset-0 z-50 bg-black/95 backdrop-blur-md flex items-center justify-center p-4">
    <div class="bg-black border border-white/30 rounded-2xl p-6 sm:p-8 max-w-md w-full shadow-2xl relative">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-2">
          <span class="text-xs font-mono uppercase tracking-[0.2em] text-white/60">DORM INSPECTOR</span>
        </div>
        <button id="portal-close-btn" onclick="closePortalModal()" class="text-white/50 hover:text-white text-sm font-mono p-1 cursor-pointer" title="Close">✕</button>
      </div>
      
      <h2 class="text-xl sm:text-2xl font-bold tracking-tight text-white mb-2">ACCESS DORM DASHBOARD</h2>
      <p class="text-xs text-white/60 mb-6 leading-relaxed">
        Enter your dorm code to inspect live cleaning status, duty schedules, fines, and Tsidat token rankings.
      </p>

      <div class="space-y-4">
        <div>
          <label class="block text-[10px] font-mono uppercase tracking-widest text-white/50 mb-1.5">DORM ACCESS CODE</label>
          <div class="relative">
            <input 
              id="portal-code-input" 
              type="text" 
              placeholder="e.g. DORM-101" 
              class="w-full bg-black border border-white/40 focus:border-white rounded-xl px-4 py-3 text-white font-mono uppercase text-sm tracking-wider focus:outline-none transition"
              onkeydown="if(event.key==='Enter') submitPortalCode()"
              autocomplete="off"
              spellcheck="false"
            />
          </div>
        </div>

        <div id="portal-error-text" class="hidden text-xs font-mono text-white/80 bg-white/5 border border-white/20 p-2.5 rounded-lg">
          Dorm code not found.
        </div>

        <button 
          id="portal-submit-btn" 
          onclick="submitPortalCode()" 
          class="w-full bg-white hover:bg-neutral-200 text-black font-bold text-xs uppercase tracking-widest py-3 rounded-xl transition cursor-pointer"
        >
          VIEW DASHBOARD →
        </button>

        <div class="pt-3 border-t border-white/10 text-center">
          <p class="text-[11px] font-mono text-white/40 leading-normal">
            Need your dorm code? Ask in your dorm Telegram group or type <span class="text-white/80 bg-white/10 px-1 py-0.5 rounded font-bold">/code</span> in the chat.
          </p>
        </div>
      </div>
    </div>
  </div>

  <!-- Minimal Monochrome Top Header Bar -->
  <header class="flex flex-wrap items-center justify-between gap-2.5 px-4 py-2.5 rounded-xl panel mb-3 flex-shrink-0">
    <div class="flex items-center gap-3">
      <div class="w-7 h-7 rounded-lg border border-white/30 flex items-center justify-center text-xs font-mono font-bold bg-white text-black">
        DI
      </div>
      <div>
        <div class="flex items-center gap-2">
          <span class="font-extrabold text-sm text-white tracking-tight uppercase">Dorm Inspector</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono font-bold bg-white/10 text-white border border-white/30 tracking-widest">LIVE</span>
        </div>
        <p id="header-domain" class="text-[10px] text-white/50 font-mono leading-none">inspector.app.aletcloud.com</p>
      </div>
    </div>

    <!-- Active Dorm & Code Switcher -->
    <div class="flex items-center gap-2">
      <div class="flex items-center gap-1.5 bg-black border border-white/25 rounded-lg px-2.5 py-1">
        <span class="text-white/50 text-xs font-mono">CODE:</span>
        <span id="active-dorm-code-display" class="font-mono font-bold text-white text-xs">--</span>
        <button 
          id="selected-dorm-code-badge" 
          onclick="copyDormCode()" 
          class="ml-1 text-[10px] font-mono text-white/70 hover:text-white px-1.5 py-0.5 rounded border border-white/20 hover:border-white transition"
          title="Click to copy shareable dashboard link"
        >
          COPY 📋
        </button>
      </div>

      <button 
        onclick="openPortalModal(true)" 
        class="px-2.5 py-1 rounded-lg border border-white/30 hover:border-white text-white/80 hover:text-white font-mono text-[11px] transition flex items-center gap-1 cursor-pointer"
        title="Switch to another dorm"
      >
        <span>⇄</span>
        <span>SWITCH DORM</span>
      </button>
    </div>

    <!-- Telemetry Badges & Refresh -->
    <div class="flex items-center gap-3 text-[11px]">
      <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-white/20 bg-white/5 font-mono text-white">
        <span class="w-1.5 h-1.5 rounded-full bg-white animate-pulse"></span>
        <span id="bot-status-text">ONLINE</span>
      </div>
      <div class="hidden sm:flex items-center gap-1 text-white/50 text-[10px] font-mono">
        <span>UPTIME:</span>
        <strong id="stat-uptime" class="text-white font-mono">--</strong>
      </div>
      <div class="hidden md:flex items-center gap-1 text-white/50 text-[10px] font-mono">
        <span id="stat-current-date" class="text-white">--</span>
      </div>
      <button onclick="fetchDashboardData()" class="p-1.5 rounded-lg border border-white/20 hover:border-white text-white/70 hover:text-white transition" title="Refresh Live Data">
        <svg id="refresh-icon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
        </svg>
      </button>
    </div>
  </header>

  <!-- Notification Banner -->
  <div id="code-alert-banner" class="hidden mb-2.5 px-3 py-2 rounded-xl bg-black border border-white/40 text-white text-[11px] flex items-center justify-between flex-shrink-0">
    <div class="flex items-center gap-2 font-mono">
      <span>●</span>
      <span id="code-alert-text">Notification message.</span>
    </div>
    <button onclick="clearCodeError()" class="text-white/60 hover:text-white text-xs">✕</button>
  </div>

  <!-- Main Dashboard Grid Layout -->
  <main class="flex-1 flex flex-col gap-3 overflow-y-auto lg:overflow-hidden min-h-0">
    
    <!-- Top Hero Section: 3D MasterCard Dorm Card + Active Duty Overview -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-3 flex-shrink-0">
      
      <!-- 3D MasterCard Interactive Component (Cols 1-6) -->
      <div class="lg:col-span-6 flex items-center justify-center p-2 card-scene">
        <div id="mastercard" class="mastercard-3d">
          <div id="card-glare" class="card-glare"></div>

          <!-- Card Top: Chip + Contactless + Brand -->
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-3">
              <div class="emv-chip">
                <div class="chip-line-h"></div>
                <div class="chip-line-v"></div>
                <div class="chip-core"></div>
              </div>
              <svg class="w-5 h-5 text-white/60" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M8.5 16.5a5 5 0 010-9" stroke-linecap="round"/>
                <path d="M12 19a8.5 8.5 0 000-14" stroke-linecap="round"/>
                <path d="M15.5 21.5a12 12 0 000-19" stroke-linecap="round"/>
              </svg>
            </div>
            <div class="text-right">
              <span class="text-[9px] tracking-[0.25em] font-mono text-white/50 uppercase block">INSPECTOR CARD</span>
              <span id="card-dorm-name" class="text-xs font-bold tracking-wider text-white uppercase truncate block max-w-[180px]">DORM-101</span>
            </div>
          </div>

          <!-- Card Center: Embossed Dorm Code Number -->
          <div class="my-auto py-2">
            <div class="text-[8px] tracking-[0.2em] text-white/40 uppercase font-mono mb-1">DORM IDENTIFIER</div>
            <div id="card-number-display" class="font-mono text-lg sm:text-xl tracking-[0.22em] text-white font-extrabold filter drop-shadow">
              •••• •••• •••• DORM
            </div>
          </div>

          <!-- Card Bottom: Cleaners, Expiry, MasterCard Emblem -->
          <div class="flex items-end justify-between">
            <div class="space-y-0.5">
              <div class="text-[8px] tracking-[0.2em] text-white/40 uppercase font-mono">CURRENT DUTY</div>
              <div id="card-cleaners-display" class="font-mono text-xs sm:text-sm font-bold text-white tracking-wider uppercase truncate max-w-[220px]">
                LOADING...
              </div>
              <div class="flex items-center gap-3 pt-0.5 text-[9px] font-mono text-white/60">
                <span>EXP: <strong class="text-white" id="card-expiry-display">FRI 20:00</strong></span>
                <span>STATE: <strong class="text-white" id="card-status-display">ON DUTY</strong></span>
              </div>
            </div>

            <!-- Monochrome MasterCard Logo (Interlocking Circles) -->
            <div class="flex items-center">
              <div class="relative flex items-center w-12 h-8">
                <div class="w-7 h-7 rounded-full bg-white opacity-85 absolute left-0 shadow-sm"></div>
                <div class="w-7 h-7 rounded-full border-2 border-white bg-black/60 absolute right-0 backdrop-blur-xs"></div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Active Duty Status Overview (Cols 7-12) -->
      <div class="lg:col-span-6 panel rounded-xl p-3.5 flex flex-col justify-between min-h-0">
        <div>
          <div class="flex items-center justify-between border-b border-white/10 pb-2 mb-2.5">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-white"></span>
              <span class="font-mono uppercase tracking-wider text-[11px] text-white/70">CURRENT DUTY STATUS</span>
            </div>
            <span id="selected-dorm-status-badge" class="px-2.5 py-0.5 rounded border border-white text-[10px] font-mono uppercase font-bold text-white">
              IN PROGRESS
            </span>
          </div>

          <div class="flex items-center justify-between gap-2 mb-2">
            <div>
              <h2 id="selected-dorm-title" class="text-base sm:text-lg font-bold text-white tracking-tight uppercase">--</h2>
              <p id="selected-dorm-week-header" class="text-[11px] text-white/60 font-mono mt-0.5">Week 1 Duty</p>
            </div>
            <div class="text-right">
              <span class="text-[9px] font-mono uppercase tracking-widest text-white/40 block">DEADLINE</span>
              <span class="font-mono font-bold text-xs text-white">Friday 8:00 PM</span>
            </div>
          </div>

          <!-- Cleaners List (1, 2, or 3 Cleaners) -->
          <div id="cleaners-container" class="space-y-1.5 my-2">
            <div class="panel-subtle rounded-lg p-2 text-center text-white/50 text-[10px] font-mono">
              Loading cleaners...
            </div>
          </div>
        </div>

        <!-- Approver & Fine Rate Info -->
        <div class="panel-subtle rounded-lg p-2.5 grid grid-cols-2 gap-2 text-[11px] font-mono">
          <div>
            <span class="text-white/50 block text-[9px] uppercase">APPROVER</span>
            <span id="selected-dorm-approver" class="font-bold text-white">--</span>
          </div>
          <div class="text-right">
            <span class="text-white/50 block text-[9px] uppercase">FINE RATE</span>
            <span id="selected-dorm-fine" class="font-bold text-white">100 ETB (50/50)</span>
          </div>
        </div>

        <!-- Inspection Review Banner (Dynamically shown when cleaning reported, submitted, or approved) -->
        <div id="cleaning-review-banner" class="hidden panel-subtle rounded-lg p-2 text-[10px] font-mono border border-white/20 mt-2">
          <div class="flex items-center justify-between">
            <span id="cleaning-review-stars" class="font-bold text-white">⭐⭐⭐⭐⭐</span>
            <span id="cleaning-review-badge" class="px-1.5 py-0.2 rounded bg-white text-black font-bold text-[9px]">APPROVED ✓</span>
          </div>
          <p id="cleaning-review-comment" class="text-white/70 italic text-[10px] mt-1">Inspection passed cleanly!</p>
        </div>

      </div>
    </div>

    <!-- Bottom 3 Columns: Schedule, Tsidat Tokens, Fines & History -->
    <div class="flex-1 grid grid-cols-1 md:grid-cols-3 gap-3 min-h-0 overflow-y-auto lg:overflow-hidden">
      
      <!-- Col 1: 4-Week Rotation Schedule -->
      <div class="panel rounded-xl p-3 flex flex-col min-h-0">
        <div class="flex items-center justify-between border-b border-white/10 pb-2 mb-2 flex-shrink-0">
          <div class="flex items-center gap-1.5">
            <span class="font-mono font-bold text-white uppercase tracking-wider text-[10px]">ROTATION SCHEDULE</span>
          </div>
          <span class="text-[10px] font-mono text-white/50">4 WEEKS</span>
        </div>
        <div id="schedule-table" class="space-y-1.5 flex-1 overflow-y-auto pr-0.5">
          <p class="text-white/40 text-center py-4 text-[10px] font-mono">Loading schedule...</p>
        </div>
      </div>

      <!-- Col 2: Tsidat Token Leaderboard -->
      <div class="panel rounded-xl p-3 flex flex-col min-h-0">
        <div class="flex items-center justify-between border-b border-white/10 pb-2 mb-2 flex-shrink-0">
          <div class="flex items-center gap-1.5">
            <span class="font-mono font-bold text-white uppercase tracking-wider text-[10px]">TSIDAT TOKENS</span>
          </div>
          <span class="text-[10px] font-mono text-white/50">RANKINGS</span>
        </div>
        <div id="leaderboard-list" class="space-y-1.5 flex-1 overflow-y-auto pr-0.5">
          <p class="text-white/40 text-center py-4 text-[10px] font-mono">Loading leaderboard...</p>
        </div>
      </div>

      <!-- Col 3: Unpaid Fines & Historical Log -->
      <div class="panel rounded-xl p-3 flex flex-col justify-between min-h-0">
        <div class="flex flex-col min-h-0 flex-1">
          <div class="flex items-center justify-between border-b border-white/10 pb-2 mb-2 flex-shrink-0">
            <div class="flex items-center gap-1.5">
              <span class="font-mono font-bold text-white uppercase tracking-wider text-[10px]">UNPAID FINES</span>
            </div>
            <span id="stat-fines" class="text-[11px] font-mono font-bold text-white">0 ETB</span>
          </div>
          <div id="fines-list" class="space-y-1.5 overflow-y-auto flex-1 pr-0.5">
            <p class="text-white/40 text-center py-2 text-[10px] font-mono">No unpaid fines.</p>
          </div>
        </div>

        <div class="border-t border-white/10 pt-2 flex-shrink-0 space-y-1.5 mt-2">
          <div class="flex items-center justify-between pb-1 border-b border-white/5">
            <span class="font-mono font-bold text-white/70 uppercase text-[9px] tracking-wider">AUDIT HISTORY</span>
            <span class="text-[9px] font-mono text-white/40">PAST WEEKS</span>
          </div>
          <div id="history-list" class="space-y-1 max-h-16 overflow-y-auto pr-0.5">
            <p class="text-white/40 text-center py-1 text-[9px] font-mono">No past records yet.</p>
          </div>

          <div class="panel-subtle rounded-lg p-1.5 flex items-center justify-between text-[10px] font-mono text-white/60">
            <span>MEMBERS: <strong id="stat-members" class="text-white">--</strong></span>
            <span>•</span>
            <span>DORMS: <strong id="stat-dorms" class="text-white">--</strong></span>
          </div>
        </div>
      </div>

    </div>

  </main>

  <!-- Minimal Monochrome Footer -->
  <footer class="mt-2.5 px-3 py-1.5 rounded-xl panel flex items-center justify-between text-[10px] font-mono text-white/50 flex-shrink-0">
    <div>
      DORM INSPECTOR • <span id="footer-domain" class="text-white">inspector.app.aletcloud.com</span>
    </div>
    <div class="flex items-center gap-3">
      <span>AUTO-REFRESH: <strong class="text-white">15s</strong></span>
      <span>PLATFORM: <strong class="text-white">ALETCLOUD</strong></span>
    </div>
  </footer>

  <!-- Live Client-Side Script with 3D Tilt Physics and Seamless Portal Handling -->
  <script>
    let activeDormId = null;
    let currentDormData = null;

    // --- 3D MasterCard Tilt Physics ---
    function init3DCard() {
      const card = document.getElementById('mastercard');
      const glare = document.getElementById('card-glare');
      if (!card) return;

      function handleMove(e) {
        const rect = card.getBoundingClientRect();
        const clientX = e.clientX || (e.touches && e.touches[0].clientX);
        const clientY = e.clientY || (e.touches && e.touches[0].clientY);
        if (clientX === undefined || clientY === undefined) return;

        const x = clientX - rect.left;
        const y = clientY - rect.top;
        const centerX = rect.width / 2;
        const centerY = rect.height / 2;

        const rotateX = -((y - centerY) / centerY) * 16;
        const rotateY = ((x - centerX) / centerX) * 16;

        card.style.transform = `perspective(1000px) rotateX(${rotateX.toFixed(2)}deg) rotateY(${rotateY.toFixed(2)}deg) scale3d(1.04, 1.04, 1.04)`;
        
        if (glare) {
          glare.style.opacity = '1';
          glare.style.background = `radial-gradient(circle at ${(x / rect.width * 100).toFixed(1)}% ${(y / rect.height * 100).toFixed(1)}%, rgba(255, 255, 255, 0.22) 0%, transparent 65%)`;
        }
      }

      function handleLeave() {
        card.style.transform = 'perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)';
        if (glare) glare.style.opacity = '0';
      }

      card.addEventListener('mousemove', handleMove);
      card.addEventListener('mouseleave', handleLeave);
      card.addEventListener('touchmove', handleMove, { passive: true });
      card.addEventListener('touchend', handleLeave);
    }

    // --- Portal Modal (Code Entrance) ---
    let userOpenedModal = false;

    function openPortalModal(fromUserClick = false) {
      if (fromUserClick) userOpenedModal = true;
      const modal = document.getElementById('code-portal-modal');
      const closeBtn = document.getElementById('portal-close-btn');
      const input = document.getElementById('portal-code-input');
      if (modal) {
        modal.classList.remove('hidden');
        if (closeBtn) {
          closeBtn.classList.remove('hidden');
        }
        if (input) {
          input.value = currentDormData ? currentDormData.dorm_code : (localStorage.getItem('saved_dorm_code') || '');
          setTimeout(() => input.focus(), 150);
        }
      }
    }

    function closePortalModal() {
      userOpenedModal = false;
      const modal = document.getElementById('code-portal-modal');
      if (modal) modal.classList.add('hidden');
    }

    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closePortalModal();
      }
    });

    const portalBackdrop = document.getElementById('code-portal-modal');
    if (portalBackdrop) {
      portalBackdrop.addEventListener('click', (e) => {
        if (e.target === portalBackdrop) {
          closePortalModal();
        }
      });
    }

    async function submitPortalCode() {
      const input = document.getElementById('portal-code-input');
      const btn = document.getElementById('portal-submit-btn');
      const err = document.getElementById('portal-error-text');
      const code = (input ? input.value : '').replace(/[`'"#]/g, '').trim().toUpperCase();

      if (!code) {
        if (err) {
          err.innerText = "Please enter your dorm code (check /code in Telegram).";
          err.classList.remove('hidden');
        }
        return;
      }

      if (btn) {
        btn.disabled = true;
        btn.innerText = "VERIFYING...";
      }
      if (err) err.classList.add('hidden');

      try {
        const success = await loadDormByCode(code);
        if (success) {
          closePortalModal();
        } else {
          if (err) {
            err.innerText = `Dorm code '${code}' was not found. Please verify with /code in Telegram.`;
            err.classList.remove('hidden');
          }
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerText = "VIEW DASHBOARD →";
        }
      }
    }

    // --- Banner Notifications ---
    function showCodeError(msg) {
      const banner = document.getElementById('code-alert-banner');
      const text = document.getElementById('code-alert-text');
      if (banner && text) {
        text.innerText = msg;
        banner.classList.remove('hidden');
      }
    }

    function clearCodeError() {
      const banner = document.getElementById('code-alert-banner');
      if (banner) banner.classList.add('hidden');
    }

    function copyDormCode() {
      const code = (currentDormData && currentDormData.dorm_code)
        ? currentDormData.dorm_code
        : (document.getElementById('active-dorm-code-display') ? document.getElementById('active-dorm-code-display').innerText.trim() : '');
      if (!code || code === '--') return;
      const shareUrl = `${window.location.origin}/?code=${encodeURIComponent(code)}`;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(shareUrl).then(() => {
          const badge = document.getElementById('selected-dorm-code-badge');
          if (badge) {
            badge.innerText = "COPIED! ✓";
            setTimeout(() => { badge.innerText = "COPY 📋"; }, 1500);
          }
        });
      }
    }

    // --- Load Default Dorm ---
    async function loadDefaultDorm() {
      try {
        clearCodeError();
        const res = await fetch('/api/dorms/default');
        if (!res.ok) {
          const resDef = await fetch('/api/dorms/code/DEFAULT');
          if (!resDef.ok) return false;
          const data = await resDef.json();
          currentDormData = data;
          activeDormId = data.dorm_id;
          if (data.dorm_code) localStorage.setItem('saved_dorm_code', data.dorm_code);
          applyDormDetails(data);
          return true;
        }
        const data = await res.json();
        currentDormData = data;
        activeDormId = data.dorm_id;
        if (data.dorm_code) localStorage.setItem('saved_dorm_code', data.dorm_code);
        applyDormDetails(data);
        return true;
      } catch (e) {
        console.error("loadDefaultDorm error:", e);
        return false;
      }
    }

    // --- Load Dorm by Code ---
    async function loadDormByCode(code) {
      const cleanCode = (code || '').replace(/[`'"#]/g, '').trim().toUpperCase();
      if (!cleanCode) return false;

      try {
        clearCodeError();
        const res = await fetch(`/api/dorms/code/${encodeURIComponent(cleanCode)}`);
        if (!res.ok) {
          return false;
        }

        const data = await res.json();
        currentDormData = data;
        activeDormId = data.dorm_id;
        localStorage.setItem('saved_dorm_code', data.dorm_code);

        try {
          const url = new URL(window.location);
          url.searchParams.set('code', data.dorm_code);
          window.history.replaceState({}, '', url);
        } catch (ue) {}

        applyDormDetails(data);
        return true;
      } catch (e) {
        console.error("loadDormByCode error:", e);
        return false;
      }
    }

    // --- Apply Dorm Details into DOM ---
    function applyDormDetails(data) {
      currentDormData = data;
      document.getElementById('selected-dorm-title').innerText = data.dorm_name;
      const activeCodeEl = document.getElementById('active-dorm-code-display');
      if (activeCodeEl) activeCodeEl.innerText = data.dorm_code;

      // Update 3D MasterCard display
      const cardName = document.getElementById('card-dorm-name');
      if (cardName) cardName.innerText = data.dorm_name;

      const cardNum = document.getElementById('card-number-display');
      if (cardNum) cardNum.innerText = `•••• •••• •••• ${data.dorm_code}`;

      const curWeek = data.current_week || {};
      const teamSize = data.cleaners_per_week || 2;
      const fineTotal = data.fine_amount || 100;
      const fineEach = Math.floor(fineTotal / teamSize);

      document.getElementById('selected-dorm-fine').innerText = `${fineTotal} ETB (${fineEach} each)`;
      document.getElementById('selected-dorm-approver').innerText = (curWeek && curWeek.approver) ? curWeek.approver : 'Not assigned';

      const isCompleted = curWeek.status_code === 'completed' || curWeek.final_status === 'completed';
      const isFined = curWeek.status_code === 'fined' || curWeek.final_status === 'fined';
      const isSubmitted = curWeek.status_code === 'submitted' || curWeek.photo_status === 'submitted';
      const isDoneReported = curWeek.status_code === 'done_reported' || curWeek.done_reported;
      const cleanedAt = curWeek.cleaned_at_display || '';
      const cleanedAtSuffix = cleanedAt ? ` • ${cleanedAt}` : '';

      // Week header & badge
      document.getElementById('selected-dorm-week-header').innerText = `Week ${curWeek.week_number || 1} Duty • ${curWeek.duty_state || 'In Progress'}`;

      const badge = document.getElementById('selected-dorm-status-badge');
      if (badge) {
        if (isCompleted) {
          const stars = curWeek.rating ? ` (${curWeek.rating}★)` : '';
          badge.innerText = `APPROVED${stars}${cleanedAtSuffix} ✓`;
          badge.className = 'px-2.5 py-0.5 rounded border border-white bg-white text-black text-[10px] font-mono uppercase font-bold';
        } else if (isFined) {
          badge.innerText = 'MISSED DEADLINE (FINED) ⚠️';
          badge.className = 'px-2.5 py-0.5 rounded border border-red-500 bg-red-500/20 text-red-400 text-[10px] font-mono uppercase font-bold';
        } else if (isSubmitted) {
          badge.innerText = `CLEANED${cleanedAtSuffix} • REVIEW PENDING ⏳`;
          badge.className = 'px-2.5 py-0.5 rounded border border-white/60 text-[10px] font-mono uppercase font-bold text-white';
        } else if (isDoneReported) {
          badge.innerText = `CLEANED${cleanedAtSuffix} (AWAITING PHOTO)`;
          badge.className = 'px-2.5 py-0.5 rounded border border-white/40 text-[10px] font-mono uppercase font-bold text-white/80';
        } else {
          badge.innerText = (curWeek.duty_state || 'IN PROGRESS').toUpperCase();
          badge.className = 'px-2.5 py-0.5 rounded border border-white text-[10px] font-mono uppercase font-bold text-white';
        }
      }

      // Card Status & Expiry
      const cardStatus = document.getElementById('card-status-display');
      if (cardStatus) {
        if (isCompleted) {
          cardStatus.innerText = `CLEANED${cleanedAtSuffix} & APPROVED ✓`;
        } else if (isFined) {
          cardStatus.innerText = 'FINED ⚠️ (UNPAID)';
        } else if (isSubmitted) {
          cardStatus.innerText = `CLEANED${cleanedAtSuffix} • REVIEW PENDING`;
        } else if (isDoneReported) {
          cardStatus.innerText = 'PHOTO AWAITED';
        } else {
          cardStatus.innerText = (curWeek.duty_state || 'ON DUTY').toUpperCase();
        }
      }

      // Cleaners (1, 2, or 3 cleaners)
      const cleaners = (curWeek.cleaners && curWeek.cleaners.length > 0)
        ? curWeek.cleaners
        : [curWeek.cleaner1, curWeek.cleaner2, curWeek.cleaner3].filter(Boolean);

      const cardCleaners = document.getElementById('card-cleaners-display');
      if (cardCleaners) {
        cardCleaners.innerText = cleaners.length > 0 ? cleaners.join(' & ') : 'UNASSIGNED';
      }

      const cleanersEl = document.getElementById('cleaners-container');
      if (cleanersEl) {
        if (cleaners.length === 0) {
          cleanersEl.innerHTML = `<div class="panel-subtle rounded-lg p-2 text-center text-white/50 text-[10px] font-mono">No cleaners assigned yet</div>`;
        } else {
          let cleanerTag = 'ON DUTY';
          let tagClass = 'text-white/80 font-mono font-semibold';
          if (isCompleted) {
            cleanerTag = 'CLEANED ✓';
            tagClass = 'text-white font-mono font-bold bg-white/10 px-1.5 py-0.5 rounded border border-white/40';
          } else if (isFined) {
            cleanerTag = `FINED (${fineEach} ETB) ⚠️`;
            tagClass = 'text-red-400 font-mono font-bold bg-red-500/10 px-1.5 py-0.5 rounded border border-red-500/30';
          } else if (isSubmitted) {
            cleanerTag = 'PHOTO SENT ⏳';
            tagClass = 'text-white/70 font-mono font-medium';
          } else if (isDoneReported) {
            cleanerTag = 'DONE REPORTED';
            tagClass = 'text-white/70 font-mono font-medium';
          }

          cleanersEl.innerHTML = cleaners.map((cName, idx) => `
            <div class="panel-subtle rounded-lg p-2 flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class="w-5 h-5 rounded border border-white/30 text-white font-mono font-bold flex items-center justify-center text-[10px] bg-white/5">${idx + 1}</span>
                <div>
                  <p class="text-[9px] text-white/40 uppercase font-mono">Cleaner ${idx + 1}</p>
                  <p class="font-bold text-white text-xs">${cName || 'TBD'}</p>
                </div>
              </div>
              <span class="text-[10px] ${tagClass}">${cleanerTag}</span>
            </div>
          `).join('');
        }
      }

      // Review banner
      const reviewBanner = document.getElementById('cleaning-review-banner');
      const reviewStars = document.getElementById('cleaning-review-stars');
      const reviewBadge = document.getElementById('cleaning-review-badge');
      const reviewComment = document.getElementById('cleaning-review-comment');
      if (reviewBanner) {
        if (isCompleted) {
          const starsCount = curWeek.rating || 5;
          const starsStr = '⭐'.repeat(starsCount) + ` (${starsCount}/5 Stars)`;
          if (reviewStars) reviewStars.innerText = starsStr;
          if (reviewBadge) {
            reviewBadge.innerText = 'APPROVED ✓';
            reviewBadge.className = 'px-1.5 py-0.5 rounded bg-white text-black font-bold text-[9px]';
          }
          if (reviewComment) {
            const review = curWeek.review_text ? `"${curWeek.review_text}"` : 'Inspection passed cleanly!';
            const bonus = curWeek.early_bonus ? ` +${curWeek.early_bonus} early-cleaning bonus tokens.` : '';
            reviewComment.innerText = `${review} Cleaned at ${cleanedAt || 'time not recorded'}.${bonus}`;
          }
          reviewBanner.classList.remove('hidden');
        } else if (isFined) {
          if (reviewStars) reviewStars.innerText = '⚠️ DEADLINE MISSED';
          if (reviewBadge) {
            reviewBadge.innerText = 'FINED ⚠️';
            reviewBadge.className = 'px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/40 font-mono font-bold text-[9px]';
          }
          if (reviewComment) {
            reviewComment.innerText = `Duty was not completed by Friday 8:00 PM. Fined ${fineTotal} ETB total (${fineEach} ETB each).`;
          }
          reviewBanner.classList.remove('hidden');
        } else if (isSubmitted) {
          if (reviewStars) reviewStars.innerText = '📸 PHOTO SUBMITTED';
          if (reviewBadge) {
            reviewBadge.innerText = 'PENDING REVIEW ⏳';
            reviewBadge.className = 'px-1.5 py-0.5 rounded border border-white/40 text-white font-mono text-[9px]';
          }
          if (reviewComment) {
            reviewComment.innerText = `Cleaned at ${cleanedAt || 'time not recorded'}. Photo submitted; waiting for ${curWeek.approver || 'approver'} to review.`;
          }
          reviewBanner.classList.remove('hidden');
        } else if (isDoneReported) {
          if (reviewStars) reviewStars.innerText = '🧹 CLEANING REPORTED';
          if (reviewBadge) {
            reviewBadge.innerText = 'PHOTO AWAITED 📷';
            reviewBadge.className = 'px-1.5 py-0.5 rounded border border-white/40 text-white font-mono text-[9px]';
          }
          if (reviewComment) {
            reviewComment.innerText = `Cleaners marked cleaning done at ${cleanedAt || 'time not recorded'}. Photo awaited.`;
          }
          reviewBanner.classList.remove('hidden');
        } else {
          reviewBanner.classList.add('hidden');
        }
      }

      // Render schedule, leaderboard, fines, history
      renderSchedule(data.schedule, isCompleted, isFined);
      renderLeaderboard(data.leaderboard);
      renderFines(data.fines);
      renderHistory(data.history);
    }

    function renderSchedule(schedule, isCompleted, isFined) {
      const el = document.getElementById('schedule-table');
      if (!schedule || schedule.length === 0) {
        el.innerHTML = `<p class="text-white/40 text-center py-4 text-[10px] font-mono">No upcoming rotation scheduled.</p>`;
        return;
      }

      el.innerHTML = schedule.map(w => {
        const isCur = w.is_current;
        const border = isCur ? 'border-white bg-white/10 font-bold' : 'panel-subtle';
        let tag = '';
        if (isCur) {
          if (isCompleted) {
            tag = '<span class="text-[9px] font-mono font-bold px-1.5 py-0.2 rounded bg-white text-black">CLEANED ✓</span>';
          } else if (isFined) {
            tag = '<span class="text-[9px] font-mono font-bold px-1.5 py-0.2 rounded bg-red-500/20 text-red-400 border border-red-500/40">FINED ⚠️</span>';
          } else {
            tag = '<span class="text-[9px] font-mono font-bold px-1.5 py-0.2 rounded bg-white text-black">CURRENT</span>';
          }
        }
        const namesDisp = w.cleaners_display || [w.cleaner1, w.cleaner2, w.cleaner3].filter(Boolean).join(' & ');

        return `
          <div class="p-2 rounded-lg border flex items-center justify-between text-[11px] ${border}">
            <div class="flex items-center gap-2">
              <span class="font-mono text-white">W${w.week_number}</span>
              <span class="text-white/50 text-[10px] font-mono">${w.monday}</span>
              ${tag}
            </div>
            <div class="font-mono text-white text-right truncate max-w-[160px]">
              <span>${namesDisp}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderLeaderboard(board) {
      const el = document.getElementById('leaderboard-list');
      if (!board || board.length === 0) {
        el.innerHTML = `<p class="text-white/40 text-center py-4 text-[10px] font-mono">No Tsidat tokens awarded yet.</p>`;
        return;
      }

      el.innerHTML = board.map((item, idx) => {
        const medal = idx === 0 ? '👑' : `#${idx + 1}`;
        return `
          <div class="p-2 rounded-lg panel-subtle flex items-center justify-between text-[11px]">
            <div class="flex items-center gap-2">
              <span class="font-mono text-white/50 text-[10px] w-5">${medal}</span>
              <span class="font-medium text-white">${item.name}</span>
            </div>
            <span class="font-mono font-bold text-white">${item.tsidat_balance} <span class="text-[9px] text-white/50 font-normal">pts</span></span>
          </div>
        `;
      }).join('');
    }

    function renderFines(fines) {
      const el = document.getElementById('fines-list');
      if (!fines || fines.length === 0) {
        el.innerHTML = `<div class="p-2 rounded-lg panel-subtle text-center text-white/70 text-[10px] font-mono">✓ 0 Unpaid Fines. All Clean.</div>`;
        return;
      }

      el.innerHTML = fines.map(f => `
        <div class="p-2 rounded-lg panel-subtle flex items-center justify-between text-[11px]">
          <span class="font-medium text-white">${f.user_name}</span>
          <span class="font-mono font-bold text-white">${f.total_unpaid} ETB</span>
        </div>
      `).join('');
    }

    function renderHistory(hist) {
      const el = document.getElementById('history-list');
      if (!hist || hist.length === 0) {
        el.innerHTML = `<p class="text-white/40 text-center py-1 text-[9px] font-mono">No past weeks recorded yet.</p>`;
        return;
      }

      el.innerHTML = hist.map(h => {
        const statusText = h.final_status === 'completed'
          ? `CLEANED ${h.rating ? '★' + h.rating : ''}`
          : (h.final_status === 'fined' ? 'FINED ⚠️' : 'PENDING');
        const statusClass = h.final_status === 'fined' ? 'text-red-400 font-bold' : 'text-white font-bold';

        return `
          <div class="p-1 rounded panel-subtle flex items-center justify-between text-[9px] font-mono">
            <span class="text-white/80">W${h.week_number}: ${h.cleaner1} & ${h.cleaner2}</span>
            <span class="${statusClass}">${statusText}</span>
          </div>
        `;
      }).join('');
    }

    // --- Master Dashboard Data Fetcher ---
    async function fetchDashboardData() {
      const refreshIcon = document.getElementById('refresh-icon');
      if (refreshIcon) refreshIcon.classList.add('animate-spin');

      if (window.location && window.location.host) {
        const hDom = document.getElementById('header-domain');
        const fDom = document.getElementById('footer-domain');
        if (hDom) hDom.innerText = window.location.host;
        if (fDom) fDom.innerText = window.location.host;
      }

      try {
        // Fetch platform system telemetry
        const sysRes = await fetch('/api/status');
        if (sysRes.ok) {
          const sys = await sysRes.json();
          document.getElementById('stat-dorms').innerText = sys.stats.active_dorms_count;
          document.getElementById('stat-members').innerText = sys.stats.total_members_count;
          document.getElementById('stat-uptime').innerText = sys.uptime;
          const dateEl = document.getElementById('stat-current-date');
          if (dateEl) dateEl.innerText = sys.current_date;
          document.getElementById('stat-fines').innerText = `${sys.stats.total_unpaid_fines_etb} ETB`;
        }

        // Determine target dorm code from URL query parameters
        const urlParams = new URLSearchParams(window.location.search);
        const urlCode = (urlParams.get('code') || urlParams.get('dorm') || '').trim();

        if (urlCode) {
          // Opened via Telegram bot "Open Dashboard" button: directly load into the dorm's dashboard!
          const loaded = await loadDormByCode(urlCode);
          if (loaded) {
            if (!userOpenedModal) closePortalModal();
          } else {
            // Dorm code in URL not found - try localStorage or default dorm fallback
            const savedCode = localStorage.getItem('saved_dorm_code');
            let recovered = false;
            if (savedCode && savedCode.toUpperCase() !== urlCode.toUpperCase()) {
              recovered = await loadDormByCode(savedCode);
            }
            if (!recovered) {
              recovered = await loadDefaultDorm();
            }
            if (recovered) {
              if (!userOpenedModal) closePortalModal();
              showCodeError(`Dorm code '${urlCode}' was not found. Loaded active dorm instead.`);
            } else {
              openPortalModal();
              showCodeError(`Dorm code '${urlCode}' not found. Please verify with /code in Telegram.`);
            }
          }
        } else {
          // Direct visit without ?code= in URL:
          // 1. Try previously saved dorm code from localStorage
          const savedCode = localStorage.getItem('saved_dorm_code');
          let loaded = false;
          if (savedCode) {
            loaded = await loadDormByCode(savedCode);
          }
          // 2. If no saved code or loading failed, load the default active dorm
          if (!loaded) {
            loaded = await loadDefaultDorm();
          }
          if (loaded) {
            if (!userOpenedModal) closePortalModal();
          } else {
            openPortalModal();
          }
        }
      } catch (err) {
        console.error("Dashboard refresh error:", err);
      } finally {
        if (refreshIcon) refreshIcon.classList.remove('animate-spin');
      }
    }

    // Initialize on load
    init3DCard();
    fetchDashboardData();
    setInterval(fetchDashboardData, 15000);
  </script>
</body>
</html>
"""

@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> str:
    """Serve the single-page responsive status dashboard."""
    return DASHBOARD_HTML

def run_web_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Entry point for running the dashboard server via uvicorn."""
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    run_web_server(port=port)
