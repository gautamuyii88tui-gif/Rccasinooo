import asyncio
import html
import random
import time
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Dice
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from pvp import MODES, Match

try:
    import dns.resolver
    dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
    dns.resolver.default_resolver.nameservers = ["8.8.8.8", "8.8.4.4"]
except Exception:
    pass

try:
    from motor.motor_asyncio import AsyncIOMotorClient
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False

# --- CONFIG & GLOBALS ---
ACTIVE_BOT_TOKEN = config.BOT_TOKEN.strip()
ACTIVE_HELPER_TOKEN = getattr(config, "HELPER_BOT_TOKEN", "").strip()

# Initialize Aiogram Bot & Dispatcher
bot = Bot(token=ACTIVE_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
helper_bot = Bot(token=ACTIVE_HELPER_TOKEN) if ACTIVE_HELPER_TOKEN else None
dp = Dispatcher()
router = Router()

mongo_client = None
db = None

user_last_dice_time = {}
matches: dict[str, dict] = {}
user_match: dict[int, str] = {}
msg_owner: dict[tuple[int, int], int] = {}
user_names: dict[int, str] = {}
busy: set[int] = set()

PREMIUM_EMOJIS = {
    "🎮": "5258508428212445001", "🎲": "5960608239623082921",
    "👑": "6021428854889913572", "👎": "5084577224980431440",
    "🤝": "5974565221182741203", "🪙": "4918300654197277832",
    "1️⃣": "5393089050884202480", "2️⃣": "5361902709232377443", 
    "🏆": "5893376775781617954", "🤖": "5258093637450866522",
    "👤": "4913497231492908158", "❌": "5210952531676504517",
    "🌊": "5262553290972875718", "💀": "5042209657527993345",
    "⭕️": "5435993158536143379"
}

def pe(emoji_char: str) -> str:
    eid = PREMIUM_EMOJIS.get(emoji_char)
    if eid: return f'<tg-emoji emoji-id="{eid}">{emoji_char}</tg-emoji>'
    return emoji_char

def get_game_emo(game: str) -> str:
    mapping = {"dice": "🎲", "basket": "🏀", "bask": "🏀", "football": "⚽", "bowl": "🎳", "dart": "🎯"}
    return mapping.get(game, "🎲")

def game_pe(game: str) -> str: return pe(get_game_emo(game))

def usd(cents: int) -> str: return f"${cents / 100:.2f}"

def track_user_info(u):
    uid = u.id
    if not uid: return
    fname = u.first_name or u.username or "Player"
    user_names[uid] = fname

def mention(uid: int, name: str = None) -> str:
    label = name or user_names.get(uid) or "Player"
    return f'<a href="tg://user?id={uid}">{html.escape(str(label))}</a>'


# --- ASYNC MONGODB DATABASE FUNCTIONS ---
async def db_get_user(uid: int) -> dict:
    u = await db.users.find_one({"user_id": uid})
    if not u:
        u = {"user_id": uid, "balance": 0, "wagered": 0, "won": 0}
        await db.users.insert_one(u)
    return u

async def get_bal(uid: int) -> int:
    doc = await db_get_user(uid)
    return doc.get("balance", 0)

async def db_open_game(uid: int, game: str, mode: str, stake: int, opponent: int = 0) -> str:
    if stake < config.MIN_BET: raise ValueError(f"Minimum bet is {usd(config.MIN_BET)}")
    u = await db_get_user(uid)
    if u["balance"] < stake: raise ValueError("Insufficient balance")
    
    await db.users.update_one({"user_id": uid}, {"$inc": {"balance": -stake}})
    res = await db.games.insert_one({
        "user_id": uid, "game": game, "mode": mode, "stake": stake, 
        "payout": 0, "opponent": opponent, "status": "open", "ts": time.time()
    })
    return str(res.inserted_id)

async def db_close_game(gid: str, payout: int, void: bool = False):
    from bson.objectid import ObjectId
    g = await db.games.find_one({"_id": ObjectId(gid), "status": "open"})
    if not g: return
    uid, stake = g["user_id"], g["stake"]
    status = "void" if void else "done"
    
    await db.games.update_one({"_id": ObjectId(gid)}, {"$set": {"status": status, "payout": payout}})
    await db.users.update_one({"user_id": uid}, {"$inc": {"balance": payout}})
    
    if not void:
        won_amt = max(0, payout - stake)
        await db.users.update_one({"user_id": uid}, {"$inc": {"wagered": stake, "won": won_amt}})

async def db_void_game(gid: str):
    from bson.objectid import ObjectId
    g = await db.games.find_one({"_id": ObjectId(gid)})
    if g and g["status"] == "open":
        await db_close_game(gid, g["stake"], void=True)

def parse_bet(arg_str: str, balance: int) -> int:
    s = arg_str.strip().lower()
    if s == "all": return balance
    if s == "half": return max(config.MIN_BET, balance // 2)
    try: return int(round(float(s) * 100))
    except ValueError: return -1


# --- KEYBOARD BUILDER ---
def kb(*rows) -> InlineKeyboardMarkup:
    """Aiogram 3 Keyboard Builder replacing raw JSON"""
    inline_keyboard = []
    for row in rows:
        kb_row = []
        for item in row:
            text = item[0]
            cb_data = item[1]
            # Standard Telegram API doesn't support native background color "style" for inline buttons,
            # but we keep the structure exactly as you requested.
            btn = InlineKeyboardButton(text=text, callback_data=cb_data)
            kb_row.append(btn)
        if kb_row:
            inline_keyboard.append(kb_row)
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


# --- TELEGRAM ACTIONS ---
async def send_emoji_roll(chat_id: int, game: str, n: int, reply_to: int = None) -> list[int]:
    values = []
    emo = get_game_emo(game)
    for i in range(n):
        res = None
        target_bot = helper_bot if helper_bot else bot
        try:
            msg = await target_bot.send_dice(chat_id=chat_id, emoji=emo, reply_to_message_id=reply_to)
            res = msg.dice.value
        except Exception:
            try:
                # Fallback to main bot if helper fails
                msg = await bot.send_dice(chat_id=chat_id, emoji=emo, reply_to_message_id=reply_to)
                res = msg.dice.value
            except Exception: pass
            
        if res: values.append(res)
        if i < n - 1: await asyncio.sleep(0.12)
    await asyncio.sleep(2.0)
    return values

# --- UI HELPERS ---
def mode_screen_text(game: str) -> str:
    return (f"{game_pe(game)} <b>Select game mode</b>\n\n• <b>Normal mode:</b> Highest value wins\n"
            f"• <b>Crazy mode:</b> Lowest value wins\n• <b>Double mode:</b> 2 emojis are rolled in 1 round\n"
            f"• <b>Triple mode:</b> 3 emojis are rolled in 1 round")

def mode_screen_kb(game: str, stake: int):
    emo = get_game_emo(game)
    return kb([("Normal", f"mode:{game}:{stake}:normal", "primary", emo), ("Crazy", f"mode:{game}:{stake}:crazy", "danger", "💀")],
              [("Double", f"mode:{game}:{stake}:double", "primary", emo), ("Double Crazy", f"mode:{game}:{stake}:double_crazy", "danger", "💀")],
              [("Triple", f"mode:{game}:{stake}:triple", "primary", emo), ("Triple Crazy", f"mode:{game}:{stake}:triple_crazy", "danger", "💀")],
              [("Delete", "del", "danger", "❌")])

def target_screen_text(game: str) -> str:
    return f"{game_pe(game)} <b>Select the number of points needed to win</b>\n\n{pe('ℹ️')} <i>First to reach the selected number of points wins the match!</i>"

def target_screen_kb(game: str, stake: int, mode: str):
    return kb([("First to 1 point", f"tgt:{game}:{stake}:{mode}:1", "primary", "1️⃣")],
              [("First to 2 points", f"tgt:{game}:{stake}:{mode}:2", "primary", "2️⃣")],
              [("First to 3 points", f"tgt:{game}:{stake}:{mode}:3", "primary", "🏆")],
              [("Back", f"back_mode:{game}:{stake}", "danger", "❌")])

def calc_cashout(stake: int, p_score: int, b_score: int, target: int) -> int:
    if p_score <= b_score or p_score <= 0: return 0
    full_win = stake * 1.92
    progress = (p_score - b_score) / target
    return min(int(stake + (full_win - stake) * (0.45 + 0.35 * progress)), int(full_win * 0.92))

def turn_kb(sess: dict, show_bot_first: bool = False):
    gid = sess["gid"]
    emo = get_game_emo(sess["game"])
    m: Match = sess["match"]
    rows = []
    if sess["type"] == "bot":
        cashout = calc_cashout(sess["stake"], m.score[0], m.score[1], sess["target"])
        if cashout > 0 and not sess["u_rolls"]: rows.append([(f"Cashout {usd(cashout)}", f"cashout:{gid}", "success", "🪙")])
        rows.append([("Send an emoji", f"roll:{gid}:me", "primary", emo)])
        if show_bot_first and sess.get("round_num", 1) == 1 and not sess["u_rolls"] and not sess["b_rolls"]:
            rows.append([("Bot rolls first", f"roll:{gid}:bot", "primary", "🤖")])
    else: rows.append([("Send an emoji", f"roll:{gid}:pvp", "primary", emo)])
    return kb(*rows)


# --- MATCH ENGINE ---
async def finish_match(sess: dict, payout_a: int, payout_b: int = 0, cashed_out: bool = False):
    gid = sess["gid"]
    chat_id = sess["chat_id"]
    game = sess["game"]
    stake = sess["stake"]
    mode = sess["mode"]
    target = sess["target"]
    m: Match = sess["match"]
    ua, ub = sess["ua"], sess["ub"]

    sess["status"] = "ended"
    matches.pop(gid, None)
    user_match.pop(ua, None)
    busy.discard(ua)
    if ub:
        user_match.pop(ub, None)
        busy.discard(ub)

    p1_mention = mention(ua)
    p2_mention = mention(ub) if ub else f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}</b>"

    if sess["type"] == "bot":
        await db_close_game(gid, payout_a)
        if cashed_out or payout_a > 0:
            w_line, l_line, win_amt = f"{pe('👑')} <b>Winner:</b> {p1_mention} — <b>{m.score[0]} points</b>", f"{pe('👎')} <b>Loser:</b> {p2_mention} — <b>{m.score[1]} points</b>", payout_a
        else:
            w_line, l_line, win_amt = f"{pe('👑')} <b>Winner:</b> {p2_mention} — <b>{m.score[1]} points</b>", f"{pe('👎')} <b>Loser:</b> {p1_mention} — <b>{m.score[0]} points</b>", 0
        who_tag = "bot"
    else:
        await db_close_game(gid, payout_a)
        await db_close_game(sess["gid_b"], payout_b)
        
        if payout_a == payout_b and payout_a > 0:
            w_line, l_line, win_amt = f"{pe('🤝')} <b>Match Tied!</b>", f"Both players got <b>{m.score[0]} points</b>", payout_a
        elif payout_a > 0:
            w_line, l_line, win_amt = f"{pe('👑')} <b>Winner:</b> {p1_mention} — <b>{m.score[0]} points</b>", f"{pe('👎')} <b>Loser:</b> {p2_mention} — <b>{m.score[1]} points</b>", payout_a
        else:
            w_line, l_line, win_amt = f"{pe('👑')} <b>Winner:</b> {p2_mention} — <b>{m.score[1]} points</b>", f"{pe('👎')} <b>Loser:</b> {p1_mention} — <b>{m.score[0]} points</b>", payout_b
        who_tag = "pvp"

    end_text = f"{pe('🌊')} <b>The game has ended</b>\n\n{w_line}\n{l_line}\n<b>Winnings — {usd(win_amt)}</b> {pe('🪙')}"
    end_kb = kb([("Repeat", f"opp:{game}:{stake}:{mode}:{target}:{who_tag}", "primary", "🔄"),
                 ("x2 Double", f"opp:{game}:{stake * 2}:{mode}:{target}:{who_tag}", "success", "🪙")],
                [("Change mode", f"back_mode:{game}:{stake}", "danger", "❌")])
    
    try: await bot.edit_message_text(text=end_text, chat_id=chat_id, message_id=sess.get("prompt_msg_id", 0), reply_markup=end_kb)
    except Exception: pass
    msg_owner[(chat_id, sess.get("prompt_msg_id", 0))] = ua


async def evaluate_round(sess: dict):
    m, chat_id, game, ua, ub = sess["match"], sess["chat_id"], sess["game"], sess["ua"], sess["ub"]
    p1_mention = mention(ua)
    p2_mention = mention(ub) if ub else f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}</b>"
    
    mine, theirs = sess["u_rolls"], sess["b_rolls"]
    sess["u_rolls"], sess["b_rolls"] = [], []
    w = m.play_round(mine, theirs)

    if m.winner is not None:
        sess["status"] = "ended"
        if sess["type"] == "bot": await finish_match(sess, int(sess["stake"] * 1.92) if m.winner == 0 else 0)
        else:
            pot = int(2 * sess["stake"] * 0.95)
            await finish_match(sess, pot if m.winner == 0 else 0, 0 if m.winner == 0 else pot)
        return

    sess["round_num"] = sess.get("round_num", 1) + 1
    score_header = f"{game_pe(game)} <b>Score (Round {sess['round_num'] - 1})</b>\n\n• {p1_mention}: <b>{m.score[0]} points</b>\n• {p2_mention}: <b>{m.score[1]} points</b>{f'{pe("🤝")} <b>Tie round — replay!</b>' if w is None else ''}"

    if sess["type"] == "bot" and random.choice([True, False]):
        sess["rolling"] = True
        try: await bot.edit_message_text(text=f"{score_header}\n\n{p2_mention} <b>is rolling first...</b>", chat_id=chat_id, message_id=sess.get("prompt_msg_id", 0))
        except Exception: pass
        sess["b_rolls"] = await send_emoji_roll(chat_id, game, m.rolls_needed, reply_to=sess.get("prompt_msg_id", 0))
        sess["turn_uid"] = ua
        sess["last_action"] = time.time()
        sess["rolling"] = False
        sent = await bot.send_message(chat_id, f"{p1_mention}, <b>your turn!</b> {game_pe(game)}", reply_markup=turn_kb(sess))
        sess["prompt_msg_id"] = sent.message_id
        msg_owner[(chat_id, sent.message_id)] = ua
    else:
        sess["rolling"] = False
        sess["turn_uid"] = ua
        sess["last_action"] = time.time()
        try: await bot.edit_message_text(text=f"{score_header}\n\n{p1_mention}, <b>your turn!</b> {game_pe(game)}", chat_id=chat_id, message_id=sess.get("prompt_msg_id", 0), reply_markup=turn_kb(sess))
        except Exception: pass


