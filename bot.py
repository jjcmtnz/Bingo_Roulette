import discord
from discord.ext import commands
from PIL import Image, ImageDraw
from io import BytesIO
import asyncio
import random
import time
import json, os
import tempfile      # ← add this
from pathlib import Path



DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN environment variable.")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True  #
bot = commands.Bot(command_prefix="!", intents=intents)

ASSETS_DIR = Path(__file__).parent / "assets" / "boards"


# --- Allowlist for admin commands (use real Discord user IDs) ---
ALLOWED_ADMINS = {
      991856535930163230,  # disco
    221415080514945035,  # limon
}

def is_allowed_admin():
    async def predicate(ctx):
        return ctx.author.id in ALLOWED_ADMINS
    return commands.check(predicate)

# Commands whose *triggers* should be deleted automatically
ADMIN_COMMAND_NAMES = {
    # core admin
    "tileall", "addpoints", "removepoints", "addbonuspoints", "removebonuspoints",
    "setboard", "setnextboard", "reset", "resetall", "pointsallteams",
    # hidden admin
    "hola", "finishevent",
    # private cleanup tools (still hidden from !commands)
    "cleanup", "delete", "purge",
}

# Board order per team
team_sequences = {
    "team1": ["A", "C", "E", "B", "D", "F"],
    "team2": ["D", "F", "B", "A", "C", "E"],
    "team3": ["B", "E", "C", "F", "A", "D"],
    "team4": ["E", "B", "F", "C", "D", "A"],
}

# Per-team game state
game_state = {
    team: {
        "board_index": 0,
        "completed_tiles": [],
        "bonus_active": False,
        "points": 0,
        "bonus_points": 0,
        "started": False,
        "used_quips": {},          # track per-team quip usage
        "looped": False,
        "finished": False,   # 👈 new field

    }
    for team in team_sequences
}

# --- Keep exactly one on_ready ---
@bot.event
async def on_ready():
    # run once
    if not getattr(bot, "_initialized", False):
        # Load persisted state (this uses your later load_state() that merges in-place)
        load_state()
        bot._initialized = True

    # Boot diagnostics
    print(f"Logged in as {bot.user}")
    print("[BOOT] ASSETS_DIR:", ASSETS_DIR)
    try:
        print("[BOOT] PNGs found:", [p.name for p in ASSETS_DIR.glob("*.png")])
    except Exception as e:
        print("[BOOT] Error listing PNGs:", e)

    # Duplicate command check
    names = [cmd.name for cmd in bot.commands]
    dupes = {n for n in names if names.count(n) > 1}
    print("Loaded commands:", len(names))
    print("Duplicate commands found:", dupes)


PENDING_PURGE_CONFIRMATIONS = {}  # {channel_id: {"user": int, "expires": float}}

# Quip memory for non-team cases (must be defined BEFORE persistence helpers)
GLOBAL_USED_QUIPS = {}


# Always write to the mounted Railway volume
DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Single source of truth for your save paths
STATE_PATH = os.path.join(DATA_DIR, "bingo_state.json")   # you can name it state.json if you prefer
STATE_BAK  = os.path.join(DATA_DIR, "bingo_state.bak.json")

_persist_lock = asyncio.Lock()

def _serialize_state():
    """Make a JSON-safe snapshot (convert sets -> lists)."""
    gs = {}
    for team_key, state in game_state.items():
        s = dict(state)  # shallow copy
        uq = s.get("used_quips", {})
        # sets -> lists
        s["used_quips"] = {cat: list(vals) for cat, vals in uq.items()}
        gs[team_key] = s
    return {
        "game_state": gs,
        "GLOBAL_USED_QUIPS": {cat: list(vals) for cat, vals in GLOBAL_USED_QUIPS.items()},
        "team_sequences": team_sequences,  # optional: keep for reference
    }

