import asyncio
import html
import random
import time
from datetime import datetime
import aiohttp

import config
from pvp import MODES, Match

try:
    import dns.resolver
    dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
    dns.resolver.default_resolver.nameservers = ["8.8.8.8", "8.8.4.4"]
except Exception:
    pass
try:
    from pymongo import MongoClient
    HAS_MONGO = True
except ImportError:
    HAS_MONGO = False

ACTIVE_BOT_TOKEN = config.BOT_TOKEN.strip()
ACTIVE_HELPER_TOKEN = getattr(config, "HELPER_BOT_TOKEN", "").strip()

HTTP_SESSION: aiohttp.ClientSession | None = None
mongo_db = None
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

def track_user_info(u: dict):
    uid = u.get("id")
    if not uid: return
    fname = u.get("first_name") or u.get("username") or "Player"
    user_names[uid] = fname

def mention(uid: int, name: str = None) -> str:
    label = name or user_names.get(uid) or "Player"
    return f'<a href="tg://user?id={uid}">{html.escape(str(label))}</a>'

# ---------------------------------------------------------------- MONGODB & DB LOGIC
def db_get_user(uid: int) -> dict:
    u = mongo_db.users.find_one({"user_id": uid})
    if not u:
        u = {"user_id": uid, "balance": 0, "wagered": 0, "won": 0}
        mongo_db.users.insert_one(u)
    return u

def get_bal(uid: int) -> int:
    doc = db_get_user(uid)
    return doc.get("balance", 0)

def db_open_game(uid: int, game: str, mode: str, stake: int, opponent: int = 0) -> str:
    if stake < config.MIN_BET: raise ValueError(f"Minimum bet is {usd(config.MIN_BET)}")
    u = db_get_user(uid)
    if u["balance"] < stake: raise ValueError("Insufficient balance")
    
    mongo_db.users.update_one({"user_id": uid}, {"$inc": {"balance": -stake}})
    res = mongo_db.games.insert_one({
        "user_id": uid, "game": game, "mode": mode, "stake": stake, 
        "payout": 0, "opponent": opponent, "status": "open", "ts": time.time()
    })
    return str(res.inserted_id)

def db_close_game(gid: str, payout: int, void: bool = False):
    from bson.objectid import ObjectId
    g = mongo_db.games.find_one({"_id": ObjectId(gid), "status": "open"})
    if not g: return
    uid, stake = g["user_id"], g["stake"]
    status = "void" if void else "done"
    
    mongo_db.games.update_one({"_id": ObjectId(gid)}, {"$set": {"status": status, "payout": payout}})
    mongo_db.users.update_one({"user_id": uid}, {"$inc": {"balance": payout}})
    
    if not void:
        won_amt = max(0, payout - stake)
        mongo_db.users.update_one({"user_id": uid}, {"$inc": {"wagered": stake, "won": won_amt}})

def db_void_game(gid: str):
    from bson.objectid import ObjectId
    g = mongo_db.games.find_one({"_id": ObjectId(gid)})
    if g and g["status"] == "open":
        db_close_game(gid, g["stake"], void=True)

