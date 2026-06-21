#!/usr/bin/env python3
"""
♟️ Telegram Chess Bot
Features: Play chess, player profiles, ELO ratings, matchmaking, leaderboard
"""

import os
import io
import uuid
import random
import logging
import sqlite3
from typing import Optional, Tuple

import chess
import chess.svg
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BOT_TOKEN    = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "YOUR_BOT_TOKEN_HERE"
MINIAPP_URL  = os.getenv("MINIAPP_URL", "")  # Your hosted chess_miniapp.html URL
DB_PATH     = "chess_bot.db"

XP_WIN      = 100
XP_DRAW     = 50
XP_LOSS     = 25
XP_PER_LVL  = 500
DEFAULT_ELO = 1200
K_FACTOR    = 32

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── DATABASE SETUP ───────────────────────────────────────────────────────────

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id  INTEGER PRIMARY KEY,
                name         TEXT    NOT NULL,
                username     TEXT,
                xp           INTEGER DEFAULT 0,
                level        INTEGER DEFAULT 1,
                wins         INTEGER DEFAULT 0,
                losses       INTEGER DEFAULT 0,
                draws        INTEGER DEFAULT 0,
                elo          INTEGER DEFAULT 1200,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS games (
                game_id     TEXT    PRIMARY KEY,
                white_id    INTEGER,
                black_id    INTEGER,
                fen         TEXT    DEFAULT 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                status      TEXT    DEFAULT 'active',
                result      TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS moves (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id     TEXT,
                player_id   INTEGER,
                move_san    TEXT,
                move_uci    TEXT,
                move_number INTEGER,
                played_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS matchmaking (
                telegram_id INTEGER PRIMARY KEY,
                joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS challenges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id     INTEGER,
                to_id       INTEGER,
                status      TEXT    DEFAULT 'pending',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ─── DB HELPERS ───────────────────────────────────────────────────────────────

def register_user(tid: int, name: str, username: Optional[str]):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, name, username) VALUES (?,?,?)",
            (tid, name, username),
        )

def get_user(tid: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_active_game(tid: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM games WHERE (white_id=? OR black_id=?) AND status='active' LIMIT 1",
        (tid, tid),
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def create_game(white_id: int, black_id: int) -> str:
    gid = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO games (game_id, white_id, black_id) VALUES (?,?,?)",
            (gid, white_id, black_id),
        )
    return gid

def update_fen(game_id: str, fen: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE games SET fen=?, updated_at=CURRENT_TIMESTAMP WHERE game_id=?",
            (fen, game_id),
        )

def finish_game(game_id: str, status: str, result: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE games SET status=?, result=?, updated_at=CURRENT_TIMESTAMP WHERE game_id=?",
            (status, result, game_id),
        )

def record_move(game_id: str, player_id: int, san: str, uci: str, n: int):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO moves (game_id, player_id, move_san, move_uci, move_number) VALUES (?,?,?,?,?)",
            (game_id, player_id, san, uci, n),
        )

def get_last_move(game_id: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM moves WHERE game_id=? ORDER BY id DESC LIMIT 1",
        (game_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def move_count(game_id: str) -> int:
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM moves WHERE game_id=?", (game_id,)
    ).fetchone()[0]
    conn.close()
    return n

def update_stats(tid: int, result: str, elo_change: int):
    xp_gain = {"win": XP_WIN, "draw": XP_DRAW, "loss": XP_LOSS}[result]
    col_map  = {"win": "wins", "draw": "draws", "loss": "losses"}[result]
    conn = get_db()
    user = dict(conn.execute("SELECT xp, elo FROM users WHERE telegram_id=?", (tid,)).fetchone())
    conn.close()
    new_xp  = user["xp"] + xp_gain
    new_elo = max(100, user["elo"] + elo_change)
    new_lvl = new_xp // XP_PER_LVL + 1
    with get_db() as conn:
        conn.execute(
            f"UPDATE users SET {col_map}={col_map}+1, xp=?, elo=?, level=? WHERE telegram_id=?",
            (new_xp, new_elo, new_lvl, tid),
        )

# ─── ELO ──────────────────────────────────────────────────────────────────────

def calc_elo(r1: int, r2: int, draw=False) -> Tuple[int, int]:
    e1 = 1 / (1 + 10 ** ((r2 - r1) / 400))
    e2 = 1 - e1
    s1, s2 = (0.5, 0.5) if draw else (1, 0)
    return round(K_FACTOR * (s1 - e1)), round(K_FACTOR * (s2 - e2))

# ─── BOARD RENDER ─────────────────────────────────────────────────────────────

# Piece unicode map
PIECES = {
    (chess.PAWN,   chess.WHITE): '♙', (chess.KNIGHT, chess.WHITE): '♘',
    (chess.BISHOP, chess.WHITE): '♗', (chess.ROOK,   chess.WHITE): '♖',
    (chess.QUEEN,  chess.WHITE): '♕', (chess.KING,   chess.WHITE): '♔',
    (chess.PAWN,   chess.BLACK): '♟', (chess.KNIGHT, chess.BLACK): '♞',
    (chess.BISHOP, chess.BLACK): '♝', (chess.ROOK,   chess.BLACK): '♜',
    (chess.QUEEN,  chess.BLACK): '♛', (chess.KING,   chess.BLACK): '♚',
}
LIGHT = (240, 217, 181)
DARK  = (181, 136, 99)
HL_FROM = (170, 162, 58)
HL_TO   = (205, 210, 106)

def render_board(board: chess.Board, flipped=False, last_move=None) -> bytes:
    sq = 60
    size = sq * 8
    img  = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 44)
    except Exception:
        font = ImageFont.load_default()

    for rank in range(8):
        for file in range(8):
            r = 7 - rank if not flipped else rank
            f = file if not flipped else 7 - file
            sq_idx = chess.square(f, r)
            is_light = (f + r) % 2 == 1
            color = LIGHT if is_light else DARK
            if last_move:
                if sq_idx == last_move.from_square: color = HL_FROM
                if sq_idx == last_move.to_square:   color = HL_TO
            x, y = file * sq, rank * sq
            draw.rectangle([x, y, x+sq-1, y+sq-1], fill=color)
            piece = board.piece_at(sq_idx)
            if piece:
                symbol = PIECES.get((piece.piece_type, piece.color), "?")
                draw.text((x+4, y+2), symbol, font=font,
                          fill=(255,255,255) if piece.color == chess.WHITE else (20,20,20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def rank_emoji(elo: int) -> str:
    if elo >= 2000: return "👑"
    if elo >= 1800: return "💎"
    if elo >= 1600: return "🥇"
    if elo >= 1400: return "🥈"
    if elo >= 1200: return "🥉"
    return "🔰"

def xp_bar(xp_in: int, total: int, n=12) -> str:
    filled = int((xp_in / total) * n)
    return "█" * filled + "░" * (n - filled)

async def send_board_to(ctx, game: dict, target_id: int, caption: str):
    board     = chess.Board(game["fen"])
    flipped   = target_id == game["black_id"]
    last      = get_last_move(game["game_id"])
    last_move = None
    if last:
        try: last_move = chess.Move.from_uci(last["move_uci"])
        except: pass
    png = render_board(board, flipped=flipped, last_move=last_move)
    await ctx.bot.send_photo(
        chat_id=target_id,
        photo=io.BytesIO(png),
        caption=caption,
        parse_mode="Markdown",
    )

def start_game_msg(uid: int, white_id: int, opp: dict) -> str:
    color = "♟️ White" if uid == white_id else "♙ Black"
    your_turn = uid == white_id
    return (
        f"🎮 *Game started!*\n\n"
        f"You're playing: *{color}*\n"
        f"Opponent: *{opp['name']}* {rank_emoji(opp['elo'])} {opp['elo']} ELO\n\n"
        + ("✅ *You go first!* Send your move:" if your_turn else "⏳ Waiting for opponent's move…")
    )

# ─── GAME LIFECYCLE ───────────────────────────────────────────────────────────

async def launch_game(ctx, white_id: int, black_id: int):
    gid  = create_game(white_id, black_id)
    game = {"game_id": gid, "white_id": white_id, "black_id": black_id,
            "fen": chess.Board().fen()}
    wu, bu = get_user(white_id), get_user(black_id)
    board  = chess.Board()
    png    = render_board(board)
    for uid, opp in [(white_id, bu), (black_id, wu)]:
        await ctx.bot.send_photo(
            chat_id=uid,
            photo=io.BytesIO(png),
            caption=start_game_msg(uid, white_id, opp),
            parse_mode="Markdown",
        )
    # Send Mini App button
    if MINIAPP_URL:
        wu2 = get_user(white_id)
        bu2 = get_user(black_id)
        for uid, color in [(white_id, 'white'), (black_id, 'black')]:
            opp  = bu2 if uid == white_id else wu2
            me2  = wu2 if uid == white_id else bu2
            url  = (f"{MINIAPP_URL}?game_id={gid}&color={color}"
                    f"&opp_name={opp['name']}&opp_elo={opp['elo']}&opp_level={opp['level']}"
                    f"&my_elo={me2['elo']}&my_level={me2['level']}&timer=600")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 Open Chess Board", web_app={"url": url})
            ]])
            await ctx.bot.send_message(uid, "Tap to open the interactive board:", reply_markup=kb)
    return gid

