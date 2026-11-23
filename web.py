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
# Single-Page HTML Dashboard UI
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
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ['"Plus Jakarta Sans"', 'sans-serif'],
          },
          colors: {
            brand: {
              50: '#ecfdf5',
              100: '#d1fae5',
              400: '#34d399',
              500: '#10b981',
              600: '#059669',
              700: '#047857',
            }
          }
        }
      }
    }
  </script>
  <style>
    body {
      background-color: #0b0f19;
      color: #f3f4f6;
    }
    .glass-card {
      background: rgba(17, 24, 39, 0.75);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .glass-card-hover:hover {
      border-color: rgba(52, 211, 153, 0.3);
      box-shadow: 0 8px 30px rgba(16, 185, 129, 0.1);
    }
    .badge-completed { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-progress { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-fined { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    .badge-submitted { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); }
  </style>
</head>
<body class="min-h-screen flex flex-col font-sans antialiased selection:bg-brand-500 selection:text-white">

  <!-- Top Navigation Header -->
  <header class="border-b border-gray-800/80 bg-gray-950/70 backdrop-blur-md sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-brand-600 to-emerald-400 flex items-center justify-center shadow-lg shadow-emerald-500/20">
          <span class="text-xl">🧼</span>
        </div>
        <div>
          <h1 class="text-lg font-bold tracking-tight text-white flex items-center gap-2">
            Dorm Inspector
            <span class="text-xs px-2 py-0.5 rounded-full bg-brand-500/10 text-brand-400 border border-brand-500/30 font-semibold uppercase tracking-wider">Production</span>
          </h1>
          <p class="text-xs text-gray-400 hidden sm:block">Automated Cleaning Rotation & Room Inspection</p>
        </div>
      </div>

      <div class="flex items-center space-x-4">
        <!-- Live Status Pill -->
        <div class="flex items-center space-x-2 px-3 py-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-medium">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          <span id="bot-status-text">Bot Online</span>
        </div>

        <button onclick="fetchDashboardData()" class="p-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white transition flex items-center gap-1.5 text-xs font-medium border border-gray-700" title="Refresh Live Data">
          <svg id="refresh-icon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path>
          </svg>
          <span class="hidden sm:inline">Refresh</span>
        </button>
      </div>
    </div>
  </header>

  <!-- Main Content Layout -->
  <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">

    <!-- Platform Quick Metrics Row -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="glass-card rounded-2xl p-5 relative overflow-hidden transition-all duration-300 glass-card-hover">
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Active Dorms</p>
        <p id="stat-dorms" class="text-3xl font-extrabold text-white mt-1">--</p>
        <div class="text-[11px] text-emerald-400 mt-2 flex items-center gap-1">
          <span>●</span> Multi-Tenant Partitioning
        </div>
      </div>

      <div class="glass-card rounded-2xl p-5 relative overflow-hidden transition-all duration-300 glass-card-hover">
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Registered Roommates</p>
        <p id="stat-members" class="text-3xl font-extrabold text-white mt-1">--</p>
        <div class="text-[11px] text-gray-400 mt-2">1 Dorm per Student Limit</div>
      </div>

      <div class="glass-card rounded-2xl p-5 relative overflow-hidden transition-all duration-300 glass-card-hover">
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Uptime</p>
        <p id="stat-uptime" class="text-2xl font-bold text-white mt-1">--</p>
        <div class="text-[11px] text-emerald-400 mt-2">Zero Restarts Recorded</div>
      </div>

      <div class="glass-card rounded-2xl p-5 relative overflow-hidden transition-all duration-300 glass-card-hover">
        <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Unpaid Fines Ledger</p>
        <p id="stat-fines" class="text-3xl font-extrabold text-amber-400 mt-1">0 <span class="text-xs text-gray-400 font-normal">ETB</span></p>
        <div class="text-[11px] text-gray-400 mt-2">Split 50/50 on Missed Duty</div>
      </div>
    </section>

    <!-- Dorm Room Selector Tabs -->
    <section class="space-y-4">
      <div class="flex items-center justify-between border-b border-gray-800 pb-3">
        <div>
          <h2 class="text-xl font-bold text-white tracking-tight">Dorm Rooms</h2>
          <p class="text-xs text-gray-400 mt-0.5">Select a dorm room to view its schedule, duty status, and leaderboard</p>
        </div>
        <div class="text-xs text-gray-400">
          Auto-updates every <span class="text-emerald-400 font-semibold">15s</span>
        </div>
      </div>

      <div id="dorm-selector-tabs" class="flex flex-wrap gap-2">
        <div class="px-4 py-2 rounded-xl bg-gray-800/80 animate-pulse text-xs text-gray-400">Loading dorms...</div>
      </div>
    </section>

    <!-- Selected Dorm Detailed Dashboard View -->
    <section id="dorm-detail-container" class="space-y-6">
      <!-- Top Banner: Current Cleaning Week & Status -->
      <div class="glass-card rounded-3xl p-6 sm:p-8 relative overflow-hidden">
        <div class="absolute -right-12 -bottom-12 w-64 h-64 bg-emerald-500/5 rounded-full blur-3xl pointer-events-none"></div>

        <div class="flex flex-col md:flex-row md:items-center justify-between gap-6">
          <div>
            <div class="flex items-center gap-3">
              <span id="selected-dorm-title" class="text-2xl sm:text-3xl font-extrabold text-white">Loading...</span>
              <span id="selected-dorm-status-badge" class="px-3 py-1 rounded-full text-xs font-semibold badge-progress">In Progress</span>
            </div>
            <p id="selected-dorm-week-header" class="text-sm text-gray-400 mt-1">Week Duty Overview</p>
          </div>

          <div class="flex flex-wrap items-center gap-4 bg-gray-900/60 p-4 rounded-2xl border border-gray-800">
            <div class="text-center px-3">
              <p class="text-[11px] font-semibold text-gray-400 uppercase">Default Fine</p>
              <p id="selected-dorm-fine" class="text-lg font-bold text-white mt-0.5">100 ETB</p>
            </div>
            <div class="h-8 w-[1px] bg-gray-800"></div>
            <div class="text-center px-3">
              <p class="text-[11px] font-semibold text-gray-400 uppercase">Approver</p>
              <p id="selected-dorm-approver" class="text-lg font-bold text-emerald-400 mt-0.5">--</p>
            </div>
          </div>
        </div>

        <!-- Cleaners in Spotlight Card -->
        <div class="mt-8 grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="p-5 rounded-2xl bg-gray-900/90 border border-gray-800/80 flex items-center space-x-4">
            <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-emerald-600 to-teal-400 flex items-center justify-center font-bold text-lg text-white shadow-md shadow-emerald-500/20">
              1
            </div>
            <div>
              <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Primary Cleaner</p>
              <p id="cleaner-1-name" class="text-lg font-bold text-white mt-0.5">--</p>
              <p class="text-xs text-emerald-400/90 mt-0.5">On Duty This Week</p>
            </div>
          </div>

          <div class="p-5 rounded-2xl bg-gray-900/90 border border-gray-800/80 flex items-center space-x-4">
            <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-emerald-600 to-teal-400 flex items-center justify-center font-bold text-lg text-white shadow-md shadow-emerald-500/20">
              2
            </div>
            <div>
              <p class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Partner Cleaner</p>
              <p id="cleaner-2-name" class="text-lg font-bold text-white mt-0.5">--</p>
              <p class="text-xs text-emerald-400/90 mt-0.5">On Duty This Week</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Schedule + Leaderboard Grid -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

        <!-- 4-Week Rotation Preview (2 cols) -->
        <div class="glass-card rounded-3xl p-6 lg:col-span-2 space-y-4">
          <div class="flex items-center justify-between border-b border-gray-800 pb-3">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <span>📅</span> Upcoming Rotation Schedule
            </h3>
            <span class="text-xs text-gray-400">Next 4 Weeks</span>
          </div>

          <div id="schedule-table" class="space-y-3">
            <p class="text-xs text-gray-500 py-4 text-center">Loading schedule...</p>
          </div>
        </div>

        <!-- Tsidat Tokens Leaderboard (1 col) -->
        <div class="glass-card rounded-3xl p-6 space-y-4">
          <div class="flex items-center justify-between border-b border-gray-800 pb-3">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <span>🪙</span> Tsidat Leaderboard
            </h3>
            <span class="text-xs text-emerald-400 font-medium">Rankings</span>
          </div>

          <div id="leaderboard-list" class="space-y-2.5">
            <p class="text-xs text-gray-500 py-4 text-center">No tokens awarded yet.</p>
          </div>
        </div>

      </div>

      <!-- Unpaid Fines & Recent Inspection History Row -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        
        <!-- Fines Ledger Card -->
        <div class="glass-card rounded-3xl p-6 space-y-4">
          <div class="flex items-center justify-between border-b border-gray-800 pb-3">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <span>💸</span> Unpaid Fines Ledger
            </h3>
            <span class="text-xs text-amber-400 font-medium">Auto-calculated</span>
          </div>

          <div id="fines-list" class="space-y-2.5">
            <p class="text-xs text-gray-500 py-4 text-center">No unpaid fines! Everyone is clean. 🎉</p>
          </div>
        </div>

        <!-- History Log Card -->
        <div class="glass-card rounded-3xl p-6 space-y-4">
          <div class="flex items-center justify-between border-b border-gray-800 pb-3">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <span>📜</span> Recent Inspection History
            </h3>
            <span class="text-xs text-gray-400">Past Weeks</span>
          </div>

          <div id="history-list" class="space-y-2.5">
            <p class="text-xs text-gray-500 py-4 text-center">No past inspection records yet.</p>
          </div>
        </div>

      </div>
    </section>

  </main>

  <!-- Footer -->
  <footer class="border-t border-gray-800/80 bg-gray-950/60 mt-auto py-6">
    <div class="max-w-7xl mx-auto px-4 text-center text-xs text-gray-500 flex flex-col sm:flex-row items-center justify-between gap-2">
      <p>Dorm Inspector Bot • Powered by SQLite WAL Engine</p>
      <p class="text-gray-400">Telegram University Dorm Management System</p>
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
          document.getElementById('stat-fines').innerHTML = `${sys.stats.total_unpaid_fines_etb} <span class="text-xs text-gray-400 font-normal">ETB</span>`;
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
            document.getElementById('selected-dorm-week-header').innerText = "Run /setup inside your Telegram group chat to initialize.";
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
        tabs.innerHTML = `<span class="text-xs text-gray-400 py-1">No registered dorms yet. Run /setup in your group.</span>`;
        return;
      }

      tabs.innerHTML = dorms.map(d => {
        const isSelected = d.dorm_id === activeDormId;
        const activeClass = isSelected
          ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/50 shadow-md shadow-emerald-500/10 font-bold'
          : 'bg-gray-900/60 text-gray-400 hover:text-gray-200 border-gray-800 hover:border-gray-700';

        return `
          <button onclick="selectDorm(${d.dorm_id})" class="px-4 py-2 rounded-xl text-xs border transition flex items-center gap-2 ${activeClass}">
            <span>🏢</span>
            <span>${d.dorm_name}</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-800 text-gray-300">${d.member_count}m</span>
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
        document.getElementById('selected-dorm-fine').innerText = `${data.fine_amount} ETB`;
        document.getElementById('selected-dorm-approver').innerText = data.current_week.approver || 'Not assigned';

        // Week header & badge
        const curWeek = data.current_week;
        document.getElementById('selected-dorm-week-header').innerText = `Week ${curWeek.week_number} Duty • ${curWeek.duty_state}`;

        const badge = document.getElementById('selected-dorm-status-badge');
        badge.innerText = curWeek.duty_state;
        badge.className = 'px-3 py-1 rounded-full text-xs font-semibold ' + getBadgeClass(curWeek.status_code);

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
        el.innerHTML = `<p class="text-xs text-gray-500 py-4 text-center">No upcoming rotation scheduled.</p>`;
        return;
      }

      el.innerHTML = schedule.map(w => {
        const currentHighlight = w.is_current
          ? 'bg-emerald-500/10 border-emerald-500/30'
          : 'bg-gray-900/60 border-gray-800/80';
        const tag = w.is_current ? '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">THIS WEEK</span>' : '';

        return `
          <div class="p-3.5 rounded-xl border flex items-center justify-between text-xs ${currentHighlight}">
            <div class="flex items-center space-x-3">
              <span class="font-bold text-white w-16">Week ${w.week_number}</span>
              <span class="text-gray-400">${w.monday} – ${w.friday}</span>
              ${tag}
            </div>
            <div class="font-semibold text-gray-200">
              <span class="text-emerald-400">${w.cleaner1}</span> & <span class="text-emerald-400">${w.cleaner2}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    function renderLeaderboard(board) {
      const el = document.getElementById('leaderboard-list');
      if (!board || board.length === 0) {
        el.innerHTML = `<p class="text-xs text-gray-500 py-4 text-center">No Tsidat tokens awarded yet.</p>`;
        return;
      }

      const medals = ['🥇', '🥈', '🥉'];
      el.innerHTML = board.map((item, idx) => {
        const medal = idx < 3 ? medals[idx] : `•`;
        return `
          <div class="p-2.5 rounded-xl bg-gray-900/60 border border-gray-800/80 flex items-center justify-between text-xs">
            <div class="flex items-center space-x-2">
              <span>${medal}</span>
              <span class="font-medium text-white">${item.name}</span>
            </div>
            <span class="font-bold text-emerald-400">${item.tsidat_balance} Tsidat</span>
          </div>
        `;
      }).join('');
    }

    function renderFines(fines) {
      const el = document.getElementById('fines-list');
      if (!fines || fines.length === 0) {
        el.innerHTML = `<p class="text-xs text-gray-500 py-4 text-center">No unpaid fines! Everyone is clean. 🎉</p>`;
        return;
      }

      el.innerHTML = fines.map(f => `
        <div class="p-2.5 rounded-xl bg-gray-900/60 border border-gray-800/80 flex items-center justify-between text-xs">
          <span class="font-medium text-white">${f.user_name}</span>
          <span class="font-bold text-red-400">${f.total_unpaid} ETB</span>
        </div>
      `).join('');
    }

    function renderHistory(hist) {
      const el = document.getElementById('history-list');
      if (!hist || hist.length === 0) {
        el.innerHTML = `<p class="text-xs text-gray-500 py-4 text-center">No past inspection records yet.</p>`;
        return;
      }

      el.innerHTML = hist.map(h => {
        const statusBadge = h.final_status === 'completed'
          ? `<span class="text-[10px] text-emerald-400">Cleaned ${h.rating ? '★' + h.rating : ''}</span>`
          : (h.final_status === 'fined' ? '<span class="text-[10px] text-red-400">Fined</span>' : '<span class="text-[10px] text-amber-400">Pending</span>');

        return `
          <div class="p-2.5 rounded-xl bg-gray-900/60 border border-gray-800/80 flex items-center justify-between text-xs">
            <div class="flex items-center space-x-2">
              <span class="font-bold text-gray-300">W${h.week_number}</span>
              <span class="text-gray-400">${h.cleaner1} & ${h.cleaner2}</span>
            </div>
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