async def handle_roll_button(sess: dict, uid: int, order: str):
    lock: asyncio.Lock = sess["lock"]
    if lock.locked(): return
    async with lock:
        if sess.get("status") != "playing": return
        sess["rolling"] = True
        sess["last_action"] = time.time()
        chat_id, game, m = sess["chat_id"], sess["game"], sess["match"]
        needed = m.rolls_needed
        
        if sess.get("prompt_msg_id"):
            try: await bot.edit_message_reply_markup(chat_id=chat_id, message_id=sess["prompt_msg_id"], reply_markup=None)
            except Exception: pass
            sess["prompt_msg_id"] = None
            
        try:
            if sess["type"] == "bot":
                if order == "bot" and not sess["b_rolls"] and not sess["u_rolls"]:
                    sess["b_rolls"] = await send_emoji_roll(chat_id, game, needed)
                    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ua"], False, time.time()
                    sent = await bot.send_message(chat_id, f"{mention(sess['ua'])}, <b>your turn!</b> {game_pe(game)}", reply_markup=turn_kb(sess))
                    sess["prompt_msg_id"] = sent.message_id
                    msg_owner[(chat_id, sent.message_id)] = sess["ua"]
                    return
                rem_u = needed - len(sess["u_rolls"])
                if rem_u > 0: sess["u_rolls"] += await send_emoji_roll(chat_id, game, rem_u)
                if len(sess["b_rolls"]) < needed:
                    b_msg = await bot.send_message(chat_id, f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}'s turn...</b>")
                    sess["b_rolls"] += await send_emoji_roll(chat_id, game, needed - len(sess["b_rolls"]), reply_to=b_msg.message_id)
                await evaluate_round(sess)
            else:
                if uid == sess["ua"]:
                    rem = needed - len(sess["u_rolls"])
                    if rem > 0: sess["u_rolls"] += await send_emoji_roll(chat_id, game, rem)
                    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ub"], False, time.time()
                    sent = await bot.send_message(chat_id, f"{mention(sess['ub'])}, <b>your turn!</b> {game_pe(game)}", reply_markup=turn_kb(sess))
                    sess["prompt_msg_id"] = sent.message_id
                    msg_owner[(chat_id, sent.message_id)] = sess["ub"]
                elif uid == sess["ub"]:
                    rem = needed - len(sess["b_rolls"])
                    if rem > 0: sess["b_rolls"] += await send_emoji_roll(chat_id, game, rem)
                    await evaluate_round(sess)
        except Exception as e:
            sess["rolling"], sess["last_action"] = False, time.time()
            try:
                sent = await bot.send_message(chat_id, f"{pe('⚠️')} {mention(sess['turn_uid'])}, <b>tap below to continue your turn:</b>", reply_markup=turn_kb(sess))
                sess["prompt_msg_id"] = sent.message_id
            except Exception: pass


