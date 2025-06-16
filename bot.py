#!/usr/bin/env python3
# bot.py – Gestion de missions & réunions (deadline picker corrigé)
# =================================================================

import os
import datetime as dt
import logging

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ─── Configuration & logging ─────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("MgmtBot")

# ─── Intents & Bot ───────────────────────────────────────────────
INTENTS = discord.Intents.default()
INTENTS.guilds  = True
INTENTS.members = True

bot = commands.Bot(command_prefix=None, intents=INTENTS)
DB_PATH = "missions.db"

# ─── Rôles autorisés ─────────────────────────────────────────────
ASSIGNER_ROLES = [
    1379270374914789577,  # chef (attribue sans recevoir)
    1379270378672885811,
    1379270389343191102,
    1379270382405554266,
    1379270385861660703,
    1379270400688652342,
    1382834652162818089,
]
BLOCKED_RECEIVER = 1379270374914789577  # ne peut pas recevoir de mission

def is_assigner(inter: discord.Interaction) -> bool:
    m = inter.user
    return isinstance(m, discord.Member) and any(r.id in ASSIGNER_ROLES for r in m.roles)

def guard():
    async def predicate(inter: discord.Interaction):
        if not is_assigner(inter):
            raise app_commands.CheckFailure("🚫 Vous n’avez pas le rôle requis.")
        return True
    return app_commands.check(predicate)