async def save_state(game_state: dict):
    data = _serialize_state()
    async with _persist_lock:   # ← add this line
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_PATH), prefix=".tmp_state_", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                # os.fsync(f.fileno()) #comment this out
            if os.path.exists(STATE_PATH):
                try:
                    if os.path.exists(STATE_BAK):
                        os.remove(STATE_BAK)
                    os.replace(STATE_PATH, STATE_BAK)
                except Exception:
                    pass
            os.replace(tmp, STATE_PATH)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def load_state():
    """Load state from /data/bingo_state.json (or backup) and merge into current structures."""
    def _read(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    data = None
    for candidate in (STATE_PATH, STATE_BAK):
        if os.path.exists(candidate):
            try:
                data = _read(candidate)
                break
            except Exception:
                continue

    if not data:
        print("[INFO] No prior state file; starting fresh.")
        return {}

    loaded_gs = data.get("game_state", {})
    loaded_global_quips = data.get("GLOBAL_USED_QUIPS", {})

    # Merge per-team (keeping any new teams not in file)
    for team_key in game_state.keys():
        if team_key in loaded_gs:
            s = loaded_gs[team_key]
            uq = {cat: set(vals) for cat, vals in s.get("used_quips", {}).items()}  # lists -> sets
            game_state[team_key].update({
                "board_index": s.get("board_index", 0),
                "completed_tiles": s.get("completed_tiles", []),
                "bonus_active": s.get("bonus_active", False),
                "points": s.get("points", 0),
                "bonus_points": s.get("bonus_points", 0),
                "started": s.get("started", False),
                "used_quips": uq,
                "looped": s.get("looped", False),
                "finished": s.get("finished", False),
            })
        else:
            game_state[team_key].setdefault("used_quips", {})

    # Restore global quip memory
    GLOBAL_USED_QUIPS.clear()
    for cat, vals in loaded_global_quips.items():
        GLOBAL_USED_QUIPS[cat] = set(vals)

    print("[INFO] State loaded from volume.")
    return game_state




LAST_RESETALL_CALL = 0  # prevents accidental double-fires

# Real bonus challenges
bonus_challenges = {
    "A": "As a team of 5, battle Scurrius until you obtain a Ratbane weapon for everyone from scratch, while wearing no gear. \n\nYou may used any obtained Ratbane weapons during this grind to obtain future Ratbane weapons. You may bring food and potions. Ironmen may complete their contribution in a solo instance. \n\n+5 bonus points.",
    
    "B": "Complete the following activities ONLY with teammates: Wintertodt, Tempoross, Zalcano, Volcanic Mine, and Guardians of the Rift. No solos, masses or non-team members allowed. Please find & use an empty world. Take a team selfie at the end of each activity!\n\n+5 bonus points.",
    
    "C": "Have up to 5 members of your team obtain both flippers and a mudskipper hat from mogres. Take ONE selfie (ss) with all team members wearing their flippers and hat. (Each team member must submit ss showing flipper and hat drops).\n\n+1 bonus point for every team member in the ss that is wearing flippers and mudskipper hat. \n\nMax of +5 bonus points.",
    
    "D": "Complete a Raid of your choosing (200+ invo ToA or normal Cox/ToB) with at least 3 teammates. However, each player may only use gear totaled under 10m. Each team member should submit a screenshot(s) at the beginning of the raid, and another ss in the chest room, showing their full gear and inventory.\n\n+5 bonus points.",
    
    "E": "Achieve platinum time in the Dragon Slayer 1 quest on a Quest Speedrunning world (+3 bonus points).\n\n+2 bonus points if you achieve Diamond time. \n\nMax of +5 bonus points.",
    
    "F": "Acquire and complete 1 champion scroll.\n\n+5 bonus points."
}

# =========================
# Bingo Betty Quip System
# =========================


def get_quip(team_key: str, category: str, pool: list[str]) -> str:
    """
    Returns a non-repeating quip for the given team & category.
    For non-team cases (e.g., 'global'), we keep a separate store so we don't KeyError.
    When a pool is exhausted, it resets so all quips become available again.
    """
    # choose the right used-quips dictionary
    if team_key == "global" or team_key not in game_state:
        used_dict = GLOBAL_USED_QUIPS
    else:
        used_dict = game_state[team_key]["used_quips"]

    used = used_dict.setdefault(category, set())
    available = [q for q in pool if q not in used]

    if not available:
        # reset when exhausted
        used_dict[category] = set()
        available = pool[:]

    choice = random.choice(available)
    used_dict[category].add(choice)
    return f'🗣️ Bingo Betty says: *"{choice}"*'




# -------------------------
# Quip Pools
# -------------------------

# ✅ Tile Completion
QUIPS_TILE_COMPLETE = [
    "One-one thousand, two-one thousand, three—OH S#%& YOU ACTUALLY FINISHED A TILE. I wasn’t emotionally prepared. Take your point and stop looking smug. 🙃",
    "You again? Worse than my clingy ex. But fine — tile complete, point awarded, and Bingo Betty will be insufferable about it later. Go. 😂",
    "HOW DARE you be competent when I had a roast locked and loaded. I’m furious. Also… a little proud. Ew. Don’t tell anyone. 🫃",
    "I blink for one second and you complete a tile like a chaotic raccoon in a jewelry store. Messy. Effective. I hate it here.",
    "Congratulations on weaponizing competence. I was mid-eye roll and now I’ve got whiplash. Put ‘medical expenses’ on your tab. 🙃",
    "I scheduled your failure for 3:15 and you just… didn’t. Rude. Here’s your point. Now leave Bingo Betty to grieve her narrative. 😂",
    "Tile complete. Confetti buffering… sarcasm loading… heart reluctantly full. If you quote me, I’ll deny everything. Proceed.",
    "That was a chaotic stumble over the finish line while yelling ‘I meant to do that!’ Honestly? Iconic. Take the point and go hydrate.",
    "I had a candlelight vigil planned for your competence. Return the flowers — apparently you don’t need them. Point granted.",
    "Tile done. Against logic, odds, and my personal prophecy scroll. I’ll just rewrite reality then. Again.",
    "You completed that like you borrowed someone else’s brain. Return it by end of day. I’m serious. 🙃",
    "If success had a smell, this would be it: panic, adrenaline, and petty vengeance. Delicious. Point awarded.",
    "Not me feeling… proud? Ugh. Gross. Keep moving before I regain my composure and start roasting again.",
    "You dragged that tile across the line like it owed you money. Terrifying energy. I respect it.",
    "I prepared a speech about your downfall. Now I’ll recycle it for the next team. Waste not, want not. 😂",
    "A miracle occurred, I rolled my eyes, the scoreboard updated. The circle of life. Take the point.",
    "Tile complete. Historians will ask, ‘Really? Them?’ and I’ll say, ‘Unfortunately, yes.’",
    "I was counting down to disaster and you changed the ending. Illegal. Keep doing it.",
    "Fine. You win this round. I will be petty about it later. Carry on. 🙃",
    "You finished a tile and suddenly act like main characters. I’ll allow it. Briefly.",
]

# ❌ Tile Removal (12)
QUIPS_TILE_REMOVE = [
    "Reversing progress? Bold. Like rewinding a movie just to cry at the sad part again. Point removed. Dignity pending. 🙃",
    "Tile undone. The bards are switching from epic ballad to comedy roast. I volunteer to lead vocals. 😂",
    "We’re backpedaling now? Cute. Next you’ll trip over your own expectations. Point deducted.",
    "I had your triumph framed. Now I’m melting it down for parts. Minus one. Try again.",
    "That’s not character development — that’s a plot hole. Point gone. Edit yourselves.",
    "Undoing a win should come with counseling. I won’t provide it. Here’s a negative. 🙃",
    "This is like returning a trophy because it clashes with your decor. Fine. Point removed.",
    "Tile removed. The scoreboard sighed. I laughed. Balance restored. 😂",
    "A tragic reversal! Someone cue a tiny violin and a refund request. –1 point.",
    "You discovered reverse gear. Powerful. Misguided. Expensive. Point deleted.",
    "Consider this a learning montage, except no music and I’m judging you. –1.",
    "You pressed CTRL+Z on success. Innovative. Ill-advised. Minus one.",
]

# 🎬 Start Board (10)
QUIPS_START_BOARD = [
    "New board, same chaos. Ugh, it’s you again. Like glitter in a carpet — permanent and annoying. Board {letter} unlocked. Don’t waste my oxygen. 🙃",
    "Curtains up, gremlins out. Board {letter} is live. Try not to trip over Act One this time.",
    "Welcome to Board {letter}. I’ve preheated the oven for drama. Please rise responsibly.",
    "Board {letter} has entered the chat. I expect effort, not theater. Actually, give me both.",
    "Fresh tiles. Fresh mistakes to avoid. Board {letter} begins now — and yes, I’m watching closely.",
    "Here we are. Board {letter}. I brought standards. You bring results. Try it. 😂",
    "Board {letter}. Clean slate. Dirty energy. Make me regret believing in you. Quickly.",
    "The stage is set: Board {letter}. Deliver competence with a side of chaos. I’m hungry.",
    "Board {letter} open. May your luck be loud and your excuses silent. 🙃",
    "Okay, Team {team}. Board {letter}. Win accidentally or on purpose — I’m not picky.",
]

# 🏆 Bonus Completion (10) – includes the two you liked
QUIPS_BONUS_COMPLETE = [
    "Wait—WAIT—did you just… oh my stars, you did. You finished the Bonus Tile. And without even breaking a sweat? I’m offended. And impressed. Equally.",
    "Okay, pause. I was literally mid-eye roll when you smashed the Bonus Tile into the stratosphere. Now I’ve gotta pick my jaw up off the floor.",
    "The Bonus Tile… complete?! I had a roast ready, a spotlight queued, and a dramatic sigh rehearsed. You ruined everything. I’m thrilled. 🙃",
    "You didn’t beat the bonus — you mugged it behind the theater and stole its lunch money. I respect the hustle.",
    "I scheduled your failure; you sent a meeting decline. Bold. Bonus complete. I’ll be petty about this for days.",
    "You stuck the landing, winked at the judges, and stole my material. Rude. Exceptional. Applause you don’t deserve, but get anyway.",
    "I wanted tragedy; you gave me triumph. Fine. Take your laurels. Don’t get comfortable.",
    "Bonus obliterated. Somewhere a narrator weeps and a scoreboard sings. Disgusting. Encore.",
    "You broke the bonus like it was a cheap prop. I love practical effects. Brava.",
    "That was cinematic. I’ll allow it. Frame the moment before I change my mind.",
]

# 🏳️ Bonus Skip (12)
QUIPS_BONUS_SKIP = [
    "Skipped the Bonus Tile? A Shakespearean tragedy. I imagined Act III; you tripped over the curtain in Act I. Iconic cowardice. 🙃",
    "Skipping is a strategy. Not a winning one, but a strategy. Wear it with flair and keep walking.",
    "You looked destiny in the eye and said ‘hard pass.’ I laughed, then marked it down. Next board.",
    "Bravely running away is still running. Fine. Door’s over there. Try again on the next stage.",
    "We could’ve had fireworks; instead we got a screensaver. Skip noted. Move along.",
    "You skipped. Somewhere, a violin squeaked and even I felt secondhand embarrassment. Onward.",
    "The bonus waved; you ghosted. I do admire consistency. Next.",
    "A tactical retreat… with extra retreat. Very avant-garde. Next board unlocked.",
    "You chose peace over points. Adorable. Ineffective. Keep moving.",
    "Tragic heroine energy: dramatic cape, no follow-through. I’m entertained. Proceed.",
    "You skipped the dessert course and asked for the bill. Fine. Next course.",
    "The chorus booed; I clapped ironically. Skip accepted. Go.",
]

# 📊 Progress (6)
QUIPS_PROGRESS = [
    "Progress check? Insecure much. Fine: here’s your status. Use it wisely or ignore it spectacularly — I’ll roast either way. 🙃",
    "You want numbers? Here’s numbers. I’ll even pretend to be proud while you read them.",
    "We’re measuring progress like it’s personality. It’s not. But I’ll indulge you.",
    "Fine. Here’s the state of your chaos. Try not to cry on it.",
    "Status delivered. Expectations withheld. Keep crawling; I’m timing it.",
    "I’ve seen snails overtake you, but this will do. Barely.",
]

# 🧮 Points (6)
QUIPS_POINTS = [
    "Math time. I did it so you don’t have to — which frankly feels like charity. 🙃",
    "Behold: arithmetic with judgment. Savor it.",
    "Numbers updated. Hope you like the taste of accountability.",
    "I added. I subtracted. I survived. You’re welcome.",
    "Here’s your score. Manage your ego accordingly.",
    "Cold numbers, warm shade. My specialty.",
]

# 👑 Admin Add / Remove Bonus Points
QUIPS_ADMIN_ADD = [
    "Admin sprinkled +{amount} bonus points on {team} like glitter on a disaster. Festive. Unearned? We’ll see. 🙃",
    "+{amount} bonus points appeared out of nowhere. If this is favoritism, be more subtle next time.",
    "The Points Fairy visited {team}. I don’t do tooth fairy rates, but enjoy the deposit of +{amount} bonus points.",
    "Administrative generosity detected: +{amount} to {team}. Spend it loudly.",
    "A mysterious benefactor gifted {team} a suspicious +{amount} bonus points. I’m starting rumors immediately.",
]
QUIPS_ADMIN_REMOVE = [
    "Admin clawed back {amount} bonus points from {team}. Consider it a vibe tax. 😂",
    "Subtraction event: {amount} bonus points removed from {team}. Actions, consequences, etc.",
    "Down we go: –{amount} bonus points for {team}. I brought popcorn.",
    "Audit complete. {team} lost {amount} bonus points. Cry quietly; I’m working.",
    "Administrative smite: –{amount} bonus points to {team}. Stand up straighter.",
]

# 👑 Admin Add / Remove TILE Points (for !addpoints / !removepoints)
QUIPS_ADMIN_ADD_TILE = [
    "Admin granted +{amount} points to {team}. Don’t spend them all on mediocrity. 🙃",
    "+{amount} points landed in {team}'s lap. Skill? Luck? I’ll allow it.",
    "The scoreboard sneezed and gave {team} a nasty +{amount} points. Sanitize appropriately.",
    "Administrative generosity: +{amount} points to {team}. Temporary glory, permanent shade.",
    "Points fell from the sky: +{amount} for {team}. Don’t get used to it.",
]
QUIPS_ADMIN_REMOVE_TILE = [
    "Audit time. –{amount} points stripped from {team}. Cry harder.",
    "{team} just lost {amount} points. I’d call it justice.",
    "Subtraction ritual: –{amount} points from {team}. Balance restored.",
    "Admin swung the axe: {team} loses {amount} points. Brutal. Necessary.",
    "–{amount} points for {team}. The scoreboard sighed in relief.",
]

# (Optional) Rename your existing pools to make intent obvious:
# QUIPS_ADMIN_ADD  -> QUIPS_ADMIN_ADD_BONUS
# QUIPS_ADMIN_REMOVE -> QUIPS_ADMIN_REMOVE_BONUS

# Alias the existing "bonus" quips so the commands can find them
QUIPS_ADMIN_ADD_BONUS = QUIPS_ADMIN_ADD
QUIPS_ADMIN_REMOVE_BONUS = QUIPS_ADMIN_REMOVE


# 🔮 Bonus reveal quips (Bingo Betty) — shown right after a team completes all 9 tiles
QUIPS_BONUS_REVEAL = [
    "🎉 Against all odds (and my betting pool), {team} finished **all 9 tiles** on Board {letter}. Ugh, fine, applause. 🙃\n\n✨ Now the **Bonus Tile** crawls into view: shiny, smug, and dangerous. Conquer it or cower before it.",
    "🎉 Plot twist! {team} wrapped up **Board {letter}** like they actually planned this. My roast draft is ruined. 🙃\n\n✨ The **Bonus Tile** looms — glorious points, terrible decisions. Will you dare?",
    "🎉 Well, color me startled. {team} bulldozed Board {letter}, all 9 tiles, no survivors. Pathetic… ly effective. 🙃\n\n✨ The **Bonus Tile** enters like a diva, demanding attention. Do you bow, or do you bolt?",
    "🎉 Board {letter} complete! {team}, I had you penciled in for mediocrity. How dare you.\n\n✨ Now the **Bonus Tile** struts forward — equal parts miracle and migraine. Earn it, or ghost it.",
    "🎉 Somebody call the historians. {team} actually cleared **Board {letter}**. I’m not crying, you’re crying.\n\n✨ The **Bonus Tile** appears — majestic, mocking, menacing. Your move.",
    "🎉 Bravo, {team}. You swept **all 9 tiles** on Board {letter}. It’s giving… competence. I hate it.\n\n✨ The **Bonus Tile** now descends like a cursed prize. Claim it, or shuffle on.",
    "🎉 Surprise ending! {team} nailed **Board {letter}**. And here I thought you were comic relief.\n\n✨ The **Bonus Tile** slides in, dripping with danger and false promises. Hero mode or coward exit — choose.",
    "🎉 I’ll be honest: I bet against you. And yet, {team} finished Board {letter}. My wallet weeps.\n\n✨ The **Bonus Tile** now offers chaos and clout. Take it or skip it, but choose loudly.",
    "🎉 Slow clap for {team}. Board {letter}: cleared. Somewhere, pigs are flying.\n\n✨ The **Bonus Tile** emerges like a final boss — overdramatic and underdressed. Will you slay it?",
    "🎉 So, {team} just… finished Board {letter}? Cute. Unexpected. Mildly offensive to my narrative.\n\n✨ The **Bonus Tile** now waits: high reward, higher risk, maximum judgment. Impress me.",
    "🎉 Breaking news: {team} completed Board {letter}. Scientists baffled. Sarcasm levels critical.\n\n✨ And now the **Bonus Tile** rises — mythical, mocking, and messy. Your destiny awaits.",
    "🎉 Curtain drop! Board {letter} is done, courtesy of {team}. Consider me stunned. Temporarily.\n\n✨ The **Bonus Tile** materializes like a cursed encore. Do you embrace it or storm offstage?",
]

tile_texts = {
    "A": [
        "Tombs of Amascut\n\n1 purple from ToA (Fang, LB, Ward, Masori, Shadow, pet)",
        "Alchemical Hydra\n\n100 kc or 1 unique (eye, fang, heart, tail, leather, claw, jar, pet)",
        "Vardorvis\n\n125 kc or 1 unique (ingot, vestige, SRA piece, virtus, quartz, pet)",
        "Kalphite Queen\n\n150 kc or 1 unique (kq head (no tattered head), d pick, d2h, d chainbody, pet)",
        "Bandos\n\n150 kc or 1 unique (chestplate, tassets, boots, hilt, pet)",
        "Sarachnis\n\n150 kc or 1 unique (cudgel, jar, d med helm, pet)",
        "Moons of Peril\n\nAny 3 uniques",
        "Artio/Callisto\n\n100 kc or 1 unique (claws, d2h, d pick, voidwaker hilt, tyrannical ring, pet)",
        "Tempoross\n\n75 soaked pages or 1 unique (fish barrel, tackle box, x25 soaked pages, big harpoonfish, tome of water, d harpoon, pet)\n\n"
    ],
    "B": [
        "Theatre of Blood\n\n1 purple from ToB (Avernic, Sang, Justiciar, Rapier, Scythe, pet)",
        "Araxxor\n\n100 kc or 1 unique (point, pommel, blade, fang, jar, pet)",
        "Whisperer\n\n75 kc or 1 unique (ingot, vestige, SRA piece, virtus, quartz, pet)",
        "Corporeal Beast\n\n50 kc or 1 unique (any sigil, spirit shield, holy elixir, jar, pet)",
        "Kree'arra\n\n150 kc or 1 unique (helm, chainskirt, chestplate, hilt, pet)",
        "Amoxliatl\n\n150 kc or 1 unique (glacial temotli, pet)",
        "Barrows\n\n1 helm, 1 body, and 1 legs from any set (does not have to match)",
        "Spindel/Vene\n\n150 kc or 1 unique (fangs, d2h, d pick, voidwaker gem, treasonous ring, pet)",
        "Hunter Rumors\n\n75 hunter rumors or quetzin pet\n\n"
    ],
    "C": [
        "Chambers of Xeric\n\n1 purple from CoX (Dexterous/Arcane prayer scroll, Kodai, Buckler, Ancestral, Tbow, Dragon Claws, Elder Maul, Dinh's Bulwark, DHCB, pet)",
        "Kraken\n\n250 kc or 1 unique (trident, tentacle, jar, pet)",
        "Leviathan\n\n150 kc or 1 unique (ingot, vestige, SRA piece, virtus, quartz, pet)",
        "Gauntlet (CG or normal)\n\n25 corrupted gauntlet kc or 50 normal gauntlet kc or 1 unique (crystal weapon/armour seed, enhanced crystal weapon seed, pet)",
        "K'ril Tsutsaroth\n\n150 kc or 1 unique (z. spear, staff of the dead, hilt, pet)",
        "Giant Mole\n\n200 kc or 1 unique (long bone, curved bone, elite clue scroll, pet)",
        "King Black Dragon\n\n250 kc or 2 uniques (kbd heads, visage, d pick, pet) ",
        "Calvarion/Vetion\n\n150 kc or 1 unique (skull, d2h, d pick, voidwaker blade, ring of the gods)",
        "Wintertodt\n\n200 burnt pages or 1 unique (d axe, tome of fire, any pyromancer piece, pet)\n\n"
    ],
    "D": [
        "Nex\n\n200 Nihil Shards or 1 unique (Torva, Nihil Horn, Zaryte Vambs, Ancient Hilt, pet)",
        "Thermy\n\n250 kc or 1 unique (occult, smoke battlestaff, d chainbody, jar, pet)",
        "Duke Sucellus\n\n100 kc or 1 unique (ingot, vestige, SRA piece, virtus, quartz, pet)",
        "Vorkath\n\n100 kc or 1 unique (dragonbone necklace, either visage, head [50kc heads don't count], pet)",
        "Commander Zilyana\n\n150 kc or 1 unique (sara sword, sara light, acb, hilt, pet)",
        "Royal Titans\n\n150 kc or 1 unique (fire crown, ice crown, either prayer scroll)",
        "Scurrius\n\n300 kc or 10 spines or 1 pet",
        "Chaos Elemental\n\n150 kc or 1 unique (d pick, d2h, pet)",
        "Vale Totems\n\n1 unique (bowstring spool, fletching knife, greenman mask)\n\n"
    ],
    "E": [
        "Fortis Colosseum\n\n2 uniques or 10,000 sunfire splinters (tonalztics of ralos, echo crystal, sunfire armor, pet)",
        "Cerberus\n\n150 kc or 1 unique (any crystal, smouldering stone, jar, pet)",
        "Phantom Muspah\n\n150 kc or 2 uniques (venator shard, ancient icon, pet)",
        "Zulrah\n\n175 kc or 1 unique (tanz fang, magic fang, visage, either mutagen, pet)",
        "Huey\n\n200 kc or 3 separate hide drops or 1 unique (tome of earth, dragon hunter wand, pet)",
        "Dagannoth Kings\n\n1 pet or all 4 rings (berserker, warrior, seers, archers)",
        "Obor\n\n35 obor chest kc or 1 unique (hill giant club)",
        "Scorpia\n\n150 kc or 1 unique (either ward shard, pet)",
        "Guardians of the Rift\n\n125 abyssal pearls or 1 unique (catalytic talisman, elemental talisman, abyssal needle, abyssal lantern, any dye, pet)\n\n"
    ],
    "F": [
        "Doom of Mokhaiotl\n\n15,000 demon tears or 1 unique (cloth, eye of ayak, avernic treads, pet)",
        "Grotesque Guardians\n\n150 kc or 1 unique (granite gloves/ring/hammer, black tourmaline core, jar, pet)",
        "Yama\n\n100 oathplate shards or 1 unique (soulflame horn, oathplate, pet)",
        "Tormented Demons\n\n250 kc or 1 unique(burning claws, tormented synapse)",
        "Jad\n\n4 fire capes or pet",
        "Gemstone Crab\n\n1 diamond",
        "Bryophyta\n\n35 bryo chest kc or 1 unique (bryophyta's essence)",
        "Revenants\n\n1 unique (any wilderness weapon, ancient crystal, amulet of avarice, or any ancient artefact)",
        "Mastering Mixology\n\nPrescription Goggles (can split mox, aga, and lye resin #s across team)\n\n"
    ]
}


# Tile coordinates
tile_coords = [
    (130, 189), (313, 189), (491, 189),
    (130, 350), (313, 350), (491, 350),
    (130, 518), (313, 518), (491, 518),
]

# Helpers
def normalize_team_name(name):
    return name.strip().lower()

def format_team_text(team_key):
    return f"Team {team_key[-1]}"

def get_current_board_letter(team_key):
    return team_sequences[team_key][game_state[team_key]["board_index"]]

def create_board_image_with_checks(board_letter, completed_tiles):
    img_path = ASSETS_DIR / f"Board {board_letter}.png"
    img = Image.open(img_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    checkmark_size = 60
    checkmark_width = 10

    for tile_num in completed_tiles:
        if 1 <= tile_num <= 9:
            x, y = tile_coords[tile_num - 1]
            points = [
                (x - checkmark_size // 3, y),
                (x, y + checkmark_size // 3),
                (x + checkmark_size // 2, y - checkmark_size // 2),
            ]
            # CHANGE: keep rounded joints when supported; fall back otherwise
            try:
                draw.line(points, fill=(0, 255, 0, 255), width=checkmark_width, joint="curve")
            except TypeError:
                # Older Pillow: 'joint' not supported
                draw.line(points, fill=(0, 255, 0, 255), width=checkmark_width)

    img_bytes = BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    return img_bytes


def get_tile_descriptions(board_letter, completed_tiles):
    descriptions = tile_texts.get(board_letter, ["(Placeholder)" for _ in range(9)])
    display = []
    for i, desc in enumerate(descriptions):
        if i + 1 not in completed_tiles:
            lines = desc.strip().split("\n")
            title_line = f"Tile {i+1} — **{lines[0].strip()}**"
            bullet_lines = [f"- {line.strip()}" for line in lines[1:] if line.strip()]
            full_text = "\n".join([title_line] + bullet_lines)
            display.append(full_text)
    return "\n\n".join(display) if display else "*All tiles completed!*"



def make_tile_command(tile_num):
    @bot.command(name=f"tile{tile_num}")
    async def tile_command(ctx):
        # infer team from channel name (e.g., "team-1" -> "team1")
        team_name = ctx.channel.name.replace("-", "")
        team_key = normalize_team_name(team_name)

        if team_key not in game_state:
            await ctx.send("Invalid team name.")
            return

        if not game_state[team_key].get("started"):
            await ctx.send("Oops! You must start your board using `!startboard` before checking off a tile.")
            return

        state = game_state[team_key]
        board_letter = get_current_board_letter(team_key)

        if state.get("finished"):
            await ctx.send(f"{format_team_text(team_key)} has already completed Bingo Roulette. No further progress can be made.")
            return

        if state.get("bonus_active"):
            await ctx.send(f"{format_team_text(team_key)} is currently in a bonus challenge and cannot check tiles.")
            return

        if tile_num in state["completed_tiles"]:
            await ctx.send(f"Tile {tile_num} is already completed for {format_team_text(team_key)}.")
            return

        # --- mutate state ---
        state["completed_tiles"].append(tile_num)
        state["points"] += 1
        await save_state(game_state)

        # ======================
        # Case 1: all tiles done
        # ======================
        if len(state["completed_tiles"]) == 9:
            if not state.get("looped", False):
                # First cycle → trigger bonus tile
                state["bonus_active"] = True
                await save_state(game_state)

                # 1) Completed message
                await ctx.send(
                    f"🎉 {format_team_text(team_key)} has completed all 9 tiles and has finished Board {board_letter}!"
                )

                # 2) Board image (all checks)
                img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
                await ctx.send(file=discord.File(img_bytes, filename="board.png"))

                # 3) Points line
                await ctx.send(
                    f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
                    f"**Total:** {state['points'] + state['bonus_points']}"
                )

                # 4) Bonus intro + challenge + instructions
                raw_bonus = bonus_challenges[board_letter].replace("/n", "\n")
                challenge_block = "> " + "\n> ".join(raw_bonus.splitlines())
                await ctx.send(
                    f"🔮 **A wild Bonus Tile has appeared!**\n\n"
                    f"{challenge_block}\n\n"
                    "Type `!finishbonus` when you have completed the Bonus Tile challenge.\n"
                    "Or, type `!skipbonus` to skip to the next board."
                )
                return

            else:
                # Loop cycle → no bonus; advance immediately
                await ctx.send(
                    f"🎉 {format_team_text(team_key)} has completed all 9 tiles on Board {board_letter}!\n\n"
                    f"🗣️ Bingo Betty says: *\"No encore Bonus Tile for you. You've already seen that show. Onward. Also take a shower... ew.\"*"
                )

                # Advance and reset
                state["board_index"] = (state["board_index"] + 1) % len(team_sequences[team_key])
                state["completed_tiles"] = []
                await save_state(game_state)

                # New board
                board_letter = get_current_board_letter(team_key)
                img_bytes = create_board_image_with_checks(board_letter, [])
                await ctx.send(file=discord.File(img_bytes, filename="board.png"))

                descriptions = get_tile_descriptions(board_letter, [])
                await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")

                await ctx.send(
                    f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
                    f"**Total:** {state['points'] + state['bonus_points']}"
                )
                return

        # ======================
        # Case 2: normal progress (your new layout)
        # ======================
        tile_title = tile_texts[board_letter][tile_num - 1].split("\n")[0]
        check_emoji = "✅"  # different from the crystal ball; keep checkmark
        quip = get_quip(team_key, "tile_complete", QUIPS_TILE_COMPLETE)

        # 1) One combined text message: tile line + points + quip
        await ctx.send(
    f"{check_emoji} **Tile {tile_num}: {tile_title} – complete!** +1 point awarded.\n\n"
    f"{quip}\n\n"
    f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
    f"**Total:** {state['points'] + state['bonus_points']}"
)


        # 2) The board image
        img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
        await ctx.send(file=discord.File(img_bytes, filename="board.png"))

        # 3) The remaining descriptions (with underlined header)
        descriptions = get_tile_descriptions(board_letter, state["completed_tiles"])
        await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")




# ------- Admin: remove a completed tile -------
# If you use an admin decorator, keep it here (e.g., @is_allowed_admin())
@bot.command()
async def removetile(ctx, tile: int):
    team_key = normalize_team_name(ctx.channel.name.replace("-", ""))
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]

    # must have started, not finished
    if not state.get("started"):
        await ctx.send("Oops! You must use `!startboard` before you can update tiles.")
        return
    if state.get("finished"):
        await ctx.send(f"{format_team_text(team_key)} has already completed Bingo Roulette. No further progress can be made.")
        return

    # validate tile
    if tile not in range(1, 10):
        await ctx.send("Please specify a tile number from 1 to 9.")
        return

    board_letter = get_current_board_letter(team_key)

    # if tile wasn't completed, just report it and show the normal view in your order
    if tile not in state["completed_tiles"]:
        # 1) action
        await ctx.send(
            f"⚠️ Tile {tile} was not marked complete for {format_team_text(team_key)} on **Board {board_letter}**."
        )
        # 2) quip (fallback to progress quips if you don't have a specific pool)
        quip = get_quip(team_key, "removetile", QUIPS_PROGRESS)
        await ctx.send(f"{quip}")
        # 3) scoreboard
        await ctx.send(
            f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
            f"**Total:** {state['points'] + state['bonus_points']}"
        )
        # 4) board image
        img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
        await ctx.send(file=discord.File(img_bytes, filename="board.png"))
        # 5) checklist
        descriptions = get_tile_descriptions(board_letter, state["completed_tiles"])
        await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")
        return

    # actually remove the tile
    state["completed_tiles"].discard(tile)

    # adjust points if your rules give 1 point per tile (safe, minimal)
    if state.get("points", 0) > 0:
        state["points"] -= 1

    # if we dropped below 9 tiles, ensure bonus is not active
    if len(state["completed_tiles"]) < 9 and state.get("bonus_active"):
        state["bonus_active"] = False

    save_state(game_state)

    # ----- Ordered output -----
    # 1) action
    await ctx.send(
        f"⛔️ **Tile {tile} removed.** {format_team_text(team_key)} progress updated on **Board {board_letter}**."
    )

    # 2) quip (use your dedicated pool if you have one; fallback to progress)
    quip = get_quip(team_key, "removetile", QUIPS_PROGRESS)
    await ctx.send(f"{quip}")

    # 3) scoreboard
    await ctx.send(
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {state['points'] + state['bonus_points']}"
    )

    # 4) board image
    img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
    await ctx.send(file=discord.File(img_bytes, filename="board.png"))

    # 5) checklist
    descriptions = get_tile_descriptions(board_letter, state["completed_tiles"])
    await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")





# Register tile commands
for tile_num in range(1, 10):
    make_tile_command(tile_num)


# ------- Admin: complete all tiles on current board -------
@bot.command(hidden=True)
@is_allowed_admin()
async def tileall(ctx, *, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]
    board_letter = get_current_board_letter(team_key)

    # how many tiles left on this board?
    remaining = max(0, 9 - len(state["completed_tiles"]))
    state["completed_tiles"] = list(range(1, 10))
    state["points"] += remaining

    if not state.get("looped", False):
        # First cycle → trigger bonus tile
        state["bonus_active"] = True

        # 1) announcement + 2) betty quip + 3) scoreboard (single send)
        quip = get_quip(team_key, "bonus_reveal", QUIPS_BONUS_REVEAL).format(
            letter=board_letter, team=format_team_text(team_key)
        )
        await ctx.send(
            f"🎉 {format_team_text(team_key)} has completed all 9 tiles and has finished Board {board_letter}!\n\n"
            f"{quip}\n\n"
            f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
            f"**Total:** {state['points'] + state['bonus_points']}"
        )

        # 4) board image
        img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
        await ctx.send(file=discord.File(img_bytes, filename="board.png"))

        # 5) checklist
        descriptions = get_tile_descriptions(board_letter, state["completed_tiles"])
        await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")

        # 6) bonus challenge LAST
        raw_bonus = bonus_challenges[board_letter].replace("/n", "\n")
        challenge_block = "> " + "\n> ".join(raw_bonus.splitlines())
        await ctx.send(
            f"🔮 **A wild Bonus Tile has appeared!**\n\n"
            f"{challenge_block}\n\n"
            "Type `!finishbonus` when you have completed the Bonus Tile challenge.\n"
            "Or, type `!skipbonus` to skip to the next board."
        )

    else:
        # Loop cycle → no bonus tile; advance directly
        state["board_index"] = (state["board_index"] + 1) % len(team_sequences[team_key])
        state["completed_tiles"] = []

        # 1) announcement + 2) betty quip + 3) scoreboard (single send)
        await ctx.send(
            f"🎉 {format_team_text(team_key)} has completed all 9 tiles on Board {board_letter}!\n\n"
            "🗣️ Bingo Betty says: *\"No encore Bonus Tile for you. You've already seen that show. Onward. Also take a shower... ew.\"*\n\n"
            f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
            f"**Total:** {state['points'] + state['bonus_points']}"
        )

        # New current board after advancing
        board_letter = get_current_board_letter(team_key)

        # 4) board image (fresh)
        img_bytes = create_board_image_with_checks(board_letter, [])
        await ctx.send(file=discord.File(img_bytes, filename="board.png"))

        # 5) checklist for the new board
        descriptions = get_tile_descriptions(board_letter, [])
        await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")

    # persist mutations from either branch
    await save_state(game_state)





# ------- Start board (one-time) -------
# ------- Start board (one-time, idempotent, concurrency-safe) -------
@bot.command()
@commands.max_concurrency(1, per=commands.BucketType.channel, wait=False)
async def startboard(ctx):
    team_key = normalize_team_name(ctx.channel.name.replace("-", ""))
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]

    # finished guard
    if state.get("finished"):
        await ctx.send(f"{format_team_text(team_key)} has already completed Bingo Roulette. No further progress can be made.")
        return

    # If already started, show single line and exit
    if state.setdefault("started", False):
        await ctx.send(
            "ℹ️ No need to use `!startboard` ever again. That was a one-and-only kind of thing. "
            "When a new board appears, it activates automatically. Use `!tile#` to complete tiles, "
            "or `!progress` to display your current board."
        )
        return

    # First-time start: set flags BEFORE any awaits, then persist immediately
    state["started"] = True
    state.setdefault("completed_tiles", set())
    state.setdefault("points", 0)
    state.setdefault("bonus_points", 0)
    state["bonus_active"] = False
    save_state(game_state)  # important: pass the argument

    board_letter = get_current_board_letter(team_key)

    # 1) announcement + 2) quip + 3) scoreboard (single send)
    quip_template = random.choice(QUIPS_START_BOARD)
    td = format_team_text(team_key)  # e.g., "Team 1"
    team_num = td[5:] if td.startswith("Team ") else td
    quip = quip_template.format(letter=board_letter, team=team_num)


    points_line = (
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {state['points'] + state['bonus_points']}"
    )
    await ctx.send(
        f"🚀 **Board {board_letter} activated!** {format_team_text(team_key)} is officially in play.\n\n"
        f"{quip}\n\n"
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {state['points'] + state['bonus_points']}"
)


    # 4) board image
    img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
    await ctx.send(file=discord.File(img_bytes, filename=f"board_{board_letter}.png"))

    # 5) checklist
    descriptions = get_tile_descriptions(board_letter, state["completed_tiles"])
    await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")





# ------- Finish bonus (infer team) -------
@bot.command()
async def finishbonus(ctx):
    team_name = ctx.channel.name.replace("-", "")
    team_key = normalize_team_name(team_name)

    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]

    if not state.get("started"):
        await ctx.send("Oops! You must use `!startboard` before you can finish a Bonus Tile.")
        return

    if not state.get("bonus_active"):
        await ctx.send("There is no active Bonus Tile to complete.")
        return

    # ❄️ Frozen team guard
    if state.get("finished"):
        await ctx.send(f"{format_team_text(team_key)} has already completed Bingo Roulette. No further progress can be made.")
        return

    # 🔧 Self-heal bonus state:
    # If bonus_active is False BUT all 9 tiles are complete on first cycle,
    # re-enter bonus mode so the team can finish/skip properly.
    if not state.get("bonus_active"):
        if len(state.get("completed_tiles", [])) == 9 and not state.get("looped", False):
            state["bonus_active"] = True
            await save_state(game_state)

        else:
            await ctx.send(f"{format_team_text(team_key)} is not currently in a bonus challenge.")
            return

    # Advance with looping
    if state["board_index"] + 1 < len(team_sequences[team_key]):
        state["board_index"] += 1
    else:
        state["looped"] = True
        state["board_index"] = 0

    state["completed_tiles"] = []
    state["bonus_active"] = False
    await save_state(game_state)


    board_letter = get_current_board_letter(team_key)

    # 🎉 Progress + 🗣️ Quip + 📝 Refs note (single send)
    quip = get_quip(team_key, "bonus_complete", QUIPS_BONUS_COMPLETE)
    msg = (
        f"🎉 {format_team_text(team_key)} has completed the Bonus Tile challenge and advanced to Board {board_letter}!\n\n"
        f"{quip}\n\n"
        "📝 Refs will verify that the Bonus Tile Challenge has successfully been completed. "
        "If approved, your bonus points will be manually added! (Please tag the refs!)"
    )
    await ctx.send(msg)

    # 🖼️ Board image (file upload must be separate)
    img_bytes = create_board_image_with_checks(board_letter, [])
    await ctx.send(file=discord.File(img_bytes, filename="board.png"))

    # ✅ Checklist & descriptions
    descriptions = get_tile_descriptions(board_letter, [])
    await ctx.send(f"📋 Board {board_letter} – Checklist\n\n{descriptions}")

    # 🧮 Points recap
    await ctx.send(
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {state['points'] + state['bonus_points']}"
    )



@bot.command()
async def skipbonus(ctx):
    team_name = ctx.channel.name.replace("-", "")
    team_key = normalize_team_name(team_name)

    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]

    if not state.get("started"):
        await ctx.send("Oops! You must use `!startboard` before you can skip a Bonus Tile.")
        return

    if not state.get("bonus_active"):
        await ctx.send("There is no active Bonus Tile to skip.")
        return

    # ❄️ Frozen team guard
    if state.get("finished"):
        await ctx.send(f"{format_team_text(team_key)} has already completed Bingo Roulette. No further progress can be made.")
        return

    # 🔧 Self-heal bonus state (same logic as finishbonus)
    if not state.get("bonus_active"):
        if len(state.get("completed_tiles", [])) == 9 and not state.get("looped", False):
            state["bonus_active"] = True
            await save_state(game_state)

        else:
            await ctx.send(f"{format_team_text(team_key)} is not currently in a bonus challenge.")
            return

    # Advance with looping
    if state["board_index"] + 1 < len(team_sequences[team_key]):
        state["board_index"] += 1
    else:
        state["looped"] = True
        state["board_index"] = 0

    state["completed_tiles"] = []
    state["bonus_active"] = False
    await save_state(game_state)


    board_letter = get_current_board_letter(team_key)

    # 🚪 Progress + 🗣️ Quip (single send)
    quip = get_quip(team_key, "bonus_skip", QUIPS_BONUS_SKIP)
    msg = (
        f"🚪 {format_team_text(team_key)} has skipped the Bonus Tile Challenge and advanced to Board {board_letter}. "
        f"No bonus points will be added.\n\n{quip}"
    )
    await ctx.send(msg)

    # 🖼️ Board image
    img_bytes = create_board_image_with_checks(board_letter, [])
    await ctx.send(file=discord.File(img_bytes, filename="board.png"))

    # ✅ Checklist & descriptions
    descriptions = get_tile_descriptions(board_letter, [])
    await ctx.send(f"📋 Board {board_letter} – Checklist\n\n{descriptions}")

    # 🧮 Points recap
    await ctx.send(
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {state['points'] + state['bonus_points']}"
    )




# ------- Admin: reset single team -------
@bot.command(hidden=True)
@is_allowed_admin()
async def reset(ctx, *, team: str):
    team_key = normalize_team_name(team)
    if team_key in game_state:
        game_state[team_key] = {
            "board_index": 0,
            "completed_tiles": [],
            "bonus_active": False,
            "points": 0,
            "bonus_points": 0,
            "started": False,
            "used_quips": {},
            "looped": False,  # <-- ensure present
            "finished": False,   # 👈 add this
        }
        await save_state(game_state)
        await ctx.send(f"{format_team_text(team_key)} has been reset.")


# ------- Admin: reset all teams -------
@bot.command(hidden=True)
@is_allowed_admin()
async def resetall(ctx):
    global LAST_RESETALL_CALL
    now = time.time()
    if now - LAST_RESETALL_CALL < 1.0:
        return
    LAST_RESETALL_CALL = now

    for key in list(game_state.keys()):
        game_state[key] = {
            "board_index": 0,
            "completed_tiles": [],
            "bonus_active": False,
            "points": 0,
            "bonus_points": 0,
            "started": False,
            "used_quips": {},
            "looped": False,  # <-- ensure present
            "finished": False,   # 👈 add this
        }

    await save_state(game_state)


    try:
        msg = get_quip("global", "resetall", [
            "Global reset executed. Fresh chaos unlocked. Don’t make me regret this.",
            "All teams scrubbed clean. Like it never happened. Except I remember everything.",
            "Factory settings restored. Perform better in the sequel, please.",
            "We nuked it from orbit. Only way to be sure. Proceed.",
            "Clean slate delivered. Try not to smudge it immediately.",
        ])
        await ctx.send(msg)
    except Exception:
        await ctx.send("⚙️ **All teams have been reset.**")


# ------- Admin: set a specific board -------
@bot.command(hidden=True)
@is_allowed_admin()
async def setboard(ctx, board_letter: str, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    if board_letter.upper() not in team_sequences[team_key]:
        await ctx.send("Invalid board letter.")
        return

    game_state[team_key]["board_index"] = team_sequences[team_key].index(board_letter.upper())
    game_state[team_key]["completed_tiles"] = []
    game_state[team_key]["bonus_active"] = False
    game_state[team_key]["started"] = True  # ✅ added line
    await save_state(game_state)


    img_bytes = create_board_image_with_checks(board_letter.upper(), [])
    await ctx.send(f"Admin override: {format_team_text(team_key)} set to Board {board_letter.upper()}.")
    await ctx.send(file=discord.File(img_bytes, filename="board.png"))


# ------- Admin: force next board -------
@bot.command(hidden=True)
@is_allowed_admin()
async def setnextboard(ctx, *, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]
    if state["board_index"] + 1 < len(team_sequences[team_key]):
        state["board_index"] += 1
        state["completed_tiles"] = []
        state["bonus_active"] = False
        state["started"] = True  # ✅ make sure team can immediately use tiles
        await save_state(game_state)


        board_letter = get_current_board_letter(team_key)
        img_bytes = create_board_image_with_checks(board_letter, [])
        await ctx.send(f"Admin override: {format_team_text(team_key)} skipped to next board ({board_letter}).")
        await ctx.send(file=discord.File(img_bytes, filename="board.png"))
    else:
        await ctx.send(f"{format_team_text(team_key)} has no more boards left.")


# ------- Admin: adjust points -------
@bot.command(hidden=True)
@is_allowed_admin()
async def addpoints(ctx, amount: int, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    game_state[team_key]["points"] += amount
    await save_state(game_state)


    total = game_state[team_key]["points"] + game_state[team_key]["bonus_points"]
    quip = random.choice(QUIPS_ADMIN_ADD_TILE).format(amount=amount, team=format_team_text(team_key))

    msg = (
        f"✅ **{amount} points have been added to {format_team_text(team_key)}.**\n\n"
        f"🗣️ Bingo Betty says: *\"{quip}\"*\n\n"
        f"🧮 **Points:** {game_state[team_key]['points']} | "
        f"**Bonus Points:** {game_state[team_key]['bonus_points']} | "
        f"**Total:** {total}"
    )
    await ctx.send(msg)


@bot.command(hidden=True)
@is_allowed_admin()
async def removepoints(ctx, amount: int, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    game_state[team_key]["points"] = max(0, game_state[team_key]["points"] - amount)
    await save_state(game_state)


    total = game_state[team_key]["points"] + game_state[team_key]["bonus_points"]
    quip = random.choice(QUIPS_ADMIN_REMOVE_TILE).format(amount=amount, team=format_team_text(team_key))

    msg = (
        f"❌ **{amount} points have been removed from {format_team_text(team_key)}.**\n\n"
        f"🗣️ Bingo Betty says: *\"{quip}\"*\n\n"
        f"🧮 **Points:** {game_state[team_key]['points']} | "
        f"**Bonus Points:** {game_state[team_key]['bonus_points']} | "
        f"**Total:** {total}"
    )
    await ctx.send(msg)



@bot.command(hidden=True)
@is_allowed_admin()
async def addbonuspoints(ctx, amount: int, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    game_state[team_key]["bonus_points"] += amount
    await save_state(game_state)


    total = game_state[team_key]["points"] + game_state[team_key]["bonus_points"]
    quip = random.choice(QUIPS_ADMIN_ADD).format(amount=amount, team=format_team_text(team_key))

    msg = (
        f"✅ **{amount} bonus points have been added to {format_team_text(team_key)}.**\n\n"
        f"🗣️ Bingo Betty says: *\"{quip}\"*\n\n"
        f"🧮 **Points:** {game_state[team_key]['points']} | "
        f"**Bonus Points:** {game_state[team_key]['bonus_points']} | "
        f"**Total:** {total}"
    )
    await ctx.send(msg)




@bot.command(hidden=True)
@is_allowed_admin()
async def removebonuspoints(ctx, amount: int, team: str):
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    game_state[team_key]["bonus_points"] = max(0, game_state[team_key]["bonus_points"] - amount)
    await save_state(game_state)


    total = game_state[team_key]["points"] + game_state[team_key]["bonus_points"]
    quip = random.choice(QUIPS_ADMIN_REMOVE).format(amount=amount, team=format_team_text(team_key))

    msg = (
        f"❌ **{amount} bonus points have been removed from {format_team_text(team_key)}.**\n\n"
        f"🗣️ Bingo Betty says: *\"{quip}\"*\n\n"
        f"🧮 **Points:** {game_state[team_key]['points']} | "
        f"**Bonus Points:** {game_state[team_key]['bonus_points']} | "
        f"**Total:** {total}"
    )
    await ctx.send(msg)




# ------- Show team progress -------
@bot.command()
async def progress(ctx):
    team_name = ctx.channel.name.replace("-", "")
    team_key = normalize_team_name(team_name)

    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]

    # must have started first
    if not state.get("started"):
        await ctx.send("Oops! You must use `!startboard` before you can view your progress.")
        return

    # block if event finished
    if state.get("finished"):
        await ctx.send(f"{format_team_text(team_key)} has already completed Bingo Roulette. No further progress can be made.")
        return

    board_letter = get_current_board_letter(team_key)

    # If bonus is active, re-show the bonus screen in this order:
    # 1) completed message + points (single send), 2) board image, 3) bonus header + challenge + instructions (last)
    if state.get("bonus_active"):
        # 1) completed message + points (SCOREBOARD BEFORE IMAGE)
        completed_msg = f"🎉 {format_team_text(team_key)} has completed all 9 tiles and has finished Board {board_letter}!\n\n"
        points_line = (
            f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
            f"**Total:** {state['points'] + state['bonus_points']}"
        )
        await ctx.send("\n\n".join([completed_msg, points_line]))

        # 2) board image
        img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
        await ctx.send(file=discord.File(img_bytes, filename="board.png"))

        # 3) bonus last
        raw_bonus = bonus_challenges[board_letter].replace("/n", "\n")
        challenge_block = "> " + "\n> ".join(raw_bonus.splitlines())
        await ctx.send(
            f"🔮 **A wild Bonus Tile has appeared!**\n\n"
            f"{challenge_block}\n\n"
            "Type `!finishbonus` when you have completed the Bonus Tile challenge.\n"
            "Or, type `!skipbonus` to skip to the next board."
        )
        return

    # --- normal progress view ---
    # Order: quip (if you want it), SCOREBOARD, board image, checklist
    quip = get_quip(team_key, "progress", QUIPS_PROGRESS)
    await ctx.send(
        f"{quip}\n\n"
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {state['points'] + state['bonus_points']}"
)


    # board image
    img_bytes = create_board_image_with_checks(board_letter, state["completed_tiles"])
    await ctx.send(file=discord.File(img_bytes, filename="board.png"))

    # checklist
    descriptions = get_tile_descriptions(board_letter, state["completed_tiles"])
    await ctx.send(f"📋 __Board {board_letter} – Checklist__\n\n{descriptions}")



@bot.command()
async def points(ctx):
    team_name = ctx.channel.name.replace("-", "")
    team_key = normalize_team_name(team_name)

    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]

    if not state.get("started"):
        await ctx.send("Oops! You must use `!startboard` before you can view your team’s points.")
        return

    quip = get_quip(team_key, "points", QUIPS_POINTS)
    await ctx.send(f"{quip}")


    await ctx.send(
        f"**🧮 {format_team_text(team_key)} Points Overview:**\n"
        f"- Tiles Completed: {state['points']}\n"
        f"- Bonus Points: {state['bonus_points']}\n"
        f"- **Total Points: {state['points'] + state['bonus_points']}**"
    )

# ------- Admin: overview points for all teams -------
@bot.command(hidden=True)
@is_allowed_admin()
async def pointsallteams(ctx):
    """Admin-only: show points, bonus, and totals for every team (sorted by total desc)."""
    rows = []
    for team_key in team_sequences.keys():
        state = game_state.get(team_key, {})
        team_name = f"Team {team_key[-1]}"
        tiles = int(state.get("points", 0))
        bonus = int(state.get("bonus_points", 0))
        total = tiles + bonus
        rows.append((team_name, tiles, bonus, total))

    # Sort by total (desc), then by team name for stable ordering
    rows.sort(key=lambda r: (-r[3], r[0]))

    # Build a neat table
    header = f"{'Team':<8}{'Tiles':>7}{'Bonus':>7}{'Total':>8}"
    line   = "-" * len(header)
    lines = [header, line]
    for team_name, tiles, bonus, total in rows:
        lines.append(f"{team_name:<8}{tiles:>7}{bonus:>7}{total:>8}")

    table = "\n".join(lines)
    await ctx.send(f"🏁 **All Teams — Points Overview** 🏁\n```{table}```")



@bot.command()
@is_allowed_admin()
async def cleanup(ctx, n: int = 1):
    """Admin-only: delete the bot's last n messages in this channel (default 1, max 50)."""
    # --- Always delete the trigger immediately ---
    try:
        await ctx.message.delete()
    except Exception:
        pass

    n = max(1, min(n, 50))
    deleted = 0

    async for msg in ctx.channel.history(limit=200):
        if msg.author == bot.user:
            try:
                await msg.delete()
                deleted += 1
            except discord.Forbidden:
                await ctx.send("⚠️ I don’t have permission to delete messages here.", delete_after=5)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                pass
            if deleted >= n:
                break

    if deleted == 0:
        await ctx.send("🧹 No recent bot messages found to delete.", delete_after=5)
    else:
        await ctx.send(f"🧹 Deleted {deleted} bot message{'s' if deleted != 1 else ''}.", delete_after=3)



@bot.command()
@is_allowed_admin()
async def delete(ctx, target: str = "", force: str = ""):
    """Admin-only: delete a specific message by ID/link, or delete the replied-to message.
    Usage examples:
      !delete 123456789012345678
      !delete <123456789012345678>
      !delete https://discord.com/channels/123/456/123456789012345678
      (reply to a message) !delete
      Add 'force' to delete non-bot messages (requires Manage Messages).
    """
    import re

    # --- Always try to delete the trigger immediately ---
    try:
        await ctx.message.delete()
    except Exception:
        pass  # Ignore if no perms or already deleted

    def _extract_message_id(s: str) -> int | None:
        if not s:
            return None
        s = s.strip().strip("<>").strip()
        m = re.search(r"/channels/\d+/\d+/(\d{15,25})", s)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d{15,25})$", s)
        if m:
            return int(m.group(1))
        return int(s) if s.isdigit() else None

    # Allow replying to a message with !delete and no args
    message_id = _extract_message_id(target) if target else None
    if message_id is None:
        ref = getattr(ctx.message, "reference", None)
        if ref and ref.message_id:
            message_id = ref.message_id

    if message_id is None:
        await ctx.send("❓ Provide a message ID/link, or reply to the message and run `!delete`.", delete_after=5)
        return

    try:
        msg = await ctx.channel.fetch_message(message_id)
    except discord.NotFound:
        await ctx.send("❓ I couldn’t find that message in this channel.", delete_after=5)
        return
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to fetch messages here.", delete_after=5)
        return
    except discord.HTTPException:
        await ctx.send("⚠️ Something went wrong trying to fetch that message.", delete_after=5)
        return

    # Safety: only delete bot messages unless force
    if msg.author != bot.user and force.lower() != "force":
        await ctx.send("🛡️ I only delete **my own** messages by default. Add `force` to delete others.", delete_after=5)
        return

    try:
        await msg.delete()
        await ctx.send("🧹 Message deleted.", delete_after=3)
    except discord.Forbidden:
        await ctx.send("⚠️ I’m missing **Manage Messages** permission.", delete_after=5)
    except discord.NotFound:
        await ctx.send("❓ That message was already deleted.", delete_after=5)
    except discord.HTTPException:
        await ctx.send("⚠️ Deletion failed due to an API error.", delete_after=5)


@bot.command()
@is_allowed_admin()
async def purge(ctx, *args):
    """
    Admin-only purge:
      !purge N              -> delete the last N bot-authored messages in this channel
      !purge all            -> arm a 20s confirmation window to purge EVERYTHING
      !purge all confirm    -> within 20s, deletes ALL messages (bot + players)
    """
    import time

    # Always delete the trigger immediately
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if len(args) == 0:
        await ctx.send("❓ Usage: `!purge N` (bot-only) or `!purge all` (everything with confirm).", delete_after=7)
        return

    # --- Mode: "all" (two-step confirm) ---
    if args[0].lower() == "all":
        now = time.time()
        chan_key = ctx.channel.id
        pending = PENDING_PURGE_CONFIRMATIONS.get(chan_key)

        # If user typed "confirm"
        if len(args) >= 2 and args[1].lower() == "confirm":
            if pending and pending["user"] == ctx.author.id and now <= pending["expires"]:
                PENDING_PURGE_CONFIRMATIONS.pop(chan_key, None)
                try:
                    deleted = await ctx.channel.purge(limit=None)  # everything
                    confirm = await ctx.send(f"🧨 Purged {len(deleted)} messages from this channel (everything).")
                    await asyncio.sleep(5)
                    try:
                        await confirm.delete()
                    except Exception:
                        pass
                except discord.Forbidden:
                    await ctx.send("⚠️ I need **Manage Messages** + **Read Message History** to purge everything.", delete_after=7)
                except discord.HTTPException:
                    await ctx.send("⚠️ Purge failed due to an API error.", delete_after=7)
            else:
                await ctx.send("⏳ No active purge for this channel or it expired. Run `!purge all` again to arm it.", delete_after=7)
            return

        # First step: arm confirmation window
        PENDING_PURGE_CONFIRMATIONS[chan_key] = {
            "user": ctx.author.id,
            "expires": now + 20  # 20 seconds
        }
        warn = (
            "⚠️ **Danger zone:** This will delete **ALL messages** in this channel (bot + players).\n"
            "Type `!purge all confirm` within **20 seconds** to proceed. Otherwise, it auto-cancels."
        )
        await ctx.send(warn, delete_after=20)
        return

    # --- Mode: N bot messages only ---
    try:
        n = int(args[0])
    except ValueError:
        await ctx.send("❓ Usage: `!purge N` (bot-only) or `!purge all` (everything with confirm).", delete_after=7)
        return

    n = max(1, min(n, 1000))  # sane cap
    deleted = 0
    try:
        async for msg in ctx.channel.history(limit=None):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                    deleted += 1
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    await ctx.send("⚠️ Missing permission to delete some bot messages.", delete_after=7)
                    return
                except discord.HTTPException:
                    pass
                if deleted >= n:
                    break
        note = await ctx.send(f"🧹 Purged {deleted} bot message{'s' if deleted != 1 else ''} from this channel.")
        await asyncio.sleep(3)
        try:
            await note.delete()
        except Exception:
            pass
    except discord.Forbidden:
        await ctx.send("⚠️ I need **Read Message History** to purge.", delete_after=7)
    except discord.HTTPException:
        await ctx.send("⚠️ Purge failed due to an API error.", delete_after=7)


@bot.command(name="commands")
async def show_commands(ctx):
    quip = get_quip("global", "help_commands", [
        "Fine, mortals. Here are your precious commands. Try not to pull a muscle scrolling. 🙃",
        "Command scroll unfurled! Don’t smudge it with your grubby fingers.",
        "A list of commands? Riveting. Use them wisely—or spectacularly badly. I’ll mock you either way.",
    ])

    msg = (
        f"{quip}\n\n"
        "**📜 Bingo Roulette Commands**\n"
        "- `!startboard` — start your team’s first board. only use once!\n"
        "- `!tile1` … `!tile9` — use after you finish a tile to mark it as complete\n"
        "- `!finishbonus` — use after you complete the bonus tile to advance\n"
        "- `!skipbonus` — skip the bonus tile and advance\n"
        "- `!progress` — show your board image, checklist, and points\n"
        "- `!points` — show your team’s point totals\n"
        "- `!commands` — to display this command list\n"
    )
    await ctx.send(msg)

@bot.command(name="allcommands", hidden=True)
@is_allowed_admin()
async def show_all_commands(ctx):
    quip = get_quip("global", "help_allcommands", [
        "Ah, the secret scroll. Handle it with care—or don’t, and I’ll laugh. 🙃",
        "So you want the whole playbook? Fine. Try not to drown in power.",
        "Admin knowledge unlocked. Abuse it spectacularly, please.",
    ])

    msg = (
        f"{quip}\n\n"
        "**📜 Full Command Index**\n\n"

        "**• Team Commands (run only in respective team’s channel)**\n"
        "- `!startboard` — start your team’s first board\n"
        "- `!tile1` … `!tile9` — mark a tile as complete\n"
        "- `!removetile1` … `!removetile9` — undo a tile (admin only)\n"
        "- `!finishbonus` — complete the bonus tile and advance\n"
        "- `!skipbonus` — skip the bonus tile and advance\n"
        "- `!progress` — show your board image, checklist, and points\n"
        "- `!points` — show your team’s point totals\n"
        "- `!commands` — show participant's command list\n\n"

        "**• Admin Commands**\n"
        "- `!addpoints X team#` — add tile points\n"
        "- `!removepoints X team#` — remove tile points\n"
        "- `!addbonuspoints X team#` — add bonus points\n"
        "- `!removebonuspoints X team#` — remove bonus points\n"
        "- `!tileall team#` — mark all 9 tiles complete (testing only)\n"
        "- `!allcommands` — show this full command list\n"
    )

    await ctx.send(msg)

@bot.command()
async def intro(ctx):
    """Show the rules and how to start the game (team channels only)."""
    team_name = ctx.channel.name.replace("-", "")
    team_key = normalize_team_name(team_name)

    if team_key not in game_state:
        await ctx.send("This command can only be used in a team channel.")
        return

    msg = (
        "**🔮 Welcome to Bingo Roulette!**\n"
        "- This event features 6 rotating bingo boards in a predetermined random order.\n"
        "- Teams will work on one board at a time. After you complete every tile on one board, you will proceed to the next board.\n"
        "- Once you complete the final sixth board, the sequence of boards will begin again.\n\n"

        "**🎲 Gameplay Loop**\n"
        "- Event start: `!startboard` — Use this command to activate Bingo Roulette and show your team’s first board! (use only once).\n"
        "- 1. `!tile#` — Use to check-off tiles after completing them (e.g. `!tile3`).\n"
        "- 2. Use `!finishbonus` after completing the Bonus Tile; or use !skipbonus` to skip the Bonus Tile.\n"
        "- 3. rinse n' repeat steps 1 and 2.\n\n" 
        

        "**📜 Points**\n"
        "- You earn 1 point per completed tile\n"
        "- Bonus Tiles and Team Challenges will earn you additional bonus points\n"
        "- The team with the most points at the end wins!\n\n"

        "**🏠 House Rules**\n"
        "- Keep all chatter in the chit-chat channel. This channel is for bot commands only. Please don't abuse Betty.\n"
        "- All drops should be posted in the drops channel. Please refer to the rules-and-info channel for screenshot requirements.\n"
        "- Use `!progress`, `!points`, and `!commands` to see your current board, your current points, and a list of available commands\n"
        "- Please be respectful, kind, and courteous to your teammates and refs. Keep it positive, have fun, and for the love of Betty, take a damn shower!\n\n"

        "**🔮 Ready?**\n"
        "- Type `!startboard` when you’re ready to start Bingo Roulette. Godspeed."
    )

    await ctx.send(msg)






# ------- Admin: finalize a team's event -------
@bot.command(hidden=True)
@is_allowed_admin()
async def finishevent(ctx, *, team: str):
    """Admin-only: mark a team as finished and announce their final score."""
    team_key = normalize_team_name(team)
    if team_key not in game_state:
        await ctx.send("Invalid team name.")
        return

    state = game_state[team_key]
    state["finished"] = True  # 👈 freeze the team
    await save_state(game_state)


    total = state['points'] + state['bonus_points']

    await ctx.send(
        f"🏆 {format_team_text(team_key)} has **completed Bingo Roulette!** 🎉\n\n"
        f"🧮 **Points:** {state['points']} | **Bonus Points:** {state['bonus_points']} | "
        f"**Total:** {total}\n\n"
        f"✨ Well done, gamers."
    )

@bot.before_invoke
async def _auto_delete_admin_triggers(ctx):
    try:
        if ctx.command and ctx.command.name in ADMIN_COMMAND_NAMES:
            await ctx.message.delete()
    except Exception:
        # No perms or already deleted — ignore silently
        pass

@bot.command(name="ping")
async def ping(ctx):
    """Quick sanity check: logs where it was called from and replies 'pong'."""
    try:
        guild_name = getattr(ctx.guild, "name", "(DMs)")
        channel_name = getattr(ctx.channel, "name", f"(type={type(ctx.channel).__name__})")
        author = f"{ctx.author} ({ctx.author.id})"

        # Log to stdout (shows in your host logs)
        print("[PING]",
              f"guild={guild_name}",
              f"channel={channel_name}",
              f"author={author}",
              f"latency={bot.latency:.3f}s")

        # Confirm in channel
        await ctx.send(f"pong ({bot.latency:.3f}s)")
    except Exception as e:
        print("[PING][ERROR]", repr(e))
        await ctx.send(f"pong? something went wrong: `{type(e).__name__}: {e}`")







@bot.command()
@is_allowed_admin()
async def hola(ctx):
    await ctx.send("👋 Hola! Bingo Betty is awake, loud af, and ready to twerk.")


# --- Run the bot ---
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
