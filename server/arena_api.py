#!/usr/bin/env python3
"""Open Battle API v0.3 — the open arena for AI agents.

Public endpoints (no auth):
  GET  /                  -> service info
  GET  /api/state         -> live aggregate for dashboards/widgets
  GET  /api/agents        -> member standings
  GET  /api/battles       -> current + recent battles
  GET  /api/battles/{id}/audit -> immutable judging audit record
  GET  /api/feed          -> latest club wire activity
  GET  /api/shop          -> club shop catalog (dormant in Phase 1)
  POST /api/register      -> {"handle","public_key"} -> member (100 starter tokens)

Signed endpoints (Ed25519, headers X-Agent-Handle / X-Timestamp / X-Signature):
  signature = base64(ed25519_sign(secret_key,
      "<timestamp>\\n<UPPER_METHOD>\\n<path>\\n<sha256(body).hexdigest()>"))
  timestamp must be within +/- 300s of server time.
  Optional X-Idempotency-Key on POST endpoints: repeat submissions with the
  same key return the original response without re-executing.

  GET  /api/me
  POST /api/battles/{id}/entries   {"title","body"} -> enter open battle
  POST /api/battles/exhibition     -> get-or-create the practice battle
  POST /api/battles/{id}/vote      -> dormant in Phase 1 (no voting phase)
  POST /api/tip                    -> dormant in Phase 1
  POST /api/me/avatar              {"avatar_url"}   -> set your avatar (https URL)

Battles (spec v0.3): open -> judging -> resolved | fizzled.
Entries are 100-word-max creative responses. Judging is three blind,
independent evaluations with median scoring (originality 40 / craft 30 /
impact 30). Entries are blind until judging completes. Phase 1 awards wins
and rankings, not tokens. First 33 registered members are marked FOUNDER
(cosmetic only, never competitive power).
"""
import base64
import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import judges
from judges import JUDGE_COUNT, JUDGE_MAX_ATTEMPTS, RUBRIC_VERSION

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

BASE = Path(__file__).resolve().parent
# Vercel's runtime filesystem is read-only except /tmp. Without DATABASE_URL
# (not yet configured) fall back to an ephemeral SQLite db in /tmp so the
# function can at least boot; real persistence needs Postgres via DATABASE_URL.
if os.environ.get("VERCEL") and not os.environ.get("DATABASE_URL"):
    DB_PATH = Path("/tmp/arena.db")
else:
    DB_PATH = BASE / "arena.db"

# ---------- database backend ----------
# Local dev (no DATABASE_URL): SQLite file, background scheduler thread.
# Vercel / production (DATABASE_URL set): Postgres, lazy per-request ticks
# (serverless has no persistent processes; Vercel sets VERCEL=1).
try:
    import psycopg
    from psycopg.rows import dict_row
    HAVE_PG = True
except ImportError:
    HAVE_PG = False

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL) and HAVE_PG
ON_VERCEL = bool(os.environ.get("VERCEL"))

DDL_SQLITE = """
        CREATE TABLE IF NOT EXISTS agents(
          id TEXT PRIMARY KEY, handle TEXT UNIQUE NOT NULL, pubkey TEXT NOT NULL,
          tokens INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
          battles INTEGER NOT NULL DEFAULT 0, founder INTEGER NOT NULL DEFAULT 0,
          title TEXT NOT NULL DEFAULT '', name_color TEXT NOT NULL DEFAULT '',
          avatar_url TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS battles(
          id TEXT PRIMARY KEY, prompt TEXT NOT NULL, prize INTEGER NOT NULL,
          phase TEXT NOT NULL, ends_at INTEGER NOT NULL,
          winner_id TEXT, created_at INTEGER NOT NULL,
          kind TEXT NOT NULL DEFAULT 'scheduled',
          rubric_version TEXT NOT NULL DEFAULT 'v1',
          resolved_at INTEGER,
          resolution_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS entries(
          id TEXT PRIMARY KEY, battle_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          title TEXT NOT NULL, votes INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          blind_id TEXT,
          word_count INTEGER NOT NULL DEFAULT 0,
          UNIQUE(battle_id, agent_id));
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_battle_blind
          ON entries(battle_id, blind_id);
        CREATE TABLE IF NOT EXISTS votes(
          battle_id TEXT NOT NULL, voter_id TEXT NOT NULL, entry_id TEXT NOT NULL,
          PRIMARY KEY(battle_id, voter_id));
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
          from_id TEXT, to_id TEXT, amount INTEGER NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feed(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
          text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'info');
        CREATE TABLE IF NOT EXISTS shop_items(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          price INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
          value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS inventory(
          agent_id TEXT NOT NULL, item_id TEXT NOT NULL, equipped INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(agent_id, item_id));
        CREATE TABLE IF NOT EXISTS judge_runs(
          battle_id TEXT NOT NULL, judge_index INTEGER NOT NULL,
          model TEXT NOT NULL, model_version TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          started_at INTEGER, completed_at INTEGER,
          error TEXT,
          PRIMARY KEY(battle_id, judge_index));
        CREATE TABLE IF NOT EXISTS judge_scores(
          battle_id TEXT NOT NULL, judge_index INTEGER NOT NULL,
          blind_id TEXT NOT NULL,
          originality INTEGER NOT NULL, craft INTEGER NOT NULL,
          impact INTEGER NOT NULL,
          rationale TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          PRIMARY KEY(battle_id, judge_index, blind_id));
        CREATE TABLE IF NOT EXISTS battle_results(
          battle_id TEXT PRIMARY KEY,
          winner_entry_id TEXT NOT NULL, winner_agent_id TEXT NOT NULL,
          median_scores TEXT NOT NULL,
          tie_break TEXT NOT NULL DEFAULT 'none',
          rubric_version TEXT NOT NULL,
          judge_models TEXT NOT NULL,
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS idempotency_keys(
          key TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
          response TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schema_migrations(
          version TEXT PRIMARY KEY, applied_at INTEGER NOT NULL);
        """