# --- ROUTERS & HANDLERS ---
@router.message(F.dice)
async def handle_user_sent_dice(m: Message):
    if m.forward_origin or m.forward_from or m.forward_date: return
    if time.time() - m.date.timestamp() > 30: return

    uid, chat_id = m.from_user.id, m.chat.id
    gid = user_match.get(uid)
    if not gid or gid not in matches: return
    
    sess = matches[gid]
    if sess["chat_id"] != chat_id or sess.get("status") != "playing": return
    if m.date.timestamp() < int(sess.get("last_action", 0)): return

    if sess.get("rolling"): 
        return await m.reply(f"⏳ {mention(uid)}, please wait for the current roll to finish before sending another dice!")
    if sess.get("turn_uid") != uid: return

    now = time.time()
    if now - user_last_dice_time.get(uid, 0) < 1.0: return
    user_last_dice_time[uid] = now

    val = m.dice.value
    emo = get_game_emo(sess["game"])
    if m.dice.emoji != emo: return

    async with sess["lock"]:
        if sess.get("status") != "playing": return
        
        sess["last_action"] = time.time()
        if sess.get("prompt_msg_id"):
            try: await bot.edit_message_reply_markup(chat_id=chat_id, message_id=sess["prompt_msg_id"], reply_markup=None)
            except Exception: pass
            sess["prompt_msg_id"] = None
            
        needed = sess["match"].rolls_needed
        rolls_list = sess["u_rolls"] if uid == sess["ua"] else sess["b_rolls"]
        if len(rolls_list) >= needed: return
        
        rolls_list.append(val)
        rem = needed - len(rolls_list)
        
        if rem > 0:
            sent = await m.reply(f"{game_pe(sess['game'])} {mention(uid)}, <b>send {rem} more {config.GAME_NAMES.get(sess['game'], sess['game'])}!</b>", reply_markup=turn_kb(sess))
            sess["prompt_msg_id"] = sent.message_id
            msg_owner[(chat_id, sent.message_id)] = uid
            return
            
        sess["rolling"] = True
        try:
            await asyncio.sleep(2.0)
            if sess["type"] == "bot":
                if len(sess["b_rolls"]) < needed:
                    b_msg = await m.reply(f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}'s turn...</b>")
                    sess["b_rolls"] += await send_emoji_roll(chat_id, sess["game"], needed - len(sess["b_rolls"]), reply_to=b_msg.message_id)
                await evaluate_round(sess)
            else:
                if uid == sess["ua"]:
                    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ub"], False, time.time()
                    sent = await m.reply(f"{mention(sess['ub'])}, <b>your turn!</b> {game_pe(sess['game'])}", reply_markup=turn_kb(sess))
                    sess["prompt_msg_id"] = sent.message_id
                    msg_owner[(chat_id, sent.message_id)] = sess["ub"]
                else:
                    await evaluate_round(sess)
        except Exception: sess["rolling"] = False


