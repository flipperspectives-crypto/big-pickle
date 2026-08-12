import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .config import settings

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    skey TEXT NOT NULL UNIQUE,
    stripe_customer_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    balance REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    retail_usd REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage (key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage (created_at);
CREATE TABLE IF NOT EXISTS stripe_charges (
    session_id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS x402_settlements (
    payment_id TEXT PRIMARY KEY,
    payer TEXT NOT NULL,
    tx_hash TEXT,
    network TEXT,
    asset TEXT,
    amount_usd REAL NOT NULL,
    key_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = _conn()
        try:
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(keys)").fetchall()}
    if "balance" not in cols:
        conn.execute("ALTER TABLE keys ADD COLUMN balance REAL DEFAULT 0")
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(usage)").fetchall()}
    if "retail_usd" not in ucols:
        conn.execute("ALTER TABLE usage ADD COLUMN retail_usd REAL DEFAULT 0")


def create_key(name: str) -> dict:
    with _lock:
        conn = _conn()
        try:
            kid = uuid.uuid4().hex[:16]
            skey = f"gw_{uuid.uuid4().hex}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO keys (id, name, skey, created_at) VALUES (?, ?, ?, ?)",
                (kid, name, skey, now),
            )
            conn.commit()
            return {"id": kid, "name": name, "skey": skey, "created_at": now}
        finally:
            conn.close()


def get_key(skey: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM keys WHERE skey = ? AND active = 1", (skey,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_key_by_name(name: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM keys WHERE name = ? AND active = 1", (name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_keys() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM keys ORDER BY created_at").fetchall()
        return [{k: v for k, v in dict(r).items() if k != "skey"} for r in rows]
    finally:
        conn.close()


def record_usage(
    key_id: str,
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> None:
    retail = round(cost_usd * settings.MARKUP, 8)
    with _lock:
        conn = _conn()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO usage (key_id, model, provider, prompt_tokens, completion_tokens, cost_usd, retail_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key_id, model, provider, prompt_tokens, completion_tokens, cost_usd, retail, now),
            )
            conn.execute("UPDATE keys SET balance = balance - ? WHERE id = ?", (retail, key_id))
            conn.commit()
        finally:
            conn.close()


def add_credits(key_id: str, amount: float) -> float | None:
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("UPDATE keys SET balance = balance + ? WHERE id = ?", (amount, key_id))
            if cur.rowcount == 0:
                return None
            conn.commit()
            row = conn.execute("SELECT balance FROM keys WHERE id = ?", (key_id,)).fetchone()
            return row["balance"] if row else None
        finally:
            conn.close()


def balance_for(key_id: str) -> float:
    conn = _conn()
    try:
        row = conn.execute("SELECT balance FROM keys WHERE id = ?", (key_id,)).fetchone()
        return row["balance"] if row else 0.0
    finally:
        conn.close()


def gateway_status() -> dict:
    """Evidence-backed gateway health: active key count + total balance.

    Returns only aggregated, non-sensitive aggregates (no secrets, no host
    info). Used by the public read-only /v1/status endpoint.
    """
    conn = _conn()
    try:
        active_keys = conn.execute(
            "SELECT COUNT(*) FROM keys WHERE active = 1"
        ).fetchone()[0]
        total_balance = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM keys WHERE active = 1"
        ).fetchone()[0]
        return {
            "active_keys": active_keys,
            "total_balance_usd": round(total_balance, 6),
        }
    finally:
        conn.close()


def usage_for(key_id: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) pt, COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM usage WHERE key_id = ?",
            (key_id,),
        ).fetchone()
        return {
            "prompt_tokens": row["pt"],
            "completion_tokens": row["ct"],
            "total_tokens": row["pt"] + row["ct"],
            "cost_usd": round(row["c"], 6),
            "requests": row["n"],
        }
    finally:
        conn.close()


def usage_all() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT key_id, COALESCE(SUM(prompt_tokens),0) pt, COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM usage GROUP BY key_id"
        ).fetchall()
        return [
            {
                "key_id": r["key_id"],
                "prompt_tokens": r["pt"],
                "completion_tokens": r["ct"],
                "total_tokens": r["pt"] + r["ct"],
                "cost_usd": round(r["c"], 6),
                "requests": r["n"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def set_stripe_customer(key_id: str, customer_id: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE keys SET stripe_customer_id = ? WHERE id = ?",
                (customer_id, key_id),
            )
            conn.commit()
        finally:
            conn.close()


def is_charged(session_id: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM stripe_charges WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_charged(session_id: str, key_id: str, amount_usd: float) -> None:
    with _lock:
        conn = _conn()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO stripe_charges (session_id, key_id, amount_usd, created_at) VALUES (?, ?, ?, ?)",
                (session_id, key_id, amount_usd, now),
            )
            conn.commit()
        finally:
            conn.close()


def settle_x402_credit(
    payment_id: str,
    payer: str,
    transaction: str | None,
    network: str,
    asset: str,
    amount_usd: float,
    key_id: str,
) -> bool:
    """Atomically record an x402 settlement and credit the gateway key.

    Returns True only on the first (ledger-inserting) call. A replay of the
    exact same payment reuses ``payment_id`` and returns False, so the gateway
    balance can never be credited twice for one on-chain settlement.
    """
    with _lock:
        conn = _conn()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT OR IGNORE INTO x402_settlements "
                "(payment_id, payer, tx_hash, network, asset, amount_usd, key_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (payment_id, payer, transaction, network, asset, amount_usd, key_id, now),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "UPDATE keys SET balance = balance + ? WHERE id = ?", (amount_usd, key_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()


def get_x402_settlement(payment_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM x402_settlements WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
