import os, asyncio, logging, datetime as dt
import aiosqlite, discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

INTENTS = discord.Intents.none()
bot = commands.Bot(command_prefix="!", intents=INTENTS)

DB_PATH = "tasks.db"

# IDs of roles allowed to manage tasks
ALLOWED_ROLE_IDS = [
    1379268686141063289,
    1379268700145717374,
    1379268792122605619,
    1379268795536769137,
    1379268744991215769,
    1379268748824940575,
    1379268752335569036,
    1382834652162818089,
]

# ---------------- DB helpers ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   guild_id INTEGER,
                   author_id INTEGER,
                   description TEXT NOT NULL,
                   deadline TEXT,
                   done BOOLEAN DEFAULT 0
               );"""
        )
        await db.commit()

async def add_task(guild_id, author_id, descr, deadline):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tasks (guild_id, author_id, description, deadline) VALUES (?,?,?,?)",
            (guild_id, author_id, descr, deadline),
        )
        await db.commit()

async def list_tasks(guild_id, done=False):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT id, description, deadline FROM tasks WHERE guild_id=? AND done=?",
            (guild_id, int(done)),
        )
    return rows

async def complete_task(task_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET done=1 WHERE id=? AND guild_id=?", (task_id, guild_id)
        )
        await db.commit()

# ---------------- Role guard ----------------
def user_is_allowed(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if isinstance(member, discord.Member):
        return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)
    return False

def role_guard():
    async def predicate(interaction: discord.Interaction):
        if not user_is_allowed(interaction):
            raise app_commands.CheckFailure("Vous n’avez pas le rôle requis.")
        return True
    return app_commands.check(predicate)

# ---------------- Commands ----------------
@bot.tree.command(name="tache_ajouter", description="Ajouter une nouvelle tâche")
@role_guard()
@app_commands.describe(description="Description de la tâche",
                       deadline="Date limite (AAAA-MM-JJ HH:MM) – facultatif")
async def tache_ajouter(interaction: discord.Interaction,
                        description: str,
                        deadline: str | None = None):
    await add_task(interaction.guild_id, interaction.user.id, description, deadline)
    await interaction.response.send_message("✅ Tâche enregistrée !")

@bot.tree.command(name="tache_liste", description="Lister les tâches en cours")
async def tache_liste(interaction: discord.Interaction):
    rows = await list_tasks(interaction.guild_id, done=False)
    if not rows:
        await interaction.response.send_message("🎉 Rien à faire !")
        return
    embed = discord.Embed(title="Tâches en cours", colour=discord.Colour.blue())
    for _id, descr, dl in rows:
        line = f"**#{_id}** – {descr}"
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

# ---------------- Daily Reminder every 3 days ----------------
@tasks.loop(hours=72)
async def reminder_every_three_days():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT DISTINCT guild_id FROM tasks WHERE done=0")
    for (guild_id,) in rows:
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        chan = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
        if not chan:
            continue
        tasks_open = await list_tasks(guild_id, done=False)
        if tasks_open:
            await chan.send(
                f"🔔 Rappel (tous les 3 jours) : {len(tasks_open)} tâche(s) en attente. "
                "Pensez à /tache_liste !")

# ---------------- Events ----------------
@bot.event
async def on_ready():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await bot.tree.sync()
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")
    if not reminder_every_three_days.is_running():
        reminder_every_three_days.start()

if __name__ == "__main__":
    bot.run(TOKEN)