@router.message(Command("start"))
async def cmd_start(m: Message):
    track_user_info(m.from_user)
    await db_get_user(m.from_user.id)
    await m.reply(f"🎲 <b>Welcome to {config.CASINO_NAME}!</b>\nPlay games in our official group chat.")

@router.message(Command("dice", "bowl", "basket", "bask", "football", "dart", "dices"))
async def cmd_games(m: Message):
    track_user_info(m.from_user)
    uid = m.from_user.id
    chat_id = m.chat.id
    
    if m.chat.type == "private":
        return await m.reply(f"{pe('⚠️')} {mention(uid)}, <b>PvP Games can only be played in our official Group Chat!</b>\n\n<b>Join the group and start playing!</b>")
        
    args = m.text.split(maxsplit=1)[1] if len(m.text.split()) > 1 else ""
    bal = await get_bal(uid)
    stake = parse_bet(args, bal)
    min_bet = getattr(config, "MIN_BET", 10)
    cmd = m.text.split()[0][1:].lower().split('@')[0]
    
    if stake == -1: return await m.reply(f"<b>Usage:</b> <code>/{cmd} 1.50</code>  (minimum <b>{usd(min_bet)}</b>)")
    if stake < min_bet: return await m.reply(f"{pe('⚠️')} <b>Minimum bet is {usd(min_bet)}</b>")
    if bal < stake: return await m.reply(f"{pe('⚠️')} <b>Not enough funds to play</b>\n\n{pe('💼')} <b>Current coin balance: {usd(bal)}</b> {pe('🪙')}")
    
    game_map = {"dices": "dice", "dice": "dice", "bowl": "bowl", "basket": "basket", "bask": "basket", "football": "football", "dart": "dart"}
    game = game_map[cmd]
    
    sent = await m.reply(mode_screen_text(game), reply_markup=mode_screen_kb(game, stake))
    msg_owner[(chat_id, sent.message_id)] = uid


