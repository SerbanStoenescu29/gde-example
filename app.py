import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import (
    Flask, g, jsonify, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CHAT_DB_PATH", os.path.join(BASE_DIR, "chatbot.db"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-6-v1")
SYSTEM_PROMPT = "You are a helpful, friendly and concise general-purpose assistant."
MAX_HISTORY = 40  # most recent messages sent to the model per request
# Context window limits. Tokens are estimated (~4 characters per token), so the
# budget is approximate; the oldest messages are dropped first.
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "6000"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "4000"))


def estimate_tokens(text):
    return -(-len(text) // 4)  # ceil(len / 4)


def trim_history(history, reserved_tokens):
    """Drop the oldest messages until the history fits the context budget."""
    budget = MAX_CONTEXT_TOKENS - reserved_tokens
    kept, used = [], 0
    for m in reversed(history):  # newest first
        cost = estimate_tokens(m["content"])
        if used + cost > budget:
            break
        kept.append(m)
        used += cost
    return kept[::-1]

FREE_MESSAGE_LIMIT = int(os.environ.get("FREE_MESSAGE_LIMIT", "10"))  # messages per account

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
MIN_PASSWORD_LEN = 8


def load_secret_key():
    """Use SECRET_KEY from the environment, else generate one and persist it."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    # Stored beside the database (the writable /data volume in the container).
    path = os.path.join(os.path.dirname(DB_PATH), ".secret_key")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(key)
        return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
bedrock = boto3.Session(region_name=AWS_REGION).client("bedrock-runtime")

# Used to keep login timing similar whether or not the username exists.
DUMMY_HASH = generate_password_hash("not-a-real-password")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    messages_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
"""


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # Migration: token usage columns (filled on assistant messages only).
        cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        for col in ("input_tokens", "output_tokens"):
            if col not in cols:
                db.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        # Migration: per-account free-tier usage counter (backfilled from history).
        user_cols = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        if "messages_used" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN messages_used INTEGER NOT NULL DEFAULT 0")
            if "user_id" in {r[1] for r in db.execute("PRAGMA table_info(conversations)")}:
                db.execute(
                    "UPDATE users SET messages_used = ("
                    " SELECT COUNT(*) FROM messages m JOIN conversations c"
                    " ON c.id = m.conversation_id"
                    " WHERE c.user_id = users.id AND m.role = 'user')"
                )
        # Migration: conversations belong to a user.
        conv_cols = {r[1] for r in db.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in conv_cols:
            db.execute(
                "ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id)"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)"
        )


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_title(text):
    title = " ".join(text.split())
    return title if len(title) <= 50 else title[:47].rstrip() + "…"


def conversation_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def index():
    if current_user() is None:
        return redirect(url_for("login"))
    return render_template(
        "index.html",
        username=g.user["username"],
        max_chars=MAX_MESSAGE_CHARS,
        quota=quota_dict(g.user["messages_used"]),
    )


def quota_dict(used):
    return {
        "used": used,
        "limit": FREE_MESSAGE_LIMIT,
        "remaining": max(FREE_MESSAGE_LIMIT - used, 0),
    }


def reserve_message(user_id):
    """Atomically claim one free-tier message; False if the account is out."""
    db = get_db()
    cur = db.execute(
        "UPDATE users SET messages_used = messages_used + 1 "
        "WHERE id = ? AND messages_used < ?",
        (user_id, FREE_MESSAGE_LIMIT),
    )
    db.commit()
    return cur.rowcount == 1


def refund_message(user_id):
    db = get_db()
    db.execute(
        "UPDATE users SET messages_used = MAX(messages_used - 1, 0) WHERE id = ?",
        (user_id,),
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def current_user():
    """Load the logged-in user (cached on g); clears stale sessions."""
    if "user" not in g:
        g.user = None
        uid = session.get("user_id")
        if uid is not None:
            g.user = get_db().execute(
                "SELECT id, username, messages_used FROM users WHERE id = ?", (uid,)
            ).fetchone()
            if g.user is None:
                session.clear()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return jsonify(error="Authentication required."), 401
        return view(*args, **kwargs)

    return wrapped


def start_session(user_id):
    session.clear()  # drop any previous session data (prevents fixation)
    session["user_id"] = user_id
    session.permanent = True


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user() is not None:
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("auth.html", mode="register")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    error = None
    if not USERNAME_RE.match(username):
        error = "Username must be 3-30 characters: letters, numbers, . _ -"
    elif len(password) < MIN_PASSWORD_LEN:
        error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
    elif password != confirm:
        error = "Passwords do not match."
    if error:
        return render_template("auth.html", mode="register", error=error, username=username), 400

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), now()),
        )
    except sqlite3.IntegrityError:
        return render_template(
            "auth.html", mode="register", error="That username is already taken.",
            username=username,
        ), 409
    user_id = cur.lastrowid
    # Conversations created before accounts existed are adopted by the first user.
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
        db.execute("UPDATE conversations SET user_id = ? WHERE user_id IS NULL", (user_id,))
    db.commit()

    start_session(user_id)
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("auth.html", mode="login")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    row = get_db().execute(
        "SELECT id, password_hash FROM users WHERE username = ?", (username,)
    ).fetchone()
    ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
    if row is None or not ok:
        return render_template(
            "auth.html", mode="login", error="Invalid username or password.",
            username=username,
        ), 401

    start_session(row["id"])
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Conversation API
# --------------------------------------------------------------------------- #
@app.get("/api/conversations")
@login_required
def list_conversations():
    rows = get_db().execute(
        "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
        (g.user["id"],),
    ).fetchall()
    return jsonify(conversations=[conversation_dict(r) for r in rows])


