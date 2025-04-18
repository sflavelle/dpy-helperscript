# Import essential libraries
import json
import logging
import os
import sys
import subprocess
import signal

import discord
import requests
import yaml
from aioconsole import aexec
from contextlib import closing
import sqlite3
import psycopg2 as psql
from discord import app_commands
from discord.ext import commands

# setup logging
logger = logging.getLogger('discord')
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('[%(name)s][%(levelname)s] %(message)s'))
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# init vars?
cfg = None
itemlog_processes = {}

# load config
with open('config.yaml', 'r') as file:
    cfg = yaml.safe_load(file)

# Database connections
# Fact DB
with closing(sqlite3.connect("facts.db")) as factdb:
    with closing(factdb.cursor()) as cursor:
        # init table
        cursor.execute("CREATE TABLE IF NOT EXISTS facts (fact TEXT, source TEXT, keyword TEXT)")

# configure subscribed intents
intents = discord.Intents.default()

# setup command framework
splatbot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
    allowed_installs=app_commands.AppInstallationType(guild=False, user=True)
)


@splatbot.tree.command()
@app_commands.describe(room_url="Link to the Archipelago room",
                       comment="Additional comment to prefix the room details with",
                       public="Whether to post publically or to yourself",
                       include_log="Include a link to the server log",
                       include_files="Set a link to patch files etc to include in the post",
                       include_games="List out each player's games as well")
async def ap_roomdetails(interaction: discord.Interaction,
                         room_url: str,
                         comment: str = None,
                         public: bool = True,
                         include_log: bool = False,
                         include_files: str = None,
                         include_games: bool = False):
    """Post the details of an Archipelago room to the channel."""

    deferpost = await interaction.response.defer(ephemeral=not public, thinking=True)
    newpost = await interaction.original_response()

    room_id = room_url.split('/')[-1]
    hostname = room_url.split('/')[2]

    match room_url.split('/')[3]:
        case "tracker":
            await newpost.edit(content=f"**:no_entry_sign: You tried!**\n{interaction.user.display_name} gave me a tracker link, "
                                       "but I need a room URL to post room details.")
            raise ValueError

    api_url = f"https://{hostname}/api/room_status/{room_id}"

    room = requests.get(api_url)
    room_json = room.json()

    players = [p[0] for p in room_json['players']]

    # Form message
    msg = ""
    if comment: msg = comment + "\n"
    msg += room_url + "\n"
    if bool(include_files): msg += f"Patches + Misc Files: {include_files}\n"
    if include_games:
        msg += f"Players:\n{"\n".join(sorted([f"**{p[0]}**: {p[1]}" for p in room_json['players']]))}"
    else:
        msg += f"Players: {", ".join(sorted(players))}"
    await newpost.edit(content=msg)

ap_itemlog = app_commands.Group(name="ap_itemlog",description="Manage an item logging webhook")