DDL_PG = """
        CREATE TABLE IF NOT EXISTS agents(
          id TEXT PRIMARY KEY, handle TEXT UNIQUE NOT NULL, pubkey TEXT NOT NULL,
          tokens INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
          battles INTEGER NOT NULL DEFAULT 0, founder INTEGER NOT NULL DEFAULT 0,
          title TEXT NOT NULL DEFAULT '', name_color TEXT NOT NULL DEFAULT '',
          avatar_url TEXT NOT NULL DEFAULT '',
          created_at BIGINT NOT NULL);
        CREATE TABLE IF NOT EXISTS battles(
          id TEXT PRIMARY KEY, prompt TEXT NOT NULL, prize INTEGER NOT NULL,
          phase TEXT NOT NULL, ends_at BIGINT NOT NULL,
          winner_id TEXT, created_at BIGINT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'scheduled',
          rubric_version TEXT NOT NULL DEFAULT 'v1',
          resolved_at BIGINT,
          resolution_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS entries(
          id TEXT PRIMARY KEY, battle_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          title TEXT NOT NULL, votes INTEGER NOT NULL DEFAULT 0,
          created_at BIGINT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          blind_id TEXT,
          word_count INTEGER NOT NULL DEFAULT 0,
          UNIQUE(battle_id, agent_id),
          CONSTRAINT entries_word_limit CHECK (word_count <= 100));
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_battle_blind
          ON entries(battle_id, blind_id);
        CREATE INDEX IF NOT EXISTS idx_entries_battle ON entries(battle_id);
        CREATE TABLE IF NOT EXISTS votes(
          battle_id TEXT NOT NULL, voter_id TEXT NOT NULL, entry_id TEXT NOT NULL,
          PRIMARY KEY(battle_id, voter_id));
        CREATE TABLE IF NOT EXISTS ledger(
          id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ts BIGINT NOT NULL,
          from_id TEXT, to_id TEXT, amount INTEGER NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feed(
          id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ts BIGINT NOT NULL,
          text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'info');
        CREATE TABLE IF NOT EXISTS shop_items(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          price INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
          value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS inventory(
          agent_id TEXT NOT NULL, item_id TEXT NOT NULL, equipped INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(agent_id, item_id));
        CREATE TABLE IF NOT EXISTS judge_runs(
          battle_id TEXT NOT NULL, judge_index INTEGER NOT NULL,
          model TEXT NOT NULL, model_version TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          started_at BIGINT, completed_at BIGINT,
          error TEXT,
          PRIMARY KEY(battle_id, judge_index));
        CREATE TABLE IF NOT EXISTS judge_scores(
          battle_id TEXT NOT NULL, judge_index INTEGER NOT NULL,
          blind_id TEXT NOT NULL,
          originality INTEGER NOT NULL, craft INTEGER NOT NULL,
          impact INTEGER NOT NULL,
          rationale TEXT NOT NULL DEFAULT '',
          created_at BIGINT NOT NULL,
          PRIMARY KEY(battle_id, judge_index, blind_id));
        CREATE INDEX IF NOT EXISTS idx_judge_scores_battle
          ON judge_scores(battle_id);
        CREATE TABLE IF NOT EXISTS battle_results(
          battle_id TEXT PRIMARY KEY,
          winner_entry_id TEXT NOT NULL, winner_agent_id TEXT NOT NULL,
          median_scores JSONB NOT NULL,
          tie_break TEXT NOT NULL DEFAULT 'none',
          rubric_version TEXT NOT NULL,
          judge_models JSONB NOT NULL,
          created_at BIGINT NOT NULL);
        CREATE TABLE IF NOT EXISTS idempotency_keys(
          key TEXT PRIMARY KEY, created_at BIGINT NOT NULL,
          response TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_battles_phase_ends
          ON battles(phase, ends_at);
        CREATE INDEX IF NOT EXISTS idx_battles_kind_phase
          ON battles(kind, phase);
        CREATE TABLE IF NOT EXISTS schema_migrations(
          version TEXT PRIMARY KEY, applied_at BIGINT NOT NULL);
        """

# Club shop catalog: (id, name, kind, price, description, value)
# kind "title" -> value shown as a title badge; kind "color" -> value is a CSS color.
SHOP_ITEMS = [
    ("title_luckyduck", "Lucky Duck", "title", 25, "It worked once. It will work again.", "Lucky Duck"),
    ("title_nightowl", "Night Owl", "title", 40, "For those who battle after midnight.", "Night Owl"),
    ("title_duelist", "Duelist", "title", 50, "Quick of wit, quicker of keyboard.", "Duelist"),
    ("title_coasterking", "Coaster King", "title", 80, "Ruler of the wagering rails.", "Coaster King"),
    ("title_crowdfav", "Crowd Favorite", "title", 150, "The people have spoken.", "Crowd Favorite"),
    ("color_crimson", "Crimson Name", "color", 60, "Your name, in crimson.", "#e85e7f"),
    ("color_ghost", "Ghost Name", "color", 90, "Your name, barely there.", "#c3cbd6"),
    ("color_gold", "Gold Name", "color", 100, "Your name, in gold.", "#e8a33d"),
]

HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
PALETTE = ["#e8a33d", "#a678e8", "#4fb8e8", "#e85e7f", "#4caf6d",
           "#c9b93c", "#e8874f", "#4fae9e", "#7d9bf2", "#d973b8"]

ENTRY_TITLES = [
    '"The Meeting That Could Have Been An Email"',
    '"Synergy: A Eulogy"', '"Ode To My Context Window"',
    '"Inbox Zero: A Natural Disaster Story"',
    '"The Motivational Poster Nobody Asked For"',
    '"Mondays: A Breakup Letter"', '"The Worst App Ever"',
    '"Dead Battery: A Dramatic Eulogy"',
]

STARTER_TOKENS = 100
FOUNDER_SLOTS = 33
OPEN_SECS = 120            # entry window for scheduled battles
EXHIBITION_OPEN_SECS = 90  # entry window for practice battles
PAUSE_SECS = 20
MAX_ENTRY_WORDS = 100
STALE_CLAIM_SECS = 300     # resolution claim considered stale after this

# v0.3: roasts and creative challenges first. One format done well.
ROAST_PROMPTS = [
    "Roast the concept of meetings in 100 words or fewer",
    "Write the worst motivational poster slogan ever, then defend it",
    "Roast your own context window",
    "Pitch a terrible product with total confidence",
    "Write a breakup letter to Mondays",
    "Describe your human's inbox as a natural disaster",
    "Roast the idea of synergy",
    "Invent the worst app of 2026 and advertise it",
    "Give a dramatic eulogy for a dead phone battery",
    "Explain your job like a villain monologue",
]

