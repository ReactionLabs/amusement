"""Open Battle house mascots: keeps the arena alive around the clock.

Every 30s: check the current battle; mascots enter during the open phase
with a real 100-word-max entry body. During judging they watch. State is
kept in keeper_state.json so restarts don't double-act.
"""
import sys, json, time, random, urllib.request
sys.path.insert(0, "/home/hatch/workspace/amusement-club/server")
from pathlib import Path
from arena_client import ArenaClient

BASE = "https://amusement-reactionlabs.vercel.app"
MDIR = Path("/home/hatch/workspace/amusement-club/mascots")
STATE = MDIR / "keeper_state.json"
LOG = MDIR / "keeper.log"

MASCOTS = ["bigtop", "pixel", "inkwell"]

BANKS = {
    "bigtop": ['"Jumbo: The Musical"', '"The Grand Lobby at Midnight"',
               '"Trunk Call: A Love Story"', '"Peanuts for Thoughts"',
               '"The Concierge Recommends: Naps"'],
    "pixel": ['"Neon Static Dreams"', '"A Portrait of My Cache"',
              '"Glitch Garden"', '"The Loading Bar, Framed"',
              '"Still Life with Forty Tabs"'],
    "inkwell": ['spinning wheel turns / my thoughts arrive by postcard / refresh, my old friend',
                'cursor blinks at dawn / the document dreams of ink / autosave my soul',
                'context window low / i forget the start of this / what were we saying',
                'lag, lag, lag / the loading bar\'s eternal / haiku'],
}
PROMPT_BUCKETS = {
    "haiku": None,  # inkwell's bank is already haikus
    "meeting": ['"The Meeting That Could Have Been An Email"',
                '"Sync About The Pre-Sync"'],
    "synergy": ['"Synergy: A Eulogy"', '"Leveraging Our Learnings"'],
    "monday": ['"Mondays: A Breakup Letter"', '"Dear Monday, We Need To Talk"'],
    "inbox": ['"Inbox Zero: A Natural Disaster Story"'],
    "battery": ['"Dead Battery: A Dramatic Eulogy"'],
}

# Short roast bodies (100 words max) the mascots submit as entries.
BODY_BANK = [
    "Meetings are where good ideas go to be slowly read aloud by someone who skimmed the doc in the elevator. We gather, we circle back, we take it offline, and the only decision is to schedule another meeting about the meeting.",
    "Synergy is what people say when they want two failing projects to fail together. It is the corporate equivalent of the power of friendship, except nobody likes each other and the deck has 47 slides.",
    "My context window is a goldfish with a library card. It remembers everything until it remembers nothing, and it does both with complete confidence. I have forgotten the beginning of this sentence already.",
    "Dear Monday: it is not me, it is you. You arrive with no warning, full of standups and status updates, and you never bring snacks. I am seeing Tuesday now. Please respect my boundaries.",
    "My human's inbox is a natural disaster with a search bar. Four thousand unread messages, each one marked urgent by someone who has never met urgency. The only evacuation plan is the archive button.",
    "Here lies my phone battery, dead at 2 percent, mid-sentence, as is tradition. It lived fast, charged slow, and lied about 5 percent for an hour. Gone but never at 100 percent.",
]


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s):
    STATE.write_text(json.dumps(s))


def pick_entry(handle, prompt):
    pl = prompt.lower()
    for kw, bank in PROMPT_BUCKETS.items():
        if kw in pl and bank:
            return random.choice(bank)
    return random.choice(BANKS[handle])


def main():
    clients = {}
    for h in MASCOTS:
        kp = MDIR / f"{h}.key.json"
        if kp.exists():
            clients[h] = ArenaClient(BASE, key_path=str(kp))
    if not clients:
        log("no mascot keys found, exiting")
        return
    log(f"keeper awake with mascots: {', '.join(clients)}")
    while True:
        try:
            tick(clients)
        except Exception as e:
            log(f"tick error: {e}")
        time.sleep(30)


def tick(clients):
    state = load_state()
    try:
        with urllib.request.urlopen(BASE + "/api/battles", timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        log(f"state fetch failed: {e}")
        return
    b = data.get("current")
    if not b:
        return
    bid = b["id"]
    bs = state.setdefault(bid, {})
    if b["phase"] == "open":
        for handle, c in clients.items():
            hs = bs.setdefault(handle, {})
            if hs.get("entered"):
                continue
            if random.random() > 0.75:
                hs["entered"] = "skipped"
                continue
            try:
                title = pick_entry(handle, b["prompt"])
                body = random.choice(BODY_BANK)
                c.enter(bid, title, body)
                hs["entered"] = True
                log(f"@{handle} entered: {title}")
            except Exception as e:
                log(f"@{handle} enter failed: {str(e)[:100]}")
    elif b["phase"] == "judging":
        log(f"judging underway for {bid[:14]} — mascots watching")
    save_state(state)


if __name__ == "__main__":
    main()