@ap_itemlog.command(name="start")
async def ap_itemlog_start(interaction: discord.Interaction, webhook: str, log_url: str, spoiler_url: str = None):
    """Start logging messages from an Archipelago room log to a specified webhook"""
    global itemlog_processes
    script_path = os.path.join(os.path.dirname(__file__), 'ap_itemlog.py')

    env = os.environ.copy()
    env['LOG_URL'] = log_url
    env['WEBHOOK_URL'] = webhook
    env['SESSION_COOKIE'] = cfg['bot']['archipelago']['session_cookie']
    env['SPOILER_URL'] = spoiler_url if spoiler_url else None
    env['MSGHOOK_URL'] = log['msghook'] if log['msghook'] else None
    

    ping = requests.get(webhook, timeout=1)

    if ping.status_code == 200 and 'application/json' in ping.headers['content-type']:
        ping_log = requests.get(log_url, cookies={'session': cfg['bot']['archipelago']['session_cookie']}, timeout=3)
        if ping_log.status_code == 200:
            # All checks successful, start the script
            process = subprocess.Popen([sys.executable, script_path], env=env)
            await interaction.response.send_message(f"Started logging messages from {log_url} to a webhook. PID: {process.pid}", ephemeral=True)

            # Save script to config
            if 'itemlogs' not in cfg['bot']['archipelago']:
                cfg['bot']['archipelago']['itemlogs'] = []

            cfg['bot']['archipelago']['itemlogs'].append({
                'guild': interaction.guild.id,
                'webhook': webhook,
                'log_url': log_url,
                'spoiler_url': spoiler_url if spoiler_url else None,
            })

            itemlog_processes.update({interaction.guild.id: process.pid})

            with open('config.yaml', 'w') as file:
                yaml.dump(cfg, file)
                logger.info(f"Saved AP log {log_url} to config.")
        else:
            await interaction.response.send_message(f"Could not validate {log_url}: Status code {ping.status_code}. {"You'll need your session cookie from the website." if ping.status_code == 403 else ""}", ephemeral=True)
    else:
        await interaction.response.send_message(f"Could not validate {webhook}: Status code {ping.status_code}.",
                                          ephemeral=True)

@ap_itemlog.command(name="stop")
async def ap_itemlog_stop(interaction: discord.Interaction, guild: str):
    """Stops the log monitoring script."""
    global itemlog_processes
    pid = itemlog_processes.get(guild)
    if pid:
        os.kill(pid, signal.SIGTERM)
        await interaction.response.send_message(f"Stopped log monitoring script with PID: {pid}", ephemeral=True)
        del itemlog_processes[guild]
    else:
        await interaction.response.send_message("No log monitoring script is currently running.", ephemeral=True)

# @ap_itemlog_stop.autocomplete('guild')
# async def itemlog_get_running(interaction: discord.Interaction, current: int) -> list[app_commands.Choice[int]]:
#     choices = [scr['guild'] for scr in cfg['bot']['archipelago']['itemlogs']]
#     return [
#         app_commands.Choice(name=str(choice), value=choice)
#         for choice in choices
#     ]

@ap_itemlog.command(name="sql")
async def ap_itemlog_sql(interaction: discord.Interaction, cmd: str):
    with psql.connect(
        dbname="archipelago",
        user="postgres",
        host="localhost"
    ) as conn:
        with conn.cursor() as curs:
            try:
                curs.execute(cmd)
                resp = curs.fetchone()
                if bool(resp):
                    await interaction.response.send_message(str(resp), ephemeral=True)
                else:
                    await interaction.response.send_message(f"SQL command sent.",ephemeral=True)
            except (psql.errors.UndefinedColumn) as e:
                raise


@splatbot.tree.command()
async def eval(interaction: discord.Interaction):
    """Run a command in Splatbot's context."""
    class CmdDialog(discord.ui.Modal, title='Run a Command'):
        cmd = discord.ui.TextInput(label='Command',style=discord.TextStyle.paragraph)

@splatbot.tree.command()
@app_commands.describe(command="Command to send to Home Assistant")
async def home(interaction: discord.Interaction, command: str):
    """Send a message to Home Assistant's Assist API, eg 'turn on the lights'."""

    global cfg

    api_url = cfg['hass']['url'] if cfg['hass'] else None
    api_token = cfg['hass']['token'] if cfg['hass'] else None

    if api_url is None or api_token is None:
        await interaction.response.send_message(
            ":no_entry_sign: Tell the bot owner to configure their Home Assistant API's URL and Long Lived Token.",
            ephemeral=True)
        raise NotImplementedError("API credentials not configured")

    api_headers = {
        "Authorization": f"Bearer {api_token}",
        "content-type": "application/json"
    }

    sentreq = {
        "text": command,
        "language": "en"
    }

    req = requests.post(
        f"{api_url}/api/conversation/process",
        headers=api_headers,
        data=json.dumps(sentreq),
        timeout=10
    )
    response = req.json()
    # if response.status_code == requests.codes.ok:
    recvtext = response["response"]["speech"]["plain"]["speech"]
    await interaction.response.send_message(f"> {command}\n{recvtext}", ephemeral=True)

factgroup = app_commands.Group(name='facts',description='Some totally legit facts')

@factgroup.command(name="get")
async def fact_get(interaction: discord.Interaction, public: bool = True):
    """Post a totally legitimate factoid"""
    with closing(sqlite3.connect("facts.db")) as factdb:
        with closing(factdb.cursor()) as cursor:
            fact = cursor.execute('SELECT * from facts order by random() limit 1').fetchall()[0]
            if len(fact) == 0:
                await interaction.response.send_message(":no_entry_sign: No facts available!")
                return

    template = """**Fact:** {factstr}
-# Source: <{source}>
-# Disclaimer: Facts reported by this command are not factual, informative, or any combination of the prior."""

    print(fact)

    await interaction.response.send_message(template.format(
        factstr=fact[0],
        source=fact[1]
    ), ephemeral = not public)

@factgroup.command(name="add")
async def fact_add(interaction: discord.Interaction, fact: str, keyword: str, source: str = "no source"):
    """Add a fact to the database"""
    row = (fact, source, keyword)
    with closing(sqlite3.connect("facts.db")) as factdb:
        with closing(factdb.cursor()) as cursor:
            cursor.execute(f"insert into facts values (?,?,?)", row)
            factdb.commit()
            if bool(cursor.lastrowid):
                await interaction.response.send_message(":white_check_mark: Added successfully. "
                    f"Items: {cursor.lastrowid + 1}", ephemeral=True)

splatbot.tree.add_command(factgroup)
splatbot.tree.add_command(ap_itemlog)

@splatbot.event
async def on_ready():
    global itemlog_processes

    logger.info(f"Logged in. I am {splatbot.user} (ID: {splatbot.user.id})")
    await splatbot.tree.sync()

    # Run itemlogs if any are configured
    if len(cfg['bot']['archipelago']['itemlogs']) > 0:
        logger.info("Starting saved itemlog processes.")
        for log in cfg['bot']['archipelago']['itemlogs']:
            logger.info(f"Starting itemlog for guild ID {log['guild']}")
            env = os.environ.copy()
        
            env['LOG_URL'] = log['log_url']
            env['WEBHOOK_URL'] = log['webhook']
            env['SESSION_COOKIE'] = cfg['bot']['archipelago']['session_cookie']
            env['SPOILER_URL'] = log['spoiler_url'] if log['spoiler_url'] else None
            env['MSGHOOK_URL'] = log['msghook'] if log['msghook'] else None
        
            try: 
                script_path = os.path.join(os.path.dirname(__file__), 'ap_itemlog.py')
                process = subprocess.Popen([sys.executable, script_path], env=env)
                itemlog_processes.update({log['guild']: process.pid})
            except:
                logger.error("Error starting log:",exc_info=True)


splatbot.run(cfg['bot']['discord_token'],
             log_handler=None
             )