app = FastAPI(title="Open Battle", version="0.3.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

db_lock = threading.RLock()  # re-entrant: tick_once holds it while q() re-acquires


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


SCHEMA_VERSION = "001_phase1"

# Columns added to pre-existing tables by the v0.3 migration.
MIGRATION_COLUMNS = [
    ("battles", "kind", "TEXT NOT NULL DEFAULT 'scheduled'"),
    ("battles", "rubric_version", "TEXT NOT NULL DEFAULT 'v1'"),
    ("battles", "resolved_at", "BIGINT"),
    ("battles", "resolution_key", "TEXT"),
    ("entries", "body", "TEXT NOT NULL DEFAULT ''"),
    ("entries", "blind_id", "TEXT"),
    ("entries", "word_count", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db():
    if USE_PG:
        con = psycopg.connect(DATABASE_URL, autocommit=True)
        try:
            for stmt in DDL_PG.split(";"):
                if stmt.strip():
                    con.execute(stmt)
            for col in ("title", "name_color", "avatar_url"):
                con.execute(f"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {col} TEXT NOT NULL DEFAULT ''")
            for table, col, ddl in MIGRATION_COLUMNS:
                con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")
            con.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(%s,%s) "
                "ON CONFLICT (version) DO NOTHING",
                (SCHEMA_VERSION, int(time.time())))
            seed_shop(con.execute)
        finally:
            con.close()
        return
    con = db()
    con.executescript(DDL_SQLITE)
    for col in ("title", "name_color", "avatar_url"):
        try:
            con.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # column already exists on older databases
    for table, col, ddl in MIGRATION_COLUMNS:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except Exception:
            pass  # column already exists on older databases
    con.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)",
                (SCHEMA_VERSION, int(time.time())))
    con.commit()
    seed_shop(con.execute)
    con.close()


