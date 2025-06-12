# bot.py – Discord Tasks Bot
# -------------------------------------------
import os, asyncio, logging, datetime as dt
import aiosqlite, discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# ────────────────────────────────────────────
# 1. Rôles
ASSIGNER_ROLE_IDS = [
    1379270374914789577, 1379270378672885811, 1379270389343191102,
    1379270382405554266, 1379270385861660703, 1379270400688652342,
    1382834652162818089,
]
UNASSIGNABLE_ROLE_IDS = [1379270374914789577]   # ne jamais recevoir de tâche
# ────────────────────────────────────────────

INTENTS = discord.Intents.none()        # suffisant pour les slash-commands
bot = commands.Bot(command_prefix="!", intents=INTENTS)
DB_PATH = "tasks.db"


# ──────────── Helpers rôles ────────────────
def user_is_allowed(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and any(
        r.id in ASSIGNER_ROLE_IDS for r in member.roles
    )


def role_guard():                       # décorateur
    async def predicate(interaction: discord.Interaction):
        if not user_is_allowed(interaction):
            raise app_commands.CheckFailure("Vous n’avez pas le rôle requis.")
        return True
    return app_commands.check(predicate)


def can_be_assigned(member: discord.Member) -> bool:
    return all(r.id not in UNASSIGNABLE_ROLE_IDS for r in member.roles)


# ──────────── Base SQLite ──────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   guild_id INTEGER,
                   author_id INTEGER,
                   assignee_id INTEGER,
                   description TEXT NOT NULL,
                   deadline TEXT,
                   done BOOLEAN DEFAULT 0
               );"""
        )
        await db.commit()


async def add_task(guild_id, author_id, descr, deadline, assignee_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tasks (guild_id, author_id, description, deadline, assignee_id)"
            " VALUES (?,?,?,?,?)",
            (guild_id, author_id, descr, deadline, assignee_id),
        )
        await db.commit()


async def list_tasks(guild_id, done=False):
    async with aiosqlite.connect(DB_PATH) as db:
        return await db.execute_fetchall(
            "SELECT id, description, deadline, assignee_id "
            "FROM tasks WHERE guild_id=? AND done=?",
            (guild_id, int(done)),
        )


async def complete_task(task_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET done=1 WHERE id=? AND guild_id=?", (task_id, guild_id)
        )
        await db.commit()


# ──────────── Slash-commands ───────────────
@bot.tree.command(name="tache_ajouter", description="Ajouter une nouvelle tâche")
@role_guard()
@app_commands.describe(
    description="Description de la tâche",
    membre="Membre à qui l’assigner",
    deadline="Date limite (AAAA-MM-JJ HH:MM) – facultatif",
)
async def tache_ajouter(interaction: discord.Interaction,
                        description: str,
                        membre: discord.Member,
                        deadline: str | None = None):
    if not can_be_assigned(membre):
        await interaction.response.send_message(
            f"🚫 {membre.mention} ne peut pas recevoir de tâches.",
            ephemeral=True,
        )
        return

    await add_task(interaction.guild_id, interaction.user.id,
                   description, deadline, membre.id)
    await interaction.response.send_message(
        f"✅ Tâche assignée à {membre.mention} !")


@bot.tree.command(name="tache_liste", description="Lister les tâches en cours")
async def tache_liste(interaction: discord.Interaction):
    rows = await list_tasks(interaction.guild_id, done=False)
    if not rows:
        await interaction.response.send_message("🎉 Rien à faire !")
        return

    embed = discord.Embed(title="Tâches en cours", colour=discord.Colour.blue())
    for _id, descr, dl, assignee_id in rows:
        line = f"**#{_id}** – {descr} ➜ <@{assignee_id}>"
        if dl:
            line += f" _(délai : {dl})_"
        embed.add_field(name="\u200b", value=line, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="tache_faite", description="Marquer une tâche comme terminée")
@role_guard()
@app_commands.describe(id="Numéro de la tâche (voir /tache_liste)")
async def tache_faite(interaction: discord.Interaction, id: int):
    await complete_task(id, interaction.guild_id)
    await interaction.response.send_message("🎯 Bravo, c’est coché !")


# ──────────── Rappel toutes les 72 h ───────
@tasks.loop(hours=72)
async def reminder_every_three_days():
    async with aiosqlite.connect(DB_PATH) as db:
        guild_ids = await db.execute_fetchall(
            "SELECT DISTINCT guild_id FROM tasks WHERE done=0"
        )
    for (guild_id,) in guild_ids:
        guild = bot.get_guild(guild_id)
        chan = guild.system_channel or (guild.text_channels[0] if guild and guild.text_channels else None)
        if not chan:
            continue
        tasks_open = await list_tasks(guild_id, done=False)
        if tasks_open:
            await chan.send(
                f"🔔 Rappel (tous les 3 jours) : {len(tasks_open)} tâche(s) en attente."
            )
            for _id, descr, dl, assignee_id in tasks_open:
                await chan.send(f"• <@{assignee_id}> → {descr}")


# ──────────── Événements ───────────────────
@bot.event
async def on_ready():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await bot.tree.sync()
    if not reminder_every_three_days.is_running():
        reminder_every_three_days.start()
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")


# ──────────── Lancement ────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)