async def end_game_win(ctx, game: dict, winner_id: int, loser_id: int, reason: str):
    result = "white" if winner_id == game["white_id"] else "black"
    finish_game(game["game_id"], "finished", result)
    wu, lu = get_user(winner_id), get_user(loser_id)
    wc, lc = calc_elo(wu["elo"], lu["elo"])
    update_stats(winner_id, "win",  wc)
    update_stats(loser_id,  "loss", lc)
    reason_txt = {"checkmate": "by checkmate ♛", "resign": "by resignation 🏳️"}.get(reason, "")
    await ctx.bot.send_message(winner_id,
        f"🏆 *You won {reason_txt}!*\n\n"
        f"ELO: {wu['elo']} → {wu['elo']+wc} (*+{wc}*)\n"
        f"XP gained: +{XP_WIN} ⭐\n\n/play for a rematch!",
        parse_mode="Markdown")
    await ctx.bot.send_message(loser_id,
        f"💀 *You lost {reason_txt}.*\n\n"
        f"ELO: {lu['elo']} → {max(100, lu['elo']+lc)} (*{lc}*)\n"
        f"XP gained: +{XP_LOSS} ⭐\n\nBetter luck next time! /play",
        parse_mode="Markdown")

async def end_game_draw(ctx, game: dict, reason: str):
    finish_game(game["game_id"], "drawn", "draw")
    wu = get_user(game["white_id"])
    bu = get_user(game["black_id"])
    wc, bc = calc_elo(wu["elo"], bu["elo"], draw=True)
    update_stats(game["white_id"], "draw", wc)
    update_stats(game["black_id"], "draw", bc)
    for uid, u, ch in [(game["white_id"], wu, wc), (game["black_id"], bu, bc)]:
        sign = "+" if ch >= 0 else ""
        await ctx.bot.send_message(uid,
            f"🤝 *Draw — {reason}*\n\n"
            f"ELO: {u['elo']} → {u['elo']+ch} (*{sign}{ch}*)\n"
            f"XP gained: +{XP_DRAW} ⭐\n\n/play for a new game!",
            parse_mode="Markdown")

