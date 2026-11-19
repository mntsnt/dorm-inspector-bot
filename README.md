# 🧹 Dorm Inspector Bot

An automated, robust Telegram bot in Python that manages a weekly dorm cleaning schedule for 8 roommates. Built with **python-telegram-bot v20+** (async), **SQLite** for ACID-compliant persistence, and **JobQueue** for scheduled tasks.

---

## 🌟 Key Features

- **Fixed 4-Week Rotation:** Deterministically computed from `START_DATE` (e.g. `2026-11-27` Friday). A bot restart never changes the rotation order.
- **Weekly Schedule:** Runs Monday through Friday. Weekends (Saturday & Sunday) have no cleaning duties.
- **Random Approver Selection:** Every Monday, an approver is chosen at random from the 6 members not cleaning that week.
- **Photo Verification Workflow:** Cleaners submit photos via private DM, which are forwarded to the dorm group and sent to the approver with **Approve** / **Reject** buttons.
- **Friday Check & Final Authority:** At Friday 8:00 PM, cleaners answer a verification prompt, and the approver confirms the final result. If the approver does not respond within `APPROVER_TIMEOUT_HOURS`, the bot safely falls back to the cleaners' submission.
- **Fines Ledger:** A missed week automatically issues a **100 ETB total fine** (split **50 ETB per cleaner**). Admin can mark fines as paid via `/markpaid`.
- **Role-Based Security:** Only registered members can use the bot; only assigned cleaners can submit photos; only the assigned approver can approve; only the admin can clear fines.
- **Idempotent & Restart-Resilient:** Offline during Monday morning? The bot automatically catches up and announces as soon as it comes online. Fines and announcements are never duplicated.
- **Private DM Fallback:** If the bot cannot DM a member (e.g., they haven't sent `/start`), it automatically notifies them in the dorm group chat.

---

## 📁 Project Structure

```
dorm-inspector-bot/
├── .env.example              # Environment variables template
├── requirements.txt          # Python dependencies
├── README.md                 # Complete setup and user guide
├── members.json              # 8 members and their 4 fixed pairs
├── config.py                 # Configuration loader and startup validation
├── db.py                     # Async SQLite database layer and ledger
├── scheduler.py              # Automated jobs and timeout monitor
├── bot.py                    # Main bot entry point
├── handlers/                 # Modular Telegram message and callback handlers
│   ├── __init__.py           # Handler registration
│   ├── commands.py           # /start, /help, /status, /schedule, /fines, /history
│   ├── cleaning.py           # /done, photo processing, Friday check callbacks
│   ├── approval.py           # Approve/Reject & Final confirmation callbacks
│   └── admin.py              # /markpaid <member>
└── tests/                    # Unit and integration test suite
    ├── __init__.py
    └── test_suite.py
```

---

## 👥 Members and Pairs

Members and pairs are defined in `members.json`. On startup, the bot validates that **every member appears in exactly one pair**. If not, it halts with an error.

```json
{
  "members": [
    {"user_id": 111111111, "name": "Bilisuma"},
    {"user_id": 1758752115, "name": "Nahom"},
    {"user_id": 1434634789, "name": "Dawit"},
    {"user_id": 6764559285, "name": "Zeftret"},
    {"user_id": 555555555, "name": "Hassan"},
    {"user_id": 6163528558, "name": "Mintesnot"},
    {"user_id": 1677783364, "name": "Ahmed"},
    {"user_id": 5625992953, "name": "Yonatan"}
  ],
  "pairs": [
    ["Bilisuma", "Nahom"],
    ["Hassan", "Mintesnot"],
    ["Dawit", "Zeftret"],
    ["Ahmed", "Yonatan"]
  ]
}
```

### 4-Week Rotation Cycle
1. **Week 1 (Nov 30 – Dec 04):** Bilisuma & Nahom *(Announced Monday, Nov 30 at 08:00 AM)*
2. **Week 2 (Dec 07 – Dec 11):** Hassan & Mintesnot
3. **Week 3 (Dec 14 – Dec 18):** Dawit & Zeftret
4. **Week 4 (Dec 21 – Dec 25):** Ahmed & Yonatan
5. **Week 5 (Dec 28 – Jan 01):** Cycles back to Pair 1 (Bilisuma & Nahom)

---

## 🛠️ Setup Guide

### Step 1: Create a Bot via @BotFather
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to choose a name (e.g. `Dorm Inspector Bot`) and username (e.g. `my_dorm_inspector_bot`).
3. BotFather will provide an **HTTP API Token** (e.g. `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`). Copy this token for your `.env` file.
4. Optional but recommended: Send `/setprivacy` to `@BotFather`, select your bot, and set it to **DISABLED** so the bot can reliably see commands in group chats.

### Step 2: Get Telegram User IDs
Each of the 8 dorm mates needs their numeric Telegram User ID:
1. Open Telegram and search for [@userinfobot](https://t.me/userinfobot) (or [@raw_data_bot](https://t.me/raw_data_bot)).
2. Press **Start** (`/start`). The bot will reply with your numeric `Id` (e.g., `1758752115`).
3. Replace the placeholder IDs in `members.json` with each member's real Telegram ID.
4. Set the admin's user ID as `ADMIN_ID` in your `.env` file.

> **Important:** Every member must open a private chat with your bot and send `/start` once. Telegram prohibits bots from sending unprompted private messages to users until the user starts the conversation.

### Step 3: Get the Dorm Group Chat ID
1. Create a Telegram group for your dorm (or use your existing group).
2. Add your newly created bot to the group.
3. Promote the bot to an **Administrator** with permission to send messages.
4. Temporarily add [@userinfobot](https://t.me/userinfobot) or [@RawDataBot](https://t.me/raw_data_bot) to the group. It will display the group ID (a negative number, typically starting with `-100`, e.g., `-1001234567890`).
5. Remove the helper bot and put this value as `GROUP_ID` in `.env`.

### Step 4: Configure `.env`
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `.env` with your settings:
```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
GROUP_ID=-1001234567890
ADMIN_ID=111111111

# Rotation Start Date (e.g. 2026-11-27 Friday)
START_DATE=2026-11-27

# Schedule configurations (Timezone: Africa/Addis_Ababa)
ANNOUNCE_DAY=mon
ANNOUNCE_TIME=08:00
REMINDER_DAY=thu
REMINDER_TIME=18:00
CHECK_DAY=fri
CHECK_TIME=20:00

# Approver timeout window in hours
APPROVER_TIMEOUT_HOURS=12

# Total fine in ETB per missed week (split equally: 50 ETB each)
FINE_TOTAL=100
```

---

## 🚀 Installation & Running

### Prerequisites
- Python 3.11 or higher
- Git

### 1. Clone repository & enter directory
```bash
cd c:\Users\hp\Desktop\dorm-inspector-bot
```

### 2. Create and activate a virtual environment
**On Windows:**
```powershell
python -m venv venv
venv\Scripts\activate
```

**On Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Run tests
Verify that configuration, pairing validation, and database operations pass:
```bash
python -m unittest discover -s tests -p "test_*.py"
```

### 5. Launch the bot
```bash
python bot.py
```

---

## 🔄 Weekly Lifecycle & Automation Flow

```
   ┌─────────────────────────────────────────────────────────────┐
   │ MONDAY 08:00 AM (Africa/Addis_Ababa)                        │
   │ • Group Announcement: Cleaners (Pair) & Random Approver     │
   │ • Private DM to Cleaners: "You're on duty, deadline Fri 8PM"│
   │ • Private DM to Approver: "You're this week's approver"     │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ MONDAY – FRIDAY: Cleaning & Photo Review                    │
   │ • Cleaners press "We cleaned" or send /done in private chat │
   │ • Cleaners send a photo of the cleaned dorm                 │
   │ • Photo forwarded to group & sent to Approver with buttons  │
   │   - [✅ Approve] -> Marked completed, group notified        │
   │   - [❌ Reject]  -> Cleaners told to redo before deadline   │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ THURSDAY 06:00 PM                                           │
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
   │ • Confirm Not Cleaned: 100 ETB total fine (50 ETB each)     │
   │ • Timeout Fallback: If approver silent, fallback to cleaner │
   │   answer and photo submission status                        │
   └─────────────────────────────────────────────────────────────┘
```

---

## 💬 Command Reference

| Command | Allowed Users | Description |
| :--- | :--- | :--- |
| `/start` | Dorm Members | Registers user chat ID to enable private bot notifications. |
| `/status` | Dorm Members | Displays current week cleaners, approver, and duty state. |
| `/schedule` | Dorm Members | Shows the upcoming 4-week cleaning schedule with dates. |
| `/done` | Active Cleaners | Initiates cleaning completion and prompts for dorm photo. |
| `/fines` | Dorm Members | Displays the unpaid fines ledger for all members. |
| `/history` | Dorm Members | Lists cleaning results for the last 8 weeks. |
| `/help` | Dorm Members | Shows the user guide and weekly timeline. |
| `/markpaid <member>` | **Admin Only** | Marks all unpaid fines for `<member>` as paid. |

---

## 🔒 Security & Data Integrity

1. **Member Whitelist:** Only the 8 user IDs defined in `members.json` can interact with the bot. Unauthorized users receive a polite refusal.
2. **Action Validation:** Photo submissions and `/done` are restricted to the 2 active cleaners. Approval decisions are restricted to the assigned approver.
3. **Admin Privilege:** The `/markpaid` command checks that `user.id == ADMIN_ID`.
4. **Callback Query Token Validation:** Every button callback encodes the `week_start` and checks current database state to prevent replaying old buttons.
5. **Idempotent Fines:** SQLite table enforces `UNIQUE(week_start, user_id)`, making it impossible to issue duplicate fines for the same week.
6. **Timezone Accuracy:** All scheduling is bound to `Africa/Addis_Ababa` (East Africa Time, UTC+3).

---

## ⚙️ Running 24/7 as a Background Service (Linux)

To run the bot continuously on a VPS (e.g. Ubuntu):

1. Create a systemd service file:
```bash
sudo nano /etc/systemd/system/dorm-bot.service
```

2. Add the following configuration:
```ini
[Unit]
Description=Dorm Inspector Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/dorm-inspector-bot
ExecStart=/home/ubuntu/dorm-inspector-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

3. Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable dorm-bot
sudo systemctl start dorm-bot
sudo systemctl status dorm-bot
```
