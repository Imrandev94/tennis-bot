"""
bot.py — Bot Telegram de paris sportifs (tennis / Roland Garros)
Nécessite : pip install python-telegram-bot==20.*

Variables d'environnement requises :
  BOT_TOKEN     — Token du bot Telegram (via @BotFather)
  ADMIN_IDS     — IDs Telegram des admins, séparés par des virgules
  TENNIS_API_KEY — (optionnel) clé API tennis pour les vrais matchs
"""
from dotenv import load_dotenv
load_dotenv()

import os
import logging
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, ConversationHandler
)
from telegram.constants import ParseMode

import database as db
import tennis_api as api

# ─── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "VOTRE_TOKEN_ICI")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]

# États ConversationHandler pour le flux de pari
CHOOSE_MATCH, CHOOSE_PLAYER, CHOOSE_AMOUNT = range(3)

BET_DATA_KEY = "bet_flow"   # clé dans context.user_data


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")


def safe_name(row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    return row["first_name"] or f"User{row['user_id']}"


async def register(update: Update):
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")


# ─── /start ───────────────────────────────────────────────────────────────────

def get_main_keyboard():
    """Retourne le clavier persistant principal."""
    keyboard = [
        [KeyboardButton("🎾 MATCHS")],
        [KeyboardButton("🎯 PARIS"), KeyboardButton("👤 PROFIL")],
        [KeyboardButton("🏆 CLASSEMENT"), KeyboardButton("📊 STATS")],
        [KeyboardButton("💰 SOLDE"), KeyboardButton("📋 MES PARIS")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    user = db.get_user(update.effective_user.id)
    stats = db.get_user_stats(update.effective_user.id)
    rows = db.get_leaderboard(100)
    open_matches = db.get_all_open_matches()

    # Calcul classement
    rank = next((i+1 for i, r in enumerate(rows) if r["user_id"] == update.effective_user.id), "?")

    # Calcul winrate
    wins = stats["wins"] or 0
    total = (stats["wins"] or 0) + (stats["losses"] or 0)
    winrate = round(wins / total * 100) if total > 0 else 0

    first_name = update.effective_user.first_name or "Champion"

    text = (
        f"🏆 *TENNISBET*\n"
        f"_Roland Garros 2026_ 🎾\n\n"
        f"☀️ Bonjour *{first_name}* !\n\n"
        f"💎 Solde · 💰 *{user['points']} pts*\n"
        f"✅ *{wins}* paris gagnés · 🎯 *{winrate}%*\n\n"
        f"🟢 *{len(open_matches)}* match(s) disponible(s) — va parier !\n"
        f"🏅 Classement : *#{rank}*\n\n"
        f"_Les points sont virtuels — aucun argent réel !_ 🎲"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard()
    )


# ─── /matchs ──────────────────────────────────────────────────────────────────

async def cmd_matchs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    matches = db.get_all_open_matches()
    if not matches:
        await update.message.reply_text(
            "😴 Aucun match ouvert aux paris pour l'instant.\n"
            "Reviens plus tard ou demande à un admin d'en ajouter !"
        )
        return

    lines = ["🎾 *Matchs disponibles pour parier :*\n"]
    for i, m in enumerate(matches, 1):
        lines.append(f"*{i}.* {api.format_match_for_display(m)}\n")

    lines.append("\n_Utilise /parier pour placer un pari !_")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# ─── /parier — ConversationHandler ────────────────────────────────────────────

async def cmd_parier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    matches = db.get_all_open_matches()
    if not matches:
        await update.message.reply_text("😴 Aucun match disponible pour parier.")
        return ConversationHandler.END

    buttons = []
    for m in matches:
        label = f"{m['player1']} vs {m['player2']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"match_{m['match_id']}")])
    buttons.append([InlineKeyboardButton("❌ Annuler", callback_data="cancel_bet")])

    context.user_data[BET_DATA_KEY] = {}
    await update.message.reply_text(
        "🎰 *Quel match veux-tu parier ?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CHOOSE_MATCH


async def choose_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_bet":
        await query.edit_message_text("❌ Paris annulé.")
        return ConversationHandler.END

    match_id = query.data.replace("match_", "")
    match = db.get_match(match_id)
    if not match:
        await query.edit_message_text("⚠️ Match introuvable.")
        return ConversationHandler.END

    context.user_data[BET_DATA_KEY]["match_id"] = match_id
    context.user_data[BET_DATA_KEY]["match"] = dict(match)

    counts = db.get_match_bets_count(match_id)
    p1_pct = p2_pct = 50
    total_bets = (counts["p1_bets"] or 0) + (counts["p2_bets"] or 0)
    if total_bets > 0:
        p1_pct = round(counts["p1_bets"] / total_bets * 100)
        p2_pct = 100 - p1_pct

    text = (
        f"🎾 *{match['player1']}* vs *{match['player2']}*\n"
        f"🏆 {match['tournament']} — {match['round']}\n\n"
        f"📊 Paris communauté : "
        f"{match['player1']} {p1_pct}% — {p2_pct}% {match['player2']}\n\n"
        "*Sur qui tu mises ?*"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🎾 {match['player1']}", callback_data="pick_player1"),
            InlineKeyboardButton(f"🎾 {match['player2']}", callback_data="pick_player2"),
        ],
        [InlineKeyboardButton("❌ Annuler", callback_data="cancel_bet")]
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return CHOOSE_PLAYER


async def choose_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_bet":
        await query.edit_message_text("❌ Paris annulé.")
        return ConversationHandler.END

    prediction = "player1" if query.data == "pick_player1" else "player2"
    match = context.user_data[BET_DATA_KEY]["match"]
    chosen_name = match["player1"] if prediction == "player1" else match["player2"]

    context.user_data[BET_DATA_KEY]["prediction"] = prediction
    context.user_data[BET_DATA_KEY]["chosen_name"] = chosen_name

    user = db.get_user(update.effective_user.id)
    solde = user["points"]

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("50 pts",  callback_data="amount_50"),
            InlineKeyboardButton("100 pts", callback_data="amount_100"),
            InlineKeyboardButton("250 pts", callback_data="amount_250"),
        ],
        [
            InlineKeyboardButton("500 pts", callback_data="amount_500"),
            InlineKeyboardButton("All-in 🎲", callback_data=f"amount_{solde}"),
        ],
        [InlineKeyboardButton("❌ Annuler", callback_data="cancel_bet")]
    ])
    await query.edit_message_text(
        f"✅ Tu as choisi *{chosen_name}*.\n\n"
        f"💰 Ton solde : *{solde} pts*\n\n"
        "*Combien veux-tu miser ?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )
    return CHOOSE_AMOUNT


async def choose_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_bet":
        await query.edit_message_text("❌ Paris annulé.")
        return ConversationHandler.END

    amount = int(query.data.replace("amount_", ""))
    data = context.user_data[BET_DATA_KEY]
    user_id = update.effective_user.id

    success, result = db.place_bet(
        user_id, data["match_id"], data["prediction"], amount
    )

    if not success:
        await query.edit_message_text(f"⚠️ Pari refusé : {result}")
        return ConversationHandler.END

    match = data["match"]
    user = db.get_user(user_id)
    await query.edit_message_text(
        f"🎉 *Pari enregistré !*\n\n"
        f"🎾 Match : *{match['player1']}* vs *{match['player2']}*\n"
        f"✅ Ton choix : *{data['chosen_name']}*\n"
        f"💰 Mise : *{amount} pts*\n"
        f"💵 Gain potentiel : *{amount * 2} pts*\n\n"
        f"Solde restant : *{user['points']} pts*\n\n"
        "_Bonne chance ! 🤞_",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


async def cancel_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Paris annulé.")
    return ConversationHandler.END


# ─── /mesparis ────────────────────────────────────────────────────────────────

async def cmd_mesparis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    bets = db.get_user_bets(update.effective_user.id, limit=15)

    if not bets:
        await update.message.reply_text("Tu n'as encore placé aucun pari. Utilise /parier !")
        return

    status_map = {
        "pending": "⏳ En attente",
        "won":     "✅ Gagné",
        "lost":    "❌ Perdu",
        "cancelled": "🚫 Annulé",
    }

    lines = ["📋 *Tes derniers paris :*\n"]
    pending, won, lost = [], [], []
    for b in bets:
        chosen = b["player1"] if b["prediction"] == "player1" else b["player2"]
        status_label = status_map.get(b["status"], b["status"])
        gain_str = ""
        if b["status"] == "won":
            gain_str = f" (+{b['amount']*2} pts)"
        elif b["status"] == "lost":
            gain_str = f" (-{b['amount']} pts)"

        entry = (
            f"• *{b['player1']} vs {b['player2']}*\n"
            f"  🏆 {b['tournament']}\n"
            f"  🎯 Choix : {chosen} | Mise : {b['amount']} pts\n"
            f"  {status_label}{gain_str}"
        )
        if b["status"] == "pending":
            pending.append(entry)
        elif b["status"] == "won":
            won.append(entry)
        else:
            lost.append(entry)

    if pending:
        lines.append("⏳ *EN COURS :*")
        lines.extend(pending)
        lines.append("")
    if won:
        lines.append("✅ *GAGNÉS :*")
        lines.extend(won)
        lines.append("")
    if lost:
        lines.append("❌ *PERDUS :*")
        lines.extend(lost)

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# ─── /stats ───────────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    stats = db.get_user_stats(user_id)

    total = stats["total"] or 0
    wins = stats["wins"] or 0
    losses = stats["losses"] or 0
    winrate = round(wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

    filled = round(winrate / 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    text = (
        f"📊 *Tes statistiques*\n\n"
        f"💰 Solde actuel : *{user['points']} pts*\n\n"
        f"🎲 Total paris : *{total}*\n"
        f"✅ Gagnés : *{wins}*\n"
        f"❌ Perdus : *{losses}*\n"
        f"⏳ En attente : *{stats['pending']}*\n\n"
        f"📈 Winrate : *{winrate}%*\n"
        f"{bar}\n\n"
        f"💵 Points gagnés : +{stats['earned']}\n"
        f"📉 Points perdus : -{stats['lost_pts']}\n"
        f"🏅 Bilan net : *{stats['earned'] - stats['lost_pts']:+d} pts*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ─── /classement ──────────────────────────────────────────────────────────────

async def cmd_classement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    rows = db.get_leaderboard(15)

    if not rows:
        await update.message.reply_text("Le classement est vide pour l'instant !")
        return

    lines = ["🏆 *LEADERBOARD — Top 15*\n"]
    user_id = update.effective_user.id
    user_rank = None

    for i, row in enumerate(rows, 1):
        total = row["total"] or 0
        wins = row["wins"] or 0
        winrate = round(wins / total * 100) if total > 0 else 0
        name = safe_name(row)
        marker = " ◀️" if row["user_id"] == user_id else ""
        lines.append(
            f"{medal(i)} {name}{marker}\n"
            f"   💰 {row['points']} pts  |  ✅ {wins}/{total}  ({winrate}%)"
        )
        if row["user_id"] == user_id:
            user_rank = i

    if user_rank:
        lines.append(f"\n_Tu es classé(e) *#{user_rank}* 🎯_")

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# ─── /solde ───────────────────────────────────────────────────────────────────

async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    user = db.get_user(update.effective_user.id)
    await update.message.reply_text(
        f"💰 Ton solde : *{user['points']} points* virtuels",
        parse_mode=ParseMode.MARKDOWN
    )


# ─── ADMIN : /addmatch ────────────────────────────────────────────────────────

async def cmd_addmatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Commande réservée aux admins.")
        return

    try:
        args = [a.strip() for a in " ".join(context.args).split("|")]
        if len(args) == 7:
            match_id, tournament, round_, p1, p2, dt, surface = args
        elif len(args) == 6:
            match_id, tournament, round_, p1, p2, dt = args
            surface = "Terre battue"
        else:
            raise ValueError("Nombre de champs incorrect")
        db.upsert_match(match_id, tournament, round_, p1, p2, dt + ":00", surface=surface)
        await update.message.reply_text(
            f"✅ Match ajouté : *{p1}* vs *{p2}*", parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Format incorrect.\nUsage :\n"
            "`/addmatch id|Tournoi|Tour|Joueur1|Joueur2|2026-05-30 14:00|Terre battue`",
            parse_mode=ParseMode.MARKDOWN
        )


# ─── ADMIN : /addmatchs (plusieurs matchs d'un coup) ─────────────────────────

async def cmd_addmatchs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Commande réservée aux admins.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage :\n`/addmatchs id|Tournoi|Tour|J1|J2|Date ; id|Tournoi|Tour|J1|J2|Date`\n\nSépare chaque match par ` ; `",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    texte = " ".join(context.args)
    matchs = texte.split(";")
    ajouts = []
    erreurs = []

    for m in matchs:
        m = m.strip()
        try:
            args = [a.strip() for a in m.split("|")]
            if len(args) == 7:
                match_id, tournament, round_, p1, p2, dt, surface = args
            elif len(args) == 6:
                match_id, tournament, round_, p1, p2, dt = args
                surface = "Terre battue"
            else:
                raise ValueError("Nombre de champs incorrect")
            db.upsert_match(match_id, tournament, round_, p1, p2, dt + ":00", surface=surface)
            ajouts.append(f"✅ {p1} vs {p2}")
        except Exception:
            erreurs.append(f"❌ Erreur : `{m}`")

    reponse = f"*{len(ajouts)} match(es) ajouté(s) :*\n" + "\n".join(ajouts)
    if erreurs:
        reponse += "\n\n*Erreurs :*\n" + "\n".join(erreurs)

    await update.message.reply_text(reponse, parse_mode=ParseMode.MARKDOWN)


# ─── ADMIN : /resoudre ────────────────────────────────────────────────────────

async def cmd_resoudre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Commande réservée aux admins.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : `/resoudre <match_id> <player1|player2> [score]`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    match_id = context.args[0]
    winner = context.args[1]
    score = " ".join(context.args[2:]) if len(context.args) > 2 else ""

    match = db.get_match(match_id)
    if not match:
        await update.message.reply_text("⚠️ Match introuvable.")
        return

    results = db.resolve_match(match_id, winner, score)

    winner_name = match["player1"] if winner == "player1" else match["player2"]
    text = (
        f"✅ *Match résolu !*\n"
        f"🎾 {match['player1']} vs {match['player2']}\n"
        f"🏆 Vainqueur : *{winner_name}*"
    )
    if score:
        text += f"\n📊 Score : {score}"
    text += f"\n\n💰 *{len(results)} pari(s) réglé(s)*"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    for user_id, status, gain in results:
        try:
            if status == "won":
                msg = (
                    f"🎉 *Pari GAGNÉ !*\n"
                    f"Match : {match['player1']} vs {match['player2']}\n"
                    f"Tu as gagné *+{gain} pts* ! 💰"
                )
            else:
                msg = (
                    f"😔 *Pari perdu...*\n"
                    f"Match : {match['player1']} vs {match['player2']}\n"
                    f"Vainqueur : {winner_name}\n"
                    "Retente ta chance sur le prochain match !"
                )
            await context.bot.send_message(
                chat_id=user_id, text=msg, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass


# ─── ADMIN : /refresh ─────────────────────────────────────────────────────────

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Commande réservée aux admins.")
        return
    matches = api.refresh_matches()
    await update.message.reply_text(
        f"✅ {len(matches)} match(es) chargé(s) depuis l'API."
    )



# ─── Gestion des boutons du clavier persistant ────────────────────────────────

async def handle_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register(update)
    text = update.message.text

    if text == "🎾 MATCHS":
        await cmd_matchs(update, context)
    elif text == "🎯 PARIS":
        await cmd_parier(update, context)
        return
    elif text == "👤 PROFIL":
        await cmd_stats(update, context)
    elif text == "🏆 CLASSEMENT":
        await cmd_classement(update, context)
    elif text == "📊 STATS":
        await cmd_stats(update, context)
    elif text == "💰 SOLDE":
        await cmd_solde(update, context)
    elif text == "📋 MES PARIS":
        await cmd_mesparis(update, context)

# ─── Callbacks inline (boutons du /start) ─────────────────────────────────────

async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "show_matches":
        matches = db.get_all_open_matches()
        if not matches:
            await query.message.reply_text("😴 Aucun match disponible.")
            return
        lines = ["🎾 *Matchs disponibles pour parier :*\n"]
        for i, m in enumerate(matches, 1):
            lines.append(f"*{i}.* {api.format_match_for_display(m)}\n")
        lines.append("\n_Utilise /parier pour placer un pari !_")
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    elif data == "leaderboard":
        await cmd_classement(update, context)
    elif data == "my_stats":
        await cmd_stats(update, context)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    db.init_db()
    api.refresh_matches()

    app = Application.builder().token(BOT_TOKEN).build()

    bet_conv = ConversationHandler(
        entry_points=[
            CommandHandler("parier", cmd_parier),
            CallbackQueryHandler(
                lambda u, c: cmd_parier(u, c), pattern="^start_bet$"
            ),
        ],
        states={
            CHOOSE_MATCH:  [CallbackQueryHandler(choose_match,  pattern="^(match_|cancel_bet)")],
            CHOOSE_PLAYER: [CallbackQueryHandler(choose_player, pattern="^(pick_|cancel_bet)")],
            CHOOSE_AMOUNT: [CallbackQueryHandler(choose_amount, pattern="^(amount_|cancel_bet)")],
        },
        fallbacks=[CommandHandler("annuler", cancel_bet)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("matchs", cmd_matchs))
    app.add_handler(CommandHandler("mesparis", cmd_mesparis))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("classement", cmd_classement))
    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(CommandHandler("addmatch", cmd_addmatch))
    app.add_handler(CommandHandler("addmatchs", cmd_addmatchs))
    app.add_handler(CommandHandler("resoudre", cmd_resoudre))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(bet_conv)
    app.add_handler(CallbackQueryHandler(inline_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_keyboard
    ))

    import asyncio
    print("🤖 Bot démarré ! Ctrl+C pour arrêter.")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