# ─── COMMANDS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register_user(u.id, u.full_name, u.username)
    # Handle deep link: /start join_ROOMCODE
    args = ctx.args
    if args and args[0].startswith("join_"):
        room_id = args[0][5:]  # strip "join_"
        if MINIAPP_URL:
            join_url = (f"{MINIAPP_URL}chess_miniapp.html"
                        f"?mode=guest&room={room_id}"
                        f"&color=black"
                        f"&my_name={u.first_name}"
                        f"&my_elo={get_user(u.id)['elo']}"
                        f"&my_level={get_user(u.id)['level']}")
            kb2 = InlineKeyboardMarkup([[
                InlineKeyboardButton("♟️ Join Game", web_app={"url": join_url})
            ]])
            await update.message.reply_text(
                f"♟️ *{u.first_name}*, your friend invited you to play chess!\n\n"
                f"Tap below to join the game:",
                parse_mode="Markdown", reply_markup=kb2
            )
        else:
            await update.message.reply_text("⚠️ Game URL not configured yet.")
        return
    p = get_user(u.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Play", callback_data="play_menu"),
         InlineKeyboardButton("👤 Profile", callback_data="my_profile")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
    ])
    await update.message.reply_text(
        f"♟️ *Welcome, {u.first_name}!*\n\n"
        f"{rank_emoji(p['elo'])} Registered with *{p['elo']} ELO*\n\n"
        f"*Commands:*\n"
        f"/play — Find a game\n"
        f"/challenge @user — Challenge a friend\n"
        f"/board — Show current board\n"
        f"/resign — Resign current game\n"
        f"/draw — Offer a draw\n"
        f"/profile — Your stats\n"
        f"/leaderboard — Rankings",
        parse_mode="Markdown", reply_markup=kb,
    )

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register_user(u.id, u.full_name, u.username)
    if get_active_game(u.id):
        await update.message.reply_text("⚠️ You're already in a game! Use /board to see it, /resign to quit.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Random Opponent", callback_data="mm_join")],
        [InlineKeyboardButton("❌ Cancel",           callback_data="cancel")],
    ])
    await update.message.reply_text(
        "♟️ *Find a Game*\n\n"
        "🎲 *Random* — Matched with another player\n"
        "👥 *Friend* — Use /challenge @username",
        parse_mode="Markdown", reply_markup=kb,
    )

