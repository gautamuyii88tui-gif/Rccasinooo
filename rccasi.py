import asyncio
import html
import random
import time
import aiohttp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery
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

# --- CONFIG & BOT INITIALIZATION ---
ACTIVE_BOT_TOKEN = config.BOT_TOKEN.strip()
ACTIVE_HELPER_TOKEN = getattr(config, "HELPER_BOT_TOKEN", "").strip()

bot = Bot(token=ACTIVE_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
helper_bot = Bot(token=ACTIVE_HELPER_TOKEN) if ACTIVE_HELPER_TOKEN else None

dp = Dispatcher()
router = Router()

mongo_client = None
db = None
HTTP_SESSION: aiohttp.ClientSession | None = None

matches: dict[str, dict] = {}
user_match: dict[int, str] = {}
msg_owner: dict[tuple[int, int], int] = {}
user_names: dict[int, str] = {}
busy: set[int] = set()
user_last_dice_time: dict[int, float] = {}

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


# --- ASYNC DATABASE LOGIC ---
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
    if u["balance"] < stake: raise ValueError(f"Not enough funds! Need {usd(stake)}.")
    
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

async def parse_bet(uid: int, arg_str: str) -> tuple[int, int]:
    s = arg_str.strip().lower()
    if s in ["all", "half"]:
        bal = await get_bal(uid)
        return (bal if s == "all" else max(config.MIN_BET, bal // 2)), bal
    else:
        try: return int(round(float(s) * 100)), None
        except ValueError: return -1, None


# --- INSTANT RAW UI API (0-Delay Connection Pooling) ---
async def get_session() -> aiohttp.ClientSession:
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        # Aggressive keepalive and DNS cache to eliminate the 0.5s button delay completely
        connector = aiohttp.TCPConnector(limit=1000, keepalive_timeout=600, ttl_dns_cache=600, use_dns_cache=True)
        timeout = aiohttp.ClientTimeout(total=5, connect=2)
        HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return HTTP_SESSION

async def tg(method: str, retries: int = 1, **params):
    clean = {k: v for k, v in params.items() if v is not None}
    base = f"https://api.telegram.org/bot{ACTIVE_BOT_TOKEN}/"
    for attempt in range(retries + 1):
        try:
            s = await get_session()
            async with s.post(base + method, json=clean) as r:
                res = await r.json()
            if res.get("ok"): return res["result"]
        except Exception:
            pass

async def send_msg(chat_id: int, text: str, markup=None, parse_mode="HTML", reply_to: int = None, owner_id: int = None):
    res = await tg("sendMessage", chat_id=chat_id, text=text, reply_markup=markup, parse_mode=parse_mode, reply_to_message_id=reply_to)
    if owner_id and res and "message_id" in res: msg_owner[(chat_id, res["message_id"])] = owner_id
    return res

async def edit_msg(chat_id: int, message_id: int, text: str, markup=None, parse_mode="HTML", owner_id: int = None):
    res = await tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode=parse_mode)
    if owner_id: msg_owner[(chat_id, message_id)] = owner_id
    return res

async def clear_buttons(chat_id: int, message_id: int):
    if not message_id: return
    await tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=None)

def btn(text: str, data: str = None, style: str = None, icon: str = None) -> dict:
    b = {"text": str(text), "callback_data": str(data) if data else "noop"}
    if style and style in ["primary", "success", "danger"]: b["style"] = style
    if icon and icon in PREMIUM_EMOJIS: b["icon_custom_emoji_id"] = PREMIUM_EMOJIS[icon]
    return b

def kb(*rows) -> dict:
    board = []
    for row in rows:
        r = []
        for item in row:
            if len(item) >= 4: r.append(btn(item[0], item[1], style=item[2], icon=item[3]))
            elif len(item) == 3: r.append(btn(item[0], item[1], style=item[2]))
            elif len(item) == 2: r.append(btn(item[0], item[1]))
        if r: board.append(r)
    return {"inline_keyboard": board}


# --- HELPER ROLLER ---
async def send_emoji_roll(chat_id: int, game: str, n: int, reply_to: int = None) -> list[int]:
    emo = get_game_emo(game)
    active_bot = helper_bot if helper_bot else bot
    
    tasks = [active_bot.send_dice(chat_id=chat_id, emoji=emo, reply_to_message_id=reply_to) for _ in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    values = []
    for res in results:
        if isinstance(res, Message) and res.dice: values.append(res.dice.value)
            
    await asyncio.sleep(1.2) # Visually optimal animation delay
    return values


# --- COLORED KEYBOARDS ---
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

def turn_kb(sess: dict, show_bot_first: bool = False):
    gid = sess["gid"]
    emo = get_game_emo(sess["game"])
    m: Match = sess["match"]
    rows = []
    if sess["type"] == "bot":
        cashout = calc_cashout(sess["stake"], m.score[0], m.score[1], sess["target"])
        
        # CASHOUT LOGIC: Button only appears if at least 1 round is finished (round_num > 1)
        if cashout > 0 and sess.get("round_num", 1) > 1 and not sess["u_rolls"]: 
            rows.append([(f"Cashout {usd(cashout)}", f"cashout:{gid}", "success", "🪙")])
            
        rows.append([("Send an emoji", f"roll:{gid}:me", "primary", emo)])
        if show_bot_first and sess.get("round_num", 1) == 1 and not sess["u_rolls"] and not sess["b_rolls"]:
            rows.append([("Bot rolls first", f"roll:{gid}:bot", "primary", "🤖")])
    else: rows.append([("Send an emoji", f"roll:{gid}:pvp", "primary", emo)])
    return kb(*rows)


# --- SMART CASHOUT CALCULATOR (Even supports loss recovery) ---
def calc_cashout(stake: int, p_score: int, b_score: int, target: int) -> int:
    # Do not offer cashout before any round completes
    if p_score == 0 and b_score == 0: 
        return 0 
        
    full_win = stake * 1.92
    diff = p_score - b_score
    
    if diff == 0:
        return int(stake * 0.85) # Tie round (1-1) -> Return 85% of stake
    elif diff > 0:
        progress = diff / target
        return min(int(stake + (full_win - stake) * (0.45 + 0.35 * progress)), int(full_win * 0.92))
    else:
        return int(stake * (0.45 ** abs(diff))) # Losing round -> Return partial fraction


# --- MATCH ENGINE ---
async def finish_match(sess: dict, payout_a: int, payout_b: int = 0, cashed_out: bool = False):
    gid, chat_id, game = sess["gid"], sess["chat_id"], sess["game"]
    stake, mode, target = sess["stake"], sess["mode"], sess["target"]
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
        if cashed_out or payout_a > 0:
            w_line, l_line, win_amt = f"{pe('👑')} <b>Winner:</b> {p1_mention} — <b>{m.score[0]} points</b>", f"{pe('👎')} <b>Loser:</b> {p2_mention} — <b>{m.score[1]} points</b>", payout_a
        else:
            w_line, l_line, win_amt = f"{pe('👑')} <b>Winner:</b> {p2_mention} — <b>{m.score[1]} points</b>", f"{pe('👎')} <b>Loser:</b> {p1_mention} — <b>{m.score[0]} points</b>", 0
        who_tag = "bot"
    else:
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
    
    await edit_msg(chat_id, sess.get("prompt_msg_id", 0), end_text, markup=end_kb, owner_id=ua)
    
    asyncio.create_task(db_close_game(gid, payout_a))
    if sess["type"] != "bot": asyncio.create_task(db_close_game(sess["gid_b"], payout_b))


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
    tie_str = f"\n{pe('🤝')} <b>Tie round — replay!</b>" if w is None else ""
    score_header = f"{game_pe(game)} <b>Score (Round {sess['round_num'] - 1})</b>\n\n• {p1_mention}: <b>{m.score[0]} points</b>\n• {p2_mention}: <b>{m.score[1]} points</b>{tie_str}"

    if sess["type"] == "bot" and random.choice([True, False]):
        sess["rolling"] = True
        await edit_msg(chat_id, sess.get("prompt_msg_id", 0), f"{score_header}\n\n{p2_mention} <b>is rolling first...</b>")
        sess["b_rolls"] = await send_emoji_roll(chat_id, game, m.rolls_needed, reply_to=sess.get("prompt_msg_id", 0))
        sess["turn_uid"], sess["rolling"], sess["last_action"] = ua, False, time.time()
        sent = await send_msg(chat_id, f"{p1_mention}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=ua)
        if sent: sess["prompt_msg_id"] = sent["message_id"]
    else:
        sess["turn_uid"], sess["rolling"], sess["last_action"] = ua, False, time.time()
        await edit_msg(chat_id, sess.get("prompt_msg_id", 0), f"{score_header}\n\n{p1_mention}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=ua)


async def execute_bot_turn(sess: dict, chat_id: int, needed: int, reply_msg_id: int):
    if len(sess["b_rolls"]) < needed:
        b_msg = await send_msg(chat_id, f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}'s turn...</b>", reply_to=reply_msg_id)
        msg_id = b_msg["message_id"] if b_msg else reply_msg_id
        sess["b_rolls"] += await send_emoji_roll(chat_id, sess["game"], needed - len(sess["b_rolls"]), reply_to=msg_id)
    await evaluate_round(sess)

async def execute_pvp_transition(sess: dict, chat_id: int, game: str):
    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ub"], False, time.time()
    sent = await send_msg(chat_id, f"{mention(sess['ub'])}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=sess["ub"])
    if sent: sess["prompt_msg_id"] = sent["message_id"]

async def handle_roll_button(sess: dict, uid: int, order: str):
    lock: asyncio.Lock = sess["lock"]
    if lock.locked(): return
    
    async with lock:
        if sess.get("status") != "playing": return
        sess["rolling"], sess["last_action"] = True, time.time()
        chat_id, game, needed = sess["chat_id"], sess["game"], sess["match"].rolls_needed
    
    asyncio.create_task(clear_buttons(chat_id, sess.get("prompt_msg_id")))
    sess["prompt_msg_id"] = None
        
    try:
        if sess["type"] == "bot":
            if order == "bot" and not sess["b_rolls"] and not sess["u_rolls"]:
                sess["b_rolls"] = await send_emoji_roll(chat_id, game, needed)
                sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ua"], False, time.time()
                sent = await send_msg(chat_id, f"{mention(sess['ua'])}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=sess["ua"])
                if sent: sess["prompt_msg_id"] = sent["message_id"]
                return
                
            rem_u = needed - len(sess["u_rolls"])
            if rem_u > 0: sess["u_rolls"] += await send_emoji_roll(chat_id, game, rem_u)
            await execute_bot_turn(sess, chat_id, needed, None)
        else:
            if uid == sess["ua"]:
                rem = needed - len(sess["u_rolls"])
                if rem > 0: sess["u_rolls"] += await send_emoji_roll(chat_id, game, rem)
                await execute_pvp_transition(sess, chat_id, game)
            elif uid == sess["ub"]:
                rem = needed - len(sess["b_rolls"])
                if rem > 0: sess["b_rolls"] += await send_emoji_roll(chat_id, game, rem)
                await evaluate_round(sess)
    except Exception:
        sess["rolling"], sess["last_action"] = False, time.time()
        sent = await send_msg(chat_id, f"{pe('⚠️')} {mention(sess['turn_uid'])}, <b>tap below to continue:</b>", markup=turn_kb(sess), owner_id=sess["turn_uid"])
        if sent: sess["prompt_msg_id"] = sent["message_id"]


# --- REAL-TIME DICE RECEIVER & ENGLISH ANTI-SPAM ---
@router.message(F.dice)
async def handle_user_sent_dice(m: Message):
    if m.forward_origin or m.forward_from or m.forward_date: return
    
    uid, chat_id = m.from_user.id, m.chat.id
    gid = user_match.get(uid)
    if not gid or gid not in matches: return
    
    sess = matches[gid]
    if sess["chat_id"] != chat_id or sess.get("status") != "playing" or sess.get("turn_uid") != uid: return
    
    # SPAM PROTECTION: Strictly 1 English warning, blocks subsequent rapid dice throws instantly
    now = time.time()
    if now - user_last_dice_time.get(uid, 0) < 1.0:
        if not sess.get("spam_warned"):
            sess["spam_warned"] = True
            asyncio.create_task(send_msg(chat_id, f"⚠️ {mention(uid)}, please do not spam! Wait for the current roll to finish.", reply_to=m.message_id))
        return
    user_last_dice_time[uid] = now
    sess["spam_warned"] = False

    if sess.get("rolling"): 
        return await send_msg(chat_id, f"⏳ {mention(uid)}, wait for the current roll to finish!", reply_to=m.message_id)

    val, emo = m.dice.value, m.dice.emoji
    if emo != get_game_emo(sess["game"]): return

    async with sess["lock"]:
        if sess.get("status") != "playing": return
        needed = sess["match"].rolls_needed
        rolls_list = sess["u_rolls"] if uid == sess["ua"] else sess["b_rolls"]
        
        if len(rolls_list) >= needed: return
        rolls_list.append(val)
        sess["last_action"] = time.time()
        
        rem = needed - len(rolls_list)
        if rem == 0: sess["rolling"] = True

    if rem > 0:
        asyncio.create_task(clear_buttons(chat_id, sess.get("prompt_msg_id")))
        sent = await send_msg(chat_id, f"{game_pe(sess['game'])} {mention(uid)}, <b>send {rem} more {getattr(config, 'GAME_NAMES', {}).get(sess['game'], sess['game'])}!</b>", markup=turn_kb(sess), owner_id=uid, reply_to=m.message_id)
        if sent: sess["prompt_msg_id"] = sent["message_id"]
        return

    asyncio.create_task(clear_buttons(chat_id, sess.get("prompt_msg_id")))
    sess["prompt_msg_id"] = None
    
    if sess["type"] == "bot":
        await execute_bot_turn(sess, chat_id, needed, m.message_id)
    else:
        if uid == sess["ua"]:
            await execute_pvp_transition(sess, chat_id, sess["game"])
        else:
            await asyncio.sleep(1.2)
            await evaluate_round(sess)


@router.message(Command("start"))
async def cmd_start(m: Message):
    track_user_info(m.from_user)
    await db_get_user(m.from_user.id)
    await send_msg(m.chat.id, f"🎲 <b>Welcome to {config.CASINO_NAME}!</b>\nPlay games in our official group chat.", reply_to=m.message_id)

@router.message(Command("dice", "bowl", "basket", "bask", "football", "dart", "dices"))
async def cmd_games(m: Message):
    track_user_info(m.from_user)
    uid, chat_id = m.from_user.id, m.chat.id
    
    if m.chat.type == "private":
        return await send_msg(chat_id, f"{pe('⚠️')} {mention(uid)}, <b>PvP Games can only be played in our official Group Chat!</b>")
        
    args = m.text.split(maxsplit=1)[1] if len(m.text.split()) > 1 else ""
    stake, bal = await parse_bet(uid, args)
    min_bet = getattr(config, "MIN_BET", 10)
    cmd = m.text.split()[0][1:].lower().split('@')[0]
    
    if stake == -1: return await send_msg(chat_id, f"<b>Usage:</b> <code>/{cmd} 1.50</code>  (minimum <b>{usd(min_bet)}</b>)")
    if stake < min_bet: return await send_msg(chat_id, f"{pe('⚠️')} <b>Minimum bet is {usd(min_bet)}</b>")
    
    if bal is None: bal = await get_bal(uid)
    if bal < stake: return await send_msg(chat_id, f"{pe('⚠️')} <b>Not enough funds to play</b>\n\n{pe('💼')} <b>Current balance: {usd(bal)}</b> {pe('🪙')}")
    
    game = {"dices": "dice", "dice": "dice", "bowl": "bowl", "basket": "basket", "bask": "basket", "football": "football", "dart": "dart"}[cmd]
    await send_msg(chat_id, mode_screen_text(game), markup=mode_screen_kb(game, stake), reply_to=m.message_id, owner_id=uid)


# --- 0-LATENCY CALLBACK ROUTER ---
@router.callback_query()
async def handle_callback_all(c: CallbackQuery):
    await c.answer() # SPINNER DIES INSTANTLY

    track_user_info(c.from_user)
    uid = c.from_user.id
    data = c.data
    msg = c.message
    chat_id = msg.chat.id
    is_private = (msg.chat.type == "private")

    if data == "del":
        try: await msg.delete()
        except Exception: pass
        return

    owner = msg_owner.get((chat_id, msg.message_id))
    if owner and owner != uid: return

    if data.startswith("back_mode:"):
        if is_private: return
        _, game, stake = data.split(":")
        asyncio.create_task(edit_msg(chat_id, msg.message_id, mode_screen_text(game), markup=mode_screen_kb(game, int(stake)), owner_id=uid))
        
    elif data.startswith("mode:"):
        if is_private: return
        _, game, stake, mode = data.split(":")
        asyncio.create_task(edit_msg(chat_id, msg.message_id, target_screen_text(game), markup=target_screen_kb(game, int(stake), mode), owner_id=uid))
        
    elif data.startswith("tgt:"):
        if is_private: return
        _, game, stake, mode, target = data.split(":")
        base = f"{game}:{stake}:{mode}:{target}"
        
        # Safely fetch game title
        game_title = getattr(config, "GAME_NAMES", {}).get(game, game.title())
        
        txt = f"{game_pe(game)} <b>{game_title} · {usd(int(stake))}</b>\n<b>Player:</b> {mention(uid)}\n<b>Mode: {MODES[mode]['label']} ({target} points)</b>\n\n<b>Choose your opponent:</b>"
        markup = kb([("Play vs Bot", f"opp:{base}:bot", "success", "🤖"), ("Play PvP", f"opp:{base}:pvp", "primary", "👤")], [("Back", f"mode:{game}:{stake}:{mode}", "danger", "❌")])
        asyncio.create_task(edit_msg(chat_id, msg.message_id, txt, markup=markup, owner_id=uid))
        
    elif data.startswith("opp:"):
        if is_private: return
        _, game, stake, mode, target, who = data.split(":")
        stake, target = int(stake), int(target)
        if uid in busy: return
        
        try:
            gid = await db_open_game(uid, game, mode, stake, opponent=0 if who == "bot" else -1)
        except ValueError as e:
            return await edit_msg(chat_id, msg.message_id, f"{pe('⚠️')} {str(e)}", owner_id=uid)

        busy.add(uid)
        user_match[uid] = gid
        game_title = getattr(config, "GAME_NAMES", {}).get(game, game.title())

        if who == "bot":
            m_obj = Match(game, mode, target, stake, (uid, 0))
            sess = {"gid": gid, "type": "bot", "status": "playing", "chat_id": chat_id, "game": game, "mode": mode, "target": target, "stake": stake, "ua": uid, "ub": 0, "match": m_obj, "turn_uid": uid, "u_rolls": [], "b_rolls": [], "rolling": False, "round_num": 1, "last_action": time.time(), "lock": asyncio.Lock(), "prompt_msg_id": msg.message_id}
            matches[gid] = sess
            txt = f"{pe('🌊')} <b>The game has started</b>\n\n<b>Player 1:</b> {mention(uid)}\n<b>Player 2:</b> {pe('🤖')} <b>{html.escape(config.CASINO_NAME)}</b>\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[mode]['label']} — {target} points</b>\n\n<b>Roll the {game_title}</b> {game_pe(game)}"
            asyncio.create_task(edit_msg(chat_id, msg.message_id, txt, markup=turn_kb(sess, show_bot_first=True), owner_id=uid))
        else:
            sess = {"gid": gid, "type": "pvp", "status": "waiting", "chat_id": chat_id, "game": game, "mode": mode, "target": target, "stake": stake, "ua": uid, "ub": 0, "u_rolls": [], "b_rolls": [], "rolling": False, "round_num": 1, "created_at": time.time(), "last_action": time.time(), "lock": asyncio.Lock(), "prompt_msg_id": msg.message_id}
            matches[gid] = sess
            txt = f"{pe('🎮')} <b>PvP Match Created</b>\n\n<b>Player 1:</b> {mention(uid)}\n<b>Game:</b> {game_pe(game)} <b>{game_title}</b>\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[mode]['label']} — {target} points</b>\n\n<i>Click «Accept game» to start PvP!</i>"
            markup = kb([("Accept game", f"pvp_join:{gid}", "success", "✅")], [("Cancel game", f"cancel_pvp:{gid}", "danger", "❌")])
            asyncio.create_task(edit_msg(chat_id, msg.message_id, txt, markup=markup, owner_id=uid))

    elif data.startswith("pvp_join:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess.get("status") != "waiting" or uid == sess["ua"] or uid in busy: return

        try:
            gid_b = await db_open_game(uid, sess["game"], sess["mode"], sess["stake"], opponent=-1)
        except ValueError: return

        sess["ub"] = uid
        sess["gid_b"] = gid_b
        sess["status"] = "playing"
        sess["match"] = Match(sess["game"], sess["mode"], sess["target"], sess["stake"], (sess["ua"], uid))
        sess["turn_uid"] = sess["ua"]
        sess["last_action"] = time.time()
        busy.add(uid)
        user_match[uid] = gid

        start_txt = f"{pe('🌊')} <b>The game has started</b>\n\n<b>Player 1:</b> {mention(sess['ua'])}\n<b>Player 2:</b> {mention(uid)}\n<b>Bet: {usd(sess['stake'])}</b> {pe('🪙')}\n<b>Mode: {MODES[sess['mode']]['label']} — {sess['target']} points</b>\n\n{mention(sess['ua'])}, <b>your turn!</b>"
        asyncio.create_task(edit_msg(chat_id, msg.message_id, start_txt, markup=turn_kb(sess, show_bot_first=False), owner_id=sess["ua"]))

    elif data.startswith("cancel_pvp:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess.get("status") != "waiting" or sess["ua"] != uid: return
        
        matches.pop(gid, None); user_match.pop(uid, None); busy.discard(uid)
        asyncio.create_task(db_void_game(gid))
        asyncio.create_task(edit_msg(chat_id, msg.message_id, f"{pe('⭕️')} <b>PvP match cancelled by</b> {mention(uid)} — <b>stake refunded.</b>", owner_id=uid))

    elif data.startswith("roll:"):
        _, gid_str, order = data.split(":")
        sess = matches.get(gid_str)
        if not sess or sess.get("status") != "playing" or sess.get("turn_uid") != uid: return
        
        needed = sess["match"].rolls_needed
        my_rolls = sess["u_rolls"] if uid == sess["ua"] else sess["b_rolls"]
        if sess.get("rolling") or sess["lock"].locked() or len(my_rolls) >= needed: return
        
        asyncio.create_task(handle_roll_button(sess, uid, order))

    elif data.startswith("cashout:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess["ua"] != uid or sess.get("rolling") or sess["lock"].locked(): return
        cashout_amt = calc_cashout(sess["stake"], sess["match"].score[0], sess["match"].score[1], sess["target"])
        if cashout_amt <= 0: return
        asyncio.create_task(clear_buttons(chat_id, msg.message_id))
        await finish_match(sess, cashout_amt, cashed_out=True)


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
                    asyncio.create_task(db_void_game(gid))
                    asyncio.create_task(edit_msg(sess["chat_id"], sess["prompt_msg_id"], f"{pe('⌛')} <b>No opponent joined</b> {mention(ua)}'s <b>PvP match — stake refunded.</b>", owner_id=ua))
                elif sess.get("status") == "playing" and sess.get("rolling") and now - sess.get("last_action", now) >= 15:
                    sess["rolling"], sess["last_action"] = False, now
        except Exception: pass


# --- MAIN ENTRY ---
async def main():
    try:
        if not HAS_MONGO or not getattr(config, "MONGO_URI", None):
            print("❌ MONGODB URI MISSING! App requires MongoDB to run.")
            return
            
        global mongo_client, db
        mongo_client = AsyncIOMotorClient(config.MONGO_URI, serverSelectionTimeoutMS=6000)
        db = mongo_client["raika_casino_db"]
        print("🍃 Async MongoDB Connected! Initialized Ultimate Speed Aiogram v3 Bot.")

        dp.include_router(router)
        asyncio.create_task(sweeper())

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if HTTP_SESSION and not HTTP_SESSION.closed: await HTTP_SESSION.close()

if __name__ == "__main__":
    asyncio.run(main())