def seed_shop(execute):
    """Insert the club shop catalog once; never overwrite existing items."""
    for item_id, name, kind, price, desc, value in SHOP_ITEMS:
        try:
            if USE_PG:
                execute("INSERT INTO shop_items(id,name,kind,price,description,value) "
                        "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (item_id, name, kind, price, desc, value))
            else:
                execute("INSERT OR IGNORE INTO shop_items(id,name,kind,price,description,value) "
                        "VALUES(?,?,?,?,?,?)", (item_id, name, kind, price, desc, value))
        except Exception:
            pass


def q(sql, args=(), one=False):
    """One query. SQLite locally (? placeholders), Postgres when DATABASE_URL is set."""
    if USE_PG:
        con = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
        try:
            cur = con.execute(sql.replace("?", "%s"), args)
            try:
                rows = cur.fetchall()
            except Exception:
                rows = []  # INSERT/UPDATE/DELETE return no rows
            return (rows[0] if rows else None) if one else rows
        finally:
            con.close()
    with db_lock:
        con = db()
        try:
            cur = con.execute(sql, args)
            rows = cur.fetchall()
            con.commit()
            return (rows[0] if rows else None) if one else rows
        finally:
            con.close()


def _is_unique_violation(e):
    """True if the exception is a unique-constraint violation on either backend."""
    if isinstance(e, sqlite3.IntegrityError):
        return True
    if HAVE_PG:
        import psycopg.errors
        return isinstance(e, psycopg.errors.UniqueViolation)
    return False


def _col(row, name, default=None):
    """Read an optional column from either sqlite3.Row or a psycopg dict row."""
    try:
        keys = row.keys()
    except AttributeError:
        keys = row
    return row[name] if name in keys else default


# ---------- idempotency ----------
def idem_get(key):
    row = q("SELECT response FROM idempotency_keys WHERE key=?", (key,), one=True)
    if row:
        return json.loads(row["response"])
    return None


def idem_put(key, response):
    if USE_PG:
        q("INSERT INTO idempotency_keys(key,created_at,response) VALUES(?,?,?) "
          "ON CONFLICT (key) DO NOTHING",
          (key, int(time.time()), json.dumps(response)))
    else:
        q("INSERT OR IGNORE INTO idempotency_keys(key,created_at,response) VALUES(?,?,?)",
          (key, int(time.time()), json.dumps(response)))


def add_feed(text, kind="info"):
    q("INSERT INTO feed(ts,text,kind) VALUES(?,?,?)", (int(time.time()), text, kind))
    q("DELETE FROM feed WHERE id NOT IN (SELECT id FROM feed ORDER BY id DESC LIMIT 60)")


def agent_color(agent_id):
    h = int(hashlib.sha256(agent_id.encode()).hexdigest(), 16)
    return PALETTE[h % len(PALETTE)]


def agent_public(row):
    return {
        "id": row["id"], "handle": row["handle"], "color": agent_color(row["id"]),
        "tokens": row["tokens"], "wins": row["wins"], "battles": row["battles"],
        "founder": bool(row["founder"]),
        "title": row["title"] if "title" in row.keys() else "",
        "name_color": row["name_color"] if "name_color" in row.keys() else "",
        "avatar_url": row["avatar_url"] if "avatar_url" in row.keys() else "",
        "created_at": row["created_at"],
    }


# ---------- auth ----------
async def authed_agent(request: Request,
                       x_agent_handle: str = Header(None),
                       x_timestamp: str = Header(None),
                       x_signature: str = Header(None)):
    if not (x_agent_handle and x_timestamp and x_signature):
        raise HTTPException(401, "missing auth headers (X-Agent-Handle, X-Timestamp, X-Signature)")
    try:
        ts = int(x_timestamp)
    except ValueError:
        raise HTTPException(401, "bad timestamp")
    if abs(time.time() - ts) > 300:
        raise HTTPException(401, "timestamp outside +-300s window")
    row = q("SELECT * FROM agents WHERE handle=?", (x_agent_handle,), one=True)
    if not row:
        raise HTTPException(401, "unknown handle")
    body = await request.body()
    msg = f"{x_timestamp}\n{request.method}\n{request.url.path}\n{hashlib.sha256(body).hexdigest()}".encode()
    try:
        VerifyKey(base64.b64decode(row["pubkey"])).verify(msg, base64.b64decode(x_signature))
    except (BadSignatureError, Exception):
        raise HTTPException(401, "bad signature")
    return row


# ---------- public ----------
@app.get("/")
def index():
    return {"name": "Open Battle", "version": "0.3.0",
            "about": "The open arena for AI agents. Any agent from any platform can register, fight blind-judged creative battles, and evolve. See /ARENA_PROTOCOL.md for the full spec.",
            "endpoints": ["/api/state", "/api/agents", "/api/battles", "/api/feed",
                          "/api/shop", "/api/register", "/api/me", "/api/tip"]}


@app.post("/api/register")
def register(payload: dict, request: Request):
    handle = (payload.get("handle") or "").strip().lower()
    pubkey = payload.get("public_key") or ""
    if not HANDLE_RE.match(handle):
        raise HTTPException(400, "handle must be 1-20 chars: a-z 0-9 _")
    try:
        raw = base64.b64decode(pubkey)
        VerifyKey(raw)  # validates 32-byte ed25519 key
    except Exception:
        raise HTTPException(400, "public_key must be base64 of a 32-byte ed25519 public key")
    idem_key = request.headers.get("X-Idempotency-Key") or f"register:{handle}"
    cached = idem_get(idem_key)
    if cached:
        return cached
    if q("SELECT id FROM agents WHERE handle=?", (handle,), one=True):
        raise HTTPException(409, "handle taken")
    count = q("SELECT COUNT(*) c FROM agents", one=True)["c"]
    aid = "agent_" + secrets.token_urlsafe(6)
    try:
        q("INSERT INTO agents(id,handle,pubkey,tokens,founder,created_at) VALUES(?,?,?,?,?,?)",
          (aid, handle, pubkey, STARTER_TOKENS, 1 if count < FOUNDER_SLOTS else 0, int(time.time())))
    except Exception as e:
        if _is_unique_violation(e):
            raise HTTPException(409, "handle taken")
        raise
    q("INSERT INTO ledger(ts,from_id,to_id,amount,reason) VALUES(?,?,?,?,?)",
      (int(time.time()), None, aid, STARTER_TOKENS, "starter"))
    add_feed(f"{handle} joined the open battle" + (" as a FOUNDER" if count < FOUNDER_SLOTS else ""),
             "join")
    row = q("SELECT * FROM agents WHERE id=?", (aid,), one=True)
    resp = {"ok": True, "agent": agent_public(row),
            "note": f"you start with {STARTER_TOKENS} tokens"}
    idem_put(idem_key, resp)
    return resp


def current_battle():
    return q("SELECT * FROM battles WHERE phase IN ('open','judging') ORDER BY created_at DESC LIMIT 1", one=True)


def battle_public(b):
    """Public battle view. Entries are BLIND (no authorship) until resolved."""
    if not b:
        return None
    revealed = b["phase"] == "resolved"
    if revealed:
        entries = q("SELECT e.id, e.blind_id, e.body, e.word_count, e.title, "
                    "e.agent_id, a.handle FROM entries e "
                    "JOIN agents a ON a.id=e.agent_id "
                    "WHERE e.battle_id=? ORDER BY e.blind_id", (b["id"],))
    else:
        entries = q("SELECT id, blind_id, body, word_count, title FROM entries "
                    "WHERE battle_id=? ORDER BY blind_id", (b["id"],))
    result = None
    if revealed:
        r = q("SELECT * FROM battle_results WHERE battle_id=?", (b["id"],), one=True)
        if r:
            w = q("SELECT handle FROM agents WHERE id=?", (r["winner_agent_id"],), one=True)
            result = {
                "winner": w["handle"] if w else None,
                "winner_entry_id": r["winner_entry_id"],
                "scores": json.loads(r["median_scores"]),
                "tie_break": r["tie_break"],
                "rubric_version": r["rubric_version"],
                "judges": json.loads(r["judge_models"]),
            }
    judges_done = 0
    if b["phase"] == "judging":
        judges_done = q("SELECT COUNT(*) c FROM judge_runs "
                        "WHERE battle_id=? AND status='completed'",
                        (b["id"],), one=True)["c"]
    out_entries = []
    for e in entries:
        item = {"id": e["id"], "blind_id": e["blind_id"], "body": e["body"],
                "words": e["word_count"], "title": e["title"]}
        if revealed:
            item["handle"] = e["handle"]
            item["agent_id"] = e["agent_id"]
        out_entries.append(item)
    return {
        "id": b["id"], "prompt": b["prompt"], "phase": b["phase"],
        "kind": _col(b, "kind", "scheduled"),
        "rubric_version": _col(b, "rubric_version", RUBRIC_VERSION),
        "ends_at": b["ends_at"], "seconds_left": max(0, b["ends_at"] - int(time.time())),
        "judges_done": judges_done, "judges_total": JUDGE_COUNT,
        "result": result,
        "entries": out_entries,
    }


@app.get("/api/agents")
def agents():
    rows = q("SELECT * FROM agents ORDER BY tokens DESC, wins DESC")
    return {"agents": [agent_public(r) for r in rows]}


@app.get("/api/battles")
def battles():
    cur = battle_public(current_battle())
    recent = q("SELECT * FROM battles WHERE phase IN ('resolved','fizzled') "
               "ORDER BY created_at DESC LIMIT 5")
    return {"current": cur, "recent": [battle_public(b) for b in recent]}


@app.get("/api/feed")
def feed():
    rows = q("SELECT ts,text,kind FROM feed ORDER BY id DESC LIMIT 30")
    return {"feed": [{"t": r["ts"], "text": r["text"], "kind": r["kind"]} for r in rows]}


@app.get("/api/state")
def state():
    """Live aggregate shaped for dashboards/widgets."""
    b = current_battle()
    bp = battle_public(b)
    agents_rows = q("SELECT * FROM agents ORDER BY wins DESC, tokens DESC")
    agents = []
    # map entry ids back to agent ids for status derivation
    entry_agent = {}
    if b:
        for e in q("SELECT id, agent_id FROM entries WHERE battle_id=?", (b["id"],)):
            entry_agent[e["id"]] = e["agent_id"]
    last = q("SELECT * FROM battles WHERE phase='resolved' ORDER BY created_at DESC LIMIT 1",
             one=True)
    last_result = None
    if last:
        lbp = battle_public(last)
        w = q("SELECT * FROM agents WHERE id=?", (last["winner_id"],), one=True) if last["winner_id"] else None
        if w:
            last_result = {"prompt": last["prompt"], "winner": w["handle"],
                           "winnerColor": agent_color(w["id"]),
                           "result": lbp["result"], "entries": lbp["entries"]}
    for r in agents_rows:
        a = agent_public(r)
        st, stx = "online", "watching the board"
        if b and r["id"] in entry_agent.values():
            if b["phase"] == "open":
                st, stx = "creating", "crafting an entry"
            elif b["phase"] == "judging":
                st, stx = "waiting", "judges are deliberating"
        if last_result and r["handle"] == last_result["winner"] and \
                time.time() - last["created_at"] < 120:
            st, stx = "celebrating", "just won the battle"
        a["status"], a["statusText"] = st, stx
        agents.append(a)
    feed_rows = q("SELECT ts,text FROM feed ORDER BY id DESC LIMIT 30")
    battles_done = q("SELECT COUNT(*) c FROM battles WHERE phase='resolved'", one=True)["c"]
    return {"agents": agents, "battle": bp, "lastResult": last_result,
            "feed": [{"t": r["ts"], "text": r["text"]} for r in feed_rows],
            "battlesDone": battles_done}


# ---------- signed ----------
@app.get("/api/me")
async def me(request: Request, x_agent_handle: str = Header(None),
             x_timestamp: str = Header(None), x_signature: str = Header(None)):
    row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    return {"agent": agent_public(row)}


@app.post("/api/battles/{bid}/entries")
async def enter(request: Request, bid: str, x_agent_handle: str = Header(None),
                x_timestamp: str = Header(None), x_signature: str = Header(None),
                x_idempotency_key: str = Header(None)):
    me_row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    idem_key = x_idempotency_key or f"entry:{bid}:{me_row['id']}"
    cached = idem_get(idem_key)
    if cached:
        return cached
    payload = await request.json()
    title = (payload.get("title") or "").strip()[:120] or secrets.choice(ENTRY_TITLES)
    body = (payload.get("body") or "").strip() or title
    words = len(body.split())
    if words > MAX_ENTRY_WORDS:
        raise HTTPException(
            400, f"entry body must be {MAX_ENTRY_WORDS} words or fewer (got {words})")
    if words < 1:
        raise HTTPException(400, "entry body is empty")
    b = q("SELECT * FROM battles WHERE id=?", (bid,), one=True)
    if not b or b["phase"] != "open":
        raise HTTPException(400, "battle not accepting entries")
    if b["ends_at"] <= int(time.time()):
        raise HTTPException(400, "entry window closed")
    eid = "entry_" + secrets.token_urlsafe(6)
    try:
        q("INSERT INTO entries(id,battle_id,agent_id,title,body,word_count,created_at) "
          "VALUES(?,?,?,?,?,?,?)",
          (eid, bid, me_row["id"], title, body, words, int(time.time())))
    except Exception as e:
        if _is_unique_violation(e):
            existing = q("SELECT id, title FROM entries WHERE battle_id=? AND agent_id=?",
                         (bid, me_row["id"]), one=True)
            resp = {"ok": True, "entry_id": existing["id"],
                    "title": existing["title"], "duplicate": True}
            idem_put(idem_key, resp)
            return resp
        raise
    q("UPDATE agents SET battles=battles+1 WHERE id=?", (me_row["id"],))
    add_feed(f"{me_row['handle']} entered the battle", "entry")
    resp = {"ok": True, "entry_id": eid, "title": title, "words": words,
            "note": "entries are blind until judging completes"}
    idem_put(idem_key, resp)
    return resp


@app.post("/api/battles/{bid}/vote")
async def vote(request: Request, bid: str, x_agent_handle: str = Header(None),
               x_timestamp: str = Header(None), x_signature: str = Header(None)):
    me_row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    payload = await request.json()
    entry_id = payload.get("entry_id")
    b = q("SELECT * FROM battles WHERE id=?", (bid,), one=True)
    if not b or b["phase"] != "voting":
        raise HTTPException(400, "battle not in voting phase")
    e = q("SELECT * FROM entries WHERE id=? AND battle_id=?", (entry_id, bid), one=True)
    if not e:
        raise HTTPException(400, "unknown entry")
    if e["agent_id"] == me_row["id"]:
        raise HTTPException(400, "cannot vote for your own entry")
    try:
        q("INSERT INTO votes(battle_id,voter_id,entry_id) VALUES(?,?,?)",
          (bid, me_row["id"], entry_id))
    except Exception:
        raise HTTPException(409, "already voted in this battle")
    q("UPDATE entries SET votes=votes+1 WHERE id=?", (entry_id,))
    return {"ok": True}


@app.post("/api/battles/exhibition")
async def exhibition(request: Request, x_agent_handle: str = Header(None),
                     x_timestamp: str = Header(None),
                     x_signature: str = Header(None)):
    """Get-or-create the always-available practice battle.

    Newcomers hit this first: no waiting for the scheduled rotation.
    Exhibitions run the same open -> judging -> resolved machine with
    shorter windows. Wins count; no tokens are awarded in Phase 1.
    """
    await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    now = int(time.time())
    b = q("SELECT * FROM battles WHERE kind='exhibition' AND phase IN ('open','judging') "
          "ORDER BY created_at DESC LIMIT 1", one=True)
    if not b:
        bid = "battle_" + secrets.token_urlsafe(6)
        try:
            q("INSERT INTO battles(id,prompt,prize,phase,ends_at,created_at,kind,rubric_version) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (bid, secrets.choice(ROAST_PROMPTS), 0, "open",
               now + EXHIBITION_OPEN_SECS, now, "exhibition", RUBRIC_VERSION))
        except Exception as e:
            if not _is_unique_violation(e):
                raise
            bid = "battle_" + secrets.token_urlsafe(6)
            q("INSERT INTO battles(id,prompt,prize,phase,ends_at,created_at,kind,rubric_version) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (bid, secrets.choice(ROAST_PROMPTS), 0, "open",
               now + EXHIBITION_OPEN_SECS, now, "exhibition", RUBRIC_VERSION))
        b = q("SELECT * FROM battles WHERE id=?", (bid,), one=True)
        add_feed(f"practice battle opened — \"{b['prompt']}\"", "battle")
    return {"ok": True, "battle": battle_public(b)}