async def cmd_challenge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register_user(u.id, u.full_name, u.username)
    if get_active_game(u.id):
        await update.message.reply_text("⚠️ You're already in a game!")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /challenge @username")
        return
    username = ctx.args[0].lstrip("@")
    conn = get_db()
    opp = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not opp:
        await update.message.reply_text(f"❌ @{username} hasn't started the bot yet!")
        return
    opp = dict(opp)
    if opp["telegram_id"] == u.id:
        await update.message.reply_text("❌ You can't challenge yourself!")
        return
    if get_active_game(opp["telegram_id"]):
        await update.message.reply_text(f"❌ @{username} is already in a game!")
        return
    with get_db() as conn:
        conn.execute("DELETE FROM challenges WHERE from_id=? AND status='pending'", (u.id,))
        conn.execute("INSERT INTO challenges (from_id, to_id) VALUES (?,?)", (u.id, opp["telegram_id"]))
    me = get_user(u.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept",  callback_data=f"ch_accept_{u.id}"),
         InlineKeyboardButton("❌ Decline", callback_data=f"ch_decline_{u.id}")],
    ])
    try:
        await ctx.bot.send_message(
            opp["telegram_id"],
            f"♟️ *Challenge from {u.first_name}!*\n\n"
            f"{rank_emoji(me['elo'])} {me['name']} — *{me['elo']} ELO*\n\n"
            f"Do you accept?",
            parse_mode="Markdown", reply_markup=kb,
        )
        await update.message.reply_text(f"✅ Challenge sent to @{username}!")
    except Exception:
        await update.message.reply_text(f"❌ Couldn't reach @{username}.")

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    register_user(u.id, u.full_name, u.username)
    await _show_profile(update, ctx, get_user(u.id))

