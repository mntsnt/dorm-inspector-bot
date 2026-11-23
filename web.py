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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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
async def list_active_dorms() -> List[Dict[str, Any]]:
    """Return summary list of all active dorm rooms."""
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
        duty_status = "In Progress"
        if week_record:
            f_status = week_record.get("final_status", "pending")
            p_status = week_record.get("photo_status", "none")
            if f_status == "completed":
                duty_status = "Cleaned & Approved"
            elif f_status == "fined":
                duty_status = "Fined"
            elif p_status == "submitted":
                duty_status = "Photo Review Pending"
            elif p_status == "approved":
                duty_status = "Photo Approved"

        results.append({
            "dorm_id": d_id,
            "dorm_name": d["dorm_name"],
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

@app.get("/api/dorms/{dorm_id}", response_class=JSONResponse)
async def get_dorm_detail(dorm_id: int) -> Dict[str, Any]:
    """Detailed live metrics for a specific dorm."""
    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        raise HTTPException(status_code=404, detail="Dorm not found")

    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    current_date = config.get_current_date()
    week_info = get_dorm_week_info(dorm, pairs, members_by_id, target_date=current_date)
    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id) if week_info else None

    # Current duty status determination
    duty_state = "In progress (Deadline Friday 8:00 PM)"
    status_code = "in_progress"
    approver_name = "Not yet assigned"

    if week_record:
        if week_record.get("approver_id"):
            approver_name = members_by_id.get(week_record["approver_id"], f"ID {week_record['approver_id']}")
        f_status = week_record.get("final_status", "pending")
        p_status = week_record.get("photo_status", "none")
        if f_status == "completed":
            rating = week_record.get("rating", 5)
            duty_state = f"Cleaned & Approved ({rating}★ Stars)"
            status_code = "completed"
        elif f_status == "fined":
            duty_state = f"Missed Deadline (Fined {dorm.get('fine_amount', 100)} ETB)"
            status_code = "fined"
        elif p_status == "submitted":
            duty_state = "Photo Submitted (Review Pending)"
            status_code = "submitted"
        elif p_status == "rejected":
            duty_state = "Photo Rejected (Needs Recleaning)"
            status_code = "rejected"
        elif week_record.get("done_reported"):
            duty_state = "Cleaners Reported Done (Photo Awaited)"
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
                    "is_current": (tgt_monday == current_monday)
                })

    leaderboard = await db.get_tsidat_leaderboard(dorm_id)
    fines = await db.get_unpaid_fines_summary(dorm_id)
    history = await db.get_history(limit=6, dorm_id=dorm_id)

    formatted_history = []
    for h in history:
        c1 = members_by_id.get(h["cleaner1_id"], f"ID {h['cleaner1_id']}")
        c2 = members_by_id.get(h["cleaner2_id"], f"ID {h['cleaner2_id']}")
        formatted_history.append({
            "week_number": h["week_number"],
            "week_start": h["week_start"],
            "cleaner1": c1,
            "cleaner2": c2,
            "final_status": h.get("final_status", "pending"),
            "rating": h.get("rating")
        })

    return {
        "dorm_id": dorm_id,
        "dorm_name": dorm["dorm_name"],
        "fine_amount": dorm.get("fine_amount", 100),
        "status": dorm.get("status", "active"),
        "members": [{"user_id": m["user_id"], "name": m["name"], "is_admin": m.get("is_admin", 0)} for m in members],
        "current_week": {
            "week_number": week_info.week_number if week_info else 0,
            "week_start": week_info.week_start if week_info else "",
            "cleaner1": week_info.cleaner1_name if week_info else "TBD",
            "cleaner2": week_info.cleaner2_name if week_info else "TBD",
            "approver": approver_name,
            "duty_state": duty_state,
            "status_code": status_code,
            "photo_status": week_record.get("photo_status", "none") if week_record else "none"
        },
        "schedule": schedule_preview,
        "leaderboard": leaderboard,
        "fines": fines,
        "history": formatted_history
    }

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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    body {
      background-color: #080c15;
      color: #f3f4f6;
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    .panel {
      background: #0d1322;
      border: 1px solid rgba(255, 255, 255, 0.07);
    }
    .panel-subtle {
      background: #12192c;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }
    .badge-completed { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge-progress { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
    .badge-fined { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge-submitted { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
    ::-webkit-scrollbar { width: 4px; height: 4px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #1e283d; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #334155; }
  </style>
</head>
<body class="min-h-screen lg:h-screen lg:overflow-hidden flex flex-col justify-between p-2.5 sm:p-3 text-xs antialiased">

  <!-- Minimal Top Header Bar -->
  <header class="flex flex-wrap items-center justify-between gap-2 px-3 py-2 rounded-xl panel mb-2.5 flex-shrink-0">
    <div class="flex items-center gap-2.5">
      <div class="w-7 h-7 rounded-lg bg-emerald-500/20 border border-emerald-500/30 flex items-center justify-center text-sm shadow-sm">
        🧼
      </div>
      <div>
        <div class="flex items-center gap-2">
          <span class="font-extrabold text-sm text-white tracking-tight">Dorm Inspector</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/25 tracking-wide">ALETCLOUD</span>
        </div>
        <p class="text-[10px] text-gray-400 font-mono leading-none">dorm-inspector-bot-1.app.aletcloud.com</p>
      </div>
    </div>

    <!-- Active Dorm Selector Tabs -->
    <div id="dorm-selector-tabs" class="flex items-center gap-1.5 overflow-x-auto py-0.5">
      <div class="px-2.5 py-1 rounded-lg bg-gray-800 text-[10px] text-gray-400 animate-pulse">Loading...</div>
    </div>

    <!-- Telemetry Badges & Refresh -->
    <div class="flex items-center gap-2.5 text-[11px]">
      <div class="flex items-center gap-1.5 px-2 py-0.8 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
        <span id="bot-status-text">Online</span>
      </div>
      <div class="hidden sm:flex items-center gap-1 text-gray-400 text-[10px]">
        <span>Uptime:</span>
        <strong id="stat-uptime" class="font-mono text-gray-200">--</strong>
      </div>
      <div class="hidden md:flex items-center gap-1 text-gray-400 text-[10px]">
        <span id="stat-current-date" class="text-gray-300">--</span>
      </div>
      <button onclick="fetchDashboardData()" class="p-1.5 rounded-lg panel-subtle hover:bg-gray-800 text-gray-300 hover:text-white transition" title="Refresh Live Data">
        <svg id="refresh-icon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
        </svg>
      </button>
    </div>
  </header>

  <!-- 4-Column Minimal Fixed Dashboard Grid -->
  <main class="flex-1 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2.5 overflow-y-auto lg:overflow-hidden min-h-0">

    <!-- Card 1: Active Duty (This Week) -->
    <div class="panel rounded-xl p-3 flex flex-col justify-between min-h-0">
      <div>
        <div class="flex items-center justify-between border-b border-gray-800 pb-2 mb-2.5">
          <div class="flex items-center gap-1.5">
            <span class="text-sm">🎯</span>
            <span class="font-bold text-gray-200 uppercase tracking-wider text-[10px]">Current Duty</span>
          </div>
          <span id="selected-dorm-status-badge" class="px-2 py-0.5 rounded-full text-[10px] font-semibold badge-progress">In Progress</span>
        </div>

        <div>
          <h2 id="selected-dorm-title" class="text-base font-extrabold text-white truncate">--</h2>
          <p id="selected-dorm-week-header" class="text-[11px] text-emerald-400 font-medium mt-0.5">Week 1 Duty</p>
        </div>

        <!-- Cleaners Spotlight -->
        <div class="mt-2.5 space-y-1.5">
          <div class="panel-subtle rounded-lg p-2 flex items-center justify-between">
            <div class="flex items-center gap-2">
              <span class="w-5 h-5 rounded bg-emerald-500/20 text-emerald-300 font-bold flex items-center justify-center text-[10px]">1</span>
              <div>
                <p class="text-[9px] text-gray-400 uppercase font-semibold">Primary Cleaner</p>
                <p id="cleaner-1-name" class="font-bold text-white text-xs">--</p>
              </div>
            </div>
            <span class="text-[10px] text-emerald-400 font-medium">On Duty</span>
          </div>

          <div class="panel-subtle rounded-lg p-2 flex items-center justify-between">
            <div class="flex items-center gap-2">
              <span class="w-5 h-5 rounded bg-emerald-500/20 text-emerald-300 font-bold flex items-center justify-center text-[10px]">2</span>
              <div>
                <p class="text-[9px] text-gray-400 uppercase font-semibold">Partner Cleaner</p>
                <p id="cleaner-2-name" class="font-bold text-white text-xs">--</p>
              </div>
            </div>
            <span class="text-[10px] text-emerald-400 font-medium">On Duty</span>
          </div>
        </div>
      </div>

      <!-- Approver & Deadline Info -->
      <div class="panel-subtle rounded-lg p-2.5 space-y-1.5 mt-2">
        <div class="flex items-center justify-between text-[11px]">
          <span class="text-gray-400">Approver:</span>
          <span id="selected-dorm-approver" class="font-bold text-emerald-400">--</span>
        </div>
        <div class="flex items-center justify-between text-[11px]">
          <span class="text-gray-400">Deadline:</span>
          <span class="font-semibold text-amber-300">Friday 8:00 PM</span>
        </div>
        <div class="flex items-center justify-between border-t border-gray-800/80 pt-1.5 text-[11px]">
          <span class="text-gray-400">Missed Fine:</span>
          <span id="selected-dorm-fine" class="font-bold text-red-400">100 ETB (50/50)</span>
        </div>
      </div>
    </div>

    <!-- Card 2: 4-Week Rotation Schedule -->
    <div class="panel rounded-xl p-3 flex flex-col min-h-0">
      <div class="flex items-center justify-between border-b border-gray-800 pb-2 mb-2 flex-shrink-0">
        <div class="flex items-center gap-1.5">
          <span class="text-sm">📅</span>
          <span class="font-bold text-gray-200 uppercase tracking-wider text-[10px]">Rotation Schedule</span>
        </div>
        <span class="text-[10px] text-gray-400">4 Weeks</span>
      </div>
      <div id="schedule-table" class="space-y-1.5 flex-1 overflow-y-auto pr-0.5">
        <p class="text-gray-500 text-center py-4 text-[11px]">Loading schedule...</p>
      </div>
    </div>

    <!-- Card 3: Tsidat Leaderboard -->
    <div class="panel rounded-xl p-3 flex flex-col min-h-0">
      <div class="flex items-center justify-between border-b border-gray-800 pb-2 mb-2 flex-shrink-0">
        <div class="flex items-center gap-1.5">
          <span class="text-sm">🪙</span>
          <span class="font-bold text-gray-200 uppercase tracking-wider text-[10px]">Tsidat Tokens</span>
        </div>
        <span class="text-[10px] text-emerald-400 font-semibold">Rankings</span>
      </div>
      <div id="leaderboard-list" class="space-y-1.5 flex-1 overflow-y-auto pr-0.5">
        <p class="text-gray-500 text-center py-4 text-[11px]">Loading leaderboard...</p>
      </div>
    </div>

    <!-- Card 4: Fines Ledger & Room Telemetry -->
    <div class="panel rounded-xl p-3 flex flex-col justify-between min-h-0">
      <!-- Unpaid Fines List -->
      <div class="flex flex-col min-h-0 flex-1">
        <div class="flex items-center justify-between border-b border-gray-800 pb-2 mb-2 flex-shrink-0">
          <div class="flex items-center gap-1.5">
            <span class="text-sm">💸</span>
            <span class="font-bold text-gray-200 uppercase tracking-wider text-[10px]">Unpaid Fines</span>
          </div>
          <span id="stat-fines" class="text-[11px] font-bold text-amber-400">0 ETB</span>
        </div>
        <div id="fines-list" class="space-y-1.5 overflow-y-auto flex-1 pr-0.5">
          <p class="text-gray-500 text-center py-2 text-[11px]">No unpaid fines.</p>
        </div>
      </div>

      <!-- Past Inspections Log & Stats -->
      <div class="border-t border-gray-800 pt-2 flex-shrink-0 space-y-1.5 mt-2">
        <div class="flex items-center justify-between border-b border-gray-800/80 pb-1">
          <div class="flex items-center gap-1.5">
            <span class="text-sm">📜</span>
            <span class="font-bold text-gray-200 uppercase tracking-wider text-[10px]">Recent Inspection Log</span>
          </div>
          <span class="text-[9px] text-gray-500">History</span>
        </div>
        <div id="history-list" class="space-y-1 max-h-20 overflow-y-auto pr-0.5">
          <p class="text-gray-500 text-center py-1 text-[10px]">No past inspection records yet.</p>
        </div>

        <div class="panel-subtle rounded-lg p-1.5 flex items-center justify-between text-[10px] text-gray-400">
          <span>Roommates: <strong id="stat-members" class="text-white">--</strong></span>
          <span>•</span>
          <span>Dorms: <strong id="stat-dorms" class="text-white">--</strong></span>
          <span>•</span>
          <span class="text-emerald-400 font-medium">1 Dorm Limit</span>
        </div>
      </div>
    </div>

  </main>

  <!-- Minimal Footer -->
  <footer class="mt-2.5 px-3 py-1.5 rounded-xl panel flex items-center justify-between text-[10px] text-gray-400 flex-shrink-0">
    <div>
      Dorm Inspector Bot • <span class="text-emerald-400 font-semibold">dorm-inspector-bot-1.app.aletcloud.com</span>
    </div>
    <div class="flex items-center gap-3">
      <span>Auto-refresh: <strong class="text-emerald-400">15s</strong></span>
      <span>Platform: <strong>AletCloud</strong></span>
    </div>
  </footer>

  <!-- Live Client-Side Data Fetcher -->
  <script>
    let activeDormId = null;
    let allDormsCache = [];

    async function fetchDashboardData() {
      const refreshIcon = document.getElementById('refresh-icon');
      if (refreshIcon) refreshIcon.classList.add('animate-spin');

      try {
        // 1. Fetch system status
        const sysRes = await fetch('/api/status');
        if (sysRes.ok) {
          const sys = await sysRes.json();
          document.getElementById('stat-dorms').innerText = sys.stats.active_dorms_count;
          document.getElementById('stat-members').innerText = sys.stats.total_members_count;
          document.getElementById('stat-uptime').innerText = sys.uptime;
          const dateEl = document.getElementById('stat-current-date');
          if (dateEl) dateEl.innerText = sys.current_date;
          document.getElementById('stat-fines').innerHTML = `${sys.stats.total_unpaid_fines_etb} ETB`;
        }

        // 2. Fetch all dorms
        const dormsRes = await fetch('/api/dorms');
        if (dormsRes.ok) {
          allDormsCache = await dormsRes.json();
          renderDormTabs(allDormsCache);
          if (allDormsCache.length > 0) {
            if (!activeDormId || !allDormsCache.find(d => d.dorm_id === activeDormId)) {
              activeDormId = allDormsCache[0].dorm_id;
            }
            await fetchDormDetails(activeDormId);
          } else {
            document.getElementById('selected-dorm-title').innerText = "No Active Dorms";
            document.getElementById('selected-dorm-week-header').innerText = "Run /setup inside Telegram to initialize.";
          }
        }
      } catch (err) {
        console.error("Dashboard refresh error:", err);
      } finally {
        if (refreshIcon) refreshIcon.classList.remove('animate-spin');
      }
    }

    function renderDormTabs(dorms) {
      const tabs = document.getElementById('dorm-selector-tabs');
      if (!tabs) return;

      if (!dorms || dorms.length === 0) {
        tabs.innerHTML = `<span class="text-[10px] text-gray-400 py-1">No dorms registered yet.</span>`;
        return;
      }

      tabs.innerHTML = dorms.map(d => {
        const isSelected = d.dorm_id === activeDormId;
        const activeClass = isSelected
          ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/50 font-bold shadow-sm'
          : 'panel-subtle text-gray-400 hover:text-gray-200 border-gray-800';

        return `
          <button onclick="selectDorm(${d.dorm_id})" class="px-2.5 py-1 rounded-lg text-[10px] border transition flex items-center gap-1.5 ${activeClass}">
            <span>🏢</span>
            <span>${d.dorm_name}</span>
            <span class="text-[9px] px-1 rounded bg-black/40 text-gray-300 font-mono">${d.member_count}m</span>
          </button>
        `;
      }).join('');
    }

    async function selectDorm(dormId) {
      activeDormId = dormId;
      renderDormTabs(allDormsCache);
      await fetchDormDetails(dormId);
    }

    async function fetchDormDetails(dormId) {
      try {
        const res = await fetch(`/api/dorms/${dormId}`);
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById('selected-dorm-title').innerText = data.dorm_name;
        document.getElementById('selected-dorm-fine').innerText = `${data.fine_amount} ETB (50/50)`;
        document.getElementById('selected-dorm-approver').innerText = data.current_week.approver || 'Not assigned';

        // Week header & badge
        const curWeek = data.current_week;
        document.getElementById('selected-dorm-week-header').innerText = `Week ${curWeek.week_number} Duty • ${curWeek.duty_state}`;

        const badge = document.getElementById('selected-dorm-status-badge');
        badge.innerText = curWeek.duty_state;
        badge.className = 'px-2 py-0.5 rounded-full text-[10px] font-semibold ' + getBadgeClass(curWeek.status_code);

        document.getElementById('cleaner-1-name').innerText = curWeek.cleaner1 || 'TBD';
        document.getElementById('cleaner-2-name').innerText = curWeek.cleaner2 || 'TBD';

        // Render schedule preview
        renderSchedule(data.schedule);

        // Render leaderboard
        renderLeaderboard(data.leaderboard);

        // Render fines
        renderFines(data.fines);

        // Render history
        renderHistory(data.history);

      } catch (err) {
        console.error("Error loading dorm details:", err);
      }
    }

    function getBadgeClass(code) {
      if (code === 'completed') return 'badge-completed';
      if (code === 'fined') return 'badge-fined';
      if (code === 'submitted') return 'badge-submitted';
      return 'badge-progress';
    }

    function renderSchedule(schedule) {
      const el = document.getElementById('schedule-table');
      if (!schedule || schedule.length === 0) {
        el.innerHTML = `<p class="text-gray-500 text-center py-4 text-[10px]">No upcoming rotation scheduled.</p>`;
        return;
      }

      el.innerHTML = schedule.map(w => {
        const isCur = w.is_current;
        const border = isCur ? 'border-emerald-500/40 bg-emerald-500/10' : 'panel-subtle';
        const tag = isCur ? '<span class="text-[9px] font-bold px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300">THIS WEEK</span>' : '';

        return `
          <div class="p-2 rounded-lg border flex items-center justify-between text-[11px] ${border}">
            <div class="flex items-center gap-1.5">
              <span class="font-bold text-white">W${w.week_number}</span>
              <span class="text-gray-400 text-[10px]">${w.monday}</span>
              ${tag}
            </div>
            <div class="font-medium text-gray-300 text-right">
              <span class="text-emerald-400 font-semibold">${w.cleaner1}</span> & <span class="text-emerald-400 font-semibold">${w.cleaner2}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderLeaderboard(board) {
      const el = document.getElementById('leaderboard-list');
      if (!board || board.length === 0) {
        el.innerHTML = `<p class="text-gray-500 text-center py-4 text-[10px]">No Tsidat tokens awarded yet.</p>`;
        return;
      }

      const medals = ['🥇', '🥈', '🥉'];
      el.innerHTML = board.map((item, idx) => {
        const medal = idx < 3 ? medals[idx] : `<span class="text-gray-500 text-[10px] font-mono">#${idx+1}</span>`;
        return `
          <div class="p-1.5 rounded-lg panel-subtle flex items-center justify-between text-[11px]">
            <div class="flex items-center gap-2">
              <span>${medal}</span>
              <span class="font-medium text-white">${item.name}</span>
            </div>
            <span class="font-bold text-emerald-400">${item.tsidat_balance} <span class="text-[9px] text-gray-400 font-normal">pts</span></span>
          </div>
        `;
      }).join('');
    }

    function renderFines(fines) {
      const el = document.getElementById('fines-list');
      if (!fines || fines.length === 0) {
        el.innerHTML = `<div class="p-2 rounded-lg panel-subtle text-center text-emerald-400 text-[10px]">✓ No unpaid fines. Everyone clean.</div>`;
        return;
      }

      el.innerHTML = fines.map(f => `
        <div class="p-1.5 rounded-lg panel-subtle flex items-center justify-between text-[11px]">
          <span class="font-medium text-white">${f.user_name}</span>
          <span class="font-bold text-red-400">${f.total_unpaid} ETB</span>
        </div>
      `).join('');
    }

    function renderHistory(hist) {
      const el = document.getElementById('history-list');
      if (!hist || hist.length === 0) {
        el.innerHTML = `<p class="text-gray-500 text-center py-1 text-[10px]">No past weeks recorded yet.</p>`;
        return;
      }

      el.innerHTML = hist.map(h => {
        const statusBadge = h.final_status === 'completed'
          ? `<span class="text-[9px] text-emerald-400 font-semibold">Cleaned ${h.rating ? '★' + h.rating : ''}</span>`
          : (h.final_status === 'fined' ? '<span class="text-[9px] text-red-400 font-semibold">Fined</span>' : '<span class="text-[9px] text-amber-400 font-semibold">Pending</span>');

        return `
          <div class="p-1 rounded panel-subtle flex items-center justify-between text-[10px]">
            <span class="font-bold text-gray-300">W${h.week_number}: ${h.cleaner1} & ${h.cleaner2}</span>
            ${statusBadge}
          </div>
        `;
      }).join('');
    }

    // Initial load and periodic refresh
    fetchDashboardData();
    setInterval(fetchDashboardData, 15000);
  </script>
</body>
</html>
"""

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