def parse_bet(arg_str: str, balance: int) -> int:
    s = arg_str.strip().lower()
    if s == "all": return balance
    if s == "half": return max(config.MIN_BET, balance // 2)
    try: return int(round(float(s) * 100))
    except ValueError: return -1

# ---------------------------------------------------------------- TELEGRAM API HELPERS
async def get_session() -> aiohttp.ClientSession:
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        connector = aiohttp.TCPConnector(limit=150, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        HTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return HTTP_SESSION

async def tg(method: str, use_helper: bool = False, retries: int = 2, **params):
    clean = {k: v for k, v in params.items() if v is not None}
    tok = ACTIVE_HELPER_TOKEN if (use_helper and ACTIVE_HELPER_TOKEN) else ACTIVE_BOT_TOKEN
    base = f"https://api.telegram.org/bot{tok}/"
    req_timeout = 30 if method == "getUpdates" else 12
    for attempt in range(retries + 1):
        try:
            s = await get_session()
            async with s.post(base + method, json=clean, timeout=req_timeout) as r:
                res = await r.json()
            if res.get("ok"): return res["result"]
            if attempt < retries:
                await asyncio.sleep(1.0)
                continue
            raise RuntimeError(res.get("description", str(res)))
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if attempt < retries:
                await asyncio.sleep(0.5)
                continue

async def send_msg(chat_id: int, text: str, markup=None, parse_mode="HTML", reply_to: int = None, owner_id: int = None):
    try:
        res = await tg("sendMessage", chat_id=chat_id, text=text, reply_markup=markup, parse_mode=parse_mode, reply_to_message_id=reply_to)
    except Exception:
        res = await tg("sendMessage", chat_id=chat_id, text=text, reply_markup=markup, parse_mode=parse_mode)
    if owner_id and res and "message_id" in res:
        msg_owner[(chat_id, res["message_id"])] = owner_id
    return res

async def edit_msg(chat_id: int, message_id: int, text: str, markup=None, parse_mode="HTML", owner_id: int = None):
    try:
        res = await tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode=parse_mode)
        if owner_id: msg_owner[(chat_id, message_id)] = owner_id
        return res
    except Exception:
        return await send_msg(chat_id, text, markup=markup, parse_mode=parse_mode, owner_id=owner_id)

async def clear_buttons(chat_id: int, message_id: int):
    if not message_id: return
    try: await tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=None, retries=1)
    except Exception: pass

async def del_msg(chat_id: int, message_id: int):
    try: await tg("deleteMessage", chat_id=chat_id, message_id=message_id, retries=1)
    except Exception: pass

async def answer_cb(cb_id: str, text: str = None, show_alert: bool = False):
    try: await tg("answerCallbackQuery", callback_query_id=cb_id, text=text, show_alert=show_alert, retries=1)
    except Exception: pass

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

async def send_emoji_roll(chat_id: int, game: str, n: int, reply_to: int = None) -> list[int]:
    values = []
    emo = get_game_emo(game)
    for i in range(n):
        res = None
        if ACTIVE_HELPER_TOKEN:
            try: res = await tg("sendDice", use_helper=True, retries=1, chat_id=chat_id, emoji=emo, reply_to_message_id=reply_to)
            except Exception: res = None
        if not res:
            res = await tg("sendDice", use_helper=False, retries=2, chat_id=chat_id, emoji=emo, reply_to_message_id=reply_to)
        if res: values.append(res["dice"]["value"])
        if i < n - 1: await asyncio.sleep(0.12)
    await asyncio.sleep(2.0)
    return values

# ---------------------------------------------------------------- UI HELPERS
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

# ---------------------------------------------------------------- MATCH ENGINE
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
        db_close_game(gid, payout_a)
        if cashed_out or payout_a > 0:
            w_line = f"{pe('👑')} <b>Winner:</b> {p1_mention} — <b>{m.score[0]} points</b>"
            l_line = f"{pe('👎')} <b>Loser:</b> {p2_mention} — <b>{m.score[1]} points</b>"
            win_amt = payout_a
        else:
            w_line = f"{pe('👑')} <b>Winner:</b> {p2_mention} — <b>{m.score[1]} points</b>"
            l_line = f"{pe('👎')} <b>Loser:</b> {p1_mention} — <b>{m.score[0]} points</b>"
            win_amt = 0
        who_tag = "bot"
    else:
        db_close_game(gid, payout_a)
        db_close_game(sess["gid_b"], payout_b)
        
        if payout_a == payout_b and payout_a > 0:
            w_line = f"{pe('🤝')} <b>Match Tied!</b>"
            l_line = f"Both players got <b>{m.score[0]} points</b>"
            win_amt = payout_a
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

