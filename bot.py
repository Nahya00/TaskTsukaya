#!/usr/bin/env python3
# bot.py – Gestion de missions & réunions (avec suivi d’avancement)
# ===============================================================

import os
import datetime as dt
import logging

import aiosqlite
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# ─── Config & Logging ─────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("MgmtBot")

# ─── Intents & Bot ───────────────────────────────────────
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.guilds  = True

bot = commands.Bot(command_prefix=None, intents=INTENTS)
DB = "missions.db"

# ─── Rôles autorisés ─────────────────────────────────────
ASSIGNER_ROLES    = [
    1379270378672885811, 1379270389343191102,
    1379270382405554266, 1379270385861660703,
    1379270400688652342, 1382834652162818089,
]
BLOCKED_RECEIVER  = 1379270374914789577  # ne peut pas recevoir de mission

def is_assigner(inter: discord.Interaction) -> bool:
    return any(r.id in ASSIGNER_ROLES for r in getattr(inter.user, "roles", []))

def guard():  # décorateur pour slash-commands de gestion
    async def pred(inter: discord.Interaction):
        if not is_assigner(inter):
            raise app_commands.CheckFailure("🚫 Vous n’avez pas le rôle requis.")
        return True
    return app_commands.check(pred)

# ─── Initialisation de la BDD ────────────────────────────
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
    async with aiosqlite.connect(DB) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def add_mission(guild, author, assignee, desc, dl):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO missions (guild, author, assignee, description, deadline) VALUES (?,?,?,?,?)",
            (guild, author, assignee, desc, dl)
        )
        await db.commit()

async def list_missions(guild, done: bool | None):
    async with aiosqlite.connect(DB) as db:
        if done is None:
            sql  = "SELECT id, description, deadline, assignee, status FROM missions WHERE guild=? ORDER BY id"
            args = (guild,)
        else:
            sql  = "SELECT id, description, deadline, assignee, status FROM missions WHERE guild=? AND done=? ORDER BY id"
            args = (guild, int(done))
        return await db.execute_fetchall(sql, args)

async def complete_mission(mid, guild):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE missions SET done=1 WHERE id=? AND guild=?", (mid, guild))
        await db.commit()

async def update_mission_status(mid, guild, user, new_status):
    # vérifie que c'est bien l'assigné qui fait la mise à jour
    async with aiosqlite.connect(DB) as db:
        row = await db.execute_fetchall(
            "SELECT assignee FROM missions WHERE id=? AND guild=?", (mid, guild)
        )
        if not row or row[0][0] != user:
            return False
        await db.execute(
            "UPDATE missions SET status=? WHERE id=?", (new_status, mid)
        )
        await db.commit()
        return True