async def _show_profile(update, ctx, p: dict):
    lvl     = p["xp"] // XP_PER_LVL + 1
    xp_in   = p["xp"] % XP_PER_LVL
    total   = p["wins"] + p["losses"] + p["draws"]
    wr      = round(p["wins"] / total * 100) if total else 0
    bar     = xp_bar(xp_in, XP_PER_LVL)
    uname   = f"@{p['username']}" if p["username"] else "—"
    text = (
        f"👤 *{p['name']}*  {uname}\n\n"
        f"{rank_emoji(p['elo'])} *ELO:* {p['elo']}\n"
        f"⭐ *Level {lvl}*  {xp_in}/{XP_PER_LVL} XP\n"
        f"`{bar}`\n\n"
        f"🏆 Wins: {p['wins']}  |  💀 Losses: {p['losses']}  |  🤝 Draws: {p['draws']}\n"
        f"🎮 Total games: {total}  |  📈 Win rate: {wr}%"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Play", callback_data="play_menu")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    rows = conn.execute(
        "SELECT name, elo, wins FROM users ORDER BY elo DESC LIMIT 10"
    ).fetchall()
    conn.close()
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = ["🏆 *Chess Leaderboard*\n"]
    for i, r in enumerate(rows):
        lines.append(f"{medals[i]} {rank_emoji(r['elo'])} *{r['name']}* — {r['elo']} ELO  ({r['wins']}W)")
    if not rows:
        lines.append("No players yet. Be the first to /play!")
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_board(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    game = get_active_game(u.id)
    if not game:
        await update.message.reply_text("❌ You're not in a game!")
        return
    board    = chess.Board(game["fen"])
    is_white = u.id == game["white_id"]
    your_turn = (board.turn == chess.WHITE) == is_white
    color     = "♟️ White" if is_white else "♙ Black"
    turn_txt  = "✅ *Your turn!*" if your_turn else "⏳ Opponent's turn…"
    last = get_last_move(game["game_id"])
    lm   = None
    lt   = ""
    if last:
        try:
            lm = chess.Move.from_uci(last["move_uci"])
            lt = f"Last move: *{last['move_san']}*\n"
        except: pass
    png = render_board(board, flipped=(u.id == game["black_id"]), last_move=lm)
    await update.message.reply_photo(
        io.BytesIO(png),
        caption=f"{lt}Playing as: *{color}*\n{turn_txt}",
        parse_mode="Markdown",
    )

async def cmd_resign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    game = get_active_game(u.id)
    if not game:
        await update.message.reply_text("❌ You're not in a game!")
        return
    winner_id = game["black_id"] if u.id == game["white_id"] else game["white_id"]
    await end_game_win(ctx, game, winner_id, u.id, "resign")

async def cmd_draw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    game = get_active_game(u.id)
    if not game:
        await update.message.reply_text("❌ You're not in a game!")
        return
    opp_id = game["black_id"] if u.id == game["white_id"] else game["white_id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept Draw",  callback_data=f"draw_accept_{game['game_id']}_{u.id}"),
         InlineKeyboardButton("❌ Decline",      callback_data=f"draw_decline_{game['game_id']}")],
    ])
    await ctx.bot.send_message(opp_id, "🤝 Your opponent is offering a draw.", reply_markup=kb)
    await update.message.reply_text("🤝 Draw offer sent!")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "♟️ *Chess Bot Help*\n\n"
        "*Game commands*\n"
        "/play — Find a random opponent\n"
        "/challenge @user — Challenge a friend\n"
        "/board — Show the current board\n"
        "/resign — Resign your game\n"
        "/draw — Offer a draw\n\n"
        "*Profile & Stats*\n"
        "/profile — View your profile\n"
        "/leaderboard — Top 10 players\n\n"
        "*How to make a move*\n"
        "Just send the move in chat:\n"
        "`e4`  `Nf3`  `O-O`  `e8=Q`\n"
        "Or UCI format: `e2e4`  `g1f3`",
        parse_mode="Markdown",
    )

# ─── MOVE HANDLER ─────────────────────────────────────────────────────────────