# ─── Base SQLite & helpers ───────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild     INTEGER,
    author    INTEGER,
    assignee  INTEGER,
    description TEXT NOT NULL,
    deadline  TEXT,
    status    TEXT DEFAULT 'En cours',
    done      BOOLEAN DEFAULT 0,
    reminded_24 BOOLEAN DEFAULT 0,
    reminded_1  BOOLEAN DEFAULT 0
);
"""
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def add_mission(guild, author, assignee, desc, dl_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO missions (guild, author, assignee, description, deadline) VALUES (?,?,?,?,?)",
            (guild, author, assignee, desc, dl_iso)
        )
        await db.commit()

async def list_missions(guild, done_flag):
    async with aiosqlite.connect(DB_PATH) as db:
        if done_flag is None:
            sql  = "SELECT id, description, deadline, assignee, status FROM missions WHERE guild=? ORDER BY id"
            args = (guild,)
        else:
            sql  = "SELECT id, description, deadline, assignee, status FROM missions WHERE guild=? AND done=? ORDER BY id"
            args = (guild, int(done_flag))
        return await db.execute_fetchall(sql, args)

async def complete_mission(mid, guild):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE missions SET done=1 WHERE id=? AND guild=?", (mid, guild))
        await db.commit()

async def update_mission_status(mid, guild, user_id, new_status):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchall(
            "SELECT assignee FROM missions WHERE id=? AND guild=?", (mid, guild)
        )
        if not row or row[0][0] != user_id:
            return False
        await db.execute("UPDATE missions SET status=? WHERE id=?", (new_status, mid))
        await db.commit()
        return True

# ─── Pagination View ────────────────────────────────────────────
PAGE_SIZE = 20
class MissionPager(discord.ui.View):
    def __init__(self, data, title: str, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.data  = data
        self.title = title
        self.page  = 0

    def make_embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title, colour=discord.Colour.green())
        start = self.page * PAGE_SIZE
        for mid, desc, dl_iso, uid, status in self.data[start:start+PAGE_SIZE]:
            line = f"**#{mid}** – {desc}"
            if dl_iso:
                ts = int(dt.datetime.fromisoformat(dl_iso).timestamp())
                line += f" _(échéance <t:{ts}:F>)_"
            line += f" ➜ <@{uid}> — **{status}**"
            embed.add_field(name="\u200b", value=line, inline=False)
        total = max(1, (len(self.data)-1)//PAGE_SIZE + 1)
        embed.set_footer(text=f"Page {self.page+1}/{total}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, inter: discord.Interaction, _):
        if self.page > 0:
            self.page -= 1
            await inter.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, inter: discord.Interaction, _):
        if (self.page+1)*PAGE_SIZE < len(self.data):
            self.page += 1
            await inter.response.edit_message(embed=self.make_embed(), view=self)

# ─── Slash-Commands : Missions ────────────────────────────────
@bot.tree.command(name="mission_add", description="Ajouter une mission")
@guard()
@app_commands.describe(
    membre="Membre assigné",
    description="Description de la mission",
    deadline="Date et heure limite (UTC) – facultatif"
)
async def mission_add(
    inter: discord.Interaction,
    membre: discord.Member,
    description: str,
    deadline: dt.datetime = None
):
    if membre.id == BLOCKED_RECEIVER:
        return await inter.response.send_message(
            f"🚫 {membre.mention} ne peut pas recevoir de missions.",
            ephemeral=True
        )

    dl_iso = deadline.isoformat() if deadline else None
    await add_mission(inter.guild_id, inter.user.id, membre.id, description, dl_iso)

    msg = f"✅ Mission assignée à {membre.mention}"
    if deadline:
        ts = int(deadline.timestamp())
        msg += f", échéance <t:{ts}:F>."
    else:
        msg += "."
    await inter.response.send_message(msg)

@bot.tree.command(name="mission_list", description="Lister les missions")
@app_commands.describe(etat="open|done|all (défaut open)")
async def mission_list(inter: discord.Interaction, etat: str = "open"):
    if etat == "open":
        flag, title = False, "Missions en cours"
    elif etat == "done":
        flag, title = True,  "Missions terminées"
    elif etat == "all":
        flag, title = None,  "Toutes les missions"
    else:
        return await inter.response.send_message("🛑 état invalide.", ephemeral=True)

    rows = await list_missions(inter.guild_id, flag)
    if not rows:
        return await inter.response.send_message("📭 Aucune mission trouvée.")

    view = MissionPager(rows, title)
    await inter.response.send_message(embed=view.make_embed(), view=view)

@bot.tree.command(name="mission_done", description="Marquer une mission terminée")
@guard()
@app_commands.describe(id="ID de la mission")
async def mission_done(inter: discord.Interaction, id: int):
    await complete_mission(id, inter.guild_id)
    await inter.response.send_message("🎯 Mission terminée !")

@bot.tree.command(name="mission_update", description="Mettre à jour votre avancement")
@app_commands.describe(
    id="ID de la mission",
    statut="Votre nouveau statut / avancement"
)
async def mission_update(inter: discord.Interaction, id: int, statut: str):
    ok = await update_mission_status(id, inter.guild_id, inter.user.id, statut)
    if not ok:
        return await inter.response.send_message(
            "🚫 Vous n’êtes pas l’assigné de cette mission.", ephemeral=True
        )
    await inter.response.send_message(f"✅ Statut mis à jour : **{statut}**")

# ─── Slash-Commands : Réunions ───────────────────────────────
@bot.tree.command(name="meeting_create", description="Planifier une réunion")
@guard()
@app_commands.describe(
    sujet="Titre",
    start="Date et heure de début (UTC)",
    duration="Durée en heures"
)
async def meeting_create(
    inter: discord.Interaction,
    sujet: str,
    start: dt.datetime,
    duration: float = 1.0
):
    end = start + dt.timedelta(hours=duration)
    await inter.response.defer(thinking=True)
    event = await inter.guild.create_scheduled_event(
        name=sujet,
        start_time=start,
        end_time=end,
        description=f"Réunion planifiée par {inter.user.display_name}",
        entity_type=discord.EntityType.voice,
        privacy_level=discord.PrivacyLevel.guild_only
    )
    await inter.followup.send(
        f"📅 Réunion **{sujet}** du <t:{int(start.timestamp())}:F> au <t:{int(end.timestamp())}:F>."
    )

@bot.tree.command(name="meeting_list", description="Afficher les réunions à venir")
async def meeting_list(inter: discord.Interaction):
    now = dt.datetime.now(dt.timezone.utc)
    events = [e for e in inter.guild.scheduled_events if e.start_time > now]
    if not events:
        return await inter.response.send_message("📭 Aucune réunion programmée.")
    embed = discord.Embed(title="Réunions à venir", colour=discord.Colour.purple())
    for e in events:
        ts = int(e.start_time.timestamp())
        embed.add_field(name=e.name, value=f"<t:{ts}:F> dans {e.channel.mention}", inline=False)
    await inter.response.send_message(embed=embed)

# ─── Rappels & deadlines ──────────────────────────────────────
@tasks.loop(hours=72)
async def notify_channel():
    async with aiosqlite.connect(DB_PATH) as db:
        gids = await db.execute_fetchall("SELECT DISTINCT guild FROM missions WHERE done=0")
    for (gid,) in gids:
        guild = bot.get_guild(gid)
        chan  = guild.system_channel or guild.text_channels[0]
        rows  = await list_missions(gid, False)
        if rows:
            await chan.send(f"🔔 {len(rows)} mission(s) en cours. `/mission_list`")

@tasks.loop(minutes=1)
async def deadline_check():
    now = dt.datetime.now(dt.timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT id, description, deadline, assignee, reminded_24, reminded_1 "
            "FROM missions WHERE done=0 AND deadline IS NOT NULL"
        )
    for mid, desc, dl_iso, uid, r24, r1 in rows:
        try:
            due = dt.datetime.fromisoformat(dl_iso)
        except Exception:
            continue
        diff = (due - now).total_seconds()
        user = bot.get_user(uid)
        if not user:
            continue
        if 0 < diff <= 3600 and not r1:
            await user.send(f"⏰ La mission « {desc} » est due dans 1 h !")
            async with aiosqlite.connect(DB_PATH) as db2:
                await db2.execute("UPDATE missions SET reminded_1=1 WHERE id=?", (mid,))
                await db2.commit()
        elif 3600 < diff <= 86400 and not r24:
            await user.send(f"⏰ La mission « {desc} » est due dans 24 h !")
            async with aiosqlite.connect(DB_PATH) as db2:
                await db2.execute("UPDATE missions SET reminded_24=1 WHERE id=?", (mid,))
                await db2.commit()

# ─── Gestion d’erreurs globales ─────────────────────────────
@bot.tree.error
async def on_app_error(inter: discord.Interaction, err: app_commands.AppCommandError):
    await inter.response.send_message(f"⚠️ Erreur : {err}", ephemeral=True)
    logger.exception(err)

# ─── Lancement ﹘ on_ready ﹘ main ─────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    if not notify_channel.is_running(): notify_channel.start()
    if not deadline_check.is_running():  deadline_check.start()
    logger.info(f"Connecté comme {bot.user} ({bot.user.id})")

if __name__ == "__main__":
    bot.run(TOKEN)