@app.get("/api/conversations/<int:conv_id>")
@login_required
def get_conversation(conv_id):
    db = get_db()
    conv = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, g.user["id"]),
    ).fetchone()
    if conv is None:
        return jsonify(error="Conversation not found."), 404
    msgs = db.execute(
        "SELECT role, content, created_at, input_tokens, output_tokens "
        "FROM messages WHERE conversation_id = ? ORDER BY id",
        (conv_id,),
    ).fetchall()
    totals = db.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o "
        "FROM messages WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    return jsonify(
        conversation=conversation_dict(conv),
        messages=[dict(m) for m in msgs],
        totals={"input": totals["i"], "output": totals["o"], "total": totals["i"] + totals["o"]},
    )


@app.patch("/api/conversations/<int:conv_id>")
@login_required
def rename_conversation(conv_id):
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify(error="Title cannot be empty."), 400
    db = get_db()
    cur = db.execute(
        "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
        (title[:100], conv_id, g.user["id"]),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Conversation not found."), 404
    return jsonify(ok=True, title=title[:100])


@app.delete("/api/conversations/<int:conv_id>")
@login_required
def delete_conversation(conv_id):
    db = get_db()
    cur = db.execute(
        "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conv_id, g.user["id"])
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify(error="Conversation not found."), 404
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
@app.post("/api/chat")
@login_required
def chat():
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    conv_id = data.get("conversation_id")
    if not text:
        return jsonify(error="A message is required."), 400
    if len(text) > MAX_MESSAGE_CHARS:
        return jsonify(
            error=f"Message is too long ({len(text):,} characters). "
                  f"The limit is {MAX_MESSAGE_CHARS:,}."
        ), 413

    db = get_db()
    history = []
    if conv_id is not None:
        conv = db.execute(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, g.user["id"]),
        ).fetchone()
        if conv is None:
            return jsonify(error="Conversation not found."), 404
        rows = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (conv_id, MAX_HISTORY),
        ).fetchall()
        history = [dict(r) for r in reversed(rows)]

    # Keep the prompt inside the context budget (system prompt + new message count too).
    history = trim_history(
        history, estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(text)
    )

    # Converse needs the first message to be from the user.
    while history and history[0]["role"] != "user":
        history.pop(0)

    messages = [
        {"role": m["role"], "content": [{"text": m["content"]}]} for m in history
    ]
    messages.append({"role": "user", "content": [{"text": text}]})

    # Reserve a free-tier message up front so parallel requests can't exceed
    # the limit; it is refunded below if the model call fails.
    user_id = g.user["id"]
    if not reserve_message(user_id):
        return jsonify(
            error=f"You've used all {FREE_MESSAGE_LIMIT} free messages for this account.",
            code="free_limit_reached",
            quota=quota_dict(FREE_MESSAGE_LIMIT),
        ), 403

    try:
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            inferenceConfig={"maxTokens": 1024, "temperature": 0.5},
        )
    except NoCredentialsError:
        refund_message(user_id)
        return jsonify(error="AWS credentials not found. Run: aws configure"), 500
    except ClientError as e:
        refund_message(user_id)
        err = e.response["Error"]
        return jsonify(error=f"{err['Code']}: {err['Message']}"), 502
    except Exception:
        refund_message(user_id)
        raise

    reply = "".join(
        b["text"] for b in response["output"]["message"]["content"] if "text" in b
    )

    # Persist only after a successful model call so history stays consistent.
    ts = now()
    is_new = conv_id is None
    if is_new:
        cur = db.execute(
            "INSERT INTO conversations (title, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?)",
            (make_title(text), ts, ts, g.user["id"]),
        )
        conv_id = cur.lastrowid
    usage = response.get("usage", {})
    in_tok = usage.get("inputTokens")
    out_tok = usage.get("outputTokens")
    db.executemany(
        "INSERT INTO messages (conversation_id, role, content, created_at, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (conv_id, "user", text, ts, None, None),
            (conv_id, "assistant", reply, ts, in_tok, out_tok),
        ],
    )
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conv_id))
    db.commit()

    conv = db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    totals = db.execute(
        "SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o "
        "FROM messages WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    used = db.execute(
        "SELECT messages_used FROM users WHERE id = ?", (user_id,)
    ).fetchone()[0]
    return jsonify(
        reply=reply,
        quota=quota_dict(used),
        conversation=conversation_dict(conv),
        is_new=is_new,
        usage={
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": usage.get("totalTokens"),
        },
        totals={"input": totals["i"], "output": totals["o"], "total": totals["i"] + totals["o"]},
    )


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