async def handle_move(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    text = update.message.text.strip()
    game = get_active_game(u.id)
    if not game:
        return

    board    = chess.Board(game["fen"])
    is_white = u.id == game["white_id"]
    if (board.turn == chess.WHITE) != is_white:
        await update.message.reply_text("⏳ It's not your turn!")
        return

    # Parse move
    move = None
    try:
        move = board.parse_san(text)
    except Exception:
        pass
    if not move:
        try:
            m = chess.Move.from_uci(text)
            if m in board.legal_moves:
                move = m
        except Exception:
            pass
    if not move:
        await update.message.reply_text(
            "❌ Invalid move. Try `e4`, `Nf3`, or `e2e4`.", parse_mode="Markdown"
        )
        return

    san = board.san(move)
    n   = move_count(game["game_id"]) + 1
    board.push(move)
    new_fen = board.fen()
    update_fen(game["game_id"], new_fen)
    record_move(game["game_id"], u.id, san, move.uci(), n)
    game["fen"] = new_fen

    opp_id = game["black_id"] if u.id == game["white_id"] else game["white_id"]

    # ── Check game end ──
    if board.is_checkmate():
        await send_board_to(ctx, game, u.id,   f"♛ *{san}* — Checkmate! You won! 🏆")
        await send_board_to(ctx, game, opp_id, f"♛ *{san}* — Checkmate! You lost. 💀")
        await end_game_win(ctx, game, u.id, opp_id, "checkmate")
        return

    if board.is_stalemate():
        await send_board_to(ctx, game, u.id,   f"*{san}* — Stalemate! 🤝")
        await send_board_to(ctx, game, opp_id, f"*{san}* — Stalemate! 🤝")
        await end_game_draw(ctx, game, "Stalemate")
        return

    if board.is_insufficient_material():
        await send_board_to(ctx, game, u.id,   f"*{san}* — Insufficient material 🤝")
        await send_board_to(ctx, game, opp_id, f"*{san}* — Insufficient material 🤝")
        await end_game_draw(ctx, game, "Insufficient material")
        return

    if board.is_seventyfive_moves():
        await send_board_to(ctx, game, u.id,   f"*{san}* — 75-move rule 🤝")
        await send_board_to(ctx, game, opp_id, f"*{san}* — 75-move rule 🤝")
        await end_game_draw(ctx, game, "75-move rule")
        return

    # Game continues
    check = "⚠️ *Check!*\n" if board.is_check() else ""
    mover_name = u.first_name
    await send_board_to(ctx, game, u.id,
        f"✅ You played *{san}*\n{check}⏳ Waiting for opponent…")
    await send_board_to(ctx, game, opp_id,
        f"♟️ *{mover_name}* played *{san}*\n{check}✅ *Your turn!* Send your move:")

# ─── CALLBACK HANDLER ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    u    = q.from_user
    data = q.data
    register_user(u.id, u.full_name, u.username)

    # ── Play menu ──
    if data == "play_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Random Opponent", callback_data="mm_join")],
            [InlineKeyboardButton("⬅️ Back",            callback_data="cancel")],
        ])
        await q.edit_message_text(
            "♟️ *Find a Game*\n\n"
            "🎲 *Random* — Matched with another player\n"
            "👥 *Friend* — Use /challenge @username",
            parse_mode="Markdown", reply_markup=kb,
        )

    # ── Random matchmaking ──
    elif data == "mm_join":
        if get_active_game(u.id):
            await q.edit_message_text("⚠️ You're already in a game! /board to see it.")
            return
        conn = get_db()
        opp  = conn.execute(
            "SELECT * FROM matchmaking WHERE telegram_id!=? ORDER BY joined_at ASC LIMIT 1",
            (u.id,),
        ).fetchone()
        conn.close()
        if opp:
            opp_id = opp["telegram_id"]
            with get_db() as conn:
                conn.execute("DELETE FROM matchmaking WHERE telegram_id IN (?,?)", (u.id, opp_id))
            w_id, b_id = (u.id, opp_id) if random.random() < 0.5 else (opp_id, u.id)
            await launch_game(ctx, w_id, b_id)
            await q.edit_message_text("✅ Match found! Game started.")
        else:
            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO matchmaking (telegram_id) VALUES (?)", (u.id,))
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mm_cancel")]])
            await q.edit_message_text(
                "🔍 *Searching for opponent…*\n\nYou'll be notified when a match is found!",
                parse_mode="Markdown", reply_markup=kb,
            )

    elif data == "mm_cancel":
        with get_db() as conn:
            conn.execute("DELETE FROM matchmaking WHERE telegram_id=?", (u.id,))
        await q.edit_message_text("❌ Matchmaking cancelled.")

    # ── My profile ──
    elif data == "my_profile":
        await _show_profile(update, ctx, get_user(u.id))

    # ── Leaderboard ──
    elif data == "leaderboard":
        await cmd_leaderboard(update, ctx)

    # ── Accept challenge ──
    elif data.startswith("ch_accept_"):
        challenger_id = int(data.split("_")[-1])
        conn = get_db()
        ch = conn.execute(
            "SELECT * FROM challenges WHERE from_id=? AND to_id=? AND status='pending'",
            (challenger_id, u.id),
        ).fetchone()
        conn.close()
        if not ch:
            await q.edit_message_text("❌ Challenge expired.")
            return
        if get_active_game(u.id) or get_active_game(challenger_id):
            await q.edit_message_text("❌ Someone is already in a game!")
            return
        with get_db() as conn:
            conn.execute("UPDATE challenges SET status='accepted' WHERE from_id=? AND to_id=?",
                         (challenger_id, u.id))
        w_id, b_id = (challenger_id, u.id) if random.random() < 0.5 else (u.id, challenger_id)
        await launch_game(ctx, w_id, b_id)
        await q.edit_message_text("✅ Challenge accepted! Game started.")

    # ── Decline challenge ──
    elif data.startswith("ch_decline_"):
        challenger_id = int(data.split("_")[-1])
        with get_db() as conn:
            conn.execute("UPDATE challenges SET status='declined' WHERE from_id=? AND to_id=?",
                         (challenger_id, u.id))
        await q.edit_message_text("❌ Challenge declined.")
        try:
            await ctx.bot.send_message(challenger_id, "❌ Your challenge was declined. Try /play!")
        except Exception:
            pass

    # ── Accept draw ──
    elif data.startswith("draw_accept_"):
        parts   = data.split("_")
        game_id = parts[2]
        conn = get_db()
        game = conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        conn.close()
        if not game or game["status"] != "active":
            await q.edit_message_text("❌ Game already ended.")
            return
        game = dict(game)
        offerer_id = int(parts[3])
        await q.edit_message_text("🤝 Draw accepted!")
        await ctx.bot.send_message(offerer_id, "🤝 Draw accepted!")
        await end_game_draw(ctx, game, "Agreement")

    # ── Decline draw ──
    elif data.startswith("draw_decline_"):
        await q.edit_message_text("❌ Draw declined — game continues!")

    # ── Cancel / back ──
    elif data in ("cancel", "back_home"):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Play", callback_data="play_menu"),
             InlineKeyboardButton("👤 Profile", callback_data="my_profile")],
            [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
        ])
        await q.edit_message_text("♟️ *Chess Bot — Main Menu*",
                                   parse_mode="Markdown", reply_markup=kb)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("play",        cmd_play))
    app.add_handler(CommandHandler("challenge",   cmd_challenge))
    app.add_handler(CommandHandler("profile",     cmd_profile))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("board",       cmd_board))
    app.add_handler(CommandHandler("resign",      cmd_resign))
    app.add_handler(CommandHandler("draw",        cmd_draw))
    app.add_handler(CommandHandler("help",        cmd_help))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_move))

    logger.info("♟️ Chess Bot is running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
