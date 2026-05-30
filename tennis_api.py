"""
tennis_api.py — Récupération des matchs de tennis du jour
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import date, datetime
from database import upsert_match

API_KEY = os.getenv("TENNIS_API_KEY", "")

DEMO_MATCHES = [
    {
        "match_id": "rg2026_001",
        "tournament": "Roland Garros 2026",
        "round": "3ème tour",
        "player1": "C. Alcaraz",
        "player2": "H. Hurkacz",
        "scheduled_at": f"{date.today().isoformat()} 23:00:00",
        "surface": "Clay",
    },
    {
        "match_id": "rg2026_002",
        "tournament": "Roland Garros 2026",
        "round": "3ème tour",
        "player1": "I. Swiatek",
        "player2": "E. Rybakina",
        "scheduled_at": f"{date.today().isoformat()} 23:30:00",
        "surface": "Clay",
    },
]


def _fetch_from_api() -> list[dict]:
    if not API_KEY:
        return []

    today = date.today().isoformat()
    url = (
        "https://api-tennis.p.rapidapi.com/matches"
        f"?date={today}&tournament_id=2"
    )
    req = urllib.request.Request(
        url,
        headers={
            "X-RapidAPI-Key": API_KEY,
            "X-RapidAPI-Host": "api-tennis.p.rapidapi.com",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"⚠️  API tennis inaccessible : {e}")
        return []

    matches = []
    for m in data.get("result", []):
        matches.append({
            "match_id": str(m.get("match_id") or m.get("id", "")),
            "tournament": m.get("tournament_name", "ATP"),
            "round": m.get("round", ""),
            "player1": m.get("player_1_name", "Joueur 1"),
            "player2": m.get("player_2_name", "Joueur 2"),
            "scheduled_at": m.get("match_date", today + " 12:00:00"),
            "surface": m.get("surface", "Clay"),
        })
    return matches


def refresh_matches() -> list[dict]:
    matches = _fetch_from_api()

    if not matches:
        print("ℹ️  Mode démo : utilisation des matchs de démonstration.")
        matches = DEMO_MATCHES

    for m in matches:
        upsert_match(
            match_id=m["match_id"],
            tournament=m["tournament"],
            round_=m.get("round", ""),
            player1=m["player1"],
            player2=m["player2"],
            scheduled_at=m.get("scheduled_at"),
            surface=m.get("surface", "Clay"),
        )

    print(f"✅ {len(matches)} match(es) chargé(s).")
    return matches


def format_match_for_display(match) -> str:
    """Formate un match (Row SQLite) pour l'affichage Telegram."""
    time_str = ""
    if match["scheduled_at"]:
        try:
            dt = datetime.strptime(match["scheduled_at"], "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%H:%M")
        except Exception:
            time_str = match["scheduled_at"]

    status_emoji = {"upcoming": "🔜", "live": "🔴 LIVE", "finished": "✅"}.get(
        match["status"], "❓"
    )

    surface = match["surface"] if match["surface"] else "Clay"
    score = match["score"] if match["score"] else ""

    line = (
        f"{status_emoji} *{match['player1']}* vs *{match['player2']}*\n"
        f"   🏆 {match['tournament']} — {match['round']}\n"
        f"   🕐 {time_str}   🎾 {surface}"
    )
    if match["status"] == "finished" and score:
        line += f"\n   📊 Score : {score}"
    return line
