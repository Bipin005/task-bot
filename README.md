# 🎮 Task Buddy — Telegram Study Buddy Bot

[![Telegram Bot](https://img.shields.io/badge/Telegram-Open%20Bot-blue?logo=telegram&style=for-the-badge)](https://t.me/BypnnBot)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-yellow?logo=python&style=for-the-badge)](https://www.python.org/)
[![Database](https://img.shields.io/badge/Database-Turso%20LibSQL-teal?style=for-the-badge)](https://turso.tech/)

A gamified task management and study tracking Telegram bot designed to convert daily academic milestones into RPG quests with XP scaling, dynamic ranks, and persistent cloud sync.

---

## 🚀 Live Demo
Directly launch and test the bot on Telegram: 👉 **[Open in Telegram](https://t.me/BypnnBot)** *(For Password contact me)*

---

## ✨ Key Features

- 🎯 **Gamified Missions:** Add tasks categorized by subject (Physics, Chemistry, Math) and difficulty to earn XP.
- 🔥 **Streak & Progression System:** Maintain daily streaks with automatic multiplier bonuses and visual rank badges.
- ⚡ **Process-Level Turso Connection:** Remote LibSQL connection pooling designed for low-latency queries.
- 📊 **Weekly Analytics & Daily Archiving:** Automated calendar-day midnight rolling summaries and consistency tracking.
- 🔐 **Access Gate Security:** Integrated access-key authentication middleware.
- ⏰ **Smart Notifications:** Automated daily scheduled alerts and 15-minute pre-deadline notifications.

---

## 🎮 Command Guide

| Command | Action |
| :--- | :--- |
| `/menu` | Interactive inline button navigation dashboard |
| `/add` | Create a new study mission with subject & deadline |
| `/tasks` | View today's pending and completed missions |
| `/done <ID>` | Complete a mission, trigger streak checks, and claim XP |
| `/stats` | View current rank, level progress bar, and streak days |
| `/analytics` | Weekly consistency breakdown and subject performance |
| `/achievements` | Inspect unlocked milestone badges |
| `/leaderboard` | Global XP ranking table |

---

## 🛠 Tech Stack

- **Runtime:** Python 3.12 (`python-telegram-bot` v20+)
- **Database:** Turso LibSQL (Remote SQLite with single-connection pooling)
- **Timezone Management:** `zoneinfo` (Asia/Kolkata / IST standard)
- **Environment Handling:** `python-dotenv`

---

## ⚙️ Local Setup

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/Bipin005/task-bot.git](https://github.com/Bipin005/task-bot.git)
   cd task-bot