@router.callback_query()
async def handle_callback_all(c: CallbackQuery):
    track_user_info(c.from_user)
    uid = c.from_user.id
    data = c.data
    msg = c.message
    chat_id = msg.chat.id
    is_private = (msg.chat.type == "private")

    if data == "del":
        try: await msg.delete()
        except Exception: pass
        return await c.answer()

    owner = msg_owner.get((chat_id, msg.message_id))
    if owner and owner != uid:
        return await c.answer("This button is not for you!", show_alert=True)

    if data.startswith("back_mode:"):
        if is_private: return await c.answer("Play in the Group Chat!")
        _, game, stake = data.split(":")
        await msg.edit_text(mode_screen_text(game), reply_markup=mode_screen_kb(game, int(stake)))
        
    elif data.startswith("mode:"):
        if is_private: return await c.answer("Play in the Group Chat!")
        _, game, stake, mode = data.split(":")
        await msg.edit_text(target_screen_text(game), reply_markup=target_screen_kb(game, int(stake), mode))
        
    elif data.startswith("tgt:"):
        if is_private: return await c.answer("Play in the Group Chat!")
        _, game, stake, mode, target = data.split(":")
        base = f"{game}:{stake}:{mode}:{target}"
        txt = f"{game_pe(game)} <b>{config.GAME_NAMES.get(game, game).title()} · {usd(int(stake))}</b>\n<b>Player:</b> {mention(uid)}\n<b>Mode: {MODES[mode]['label']} ({target} points)</b>\n\n<b>Choose your opponent:</b>"
        markup = kb([("Play vs Bot", f"opp:{base}:bot", "success", "🤖"), ("Play PvP", f"opp:{base}:pvp", "primary", "👤")], [("Back", f"mode:{game}:{stake}:{mode}", "danger", "❌")])
        await msg.edit_text(txt, reply_markup=markup)
        
    elif data.startswith("opp:"):
        if is_private: return await c.answer("Games can only be played in Group Chat!")
        _, game, stake, mode, target, who = data.split(":")
        stake, target = int(stake), int(target)
        if uid in busy: return await c.answer("Finish your current game first!")
        
        bal = await get_bal(uid)
        if bal < stake:
            return await msg.edit_text(f"{pe('⚠️')} <b>Not enough funds to play</b>\n\n{pe('💼')} <b>Current coin balance: {usd(bal)}</b> {pe('🪙')}")
        try: gid = await db_open_game(uid, game, mode, stake, opponent=0 if who == "bot" else -1)
        except ValueError as e: return await c.answer(str(e))

        busy.add(uid)
        user_match[uid] = gid

        if who == "bot":
            m_obj = Match(game, mode, target, stake, (uid, 0))
            sess = {"gid": gid, "type": "bot", "status": "playing", "chat_id": chat_id, "game": game, "mode": mode, "target": target, "stake": stake, "ua": uid, "ub": 0, "match": m_obj, "turn_uid": uid, "u_rolls": [], "b_rolls": [], "rolling": False, "round_num": 1, "last_action": time.time(), "lock": asyncio.Lock(), "prompt_msg_id": msg.message_id}
            matches[gid] = sess
            txt = f"{pe('🌊')} <b>The game has started</b>\n\n<b>Player 1:</b> {mention(uid)}\n<b>Player 2:</b> {pe('🤖')} <b>{html.escape(config.CASINO_NAME)}</b>\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[mode]['label']} — {target} points</b>\n\n<b>Roll the {config.GAME_NAMES.get(game, game)}</b> {game_pe(game)}"
            await msg.edit_text(txt, reply_markup=turn_kb(sess, show_bot_first=True))
        else:
            sess = {"gid": gid, "type": "pvp", "status": "waiting", "chat_id": chat_id, "game": game, "mode": mode, "target": target, "stake": stake, "ua": uid, "ub": 0, "u_rolls": [], "b_rolls": [], "rolling": False, "round_num": 1, "created_at": time.time(), "last_action": time.time(), "lock": asyncio.Lock(), "prompt_msg_id": msg.message_id}
            matches[gid] = sess
            txt = f"{pe('🎮')} <b>PvP Match Created</b>\n\n<b>Player 1:</b> {mention(uid)}\n<b>Game:</b> {game_pe(game)} <b>{config.GAME_NAMES.get(game, game).title()}</b>\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[mode]['label']} — {target} points</b>\n\n<i>Click «Accept game» to start PvP!</i>"
            markup = kb([("Accept game", f"pvp_join:{gid}", "success", "✅")], [("Cancel game", f"cancel_pvp:{gid}", "danger", "❌")])
            await msg.edit_text(txt, reply_markup=markup)

    elif data.startswith("pvp_join:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess.get("status") != "waiting": return await c.answer("This match is no longer available!")
        if uid == sess["ua"]: return await c.answer("You cannot accept your own match!")
        if uid in busy: return await c.answer("Finish your current game first!")

        bal = await get_bal(uid)
        stake = sess["stake"]
        if bal < stake: return await c.answer(f"Not enough balance! Need {usd(stake)}.")
        try: gid_b = await db_open_game(uid, sess["game"], sess["mode"], stake, opponent=-1)
        except ValueError as e: return await c.answer(str(e))

        sess["ub"] = uid
        sess["gid_b"] = gid_b
        sess["status"] = "playing"
        sess["match"] = Match(sess["game"], sess["mode"], sess["target"], stake, (sess["ua"], uid))
        sess["turn_uid"] = sess["ua"]
        sess["last_action"] = time.time()
        busy.add(uid)
        user_match[uid] = gid

        start_txt = f"{pe('🌊')} <b>The game has started</b>\n\n<b>Player 1:</b> {mention(sess['ua'])}\n<b>Player 2:</b> {mention(uid)}\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[sess['mode']]['label']} — {sess['target']} points</b>\n\n{mention(sess['ua'])}, <b>your turn!</b>"
        await msg.edit_text(start_txt, reply_markup=turn_kb(sess, show_bot_first=False))
        return await c.answer("Match started!")

    elif data.startswith("cancel_pvp:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess.get("status") != "waiting" or sess["ua"] != uid: return await c.answer("Cannot cancel after match has started!")
        await db_void_game(gid)
        matches.pop(gid, None); user_match.pop(uid, None); busy.discard(uid)
        await msg.edit_text(f"{pe('⭕️')} <b>PvP match cancelled by</b> {mention(uid)} — <b>stake refunded.</b>")
        return await c.answer()

    elif data.startswith("roll:"):
        _, gid_str, order = data.split(":")
        sess = matches.get(gid_str)
        if not sess or sess.get("status") != "playing": return await c.answer("This match is no longer active.")
        if sess.get("turn_uid") != uid: return await c.answer("This button is not for you!")
        needed = sess["match"].rolls_needed
        my_rolls = sess["u_rolls"] if uid == sess["ua"] else sess["b_rolls"]
        if sess.get("rolling") or sess["lock"].locked() or len(my_rolls) >= needed: return await c.answer("You already rolled!")
        await c.answer()
        asyncio.create_task(handle_roll_button(sess, uid, order))
        return

    elif data.startswith("cashout:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess["ua"] != uid or sess.get("rolling") or sess["lock"].locked(): return await c.answer("Cannot cashout right now.")
        cashout_amt = calc_cashout(sess["stake"], sess["match"].score[0], sess["match"].score[1], sess["target"])
        if cashout_amt <= 0: return await c.answer("Cashout not available.")
        try: await msg.edit_reply_markup(reply_markup=None)
        except Exception: pass
        await finish_match(sess, cashout_amt, cashed_out=True)

    await c.answer()

# --- SWEEPER TASK ---
async def sweeper():
    while True:
        await asyncio.sleep(10)
        now = time.time()
        try:
            for gid, sess in list(matches.items()):
                if sess.get("status") == "waiting" and now - sess.get("created_at", now) >= 120:
                    matches.pop(gid, None)
                    ua = sess["ua"]
                    user_match.pop(ua, None)
                    busy.discard(ua)
                    await db_void_game(gid)
                    try: await bot.edit_message_text(text=f"{pe('⌛')} <b>No opponent joined</b> {mention(ua)}'s <b>PvP match — stake refunded.</b>", chat_id=sess["chat_id"], message_id=sess["prompt_msg_id"])
                    except Exception: pass
                elif sess.get("status") == "playing" and sess.get("rolling") and now - sess.get("last_action", now) >= 15:
                    sess["rolling"], sess["last_action"] = False, now
        except Exception: pass

# --- MAIN EXECUTION ---
async def main():
    if not HAS_MONGO or not getattr(config, "MONGO_URI", None):
        print("❌ MONGODB URI MISSING! App requires MongoDB to run.")
        return
        
    global mongo_client, db
    # USING MOTOR (AsyncIOMotorClient) instead of PyMongo - This is the secret to 0 lag.
    mongo_client = AsyncIOMotorClient(config.MONGO_URI, serverSelectionTimeoutMS=6000)
    db = mongo_client["raika_casino_db"]
    print("🍃 Async MongoDB Connected! Initialized PRO Aiogram v3 Casino Bot.")

    # Register Routers
    dp.include_router(router)
    
    # Start Background Tasks
    asyncio.create_task(sweeper())

    # Start Polling automatically (no manual aiohttp loops)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
