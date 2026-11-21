# 🧹 Dorm Inspector Bot

An automated, multi-tenant Telegram platform built for university dorms across Ethiopia. Built with **python-telegram-bot v20+** (async), **SQLite** (multi-tenant schema with ACID persistence), and **JobQueue** for scheduled tasks.

Any student can add the bot to their dorm's Telegram group or topic, run `/setup`, invite roommates with a one-tap deep link, auto-generate pairs, and enjoy automated cleaning schedules, meme inspections, and fine tracking.

---

## 🌟 Key Features

- 🏢 **Universal Multi-Tenant Platform:** Multiple dorms across any Ethiopian university can use the same bot instance completely isolated from one another. Schedules, photos, members, and fines never leak.
- 🔗 **1-Tap Roommate Onboarding:** Admin runs `/setup` in the group to get an instant deep link (`https://t.me/<bot>?start=join_<group_id>`). Roommates click it once in DM to link their account.
- 👥 **Flexible Dorm Sizing:** Works with 4, 6, 8, 10, or 12 roommates (or any custom even/odd count). Automatically pairs members in rotation.
- 💸 **Customizable Fines:** Each dorm configures its own fine amount (50, 100, 200 ETB, or 0 ETB). Admins settle fines via `/markpaid` or `/paid`.
- 🎭 **Ethiopian Student Meme Integration:** Features 8 integrated meme moments (`welcome.jpg`, `announcement.jpg`, `suspicious_dog.jpg`, `reminder.jpg`, `approved.jpg`, `rejected.jpg`, `fined.jpg`, `paid.jpg`).
- 🔄 **Deterministic Weekly Rotation:** Cycles pairs weekly Monday through Friday. Weekends are always off-duty.
- 🎲 **Fair Approver Rotation:** Approver is randomly picked each Monday from non-cleaning roommates.
- 📸 **Photo Verification & Inspection:** Cleaners submit photos via DM. Approver inspects and approves or rejects with inline buttons.
- ⏰ **Friday Verification & Timeout Fallback:** At Friday 8:00 PM, cleaners answer a verification prompt, and the approver confirms the final result. If the approver doesn't reply within `APPROVER_TIMEOUT_HOURS`, the bot automatically falls back to the cleaners' submission.
- 🔁 **Resilient & Idempotent:** Built-in catch-up monitors trigger missed announcements, reminders, or checks if the bot was temporarily offline.

---

## 🚀 Quick Start for Any University Dorm

### 1. Add the Bot to Your Dorm Group
1. Create or open your dorm's Telegram group chat.
2. Add `@YourDormBot` to the group and promote it to **Admin** (with permission to send messages).
3. If your group has Topics (Forum enabled), open the designated cleaning topic (e.g., `#Dorm-Cleaning`).

### 2. Run `/setup`
Send `/setup` in the group topic. The bot will reply with the interactive Setup Card:
```
🧹 Dorm Inspector Setup

Dorm: Block 4, Room 204
Admin: @YourUsername
Members joined: 1
Fine per missed week: 100 ETB

👉 Share this invite link with your roommates:
https://t.me/YourDormBot?start=join_1002378440774
```

### 3. Roommates Click to Join
Roommates tap the link once. Their Telegram DM opens with `/start`, connecting their account for private duty notices and verification prompts.

### 4. Launch Cleaning Schedule!
When all roommates have joined, the admin taps **"🚀 Launch Cleaning Schedule"**.
- The bot automatically generates cleaning pairs.
- Announces Week 1 duty with the `welcome.jpg` meme.
- Weekly automated inspections begin!

---

## 💬 Command Reference

### Member Commands
| Command | Location | Description |
| :--- | :--- | :--- |
| `/start` | Private / Group | Connects user account, registers via join links, or opens main menu. |
| `/status` | Private / Group | Shows current week cleaners, approver, and duty state. |
| `/schedule` | Private / Group | Displays the upcoming 4-week cleaning schedule. |
| `/done` | Private DM | Cleaners submit their cleaning report and prompt for photos. |
| `/fines` | Private / Group | Displays the unpaid fines ledger. |
| `/history` | Private / Group | Lists results of past cleaning weeks. |
| `/members` | Private / Group | Shows who in the dorm has connected to the bot. |
| `/help` | Private / Group | Shows the user guide and weekly timeline. |

### Admin Commands
| Command | Location | Description |
| :--- | :--- | :--- |
| `/setup` | Group Chat | Starts interactive dorm initialization and member onboarding. |
| `/resetdorm`| Group Chat | Pauses or reconfigures dorm settings and pairs. |
| `/markpaid <name>` | Private / Group | Clears unpaid fines for a member in the ledger. |
| `/paid <name>` | Private / Group | Alias for `/markpaid`. |
| `/setid <name> <id>` | Private / Group | Updates or links a member's numeric Telegram user ID. |

---

## 📅 Weekly Inspection Timeline (Africa/Addis_Ababa UTC+3)

```
   ┌─────────────────────────────────────────────────────────────┐
   │ MONDAY 08:00 AM (Duty Announcement)                         │
   │ • Posts cleaners & approver to group with announcement meme │
   │ • DMs cleaners with "We cleaned" button                     │
   │ • DMs approver about review responsibility                  │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ MONDAY – FRIDAY (Cleaning & Submission)                     │
   │ • Cleaners clean, tap "We cleaned" or send /done            │
   │ • Cleaners send photo in DM (suspicious dog meme prompt)    │
   │ • Photo forwarded to group & approver DM with buttons       │
   │ • Approver approves or rejects photo                        │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ THURSDAY 06:00 PM (Gentle Reminder)                         │
   │ • Reminder sent to cleaners if photo not yet approved       │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ FRIDAY 08:00 PM (Check Time)                                │
   │ • Cleaners asked: "Did you clean?" ([Yes] / [No])           │
   │ • Approver asked: [Confirm Cleaned] / [Confirm Not Cleaned] │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ APPROVER DECISION OR TIMEOUT (12 Hours)                     │
   │ • Confirm Cleaned: Week completed, no fines                 │
   │ • Confirm Not Cleaned: Fine recorded per cleaner            │
   │ • Timeout Fallback: If approver silent, fallback to cleaner │
   │   answer and photo submission status                        │
   └─────────────────────────────────────────────────────────────┘
```

---

## 🌐 Public Hosting & Cloud Deployment

### Environment Variables
| Variable | Required | Description |
| :--- | :--- | :--- |
| `BOT_TOKEN` | **Yes** | Telegram Bot API token from `@BotFather`. |
| `GROUP_ID` | Optional | Default single-dorm group ID for backward compatibility. |
| `ADMIN_ID` | Optional | Default single-dorm admin ID. |
| `PORT` | Optional | Port for the built-in HTTP health check server (default `3000`). |
| `START_DATE` | Optional | Default fallback start date (`YYYY-MM-DD`, e.g. `2026-11-23`). |
| `APPROVER_TIMEOUT_HOURS` | Optional | Hours before approver timeout fallback triggers (default `12`). |

### Docker Deployment
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3000
CMD ["python", "bot.py"]
```

### Free Cloud Hosting Options
1. **Render.com / Railway / Fly.io / Koyeb:**
   - Connect your GitHub repository: `https://github.com/mntsnt/dorm-inspector-bot`
   - Set environment variable `BOT_TOKEN`.
   - The bot includes a built-in HTTP server on `$PORT` that answers `/` with 200 OK to keep cloud containers alive.
2. **Ubuntu / Debian VPS:**
   - Run with `systemd` or `docker compose`.
   - Continuous 24/7 background operation.