async def evaluate_round(sess: dict):
    m, chat_id, game, ua, ub = sess["match"], sess["chat_id"], sess["game"], sess["ua"], sess["ub"]
    p1_mention = mention(ua)
    p2_mention = mention(ub) if ub else f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}</b>"
    
    mine, theirs = sess["u_rolls"], sess["b_rolls"]
    sess["u_rolls"], sess["b_rolls"] = [], []
    w = m.play_round(mine, theirs)

    if m.winner is not None:
        sess["status"] = "ended" # Game over, block further processing
        if sess["type"] == "bot": await finish_match(sess, int(sess["stake"] * 1.92) if m.winner == 0 else 0)
        else:
            pot = int(2 * sess["stake"] * 0.95)
            await finish_match(sess, pot if m.winner == 0 else 0, 0 if m.winner == 0 else pot)
        return

    sess["round_num"] = sess.get("round_num", 1) + 1
    score_header = f"{game_pe(game)} <b>Score (Round {sess['round_num'] - 1})</b>\n\n• {p1_mention}: <b>{m.score[0]} points</b>\n• {p2_mention}: <b>{m.score[1]} points</b>{f'{pe("🤝")} <b>Tie round — replay!</b>' if w is None else ''}"

    if sess["type"] == "bot" and random.choice([True, False]):
        sess["rolling"] = True
        await edit_msg(chat_id, sess.get("prompt_msg_id", 0), f"{score_header}\n\n{p2_mention} <b>is rolling first...</b>")
        sess["b_rolls"] = await send_emoji_roll(chat_id, game, m.rolls_needed, reply_to=sess.get("prompt_msg_id", 0))
        sess["turn_uid"] = ua
        sess["last_action"] = time.time()
        sess["rolling"] = False
        sent = await send_msg(chat_id, f"{p1_mention}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=ua)
        sess["prompt_msg_id"] = sent["message_id"]
    else:
        sess["rolling"] = False
        sess["turn_uid"] = ua
        sess["last_action"] = time.time()
        await edit_msg(chat_id, sess.get("prompt_msg_id", 0), f"{score_header}\n\n{p1_mention}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=ua)

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
            asyncio.create_task(clear_buttons(chat_id, sess["prompt_msg_id"]))
            sess["prompt_msg_id"] = None
        try:
            if sess["type"] == "bot":
                if order == "bot" and not sess["b_rolls"] and not sess["u_rolls"]:
                    sess["b_rolls"] = await send_emoji_roll(chat_id, game, needed)
                    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ua"], False, time.time()
                    sent = await send_msg(chat_id, f"{mention(sess['ua'])}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=sess["ua"])
                    sess["prompt_msg_id"] = sent["message_id"]
                    return
                rem_u = needed - len(sess["u_rolls"])
                if rem_u > 0: sess["u_rolls"] += await send_emoji_roll(chat_id, game, rem_u)
                if len(sess["b_rolls"]) < needed:
                    b_msg = await send_msg(chat_id, f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}'s turn...</b>")
                    sess["b_rolls"] += await send_emoji_roll(chat_id, game, needed - len(sess["b_rolls"]), reply_to=b_msg["message_id"])
                await evaluate_round(sess)
            else:
                if uid == sess["ua"]:
                    rem = needed - len(sess["u_rolls"])
                    if rem > 0: sess["u_rolls"] += await send_emoji_roll(chat_id, game, rem)
                    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ub"], False, time.time()
                    sent = await send_msg(chat_id, f"{mention(sess['ub'])}, <b>your turn!</b> {game_pe(game)}", markup=turn_kb(sess), owner_id=sess["ub"])
                    sess["prompt_msg_id"] = sent["message_id"]
                elif uid == sess["ub"]:
                    rem = needed - len(sess["b_rolls"])
                    if rem > 0: sess["b_rolls"] += await send_emoji_roll(chat_id, game, rem)
                    await evaluate_round(sess)
        except Exception:
            sess["rolling"], sess["last_action"] = False, time.time()
            try:
                sent = await send_msg(chat_id, f"{pe('⚠️')} {mention(sess['turn_uid'])}, <b>tap below to continue your turn:</b>", markup=turn_kb(sess), owner_id=sess["turn_uid"])
                sess["prompt_msg_id"] = sent["message_id"]
            except Exception: pass

async def handle_user_sent_dice(m: dict):
    if m.get("forward_origin") or m.get("forward_from") or m.get("forward_date"): return
    
    # 1. Ignore highly delayed telegram messages (> 30s old)
    if time.time() - m.get("date", time.time()) > 30: return

    uid, chat_id = m.get("from", {}).get("id"), m.get("chat", {}).get("id")
    
    gid = user_match.get(uid)
    if not gid or gid not in matches: return
    sess = matches[gid]
    if sess["chat_id"] != chat_id or sess.get("status") != "playing": return

    # 2. Block dice sent before it was officially the user's turn
    if m.get("date", 0) < int(sess.get("last_action", 0)):
        return

    # 3. Spam warning handling
    if sess.get("rolling"): 
        await send_msg(chat_id, f"⏳ {mention(uid)}, please wait for the current roll to finish before sending another dice!", reply_to=m.get("message_id"))
        return
        
    if sess.get("turn_uid") != uid: 
        return

    now = time.time()
    if now - user_last_dice_time.get(uid, 0) < 1.0: return
    user_last_dice_time[uid] = now

    dice_obj = m.get("dice", {})
    val = dice_obj.get("value")
    if not val: return

    emo = get_game_emo(sess["game"])
    if dice_obj.get("emoji") != emo: return

    async with sess["lock"]:
        if sess.get("status") != "playing": return # Strict check inside lock
        
        sess["last_action"] = time.time()
        if sess.get("prompt_msg_id"):
            asyncio.create_task(clear_buttons(chat_id, sess["prompt_msg_id"]))
            sess["prompt_msg_id"] = None
        
        needed = sess["match"].rolls_needed
        rolls_list = sess["u_rolls"] if uid == sess["ua"] else sess["b_rolls"]
        if len(rolls_list) >= needed: return
        rolls_list.append(val)
        rem = needed - len(rolls_list)
        
        if rem > 0:
            sent = await send_msg(chat_id, f"{game_pe(sess['game'])} {mention(uid)}, <b>send {rem} more {config.GAME_NAMES.get(sess['game'], sess['game'])}!</b>", markup=turn_kb(sess), owner_id=uid, reply_to=m.get("message_id"))
            sess["prompt_msg_id"] = sent["message_id"]
            return
            
        sess["rolling"] = True
        try:
            await asyncio.sleep(2.0)
            if sess["type"] == "bot":
                if len(sess["b_rolls"]) < needed:
                    b_msg = await send_msg(chat_id, f"{pe('🤖')} <b>{html.escape(config.CASINO_NAME)}'s turn...</b>", reply_to=m.get("message_id"))
                    sess["b_rolls"] += await send_emoji_roll(chat_id, sess["game"], needed - len(sess["b_rolls"]), reply_to=b_msg["message_id"])
                await evaluate_round(sess)
            else:
                if uid == sess["ua"]:
                    sess["turn_uid"], sess["rolling"], sess["last_action"] = sess["ub"], False, time.time()
                    sent = await send_msg(chat_id, f"{mention(sess['ub'])}, <b>your turn!</b> {game_pe(sess['game'])}", markup=turn_kb(sess), owner_id=sess["ub"], reply_to=m.get("message_id"))
                    sess["prompt_msg_id"] = sent["message_id"]
                else:
                    await evaluate_round(sess)
        except Exception: sess["rolling"] = False


# ---------------------------------------------------------------- MESSAGE / CALLBACK ROUTER
async def handle_message(m: dict):
    u = m.get("from", {})
    track_user_info(u) # Ensure user name is updated for linking
    
    uid = u.get("id")
    chat_id = m.get("chat", {}).get("id")
    is_private = (m.get("chat", {}).get("type") == "private")
    text = (m.get("text") or "").strip()
    msg_id = m.get("message_id")

    if "dice" in m:
        await handle_user_sent_dice(m)
        return

    if not text.startswith("/"): return

    parts = text.split(maxsplit=2)
    cmd = parts[0][1:].split("@")[0].lower()
    args = text[len(parts[0]):].strip()

    if cmd == "start":
        db_get_user(uid)
        await send_msg(chat_id, f"🎲 <b>Welcome to {config.CASINO_NAME}!</b>\nPlay games in our official group chat.", reply_to=msg_id)
        return

    if cmd in ("dice", "bowl", "basket", "bask", "football", "dart", "dices"):
        if is_private: 
            return await send_msg(chat_id, f"{pe('⚠️')} {mention(uid)}, <b>PvP Games can only be played in our official Group Chat!</b>\n\n<b>Join the group and start playing!</b>", reply_to=msg_id)
        
        bal = get_bal(uid)
        stake = parse_bet(args, bal)
        min_bet = getattr(config, "MIN_BET", 10)
        
        if stake == -1: return await send_msg(chat_id, f"<b>Usage:</b> <code>/{cmd} 1.50</code>  (minimum <b>{usd(min_bet)}</b>)", reply_to=msg_id)
        if stake < min_bet: return await send_msg(chat_id, f"{pe('⚠️')} <b>Minimum bet is {usd(min_bet)}</b>", reply_to=msg_id)
        if bal < stake: return await send_msg(chat_id, f"{pe('⚠️')} <b>Not enough funds to play</b>\n\n{pe('💼')} <b>Current coin balance: {usd(bal)}</b> {pe('🪙')}", reply_to=msg_id, owner_id=uid)
        
        game_map = {"dices": "dice", "dice": "dice", "bowl": "bowl", "basket": "basket", "bask": "basket", "football": "football", "dart": "dart"}
        game = game_map[cmd]
        
        await send_msg(chat_id, mode_screen_text(game), markup=mode_screen_kb(game, stake), reply_to=msg_id, owner_id=uid)


async def handle_callback(c: dict):
    u = c.get("from", {})
    track_user_info(u) # Ensure user name is updated for linking
    
    cb_id = c["id"]
    data = c.get("data", "")
    uid = u["id"]
    msg = c.get("message", {})
    chat_id = msg.get("chat", {}).get("id", uid)
    is_private = (msg.get("chat", {}).get("type") == "private")
    msg_id = msg.get("message_id")

    if data == "del":
        await del_msg(chat_id, msg_id)
        return await answer_cb(cb_id)

    owner = msg_owner.get((chat_id, msg_id))
    if owner and owner != uid:
        return await answer_cb(cb_id, "This button is not for you!", show_alert=False)

    if data.startswith("back_mode:"):
        if is_private: return await answer_cb(cb_id, "Play in the Group Chat!", show_alert=False)
        _, game, stake = data.split(":")
        await edit_msg(chat_id, msg_id, mode_screen_text(game), markup=mode_screen_kb(game, int(stake)), owner_id=uid)
        
    elif data.startswith("mode:"):
        if is_private: return await answer_cb(cb_id, "Play in the Group Chat!", show_alert=False)
        _, game, stake, mode = data.split(":")
        await edit_msg(chat_id, msg_id, target_screen_text(game), markup=target_screen_kb(game, int(stake), mode), owner_id=uid)
        
    elif data.startswith("tgt:"):
        if is_private: return await answer_cb(cb_id, "Play in the Group Chat!", show_alert=False)
        _, game, stake, mode, target = data.split(":")
        base = f"{game}:{stake}:{mode}:{target}"
        await edit_msg(chat_id, msg_id, f"{game_pe(game)} <b>{config.GAME_NAMES.get(game, game).title()} · {usd(int(stake))}</b>\n<b>Player:</b> {mention(uid)}\n<b>Mode: {MODES[mode]['label']} ({target} points)</b>\n\n<b>Choose your opponent:</b>", markup=kb([("Play vs Bot", f"opp:{base}:bot", "success", "🤖"), ("Play PvP", f"opp:{base}:pvp", "primary", "👤")], [("Back", f"mode:{game}:{stake}:{mode}", "danger", "❌")]), owner_id=uid)
        
    elif data.startswith("opp:"):
        if is_private: return await answer_cb(cb_id, "Games can only be played in Group Chat!", show_alert=False)
        _, game, stake, mode, target, who = data.split(":")
        stake, target = int(stake), int(target)
        if uid in busy: return await answer_cb(cb_id, "Finish your current game first!", show_alert=False)
        
        bal = get_bal(uid)
        if bal < stake:
            return await edit_msg(chat_id, msg_id, f"{pe('⚠️')} <b>Not enough funds to play</b>\n\n{pe('💼')} <b>Current coin balance: {usd(bal)}</b> {pe('🪙')}", owner_id=uid)
        try: gid = db_open_game(uid, game, mode, stake, opponent=0 if who == "bot" else -1)
        except ValueError as e: return await answer_cb(cb_id, str(e), show_alert=False)

        busy.add(uid)
        user_match[uid] = gid

        if who == "bot":
            m_obj = Match(game, mode, target, stake, (uid, 0))
            sess = {"gid": gid, "type": "bot", "status": "playing", "chat_id": chat_id, "game": game, "mode": mode, "target": target, "stake": stake, "ua": uid, "ub": 0, "match": m_obj, "turn_uid": uid, "u_rolls": [], "b_rolls": [], "rolling": False, "round_num": 1, "last_action": time.time(), "lock": asyncio.Lock(), "prompt_msg_id": msg_id}
            matches[gid] = sess
            await edit_msg(chat_id, msg_id, f"{pe('🌊')} <b>The game has started</b>\n\n<b>Player 1:</b> {mention(uid)}\n<b>Player 2:</b> {pe('🤖')} <b>{html.escape(config.CASINO_NAME)}</b>\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[mode]['label']} — {target} points</b>\n\n<b>Roll the {config.GAME_NAMES.get(game, game)}</b> {game_pe(game)}", markup=turn_kb(sess, show_bot_first=True), owner_id=uid)
        else:
            sess = {"gid": gid, "type": "pvp", "status": "waiting", "chat_id": chat_id, "game": game, "mode": mode, "target": target, "stake": stake, "ua": uid, "ub": 0, "u_rolls": [], "b_rolls": [], "rolling": False, "round_num": 1, "created_at": time.time(), "last_action": time.time(), "lock": asyncio.Lock(), "prompt_msg_id": msg_id}
            matches[gid] = sess
            await edit_msg(chat_id, msg_id, f"{pe('🎮')} <b>PvP Match Created</b>\n\n<b>Player 1:</b> {mention(uid)}\n<b>Game:</b> {game_pe(game)} <b>{config.GAME_NAMES.get(game, game).title()}</b>\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[mode]['label']} — {target} points</b>\n\n<i>Click «Accept game» to start PvP!</i>", markup=kb([("Accept game", f"pvp_join:{gid}", "success", "✅")], [("Cancel game", f"cancel_pvp:{gid}", "danger", "❌")]), owner_id=uid)

    elif data.startswith("pvp_join:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess.get("status") != "waiting": return await answer_cb(cb_id, "This match is no longer available!")
        if uid == sess["ua"]: return await answer_cb(cb_id, "You cannot accept your own match!")
        if uid in busy: return await answer_cb(cb_id, "Finish your current game first!")

        bal = get_bal(uid)
        stake = sess["stake"]
        if bal < stake: return await answer_cb(cb_id, f"Not enough balance! Need {usd(stake)}.")
        try: gid_b = db_open_game(uid, sess["game"], sess["mode"], stake, opponent=-1)
        except ValueError as e: return await answer_cb(cb_id, str(e))

        sess["ub"] = uid
        sess["gid_b"] = gid_b
        sess["status"] = "playing"
        sess["match"] = Match(sess["game"], sess["mode"], sess["target"], stake, (sess["ua"], uid))
        sess["turn_uid"] = sess["ua"]
        sess["last_action"] = time.time()
        busy.add(uid)
        user_match[uid] = gid

        start_txt = f"{pe('🌊')} <b>The game has started</b>\n\n<b>Player 1:</b> {mention(sess['ua'])}\n<b>Player 2:</b> {mention(uid)}\n<b>Bet: {usd(stake)}</b> {pe('🪙')}\n<b>Mode: {MODES[sess['mode']]['label']} — {sess['target']} points</b>\n\n{mention(sess['ua'])}, <b>your turn!</b>"
        await edit_msg(chat_id, msg_id, start_txt, markup=turn_kb(sess, show_bot_first=False), owner_id=sess["ua"])
        sess["prompt_msg_id"] = msg_id
        return await answer_cb(cb_id, "Match started!")

    elif data.startswith("cancel_pvp:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess.get("status") != "waiting" or sess["ua"] != uid: return await answer_cb(cb_id, "Cannot cancel after match has started!", show_alert=False)
        db_void_game(gid)
        matches.pop(gid, None); user_match.pop(uid, None); busy.discard(uid)
        await edit_msg(chat_id, msg_id, f"{pe('⭕️')} <b>PvP match cancelled by</b> {mention(uid)} — <b>stake refunded.</b>", owner_id=uid)
        return await answer_cb(cb_id)

    elif data.startswith("roll:"):
        _, gid_str, order = data.split(":")
        sess = matches.get(gid_str)
        if not sess or sess.get("status") != "playing": return await answer_cb(cb_id, "This match is no longer active.", show_alert=False)
        if sess.get("turn_uid") != uid: return await answer_cb(cb_id, "This button is not for you!", show_alert=False)
        needed = sess["match"].rolls_needed
        my_rolls = sess["u_rolls"] if uid == sess["ua"] else sess["b_rolls"]
        if sess.get("rolling") or sess["lock"].locked() or len(my_rolls) >= needed: return await answer_cb(cb_id, "You already rolled!", show_alert=False)
        await answer_cb(cb_id)
        asyncio.create_task(handle_roll_button(sess, uid, order))
        return

    elif data.startswith("cashout:"):
        gid = data.split(":")[1]
        sess = matches.get(gid)
        if not sess or sess["ua"] != uid or sess.get("rolling") or sess["lock"].locked(): return await answer_cb(cb_id, "Cannot cashout right now.", show_alert=False)
        cashout_amt = calc_cashout(sess["stake"], sess["match"].score[0], sess["match"].score[1], sess["target"])
        if cashout_amt <= 0: return await answer_cb(cb_id, "Cashout not available.", show_alert=False)
        asyncio.create_task(clear_buttons(chat_id, msg_id))
        await finish_match(sess, cashout_amt, cashed_out=True)

    await answer_cb(cb_id)

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
                    db_void_game(gid)
                    await edit_msg(sess["chat_id"], sess["prompt_msg_id"], f"{pe('⌛')} <b>No opponent joined</b> {mention(ua)}'s <b>PvP match — stake refunded.</b>", owner_id=ua)
                elif sess.get("status") == "playing" and sess.get("rolling") and now - sess.get("last_action", now) >= 15:
                    sess["rolling"], sess["last_action"] = False, now
        except Exception: pass

async def main():
    try:
        if not HAS_MONGO or not getattr(config, "MONGO_URI", None):
            print("❌ MONGODB URI MISSING! App requires MongoDB to run.")
            return
            
        client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=6000)
        global mongo_db
        mongo_db = client["raika_casino_db"]
        print("🍃 MongoDB Connected! Initialized clean GC Games Bot.")

        asyncio.create_task(sweeper())

        offset = 0
        while True:
            try:
                updates = await tg("getUpdates", offset=offset, timeout=25)
                for u in updates:
                    offset = u["update_id"] + 1
                    if "message" in u: asyncio.create_task(handle_message(u["message"]))
                    elif "callback_query" in u: asyncio.create_task(handle_callback(u["callback_query"]))
            except Exception: await asyncio.sleep(1.5)
    finally:
        if HTTP_SESSION and not HTTP_SESSION.closed: await HTTP_SESSION.close()

if __name__ == "__main__":
    asyncio.run(main())