# ─── Paginateur pour Missions ─────────────────────────────
PAGE_SIZE = 20
class MissionPager(discord.ui.View):
    def __init__(self, data, title:str, timeout=180):
        super().__init__(timeout=timeout)
        self.data  = data
        self.title = title
        self.page  = 0

    def make_embed(self):
        embed = discord.Embed(title=self.title, colour=discord.Colour.green())
        start = self.page * PAGE_SIZE
        for mid, desc, dl, uid, status in self.data[start:start+PAGE_SIZE]:
            line = f"**#{mid}** – {desc}"
            if dl:       line += f" _(délai : {dl})_"
            line += f" ➜ <@{uid}>"
            line += f" — **{status}**"
            embed.add_field(name="\u200b", value=line, inline=False)
        total = max(1, (len(self.data)-1)//PAGE_SIZE+1)
        embed.set_footer(text=f"Page {self.page+1}/{total}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, inter, _):
        if self.page>0:
            self.page-=1
            await inter.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, inter, _):
        if (self.page+1)*PAGE_SIZE < len(self.data):
            self.page+=1
            await inter.response.edit_message(embed=self.make_embed(), view=self)

# ─── Slash-Commands Missions ─────────────────────────────
@bot.tree.command(name="mission_add", description="Ajouter une mission")
@guard()
@app_commands.describe(
    membre="Membre assigné",
    description="Description",
    deadline="AAAA-MM-JJ HH:MM (UTC) – facultatif"
)
async def mission_add(inter: discord.Interaction,
                      membre: discord.Member,
                      description: str,
                      deadline: str|None = None):
    if membre.id == BLOCKED_RECEIVER:
        return await inter.response.send_message(
            f"🚫 {membre.mention} ne peut pas recevoir de missions.", ephemeral=True
        )
    if deadline:
        try:
            dt.datetime.strptime(deadline, "%Y-%m-%d %H:%M")
        except ValueError:
            return await inter.response.send_message(
                "📅 Format invalide (AAAA-MM-JJ HH:MM).", ephemeral=True
            )
    await add_mission(inter.guild_id, inter.user.id, membre.id, description, deadline)
    await inter.response.send_message(f"✅ Mission assignée à {membre.mention} !")

@bot.tree.command(name="mission_list", description="Lister les missions")
@app_commands.describe(etat="open|done|all (défaut open)")
async def mission_list(inter: discord.Interaction, etat: str="open"):
    match etat:
        case "open": f=False; title="Missions en cours"
        case "done": f=True;  title="Missions terminées"
        case "all":  f=None;  title="Toutes les missions"
        case _:
            return await inter.response.send_message("🛑 état invalide.", ephemeral=True)
    rows = await list_missions(inter.guild_id, f)
    if not rows:
        return await inter.response.send_message("📭 Aucune mission trouvée.")
    view = MissionPager(rows, title=title)
    await inter.response.send_message(embed=view.make_embed(), view=view)

@bot.tree.command(name="mission_done", description="Marquer mission terminée")
@guard()
@app_commands.describe(id="Numéro de la mission")
async def mission_done(inter: discord.Interaction, id: int):
    await complete_mission(id, inter.guild_id)
    await inter.response.send_message("🎯 Mission terminée !")

@bot.tree.command(name="mission_update", description="Mettre à jour votre avancement")
@app_commands.describe(
    id="ID de la mission",
    statut="Votre nouveau statut ou avancement"
)
async def mission_update(inter: discord.Interaction, id: int, statut: str):
    ok = await update_mission_status(id, inter.guild_id, inter.user.id, statut)
    if not ok:
        return await inter.response.send_message(
            "🚫 Vous n’êtes pas l’assigné de cette mission.", ephemeral=True
        )
    await inter.response.send_message(f"✅ Statut mis à jour : **{statut}**")

# ─── Slash-Commands Réunions ─────────────────────────────
@bot.tree.command(name="meeting_create", description="Planifier une réunion")
@guard()
@app_commands.describe(
    sujet="Titre",
    date="AAAA-MM-JJ (UTC)",
    heure="HH:MM (UTC)",
    canal="Salon vocal"
)
async def meeting_create(inter: discord.Interaction,
                         sujet: str,
                         date: str,
                         heure: str,
                         canal: discord.VoiceChannel):
    try:
        start = dt.datetime.strptime(f"{date} {heure}", "%Y-%m-%d %H:%M")
    except ValueError:
        return await inter.response.send_message(
            "🗓 Format date/heure invalide.", ephemeral=True
        )
    start = start.replace(tzinfo=dt.timezone.utc)
    event = await inter.guild.create_scheduled_event(
        name=sujet,
        start_time=start,
        end_time=start+dt.timedelta(hours=1),
        description=f"Réunion planifiée par {inter.user}",
        channel=canal,
        entity_type=discord.EntityType.voice,
        privacy_level=discord.PrivacyLevel.guild_only
    )
    await inter.response.send_message(
        f"📅 Réunion **{sujet}** planifiée pour <t:{int(start.timestamp())}:F>."
    )

@bot.tree.command(name="meeting_list", description="Afficher réunions à venir")
async def meeting_list(inter: discord.Interaction):
    events = [e for e in inter.guild.scheduled_events if e.start_time > dt.datetime.now(dt.timezone.utc)]
    if not events:
        return await inter.response.send_message("📭 Pas de réunion programmée.")
    embed = discord.Embed(title="Réunions à venir", colour=discord.Colour.purple())
    for e in events:
        ts = int(e.start_time.timestamp())
        embed.add_field(name=e.name, value=f"<t:{ts}:F> dans {e.channel.mention}", inline=False)
    await inter.response.send_message(embed=embed)

# ─── Rappels automatiques & deadlines ────────────────────
@tasks.loop(hours=72)
async def notify_channel():
    async with aiosqlite.connect(DB) as db:
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
    async with aiosqlite.connect(DB) as db:
        rows = await db.execute_fetchall(
            "SELECT id, description, deadline, assignee, reminded_24, reminded_1 "
            "FROM missions WHERE done=0 AND deadline IS NOT NULL"
        )
    for mid, desc, dl, uid, r24, r1 in rows:
        try:
            due = dt.datetime.strptime(dl, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        except:
            continue
        diff = (due - now).total_seconds()
        user = bot.get_user(uid)
        if not user: continue
        if 0 < diff <= 3600 and not r1:
            await user.send(f"⏰ La mission « {desc} » est due dans 1 h !")
            async with aiosqlite.connect(DB) as db2:
                await db2.execute("UPDATE missions SET reminded_1=1 WHERE id=?", (mid,))
                await db2.commit()
        elif 3600 < diff <= 86400 and not r24:
            await user.send(f"⏰ La mission « {desc} » est due dans 24 h.")
            async with aiosqlite.connect(DB) as db2:
                await db2.execute("UPDATE missions SET reminded_24=1 WHERE id=?", (mid,))
                await db2.commit()

# ─── Gestion d’erreurs globales ─────────────────────────
@bot.tree.error
async def on_app_error(inter: discord.Interaction, err: app_commands.AppCommandError):
    await inter.response.send_message(f"⚠️ Erreur : {err}", ephemeral=True)
    logger.exception(err)

# ─── Lancement ─────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    if not notify_channel.is_running(): notify_channel.start()
    if not deadline_check.is_running():  deadline_check.start()
    logger.info(f"Connecté comme {bot.user} ({bot.user.id})")

if __name__ == "__main__":
    bot.run(TOKEN)