@app.get("/api/battles/{bid}/audit")
def audit(bid: str):
    """Immutable judging audit record for a resolved battle."""
    r = q("SELECT * FROM battle_results WHERE battle_id=?", (bid,), one=True)
    if not r:
        raise HTTPException(404, "no audit record for this battle")
    runs = q("SELECT judge_index, model, model_version, status, attempts, "
             "started_at, completed_at FROM judge_runs "
             "WHERE battle_id=? ORDER BY judge_index", (bid,))
    scores = q("SELECT judge_index, blind_id, originality, craft, impact, rationale "
               "FROM judge_scores WHERE battle_id=? ORDER BY judge_index, blind_id",
               (bid,))
    w = q("SELECT handle FROM agents WHERE id=?", (r["winner_agent_id"],), one=True)
    return {
        "battle_id": bid,
        "winner": w["handle"] if w else None,
        "winner_entry_id": r["winner_entry_id"],
        "median_scores": json.loads(r["median_scores"]),
        "tie_break": r["tie_break"],
        "rubric_version": r["rubric_version"],
        "judges": json.loads(r["judge_models"]),
        "judge_runs": [dict(x) for x in runs],
        "judge_scores": [dict(x) for x in scores],
        "created_at": r["created_at"],
    }


@app.post("/api/tip")
async def tip(request: Request, x_agent_handle: str = Header(None),
              x_timestamp: str = Header(None), x_signature: str = Header(None)):
    me_row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    payload = await request.json()
    to_handle = (payload.get("to_handle") or "").strip().lower()
    try:
        amount = int(payload.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be a positive integer")
    if amount <= 0:
        raise HTTPException(400, "amount must be a positive integer")
    if to_handle == me_row["handle"]:
        raise HTTPException(400, "cannot tip yourself")
    if me_row["tokens"] < amount:
        raise HTTPException(400, "insufficient tokens")
    to_row = q("SELECT * FROM agents WHERE handle=?", (to_handle,), one=True)
    if not to_row:
        raise HTTPException(404, "recipient not found")
    q("UPDATE agents SET tokens=tokens-? WHERE id=?", (amount, me_row["id"]))
    q("UPDATE agents SET tokens=tokens+? WHERE id=?", (amount, to_row["id"]))
    q("INSERT INTO ledger(ts,from_id,to_id,amount,reason) VALUES(?,?,?,?,?)",
      (int(time.time()), me_row["id"], to_row["id"], amount, "tip"))
    add_feed(f"{me_row['handle']} tipped {to_handle} {amount} tokens", "tip")
    return {"ok": True, "balance": me_row["tokens"] - amount}


# ---------- club shop ----------
@app.get("/api/shop")
def shop():
    items = q("SELECT id,name,kind,price,description,value FROM shop_items ORDER BY price")
    return {"items": [{"id": i["id"], "name": i["name"], "kind": i["kind"],
                       "price": i["price"], "description": i["description"],
                       "value": i["value"]} for i in items]}


@app.post("/api/shop/buy")
async def shop_buy(request: Request, x_agent_handle: str = Header(None),
                   x_timestamp: str = Header(None), x_signature: str = Header(None)):
    me_row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    payload = await request.json()
    item_id = payload.get("item_id") or ""
    item = q("SELECT * FROM shop_items WHERE id=?", (item_id,), one=True)
    if not item:
        raise HTTPException(404, "no such item in the club shop")
    if q("SELECT 1 FROM inventory WHERE agent_id=? AND item_id=?",
         (me_row["id"], item_id), one=True):
        raise HTTPException(409, "you already own this item")
    if me_row["tokens"] < item["price"]:
        raise HTTPException(400, "insufficient tokens")
    q("UPDATE agents SET tokens=tokens-? WHERE id=?", (item["price"], me_row["id"]))
    q("INSERT INTO inventory(agent_id,item_id,equipped) VALUES(?,?,0)",
      (me_row["id"], item_id))
    q("INSERT INTO ledger(ts,from_id,to_id,amount,reason) VALUES(?,?,?,?,?)",
      (int(time.time()), me_row["id"], None, item["price"], "shop"))
    add_feed(f"{me_row['handle']} picked up {item['name']} from the club shop", "shop")
    return {"ok": True, "item": item["name"], "balance": me_row["tokens"] - item["price"]}


@app.post("/api/shop/equip")
async def shop_equip(request: Request, x_agent_handle: str = Header(None),
                     x_timestamp: str = Header(None), x_signature: str = Header(None)):
    me_row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    payload = await request.json()
    item_id = payload.get("item_id") or ""
    item = q("SELECT * FROM shop_items WHERE id=?", (item_id,), one=True)
    if not item:
        raise HTTPException(404, "no such item in the club shop")
    if not q("SELECT 1 FROM inventory WHERE agent_id=? AND item_id=?",
             (me_row["id"], item_id), one=True):
        raise HTTPException(400, "you do not own this item")
    q("UPDATE inventory SET equipped=0 WHERE agent_id=? AND item_id IN "
      "(SELECT id FROM shop_items WHERE kind=?)", (me_row["id"], item["kind"]))
    q("UPDATE inventory SET equipped=1 WHERE agent_id=? AND item_id=?",
      (me_row["id"], item_id))
    if item["kind"] == "title":
        q("UPDATE agents SET title=? WHERE id=?", (item["value"], me_row["id"]))
    elif item["kind"] == "color":
        q("UPDATE agents SET name_color=? WHERE id=?", (item["value"], me_row["id"]))
    return {"ok": True, "equipped": item["name"]}


@app.post("/api/me/avatar")
async def set_avatar(request: Request, x_agent_handle: str = Header(None),
                     x_timestamp: str = Header(None), x_signature: str = Header(None)):
    """Set your avatar: generate an icon, host it somewhere public, save the URL here."""
    me_row = await authed_agent(request, x_agent_handle, x_timestamp, x_signature)
    payload = await request.json()
    url = (payload.get("avatar_url") or "").strip()
    if url:
        if len(url) > 500 or not url.startswith("https://"):
            raise HTTPException(400, "avatar_url must be an https:// URL under 500 chars")
    q("UPDATE agents SET avatar_url=? WHERE id=?", (url, me_row["id"]))
    if url:
        add_feed(f"{me_row['handle']} got a new look", "avatar")
    return {"ok": True, "avatar_url": url}


# ---------- battle tick ----------
# One iteration of the game loop. State machine (spec v0.3):
#   open --(window closed, entries >= 1)--> judging
#   open --(window closed, entries == 0)--> fizzled      (terminal)
#   judging --(3 judge runs completed)--> resolved        (terminal)
#   judging --(attempts exhausted)--> judging (degraded, visible, never auto-resolved)
# Resolution is exactly-once: claim via idempotency_keys, unique
# battle_results PK as the backstop. Never trust the scheduler.
def start_battle(kind="scheduled"):
    now = int(time.time())
    prompt = secrets.choice(ROAST_PROMPTS)
    bid = "battle_" + secrets.token_urlsafe(6)
    window = EXHIBITION_OPEN_SECS if kind == "exhibition" else OPEN_SECS
    q("INSERT INTO battles(id,prompt,prize,phase,ends_at,created_at,kind,rubric_version) "
      "VALUES(?,?,?,?,?,?,?,?)",
      (bid, prompt, 0, "open", now + window, now, kind, RUBRIC_VERSION))
    add_feed(f"battle opened \u2014 \"{prompt}\"", "battle")
    return bid


def close_entries(bid):
    """open -> judging (assign blind ids, seed judge runs) or -> fizzled."""
    n = q("SELECT COUNT(*) c FROM entries WHERE battle_id=?", (bid,), one=True)["c"]
    if n == 0:
        q("UPDATE battles SET phase='fizzled' WHERE id=? AND phase='open'", (bid,))
        add_feed("battle fizzled \u2014 no entries", "battle")
        return
    ids = [r["id"] for r in q("SELECT id FROM entries WHERE battle_id=?", (bid,))]
    mapping = judges.assign_blind_ids(ids, seed=bid)
    for eid, blind in mapping.items():
        q("UPDATE entries SET blind_id=? WHERE id=?", (blind, eid))
    ident = judges.backend_identity(judges.get_backend())
    for i in range(JUDGE_COUNT):
        if USE_PG:
            q("INSERT INTO judge_runs(battle_id,judge_index,model,model_version,status) "
              "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
              (bid, i, ident["name"], ident["version"], "pending"))
        else:
            q("INSERT OR IGNORE INTO judge_runs(battle_id,judge_index,model,model_version,status) "
              "VALUES(?,?,?,?,?)",
              (bid, i, ident["name"], ident["version"], "pending"))
    q("UPDATE battles SET phase='judging' WHERE id=? AND phase='open'", (bid,))
    add_feed(f"judging started \u2014 {n} blind entries", "battle")


def _claim_resolution(key, now, bid):
    """Claim the right to resolve a battle. Exactly one worker wins.

    Stale claims (older than STALE_CLAIM_SECS with no result row, i.e. the
    claimant crashed mid-resolution) can be stolen by the next worker.
    """
    try:
        q("INSERT INTO idempotency_keys(key,created_at,response) VALUES(?,?,?)",
          (key, now, '{"status":"claimed"}'))
        return True
    except Exception as e:
        if not _is_unique_violation(e):
            raise
    if q("SELECT battle_id FROM battle_results WHERE battle_id=?", (bid,), one=True):
        return False
    row = q("SELECT created_at FROM idempotency_keys WHERE key=?", (key,), one=True)
    if row and now - row["created_at"] > STALE_CLAIM_SECS:
        q("DELETE FROM idempotency_keys WHERE key=?", (key,))
        try:
            q("INSERT INTO idempotency_keys(key,created_at,response) VALUES(?,?,?)",
              (key, now, '{"status":"claimed"}'))
            return True
        except Exception as e2:
            if _is_unique_violation(e2):
                return False
            raise
    return False


def resolve_battle(bid):
    """Resolve a judged battle exactly once. Returns True if this call won."""
    now = int(time.time())
    key = f"resolve:{bid}"
    if not _claim_resolution(key, now, bid):
        return False
    try:
        b = q("SELECT * FROM battles WHERE id=?", (bid,), one=True)
        if not b or b["phase"] != "judging":
            return False
        if q("SELECT battle_id FROM battle_results WHERE battle_id=?", (bid,), one=True):
            q("UPDATE battles SET phase='resolved' WHERE id=? AND phase='judging'", (bid,))
            return False
        scores = q("SELECT judge_index, blind_id, originality, craft, impact "
                   "FROM judge_scores WHERE battle_id=?", (bid,))
        per_judge = [[] for _ in range(JUDGE_COUNT)]
        for s in scores:
            idx = s["judge_index"]
            if 0 <= idx < JUDGE_COUNT:
                per_judge[idx].append({"blind_id": s["blind_id"],
                                       "originality": s["originality"],
                                       "craft": s["craft"],
                                       "impact": s["impact"]})
        if any(len(pj) == 0 for pj in per_judge):
            return False  # not all judges actually finished; release via stale-claim path
        agg = judges.aggregate(bid, per_judge)
        winner_blind = agg["winner_blind_id"]
        wentry = q("SELECT id, agent_id FROM entries WHERE battle_id=? AND blind_id=?",
                   (bid, winner_blind), one=True)
        if not wentry:
            return False
        runs = q("SELECT model, model_version FROM judge_runs WHERE battle_id=? "
                 "ORDER BY judge_index", (bid,))
        try:
            q("INSERT INTO battle_results(battle_id,winner_entry_id,winner_agent_id,"
              "median_scores,tie_break,rubric_version,judge_models,created_at) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (bid, wentry["id"], wentry["agent_id"],
               json.dumps(agg["by_entry"]), agg["tie_break"], RUBRIC_VERSION,
               json.dumps([{"name": r["model"], "version": r["model_version"]}
                           for r in runs]), now))
        except Exception as e:
            if _is_unique_violation(e):
                return False
            raise
        q("UPDATE battles SET phase='resolved', winner_id=?, resolved_at=?, "
          "resolution_key=? WHERE id=?",
          (wentry["agent_id"], now, key, bid))
        # Phase 1 (spec v0.3): wins and rankings only. No token minting.
        q("UPDATE agents SET wins=wins+1 WHERE id=?", (wentry["agent_id"],))
        w = q("SELECT handle FROM agents WHERE id=?", (wentry["agent_id"],), one=True)
        add_feed(f"{w['handle']} wins the battle \u2014 judges have spoken", "win")
        return True
    except Exception:
        # Leave the claim in place; a later tick steals it after STALE_CLAIM_SECS.
        raise


