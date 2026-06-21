# ♟️ Chess01 — Telegram Chess Bot

A Telegram chess bot with a full interactive Mini App board. Play against the computer or challenge a friend with a shareable link — all directly inside Telegram.

[Demo](https://t.me/Chess01Bot)

---

## Features

- 🤖 **Play vs Bot** — 3 difficulty levels (Easy / Medium / Hard) powered by a minimax engine with alpha-beta pruning and piece-square evaluation
- 👥 **Play vs Friend** — generate an invite link, share it on Telegram, and play in real time via peer-to-peer connection (no server needed)
- 🎨 **Interactive Mini App board** — click-to-move interface, legal move highlighting, check detection, promotion picker
- 🔊 **Sound effects** — move, capture, check, checkmate, and UI interaction sounds
- 🌍 **Country flags** — players pick their country once, shown to opponents automatically
- 🖼️ **Telegram avatars** — real profile photos shown on the board and home screen
- ⏱️ **Live timers** — configurable time controls per game mode

---

## Project Structure

```
chess01-project/
├── web/                    → Deploy to GitHub Pages
│   ├── index.html          → Home screen (mode picker)
│   └── chess_miniapp.html  → Game board (bot + multiplayer)
│
└── bot/                     → Deploy to Railway / Render
    ├── chess_bot.py         → Telegram bot backend
    ├── requirements.txt
    └── Procfile
```

---

## Setup

### 1. Create your bot
Talk to [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → save your token.

### 2. Deploy the Mini App (GitHub Pages)
1. Push the contents of `web/` to a public GitHub repo
2. Repo → **Settings → Pages → Branch: main → Save**
3. Your URL will be: `https://YOUR_USERNAME.github.io/REPO_NAME/`

### 3. Deploy the bot (Railway)
```bash
npm install -g @railway/cli
railway login
cd bot
railway init
railway up --detach
```

Then add environment variables in the Railway dashboard:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | your bot token from BotFather |
| `MINIAPP_URL` | your GitHub Pages URL (must end with `/`) |

### 4. Test it
Send `/start` to your bot on Telegram.

---

## Tech Stack

- **Bot:** Python, `python-telegram-bot`, SQLite
- **Mini App:** Vanilla JS, [chess.js](https://github.com/jhlywa/chess.js) for game logic, [PeerJS](https://peerjs.com/) for P2P multiplayer
- **Hosting:** Railway (bot) + GitHub Pages (Mini App)

---

## License

MIT
