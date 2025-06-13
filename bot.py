"""Discord Tasks & Meetings Bot (version sans dépendance loguru)
-----------------------------------------------------------------
- Gestion de tâches avec modals, rappels DM & channel.
- Planification de réunions (/reunion_creer) via Scheduled Events.
- Logging basé sur la bibliothèque standard `logging`.
"""

import os, datetime as dt, asyncio, logging
import aiosqlite, discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# ---------- Config & logging ----------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("TaskBot")

# ---------- Intents ----------
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
DB_PATH = "tasks.db"

# ---------- Rôles ----------
ASSIGNER_ROLE_IDS = [
    1379270374914789577, 1379270378672885811, 1379270389343191102,
    1379270382405554266, 1379270385861660703, 1379270400688652342,
    1382834652162818089,
]
UNASSIGNABLE_ROLE_IDS = [1379270374914789577]

# ---------- Helpers rôles ----------
def user_is_allowed(inter: discord.Interaction) -> bool:
    m = inter.user
    return isinstance(m, discord.Member) and any(r.id in ASSIGNER_ROLE_IDS for r in m.roles)

def role_guard():
    async def predicate(inter: discord.Interaction):
        if not user_is_allowed(inter):
            raise app_commands.CheckFailure("Vous n’avez pas le rôle requis.")
        return True
    return app_commands.check(predicate)

def can_be_assigned(member: discord.Member) -> bool:
    return all(r.id not in UNASSIGNABLE_ROLE_IDS for r in member.roles)