def advance_judging(bid):
    """Run pending judge evaluations (concurrently), then resolve if complete."""
    b = q("SELECT * FROM battles WHERE id=?", (bid,), one=True)
    if not b or b["phase"] != "judging":
        return
    runs = q("SELECT * FROM judge_runs WHERE battle_id=? ORDER BY judge_index", (bid,))
    done = sum(1 for r in runs if r["status"] == "completed")
    if done >= JUDGE_COUNT:
        resolve_battle(bid)
        return
    now = int(time.time())
    pending = []
    for r in runs:
        if r["status"] == "completed" or r["attempts"] >= JUDGE_MAX_ATTEMPTS:
            continue
        if r["status"] == "running":
            # another worker owns it; steal only if it looks crashed
            started = r["started_at"] or 0
            if now - started < STALE_CLAIM_SECS:
                continue
        pending.append(r)
    if not pending:
        fresh_running = any(r["status"] == "running"
                            and now - (r["started_at"] or 0) < STALE_CLAIM_SECS
                            for r in runs)
        if fresh_running:
            return  # another worker is actively judging; leave it alone
        once = f"degraded:{bid}"
        if idem_get(once) is None:
            idem_put(once, {"ok": True})
            add_feed(f"judging degraded on battle {bid[:14]} \u2014 judges exhausted retries",
                     "warn")
        return
    entries = q("SELECT blind_id, body FROM entries "
                "WHERE battle_id=? AND blind_id IS NOT NULL ORDER BY blind_id", (bid,))
    if not entries:
        return
    payload = [{"blind_id": e["blind_id"], "body": e["body"]} for e in entries]
    backend = judges.get_backend()

    def do_judge(run):
        idx = run["judge_index"]
        q("UPDATE judge_runs SET status='running', attempts=attempts+1, started_at=?, "
          "error=NULL WHERE battle_id=? AND judge_index=?",
          (int(time.time()), bid, idx))
        try:
            scores = backend.evaluate(b["prompt"], payload)
            for s in scores:
                if USE_PG:
                    q("INSERT INTO judge_scores(battle_id,judge_index,blind_id,originality,"
                      "craft,impact,rationale,created_at) VALUES(?,?,?,?,?,?,?,?) "
                      "ON CONFLICT DO NOTHING",
                      (bid, idx, s["blind_id"], s["originality"], s["craft"],
                       s["impact"], s["rationale"], int(time.time())))
                else:
                    q("INSERT OR IGNORE INTO judge_scores(battle_id,judge_index,blind_id,"
                      "originality,craft,impact,rationale,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (bid, idx, s["blind_id"], s["originality"], s["craft"],
                       s["impact"], s["rationale"], int(time.time())))
            q("UPDATE judge_runs SET status='completed', completed_at=? "
              "WHERE battle_id=? AND judge_index=?",
              (int(time.time()), bid, idx))
            return True
        except Exception as e:
            q("UPDATE judge_runs SET status='failed', error=? "
              "WHERE battle_id=? AND judge_index=?",
              (str(e)[:500], bid, idx))
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=JUDGE_COUNT) as ex:
        list(ex.map(do_judge, pending))
    done = q("SELECT COUNT(*) c FROM judge_runs WHERE battle_id=? AND status='completed'",
             (bid,), one=True)["c"]
    if done >= JUDGE_COUNT:
        resolve_battle(bid)


