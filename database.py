"""
database.py — Gestion de la base de données SQLite pour le bot de paris
"""

import sqlite3
import os
from datetime import datetime, date

DB_PATH = os.getenv("DB_PATH", "tennis_bot.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            points      INTEGER DEFAULT 0,
            joined_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS matches (
            match_id        TEXT PRIMARY KEY,
            tournament      TEXT NOT NULL,
            round           TEXT,
            player1         TEXT NOT NULL,
            player2         TEXT NOT NULL,
            scheduled_at    TEXT,
            status          TEXT DEFAULT 'upcoming',
            winner          TEXT,
            score           TEXT,
            surface         TEXT DEFAULT 'Clay',
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS bets (
            bet_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(user_id),
            match_id    TEXT    NOT NULL REFERENCES matches(match_id),
            prediction  TEXT    NOT NULL,
            amount      INTEGER NOT NULL,
            status      TEXT DEFAULT 'pending',
            placed_at   TEXT DEFAULT (datetime('now')),
            resolved_at TEXT,
            UNIQUE(user_id, match_id)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            tx_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(user_id),
            amount      INTEGER NOT NULL,
            reason      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        """)
    print("✅ Base de données initialisée.")


# ─── USERS ────────────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str, first_name: str):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username, first_name))


def get_user(user_id: int):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def update_points(user_id: int, delta: int, reason: str = ""):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET points = points + ? WHERE user_id = ?",
            (delta, user_id)
        )
        conn.execute(
            "INSERT INTO transactions (user_id, amount, reason) VALUES (?, ?, ?)",
            (user_id, delta, reason)
        )


def get_leaderboard(limit: int = 10):
    with get_connection() as conn:
        return conn.execute("""
            SELECT u.user_id, u.username, u.first_name, u.points,
                   COUNT(CASE WHEN b.status = 'won'  THEN 1 END) AS wins,
                   COUNT(CASE WHEN b.status = 'lost' THEN 1 END) AS losses,
                   COUNT(CASE WHEN b.status IN ('won','lost') THEN 1 END) AS total
            FROM users u
            LEFT JOIN bets b ON b.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY u.points DESC
            LIMIT ?
        """, (limit,)).fetchall()


# ─── MATCHES ──────────────────────────────────────────────────────────────────

def upsert_match(match_id, tournament, round_, player1, player2,
                 scheduled_at=None, status="upcoming", surface="Clay"):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO matches (match_id, tournament, round, player1, player2,
                                 scheduled_at, status, surface)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                status       = excluded.status,
                scheduled_at = excluded.scheduled_at
        """, (match_id, tournament, round_, player1, player2,
              scheduled_at, status, surface))


def get_open_matches():
    """
    Retourne uniquement les matchs dont l'heure n'est pas encore passée
    et dont le statut est 'upcoming'.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM matches
            WHERE status = 'upcoming'
              AND scheduled_at > ?
            ORDER BY scheduled_at
        """, (now,)).fetchall()


def get_all_open_matches():
    """Alias pour compatibilité — retourne les matchs encore ouverts."""
    return get_open_matches()


def get_match(match_id: str):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()


def resolve_match(match_id: str, winner: str, score: str = ""):
    with get_connection() as conn:
        conn.execute("""
            UPDATE matches SET status = 'finished', winner = ?, score = ?
            WHERE match_id = ?
        """, (winner, score, match_id))

        bets = conn.execute(
            "SELECT * FROM bets WHERE match_id = ? AND status = 'pending'",
            (match_id,)
        ).fetchall()

    results = []
    for bet in bets:
        if bet["prediction"] == winner:
            gain = bet["amount"] * 2
            update_points(bet["user_id"], gain,
                          f"Pari gagné sur match {match_id}")
            with get_connection() as conn:
                conn.execute("""
                    UPDATE bets SET status = 'won', resolved_at = datetime('now')
                    WHERE bet_id = ?
                """, (bet["bet_id"],))
            results.append((bet["user_id"], "won", gain))
        else:
            update_points(bet["user_id"], 0,
                          f"Pari perdu sur match {match_id}")
            with get_connection() as conn:
                conn.execute("""
                    UPDATE bets SET status = 'lost', resolved_at = datetime('now')
                    WHERE bet_id = ?
                """, (bet["bet_id"],))
            results.append((bet["user_id"], "lost", 0))

    return results


# ─── BETS ─────────────────────────────────────────────────────────────────────

def place_bet(user_id: int, match_id: str, prediction: str, amount: int):
    user = get_user(user_id)
    if not user:
        return False, "Utilisateur introuvable."
    if user["points"] < amount:
        return False, f"Solde insuffisant ({user['points']} pts)."

    match = get_match(match_id)
    if not match:
        return False, "Match introuvable."
    if match["status"] != "upcoming":
        return False, "Ce match n'accepte plus de paris (déjà commencé ou terminé)."

    # Vérification de l'heure : bloquer si le match a déjà commencé
    if match["scheduled_at"]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if match["scheduled_at"] <= now:
            return False, "❌ Les paris sont fermés, ce match a déjà commencé !"

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM bets WHERE user_id=? AND match_id=?",
            (user_id, match_id)
        ).fetchone()
        if existing:
            return False, "Tu as déjà parié sur ce match."

        conn.execute("""
            INSERT INTO bets (user_id, match_id, prediction, amount)
            VALUES (?, ?, ?, ?)
        """, (user_id, match_id, prediction, amount))
        bet_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    update_points(user_id, -amount, f"Pari placé sur match {match_id}")
    return True, bet_id


def get_user_bets(user_id: int, limit: int = 20):
    with get_connection() as conn:
        return conn.execute("""
            SELECT b.*, m.player1, m.player2, m.tournament, m.round,
                   m.winner AS match_winner, m.score
            FROM bets b
            JOIN matches m ON m.match_id = b.match_id
            WHERE b.user_id = ?
            ORDER BY b.placed_at DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()


def get_user_stats(user_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN status='won'  THEN 1 END) AS wins,
                COUNT(CASE WHEN status='lost' THEN 1 END) AS losses,
                COUNT(CASE WHEN status='pending' THEN 1 END) AS pending,
                COALESCE(SUM(CASE WHEN status='won'  THEN amount*2 END), 0) AS earned,
                COALESCE(SUM(CASE WHEN status='lost' THEN amount   END), 0) AS lost_pts
            FROM bets
            WHERE user_id = ?
        """, (user_id,)).fetchone()


def get_match_bets_count(match_id: str):
    with get_connection() as conn:
        return conn.execute("""
            SELECT
                COUNT(CASE WHEN prediction='player1' THEN 1 END) AS p1_bets,
                COUNT(CASE WHEN prediction='player2' THEN 1 END) AS p2_bets,
                SUM(CASE WHEN prediction='player1' THEN amount ELSE 0 END) AS p1_pts,
                SUM(CASE WHEN prediction='player2' THEN amount ELSE 0 END) AS p2_pts
            FROM bets WHERE match_id = ? AND status = 'pending'
        """, (match_id,)).fetchone()