# ---------- Database ----------
CREATE_SQL = """CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    author_id INTEGER,
    assignee_id INTEGER,
    description TEXT NOT NULL,
    deadline TEXT,
    done BOOLEAN DEFAULT 0,
    reminded_24 BOOLEAN DEFAULT 0,
    reminded_1 BOOLEAN DEFAULT 0
);"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def add_task(guild_id, author_id, descr, deadline, assignee_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tasks (guild_id, author_id, description, deadline, assignee_id)"
            " VALUES (?,?,?,?,?)",
            (guild_id, author_id, descr, deadline, assignee_id)
        )
        await db.commit()

async def list_tasks(guild_id, done=False):
    async with aiosqlite.connect(DB_PATH) as db:
        return await db.execute_fetchall(
            "SELECT id, description, deadline, assignee_id FROM tasks WHERE guild_id=? AND done=?",
            (guild_id, int(done))
        )

async def complete_task(task_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET done=1 WHERE id=? AND guild_id=?", (task_id, guild_id))
        await db.commit()

# ---------- UI Modal ----------
class TaskModal(discord.ui.Modal, title="Nouvelle tâche"):
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph)
    deadline = discord.ui.TextInput(
        label="Deadline (AAAA-MM-JJ HH:MM, UTC)",
        required=False,
        placeholder="Optionnel"
    )

    def __init__(self, assignee: discord.Member):
        super().__init__()
        self.assignee = assignee

    async def on_submit(self, inter: discord.Interaction):
        deadline_str = self.deadline.value.strip() or None
        if deadline_str:
            try:
                dt.datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
            except ValueError:
                await inter.response.send_message("Date invalide. Format attendu AAAA-MM-JJ HH:MM", ephemeral=True)
                return
        await add_task(inter.guild_id, inter.user.id, self.description.value, deadline_str, self.assignee.id)
        await inter.response.send_message(f"✅ Tâche pour {self.assignee.mention} créée !")

# ---------- Slash-commands ----------
@bot.tree.command(name="tache_modal", description="Créer une tâche via formulaire")
@role_guard()
@app_commands.describe(membre="Membre assigné")
async def tache_modal(inter: discord.Interaction, membre: discord.Member):
    if not can_be_assigned(membre):
        await inter.response.send_message("🚫 Ce membre ne peut pas recevoir de tâche.", ephemeral=True)
        return
    await inter.response.send_modal(TaskModal(assignee=membre))

@bot.tree.command(name="tache_liste", description="Lister les tâches en cours")
async def tache_liste(inter: discord.Interaction):
    rows = await list_tasks(inter.guild_id, done=False)
    if not rows:
        await inter.response.send_message("🎉 Rien à faire !")
        return
    embed = discord.Embed(title="Tâches en cours", colour=discord.Colour.blue())
    for _id, descr, dl, uid in rows:
        line = f"**#{_id}** – {descr} ➜ <@{uid}>"
        if dl:
            line += f" _(délai : {dl})_"
        embed.add_field(name="\u200b", value=line, inline=False)
    await inter.response.send_message(embed=embed)

@bot.tree.command(name="tache_faite", description="Marquer une tâche comme terminée")
@role_guard()
@app_commands.describe(id="Numéro de la tâche")
async def tache_faite(inter: discord.Interaction, id: int):
    await complete_task(id, inter.guild_id)
    await inter.response.send_message("🎯 Bravo, c’est coché !")

@bot.tree.command(name="reunion_creer", description="Planifier une réunion Discord")
@role_guard()
@app_commands.describe(
    sujet="Titre",
    date="Date AAAA-MM-JJ (UTC)",
    heure="Heure HH:MM (UTC)",
    canal_vocal="Salon vocal"
)
async def reunion_creer(inter: discord.Interaction,
                        sujet: str,
                        date: str,
                        heure: str,
                        canal_vocal: discord.VoiceChannel):
    try:
        start = dt.datetime.strptime(f"{date} {heure}", "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        await inter.response.send_message("Format date/heure invalide.", ephemeral=True)
        return
    end = start + dt.timedelta(hours=1)
    event = await inter.guild.create_scheduled_event(
        name=sujet,
        start_time=start,
        end_time=end,
        description=f"Planifié par {inter.user.display_name}",
        channel=canal_vocal,
        entity_type=discord.EntityType.voice,
        privacy_level=discord.PrivacyLevel.guild_only
    )
    await inter.response.send_message(
        f"📅 Réunion **{sujet}** planifiée : <t:{int(start.timestamp())}:F> dans {canal_vocal.mention}\n▶️ <#{event.id}>"
    )

# ---------- Reminders ----------
@tasks.loop(hours=72)
async def channel_reminder():
    async with aiosqlite.connect(DB_PATH) as db:
        gids = await db.execute_fetchall("SELECT DISTINCT guild_id FROM tasks WHERE done=0")
    for (gid,) in gids:
        guild = bot.get_guild(gid)
        chan = guild.system_channel or (guild.text_channels[0] if guild and guild.text_channels else None)
        if not chan: continue
        tasks_open = await list_tasks(gid, done=False)
        if tasks_open:
            await chan.send(f"🔔 Rappel (3 jours) : {len(tasks_open)} tâche(s) en attente.")
            for _id, descr, dl, uid in tasks_open:
                await chan.send(f"• <@{uid}> → {descr}")

@tasks.loop(minutes=1)
async def deadline_watchdog():
    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT id, description, deadline, assignee_id, reminded_24, reminded_1 FROM tasks WHERE done=0 AND deadline IS NOT NULL"
        )
    for _id, descr, dl, uid, rem24, rem1 in rows:
        try:
            deadline = dt.datetime.strptime(dl, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        diff = (deadline - now).total_seconds()
        member = bot.get_user(uid)
        if member is None:
            continue
        if 0 < diff <= 3600 and not rem1:
            await member.send(f"⏰ *Rappel* : la tâche « {descr} » est due dans moins d'une heure !")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE tasks SET reminded_1=1 WHERE id=?", (_id,))
                await db.commit()
        elif 3600 < diff <= 86400 and not rem24:
            await member.send(f"⏰ *Rappel* : la tâche « {descr} » est due dans 24 h.")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE tasks SET reminded_24=1 WHERE id=?", (_id,))
                await db.commit()

# ---------- Error Handling ----------
@bot.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    await inter.response.send_message(f"⛔ {error.__class__.__name__}: {error}", ephemeral=True)
    logger.exception(f"Slash error: {error}")

# ---------- Events ----------
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    if not channel_reminder.is_running():
        channel_reminder.start()
    if not deadline_watchdog.is_running():
        deadline_watchdog.start()
    logger.info(f"Connecté en tant que {bot.user} ({bot.user.id})")

# ---------- Main ----------
if __name__ == "__main__":
    bot.run(TOKEN)