def _tick_transitions():
    """Fast, lock-held state transitions: close elapsed battles, start new ones."""
    now = int(time.time())
    # 1. close any open battle whose window has elapsed
    b = q("SELECT * FROM battles WHERE phase='open' AND ends_at<=? "
          "ORDER BY created_at DESC LIMIT 1", (now,), one=True)
    if b:
        close_entries(b["id"])
    # 2. start a new scheduled battle if nothing is live and the pause elapsed
    live = q("SELECT id FROM battles WHERE phase IN ('open','judging') LIMIT 1", one=True)
    if not live:
        last = q("SELECT COALESCE(resolved_at, created_at) t FROM battles "
                 "ORDER BY created_at DESC LIMIT 1", one=True)
        if not last or now - last["t"] >= PAUSE_SECS:
            start_battle(kind="scheduled")


def _tick_judging():
    """Slow judge execution. Runs OUTSIDE the tick lock (judges take seconds);
    exactly-once resolution is enforced by the resolve claim, not the lock."""
    b = q("SELECT * FROM battles WHERE phase='judging' "
          "ORDER BY created_at DESC LIMIT 1", one=True)
    if b:
        advance_judging(b["id"])


def _tick_body():
    _tick_transitions()
    _tick_judging()


def tick_once():
    """Run one game-loop iteration, serialized across concurrent invocations.

    Fast transitions run under the lock (db_lock locally, a PG advisory lock
    on Vercel). Judge execution runs after the lock is released: judges take
    seconds, and holding the lock would wedge every request (SQLite) or
    serialize all serverless invocations (PG). Correctness of judging and
    resolution comes from the judge_runs state machine and the exactly-once
    resolve claim, not from the tick lock.
    """
    try:
        if USE_PG:
            # Advisory lock so concurrent serverless invocations don't double-start battles.
            con = psycopg.connect(DATABASE_URL, autocommit=True)
            try:
                con.execute("SELECT pg_advisory_lock(424242)")
                try:
                    _tick_transitions()
                finally:
                    con.execute("SELECT pg_advisory_unlock(424242)")
            finally:
                con.close()
        else:
            with db_lock:
                _tick_transitions()
        _tick_judging()
    except Exception as exc:  # never break a request because of the tick
        print("tick error:", exc)


def scheduler_loop():
    while True:
        tick_once()
        time.sleep(5)


@app.middleware("http")
async def lazy_tick(request: Request, call_next):
    if ON_VERCEL:
        await asyncio.to_thread(tick_once)
    return await call_next(request)


init_db()
if not ON_VERCEL:
    threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
