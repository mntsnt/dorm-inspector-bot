# 🧹 Dorm Inspector Bot

An automated, multi-tenant Telegram platform built for university dorms across Ethiopia. Built with **python-telegram-bot v20+** (async), **SQLite** (multi-tenant schema with ACID persistence), and **JobQueue** for scheduled tasks.

Any student can add the bot to their dorm's Telegram group or topic, run `/setup`, invite roommates with a one-tap deep link, auto-generate pairs, and enjoy automated cleaning schedules, meme inspections, and fine tracking.

---

## 🌟 Key Features

- 🏢 **Universal Multi-Tenant Platform:** Multiple dorms across any Ethiopian university can use the same bot instance completely isolated from one another. Schedules, photos, members, and fines never leak.
- 🔗 **1-Tap Roommate Onboarding:** Admin runs `/setup` in the group to get an instant deep link (`https://t.me/<bot>?start=join_<group_id>`). Roommates click it once in DM to link their account.
- 👥 **Flexible Dorm Sizing:** Works with 4, 6, 8, 10, or 12 roommates (or any custom even/odd count). Automatically pairs members in rotation.
- 💸 **Customizable Fines:** Each dorm configures its total missed-week fine. It is split across that week's cleaners without losing any amount. Admins settle fines via `/pay`, `/markpaid`, or `/paid`.
- 🎭 **Ethiopian Student Meme Integration:** Features 8 integrated meme moments (`welcome.jpg`, `announcement.jpg`, `suspicious_dog.jpg`, `reminder.jpg`, `approved.jpg`, `rejected.jpg`, `fined.jpg`, `paid.jpg`).
- 🔄 **Deterministic Weekly Rotation:** Cycles pairs weekly Monday through Friday. Weekends are always off-duty.
- 🎲 **Fair Approver Rotation:** Approver is randomly picked each Monday from non-cleaning roommates.
- 📸 **Photo Verification & Inspection:** Cleaners submit photos in the dorm group/topic or by DM. The assigned approver reviews and approves or rejects with a 1–5-star rating and optional note.
- ⏰ **Daytime Reminders & Friday Deadline:** Cleaners get at most one reminder every six hours between 8:00 AM and 8:00 PM (Addis Ababa time), stopping once they report cleaning. At Friday 8:00 PM, weeks without photo proof are closed and fined; pending proof is sent to the approver instead.
- 🎁 **Tsidat Rewards:** Cleaners earn up to 25 tokens from the approver's 1–5-star rating, plus an early-cleaning bonus: +5 Monday, +4 Tuesday, +3 Wednesday, +2 Thursday, or +1 Friday. The approver earns +5 for a manual review.
- 🛡️ **Review Timeout:** If submitted proof is not reviewed within 24 hours, the bot auto-approves it at 3 stars. The approver bonus is only awarded for a manual review.
- 🔐 **Authenticated Dashboard Review:** Approvers and dorm admins can inspect and rate a pending photo from the dashboard after signing in with Telegram.
- 🕒 **Recorded Completion Time:** The cleaner's “We cleaned” time (or photo-submission time if no button was used) is shown in bot status and the dashboard.
- 🔁 **Resilient & Idempotent:** Built-in catch-up monitors trigger missed announcements, reminders, or checks if the bot was temporarily offline.

---

## 🚀 Quick Start for Any University Dorm

### 1. Add the Bot to Your Dorm Group
1. Create or open your dorm's Telegram group chat.
2. Add `@YourDormBot` to the group and promote it to **Admin** (with permission to send messages).
3. If your group has Topics (Forum enabled), open the designated cleaning topic (e.g., `#Dorm-Cleaning`).

### 2. Start Setup in the Group
Send `/setup` in the dorm group or cleaning topic. The first person becomes the dorm admin and is added to the roster. The bot replies with the setup card and a private join link:
```
🧹 Dorm Inspector Setup

Dorm: Block 4, Room 204
Admin: @YourUsername
Connected roommates: not set
Fine per missed week: 100 ETB

👉 Share this invite link with your roommates:
https://t.me/YourDormBot?start=join_1002378440774
```

### 3. Set the Roster and Invite Everyone
The admin selects the expected total from the setup card, or sends `/setmembers 8` in the group. Include the admin in this count. Each roommate, including the admin, taps the private join link and presses **Start** so the bot can send duty messages and reminders.

The setup card and **Members** button show connection progress. The bot will not launch until the registered and connected roommate counts both match the expected total.

### 4. Launch the Schedule
Once everyone is connected, the admin taps **"🚀 Launch when roster is ready"**. The bot validates the roster, creates a rotation without duplicating roommates, and activates the schedule for the current or next Monday. Repeated taps cannot relaunch or repost the launch announcement.

---

## 💬 Command Reference

### Member Commands
| Command | Location | Description |
| :--- | :--- | :--- |
| `/start` | Private / Group | Connects user account, registers via join links, or opens main menu. |
| `/status` | Private / Group | Shows current week cleaners, approver, and duty state. |
| `/schedule` | Private / Group | Displays the upcoming 4-week cleaning schedule. |
| `/done` | Private DM | Cleaners submit their cleaning report and prompt for photos. |
| `/approve <1-5> [note]` | Private DM | The assigned approver reviews a submitted photo and records a rating and optional note. |
| `/fines` | Private / Group | Displays the unpaid fines ledger. |
| `/tsidat` | Private / Group | Shows the dorm's Tsidat token leaderboard. |
| `/history` | Private / Group | Lists results of past cleaning weeks. |
| `/members` | Private / Group | Shows who in the dorm has connected to the bot. |
| `/help` | Private / Group | Shows the user guide and weekly timeline. |

### Admin Commands
| Command | Location | Description |
| :--- | :--- | :--- |
| `/setup` | Group Chat | Starts interactive dorm initialization and member onboarding. |
| `/setmembers <total>` | Group Chat | Sets the expected roommate count during setup (include the admin). |
| `/resetdorm`| Group Chat | Pauses or reconfigures dorm settings and pairs. |
| `/markpaid <name>` | Private / Group | Clears unpaid fines for a member in the ledger. |
| `/paid <name>` | Private / Group | Alias for `/markpaid`. |
| `/pay <name>` | Private / Group | Alias for `/markpaid`. |
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
   │ • Cleaners clean any day and tap "We cleaned" or send /done │
   │ • Cleaner sends proof photo in the group/topic or in DM    │
   │ • Approver reviews the photo and gives a 1–5-star rating   │
   │ • Approval congratulates cleaners, awards tokens, and ends │
   │   reminders and further weekly duty announcements          │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ DAYTIME REMINDERS (8 AM, 2 PM, 8 PM; Addis Ababa time)      │
   │ • At most one reminder every six hours, while duty is open │
   │ • Reminders stop when cleaning is reported or submitted     │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ FRIDAY 08:00 PM DEADLINE                                    │
   │ • No submitted photo: duty is closed and the fine is logged │
   │ • Pending photo: approver is prompted; no fine is logged    │
   │ • Pending proof auto-approves at 3 stars after 24 hours     │
   │ • Next week's rotation continues normally                   │
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

For dashboard photo review and approval, configure the dashboard's domain with BotFather (`/setdomain`) for this bot. Review actions require a valid Telegram Login session and are limited to the assigned approver, dorm admin, or global admin.

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
