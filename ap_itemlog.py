import datetime
import json
import time
import regex as re
import random
import os
import sys
import ast
import logging
from collections import defaultdict
import requests
import fnmatch
import threading
import yaml
from cmds.ap_scripts.utils import Game, Item, Player, PlayerSettings, handle_item_tracking, handle_location_tracking, handle_location_hinting
from cmds.ap_scripts.emitter import event_emitter
from word2number import w2n
from flask import Flask, jsonify, Response
import psycopg2 as psql


with open('config.yaml', 'r', encoding='UTF-8') as file:
    cfg = yaml.safe_load(file)

sqlcfg = cfg['bot']['archipelago']['psql']
try:
    sqlcon = psql.connect(
        dbname=sqlcfg['database'],
        user=sqlcfg['user'],
        password=sqlcfg['password'] if 'password' in sqlcfg else None,
        host=sqlcfg['host'],
        port=sqlcfg['port']
    )
except psql.OperationalError:
    # TODO Disable commands that need SQL connectivity
    sqlcon = False

# Disclaimer: Copilot helped me with the initial setup of this file.
# Everything since is my own code. Thank you :-)

# URL of the log file and Discord webhook URL from environment variables
log_url = os.getenv('LOG_URL')
webhook_url = os.getenv('WEBHOOK_URL')
session_cookie = os.getenv('SESSION_COOKIE')

# Extra info for additional features
seed_url = os.getenv('SPOILER_URL')
msg_webhook = os.getenv('MSGHOOK_URL')

if not (bool(log_url) or bool(webhook_url) or bool(session_cookie)):
    logger.error("Something required isn't configured properly!")
    for var in [("LOG_URL",log_url), ("WEBHOOK_URL",webhook_url), ("SESSION_COOKIE",session_cookie)]:
        logger.error(f"{var[0]}: {var[1]}")
    sys.exit(1)

room_id = log_url.split('/')[-1]
hostname = log_url.split('/')[2]
seed_id = None

log_url = f"https://{hostname}/log/{room_id}"
api_url = f"https://{hostname}/api/room_status/{room_id}"
spoiler_url = None

if bool(seed_url):
    seed_id = seed_url.split('/')[-1]
    spoiler_url = f"https://{hostname}/dl_spoiler/{seed_id}"

seed_address = None

# setup logging
logger = logging.getLogger('ap_itemlog')
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('[%(name)s %(process)d][%(levelname)s] %(message)s'))
logfile = logging.FileHandler(f"logs/room_{room_id}.log",encoding="UTF-8")
logfile.setFormatter(logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s'))
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.addHandler(logfile)

# Time interval between checks (in seconds)
INTERVAL = 60
# Maximum Discord message length in characters
MAX_MSG_LENGTH = 2000

# Buffer to store release and related sent item messages
release_buffer = {}
message_buffer = []

# Store for players, items, settings
game = Game()
game.room_id = room_id
start_time = None

# small functions
goaled = lambda player : game.players[player].is_finished()
dim_if_goaled = lambda p : "-# " if goaled(p) else ""
to_epoch = lambda timestamp : time.mktime(datetime.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f").timetuple())

def join_words(words):
    if len(words) > 2:
        return '%s, and %s' % ( ', '.join(words[:-1]), words[-1] )
    elif len(words) == 2:
        return ' and '.join(words)
    else:
        return words[0]

# Spoiler Log Processing

def process_spoiler_log(seed_url):
    global game
    global start_time

    spoiler_url = f"https://{hostname}/dl_spoiler/{seed_id}"

    spoiler_log = requests.get(spoiler_url, timeout=10)
    spoiler_text = spoiler_log.text.split('\n')

    parse_mode = "Seed Info"
    working_player = None

    regex_patterns = {
        'location': re.compile(r'(.+) \((.+?)\): (.+) \((.+?)\)$'),
        'starting_item': re.compile(r'^(.+) \((.+?)\)$'),
        'pokemon_locations': re.compile(r'^Wild Pokemon \((.+?)\):$')
    }

    def parse_to_type(value):
        constructors = [int, float, str]
        if value == '': return None
        if value.lower() in ['yes', 'true']: return True
        elif value.lower() in ['no', 'false']: return False
        for c in constructors:
            try:
                return c(value)
            except ValueError:
                pass

    for line in spoiler_text:
        line = str(line)
        if len(line) == 0:
            continue

        if line.startswith("Archipelago Version"):
            parse_mode = "Seed Info"
        if line.startswith("Player "):
            parse_mode = "Players"
            working_player = line.strip().split(':', 1)[1].strip()
            logger.info(f"Parsing settings for player {working_player}")
        if line == "Locations:":
            parse_mode = "Locations"
            logger.info("Parsing multiworld locations")
            continue
        if line == "Starting Items:":
            parse_mode = "Starting Items"
            logger.info("Parsing starting items")
        if line in ["Entrances:","Medallions:","Fairy Fountain Bottle Fill:", "Shops:"]:
            parse_mode = None
        if line.startswith("Spoiler and info for [Jigsaw]"):
            parse_mode = "Jigsaw Info"
            working_player_num = 0
            try:
                working_player_num = int(line.rsplit(' ', 1)[-1].strip()) - 1
                working_player = game.players[list(game.players.keys())[working_player_num]].name
                logger.info(f"Parsing Jigsaw settings for player {working_player}")
            except (ValueError, IndexError):
                logger.error(f"Error parsing Jigsaw player number from line: {line}")
                working_player = None
        if match := regex_patterns['pokemon_locations'].match(line):
            parse_mode = "Pokemon Locations"
            working_player = match.group(1)
            game.players[working_player].settings['Wild Pokemon Locations'] = {}
            logger.info(f"Parsing Pokemon locations for player {working_player}")

        match parse_mode:
            case "Seed Info":
                if line.startswith("Archipelago"):
                    game.version_generator = line.split(' ')[2]
                    game.seed = parse_to_type(line.split(' ')[-1])
                    logger.info(f"Parsing seed {game.seed}")
                    logger.info(f"Generated on Archipelago version {game.version_generator}")
                    with sqlcon.cursor() as cursor:
                        game.pushdb(cursor, 'pepper.ap_all_rooms', 'seed', game.seed)
                        game.pushdb(cursor, 'pepper.ap_all_rooms', 'version', game.version_generator)
                        sqlcon.commit()
                else:
                    current_key, value = line.strip().split(':', 1)
                    if "," in value.lstrip():
                        # Parse as a list
                        game.world_settings[current_key.strip()] = [parse_to_type(v.strip()) for v in value.lstrip().split(',')]
                    else: game.world_settings[current_key.strip()] = parse_to_type(value.lstrip())

            case "Players":
                try:
                    def parse_line(line):
                        current_key, value = line.strip().split(':', 1)
                        value_str = value.lstrip()
                        key = current_key.strip().replace("_", " ")

                        return key, value_str
                    
                    key, value_str = parse_line(line)
                    if key.startswith("Player "): continue  # Skip player header lines
                    game.players[working_player].settings[key] = parse_to_type(value_str)

                    # Try to parse as a list (comma-separated, not inside brackets)
                    if "," in value_str and not (value_str.startswith("[") or value_str.startswith("{")):
                        game.players[working_player].settings[key] = [parse_to_type(v.strip()) for v in value_str.split(",")]
                        continue
                except ValueError as e:
                    logger.error(f"Error parsing line:")
                    logger.error(line)
                    logger.error(f"Error: {e}")
                    continue

                # Try to parse as JSON (object or array)
                try:
                    # If it looks like a JSON object or array
                    if value_str.startswith("{") or value_str.startswith("["):
                        game.players[working_player].settings[key] = json.loads(value_str)
                        continue
                    # If it looks like a dict without braces, add braces and try to parse
                    # Only trigger if it contains ':' and at least one comma, and does not start with '{' or '['
                    if ":" in value_str and "," in value_str and not (value_str.startswith("{") or value_str.startswith("[")):
                        json_str = "{" + value_str + "}"
                        # Add quotes to keys for JSON compatibility
                        json_str = re.sub(r'(\w[\w\- ]*):', r'"\1":', json_str)
                        game.players[working_player].settings[key] = json.loads(json_str)
                        continue
                    # If it looks like a dict without braces and only one key:value, handle that too
                    if ":" in value_str and not (value_str.startswith("{") or value_str.startswith("[")) and "," not in value_str:
                        json_str = "{" + value_str + "}"
                        json_str = re.sub(r'(\w[\w\- ]*):', r'"\1":', json_str)
                        game.players[working_player].settings[key] = json.loads(json_str)
                        continue

                    if type(game.players[working_player].settings[key]) in [list]:
                        # If the value is a list, parse it as such
                        game.players[working_player].settings[key] = [parse_to_type(v.strip()) for v in game.players[working_player].settings[key]]
                        continue
                except Exception:
                    pass

                # Fallback: store as string
                game.players[working_player].settings[key] = parse_to_type(value_str)
            case "Locations":
                if match := regex_patterns['location'].match(line):
                    item_location, sender, item, receiver = match.groups()
                    item_location = item_location.lstrip()
                    ItemObject = game.get_or_create_item(game.players[sender],game.players[receiver],item,item_location,received_timestamp=start_time)

                    if item_location == item and sender == receiver:
                        continue # Most likely an event, can be skipped
                    if ItemObject.is_location_checkable is False:
                        # If the item is not checkable, we don't need to store it
                        # But we can't delete it just yet until the checkable database is more complete
                        # TODO uncomment this when this is safer to do
                        # del ItemObject
                        # continue
                        pass

                    ItemObject.db_add_location()
                    if sender not in game.spoiler_log: game.spoiler_log.update({sender: {}})
                    game.spoiler_log[sender].update({item_location: ItemObject})
            case "Starting Items":
                if match := regex_patterns['starting_item'].match(line):
                    item, receiver = match.groups()
                    game.players[receiver].inventory.append(game.get_or_create_item("Archipelago",game.players[receiver],item,"Starting Items",received_timestamp=start_time))
            case "Jigsaw Info":
                try:
                    key, value_str = parse_line(line)
                    game.players[working_player].settings[key] = parse_to_type(value_str)
                except ValueError as e:
                    if line.startswith("Spoiler and info for [Jigsaw]"):
                        # Not a problem, just the header
                        continue
                    else:
                        raise e
            case "Pokemon Locations":
                # Some Pkmn games list the locations of wild Pokemon in the spoiler log
                if match := regex_patterns['location'].match(line):
                    try:
                        key, value_str = parse_line(line)
                        game.players[working_player].settings['Wild Pokemon Locations'][key] = parse_to_type(value_str)
                    except ValueError as e:
                        logger.error(f"Error parsing Pokemon location line: {line}")
                        logger.error(f"Error: {e}")
                        continue
            case _:
                continue

    # Some game-specific handling
    for player in game.players.values():
        if player.game == "gzDoom":
            # If gzDoom has a wildcard in the map list, we need to handle it
            expanded_levels = set()
            patterns = player.settings.get("Included Levels", [])
            for pattern in patterns:
                if "*" in pattern or "?" in pattern:
                    # Find all unique map names in player's locations that match the pattern
                    for location in player.locations:
                        map_name = location.split(" - ")[0]
                        if fnmatch.fnmatch(map_name, pattern):
                            expanded_levels.add(map_name)
                else:
                    expanded_levels.add(pattern)
            # Remove duplicates and update Included Levels
            player.settings["Included Levels"] = list(sorted(expanded_levels))
    logger.info("Done parsing the spoiler log")

def process_new_log_lines(new_lines, skip_msg: bool = False):
    global release_buffer
    global players
    global seed_address
    global start_time

    # Regular expressions for different log message types
    regex_patterns = {
        'sent_items': re.compile(r'\[(.*?)]: \(Team #\d\) (\L<players>) sent (.*?(?= to)) to (\L<players>) \((.+)\)$', players=game.players.keys()),
            'item_hints': re.compile(
                r'\[(.*?)]: Notice \(Team #\d\): \[Hint]: (\L<players>)\'s (.*) is at (.*) in (\L<players>)\'s World(?: at (?P<entrance>(.+)))?\. \((?P<hint_status>(.+))\)$', players=game.players.keys()),
        'goals': re.compile(r'\[(.*?)\]: Notice \(all\): (.*?) \(Team #\d\) has completed their goal\.$'),
        'releases': re.compile(
            r'\[(.*?)\]: Notice \(all\): (.*?) \(Team #\d\) has released all remaining items from their world\.$'),
        'messages': re.compile(r'\[(.*?)\]: Notice \(all\): (.*?): (.+)$'),
        'room_shutdown': re.compile(r'\[(.*?)\]: Shutting down due to inactivity.$'),
        'room_spinup': re.compile(r'\[(.*?)\]: Hosting game at (.+?)$'),
        'joins': re.compile(r'\[(.*?)\]: Notice \(all\): (.*?) \(Team #\d\) (playing|viewing|tracking) (.+?) has joined. Client\(([0-9\.]+)\), (?P<tags>.+)\.$'),
        'parts': re.compile(r'\[(.*?)\]: Notice \(all\): (.*?) \(Team #\d\) has left the game\. Client\(([0-9\.]+)\), (?P<tags>.+)\.$'),
    }

    def live_classification(item):

        response = item.classification
        setting = item.receiver.settings

        if response == "conditional progression":
            # Progression in certain settings, otherwise useful/filler
            if item.game == "gzDoom":
                # Weapons : extra copies can be filler
                if isinstance(item, Item) and player.get_item_count(item.name) > 1:
                    response = "filler"
            if item.game == "Here Comes Niko!":
                if item.name == "Snail Money" and (setting["Enable Achievements"] == "all_achievements" or setting['Snail Shop'] is True):
                    response = "progression"
                else: response = "filler"
            if item.game == "Ocarina of Time":
                if item.name == "Gold Skulltula Token":
                    if item.count > 50: # No more checks after 50
                        response = "filler"
                    else: response = "progression"
            if item.game == "Trackmania":
                medals = ["Bronze Medal", "Silver Medal", "Gold Medal", "Author Medal"]
                # From TMAP docs: 
                # "The quicket medal equal to or below target difficulty is made the progression medal."
                target_difficulty = setting['Target Time Difficulty']
                progression_medal_lookup = target_difficulty // 100
                progression_medal = medals[progression_medal_lookup]
                filler_medals = [item for i, item in enumerate(medals) if i != progression_medal_lookup]
                if item.name == progression_medal: response = "progression"
                elif item.name in filler_medals: response = "filler"
            # After checking everything, if not re-classified, it's probably progression
            if response == "conditional progression": response = "progression"

            item.classification = response
        return item

    for line in new_lines:
        if match := regex_patterns['sent_items'].match(line):
            timestamp, sender, item, receiver, item_location = match.groups()

            # convert timestamp to epoch time
            timestamp = to_epoch(timestamp)

            # Mark item as collected
            try:
                Item = game.get_or_create_item(game.players[sender],game.players[receiver],item,item_location,received_timestamp=timestamp)
                game.players[sender].collect_item(Item)
                game.spoiler_log[sender].update({item_location: Item})

                # If it was hinted, update the player's hint table
                for hintitem in game.players[receiver].hints['receiving']:
                    if item_location == hintitem.location:
                        del hintitem
                        Item.hinted = True
                        break
                for hintitem in game.players[sender].hints['sending']:
                    if item_location == hintitem.location:
                        del hintitem
                        Item.hinted = True
                        break

            except KeyError as e:
                logger.error(f"""Sent Item Object Creation error. Parsed item name: '{item}', Receiver: '{receiver}', Location: '{item_location}', Error: '{str(e)}'""", e, exc_info=True)
                logger.error(f"Line being parsed: {line}")


            # Update location totals
            Item.db_add_location(True)
            game.players[sender].update_locations(game)
            game.update_locations()

            # Live-Classify if the item is Conditional Progression
            Item = live_classification(Item)

            if not skip_msg: logger.info(f"{sender}: ({str(game.players[sender].collected_locations)}/{str(game.players[sender].total_locations)}/{str(round(game.players[sender].collection_percentage,2))}%) {item_location} -> {receiver}'s {item} ({Item.classification})")

            # By vote of spotzone: if it's filler, don't post it
            if Item.is_filler() or Item.is_currency(): continue

            # If this is part of a release, send it there instead
            if sender in release_buffer and not skip_msg and (timestamp - release_buffer[sender]['timestamp'] <= 2):
                release_buffer[sender]['items'][receiver].append(Item)
                logger.debug(f"Adding {item} for {receiver} to release buffer.")
            else:
                # Update item name based on settings for special items
                location = item_location
                if bool(game.players[receiver].settings):
                    try:
                        item = handle_item_tracking(game, game.players[receiver], Item)
                        location = handle_location_tracking(game, game.players[sender], Item)
                    except KeyError as e:
                        logger.error(f"Couldn't do tracking for item {item} or location {location}:", e, exc_info=True)

                # Update the message appropriately
                if Item.classification == "trap":
                    trap_messages = []

                    def trapmsg_substvars(string: str, sender: str, receiver: str, trap: str):
                        string = string.replace("$s", sender)
                        string = string.replace("$r", receiver)

                        # Full trap name
                        string = string.replace("$t", trap)
                        # Trap name without the 'Trap' suffix
                        string = string.replace("$T", trap.replace(" Trap","")) 

                        return string

                    if sender == receiver:
                        trap_messages = [
                            "**$s** needed more challenge, and collected **their own $t**",
                            "**$s** thought it was progression, but it was I, **$t**!",
                            "**$s** is a FOOL! (collected a **$t**)",
                        ]
                    else:
                        trap_messages = [
                            "$s slapped **$r** around a bit with **a large $T**",
                            "**$r**: Congratulations On Your **$t**! Love, $s",
                            "$s, did **$r** *really* deserve that **$t**?",
                            "$s definitely *did not* send **$r** a **$t**",
                            "**$r**, is this a good time for a **$t** from $s?",
                        ]

                    message = random.choice(trap_messages)
                    message = dim_if_goaled(receiver) + trapmsg_substvars(message, sender, receiver, item) + f" ({location})"
                    if not skip_msg: message_buffer.append(message.replace("_", r"\_"))
                else:
                    if sender == receiver:
                        message = f"**{sender}** found **their own {
                            "hinted " if bool(game.spoiler_log[sender][item_location].hinted) else ""
                            }{item}** ({location})"
                    elif bool(game.spoiler_log[sender][item_location].hinted):
                        message = f"{dim_if_goaled(receiver)}{sender} found **{receiver}'s hinted {item}** ({location})"
                    else:
                        message = f"{dim_if_goaled(receiver)}{sender} sent **{item}** to **{receiver}** ({location})"
                    if not skip_msg: message_buffer.append(message.replace("_",r"\_"))

                # Handle completion milestones
                # if game.players[sender].collection_percentage == 100 and game.players[sender].is_finished() is False:
                #     message = f"**That was their last check! They're probably just waiting to finish now...**"
                #     message_buffer.append(message)


        elif match := regex_patterns['item_hints'].match(line):
            timestamp = match.groups()[0]
            receiver = match.groups()[1]
            item = match.groups()[2]
            item_location = match.groups()[3]
            sender = match.groups()[4]
            if match.group('entrance'):
                entrance = match.group('entrance')
            else: entrance = None
            if match.group('hint_status'):
                hint_status = match.group('hint_status')

            if hint_status == "found": continue

            Item = game.get_or_create_item(game.players[sender],game.players[receiver],item,item_location,entrance=entrance)
            if item_location not in game.spoiler_log[sender]:
                game.spoiler_log[sender][item_location] = Item
            else: Item = game.spoiler_log[sender].get(item_location)

            # Store the hint in the player's hints dictionary
            game.players[sender].add_hint("sending", Item)
            game.players[receiver].add_hint("receiving", Item)

            message = f"**[Hint]** **{receiver}'s {item}** is at {item_location} in {sender}'s World{f" (found at {entrance})" if bool(entrance) else ''}."

            match hint_status:
                case "avoid":
                    message += " This item is not useful."
                case "priority":
                    Item.update_item_classification("progression")
                    message += " **This item will unlock more checks.**"
                case _:
                    pass

            if bool(Item.location_costs):
                message += f"\n> -# This will cost {join_words(Item.location_costs)} to obtain."
            if bool(Item.location_info):
                message += f"\n> -# {Item.location_info}"

            game.spoiler_log[sender][item_location].hint()

            if Item.is_filler() or Item.is_currency(): continue
            # Balatro shop items are hinted as soon as they appear and are usually bought right away, so skip their hints
            if Item.game == "Balatro" and any([Item.location.startswith(shop) for shop in ['Shop Item', 'Consumable Item']]): continue

            if not skip_msg and game.players[receiver].is_finished() is False and not Item.found: message_buffer.append(message)


        elif match := regex_patterns['goals'].match(line):
            timestamp, sender = match.groups()
            if sender not in game.players: game.players[sender] = {"goaled": True}
            game.players[sender].goaled = True
            message = f"**{sender} has finished!** That's {len([p for p in game.players.values() if p.is_goaled()])}/{len(game.players)} goaled! ({len([p for p in game.players.values() if p.is_finished()])}/{len(game.players)} including releases)"
            if game.players[sender].collected_locations == game.players[sender].total_locations:
                message += f"\n**Wow!** {sender} 100%ed their game before finishing, too!"
            if not skip_msg: message_buffer.append(message)
        elif match := regex_patterns['releases'].match(line):
            timestamp, sender = match.groups()
            game.players[sender].released = True
            if not skip_msg:
                logging.info("Release detected.")
                release_buffer[sender] = {
                    'timestamp': to_epoch(timestamp),
                    'items': defaultdict(list)
                }
        elif match := regex_patterns['room_shutdown'].match(line):
            game.running = False
            if not skip_msg:
                logger.info("Room has spun down due to inactivity.")
        elif match := regex_patterns['room_spinup'].match(line):
            timestamp, address = match.groups()
            game.running = True
            if not skip_msg:
                logger.info(f"Room has spun up at {address}.")
            if address != seed_address:
                seed_address = address
                logger.info(f"Seed URI has changed: {address}")
                message = f"**The seed address has changed.** Use this updated address: `{address}`"
                if not skip_msg:
                    with sqlcon.cursor() as cursor:
                        game.pushdb(cursor, 'pepper.ap_all_rooms', 'port', seed_address.split(":")[1])
                        sqlcon.commit()
                    send_chat("Archipelago", message)
                    message_buffer.append(message)
            if start_time is None:
                start_time = to_epoch(timestamp)
                logger.info(f"Start time set to {start_time} (epoch)")
        elif match := regex_patterns['messages'].match(line):
            timestamp, sender, message = match.groups()
            if msg_webhook:
                if message.startswith("!"): continue # don't send commands
                else:
                    if not skip_msg and sender in game.players:
                        logger.info(f"{sender}: {message}")
                        send_chat(sender, message)

        elif match := regex_patterns['joins'].match(line):
            timestamp, player, verb, playergame, client_version, tags = match.groups()
            try:
                tags_str = tags
                tags = ast.literal_eval(tags_str)
                game.players[player].tags = tags
            except json.JSONDecodeError:
                logger.error(f"Failed to parse player tags. {player}: {tags_str}")
                tags = tags_str
            if not skip_msg and verb == "playing":
                logger.info(f"{player} ({playergame}) is online.")
                game.players[player].set_online(True, timestamp)
            if "Tracker" in tags or verb == "tracking":
                if not skip_msg:
                    pass
                #     message = f"{player} is checking what is in logic."
                #     message_buffer.append(message)

        elif match := regex_patterns['parts'].match(line):
            timestamp, player, version, tags = match.groups()
            if not skip_msg: logger.info(f"{player} is offline.")
            game.players[player].set_online(False, timestamp)

        else:
            # Unmatched lines
            logger.debug(f"Unparsed line: {line}")

### Common non-loop functions

def log_to_file(message):
    global room_id

    os.makedirs('logs', exist_ok=True)  # Ensure logs directory exists
    with open(f'logs/{room_id}.md', 'a', encoding='UTF-8') as log_file:
        log_file.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")

def send_chat(sender, message):
    payload = {
        "username": sender,
        "content": message
    }
    try:
        response = requests.post(msg_webhook, json=payload, timeout=5)
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Error sending message to Discord: {e}")


def send_to_discord(message):
    payload = {
        "content": message
    }
    try:
        response = requests.post(webhook_url, json=payload, timeout=5)
        response.raise_for_status()
        # log_to_file(message)  # Log the message to a file
    except requests.RequestException as e:
        logging.error(f"Error sending message to Discord: {e}")


def send_release_messages():
    global release_buffer

    def handle_currency(receiver, itemlist: dict):
        currency = 0

        currency_matches = {
            'A Hat in Time': (re.compile(r'^([0-9]+) Pons$'), "Pons"),
            'Final Fantasy': (re.compile(r'^Gold([0-9]+)$'), "Gold"),
            'Jak and Daxter The Precursor Legacy': (re.compile(r'^([0-9]+) Precursor Orbs?$'), "Precursor Orbs"),
            "Links Awakening DX": (re.compile(r'^([0-9]+) Rupees$'), "Rupees"),
            'Link to the Past': (re.compile(r'^Rupees? \(([0-9]+)\)$'), "Rupees"),
            'Ocarina of Time': (re.compile(r'^Rupees? \(([0-9]+)\)$'), "Rupees"),
            'Pokemon FireRed and LeafGreen': (re.compile(r'^([0-9]+) Coins?$'), "Coins"),
            'Sonic Adventure 2 Battle': (re.compile(r'^(\w+) Coins?$'), "Coins"),
            'Super Mario World': (re.compile(r'^([0-9]+) coins?$'), "Coins"),
        }

        if game.players[receiver].game in currency_matches:
            try:
                for item, count in itemlist.copy().items():
                    if match := currency_matches[game.players[receiver].game][0].match(item):
                        if game.players[receiver].game == "Sonic Adventure 2 Battle":
                            amount = w2n.word_to_num(match.groups()[0]) # why you make me do this
                        else:
                            amount = int(match.groups()[0])
                        currency = currency + (amount * count)
                        del itemlist[item]
                if currency > 0:
                    logger.info(f"Replacing (attempting) currency in {game.players[receiver].game} with '{currency} {currency_matches[game.players[receiver].game][1]}'")
                    itemlist.update({f"{currency} {currency_matches[game.players[receiver].game][1]}": 1})
            except KeyError:
                logger.info(f"No currency handler for {game.players[receiver].game}, but handle_currency matched it anyway somehow!")
                raise

        return itemlist

    for sender, data in release_buffer.copy().items():
        if time.time() - data['timestamp'] > 1:
            message = f"**{sender}** has released their remaining items."
            running_message = message
            for receiver, items in data['items'].items():
                if game.players[receiver].is_finished():
                    continue
                item_counts = defaultdict(int)
                for item in items:
                    if item.is_filler(): continue
                    item_counts[item.name] += 1
                handle_currency(receiver,item_counts)
                item_list = ', '.join(
                    [f"{item} (x{count})" if count > 1 else item for item, count in item_counts.items()])
                running_message += f"\n{dim_if_goaled(receiver)}**{receiver}** receives: {item_list}"
                if len(running_message) > MAX_MSG_LENGTH:
                    send_to_discord(message)
                    message = running_message.replace(message, '')
                    time.sleep(1)
                else:
                    message = running_message
            send_to_discord(message)
            logger.info(f"{sender} release sent.")
            del release_buffer[sender]


def fetch_log(url):
    try:
        cookies = {'session': session_cookie}
        response = requests.get(url, cookies=cookies,timeout=10)
        response.raise_for_status()
        return response.text.splitlines()
    except requests.RequestException as e:
        logger.error(f"Error fetching log file: {e}")
        return []

### Emitter events

def handle_milestone_message(message):
    # message_buffer.append(message)
    pass

event_emitter.on("milestone", handle_milestone_message)

### Main function to watch the log file

def watch_log(url, interval):
    global release_buffer
    global players
    global game

    last_line = 0

    logger.info("Fetching room info.")
    for player in requests.get(api_url).json()["players"]:
        game.players[player[0]] = Player(
            name=player[0],
            game=player[1]
        )
        game.spoiler_log[player[0]] = {}
    del player
    if seed_url:
        logger.info("Processing spoiler log.")
        process_spoiler_log(seed_url)
    previous_lines = fetch_log(url)
    logger.info("Parsing existing log lines before we start watching it...")

    # Get the last line number we processed from the database
    with sqlcon.cursor() as cursor:
        try:
            last_line = int(game.pulldb(cursor, 'pepper.ap_all_rooms', 'last_line'))
        except TypeError:
            # Last Line probably hasn't been set yet; this room is new
            pass

    process_new_log_lines(previous_lines[:last_line], True) # Read for hints etc
    release_buffer = {}
    logger.info(f"Initial log lines: {len(previous_lines[:last_line])}")
    logger.info(f"Log lines queued up for processing: {len(previous_lines[last_line:])}")
    for p in game.players.values():
        p.update_locations(game)
        p.on_item_collected(None)
    game.update_locations()
    logger.info(f"Total Checks: {game.total_locations}")
    logger.info(f"Checks Collected: {game.collected_locations}")
    logger.info(f"Completion Percentage: {round(game.collection_percentage,2)}%")
    logger.info(f"Total Players: {len(game.players)}")
    logger.info(f"Seed Address: {seed_address}")
    with sqlcon.cursor() as cursor:
        try: 
            game.pushdb(cursor, 'pepper.ap_all_rooms', 'port', seed_address.split(":")[1])
            sqlcon.commit()
        except AttributeError:
            # Seed Address not processed/set yet
            pass

    message_buffer.clear() # Clear buffer in case we have any old messages

    if len(previous_lines) < 8: # If the seed has just started, post some info
        message = f'''
        **So begins another Archipelago...**
        **Seed ID:** `{game.seed}`
        **Seed Address:** `{seed_address}`
        **Archipelago Version:** `{game.version_generator}`
        **Players:** `{game.world_settings["Players"]}`
        **Total Checks:** `{game.total_locations}` (possibly inaccurate)'''

        message_buffer.append(message)
        logger.info("New room: Queuing initial message to Discord.")
        del message
    # classification_thread = threading.Thread(target=save_classifications)
    # classification_thread.start()


    logger.info("Ready!")
    while True:
        time.sleep(interval)
        current_lines = fetch_log(url)
        if len(current_lines) > last_line:
            new_lines = current_lines[last_line:]
            with sqlcon.cursor() as cursor:
                game.pushdb(cursor, 'pepper.ap_all_rooms', 'last_line', len(current_lines))
                sqlcon.commit()
            process_new_log_lines(new_lines)
            if message_buffer:
                # Join all messages with newlines
                all_messages = '\n'.join(message_buffer)
                if len(all_messages) > MAX_MSG_LENGTH:
                    logger.warning(f"Message buffer exceeded {MAX_MSG_LENGTH} characters, splitting into chunks.")
                    # Split into chunks not exceeding MAX_MSG_LENGTH
                    chunks = []
                    current_chunk = ""
                    for msg in message_buffer:
                        # +1 for the newline if not first message
                        if len(current_chunk) + len(msg) + (1 if current_chunk else 0) > MAX_MSG_LENGTH:
                            if current_chunk:
                                chunks.append(current_chunk)
                            current_chunk = msg
                        else:
                            if current_chunk:
                                current_chunk += '\n' + msg
                            else:
                                current_chunk = msg
                    if current_chunk:
                        chunks.append(current_chunk)
                    # Send each chunk, waiting 2 seconds between
                    for i, chunk in enumerate(chunks):
                        send_to_discord(chunk)
                        logger.debug(f"sent chunk {i+1}/{len(chunks)} ({len(chunk)} chars) to webhook")
                        if i < len(chunks) - 1:
                            time.sleep(2)
                else:
                    send_to_discord(all_messages)
                    logger.debug(f"sent {len(message_buffer)} messages ({len(all_messages)} chars) to webhook")
                message_buffer.clear()
            last_line = len(current_lines)

def process_releases():
    global release_buffer
    logger.info("Watching for releases.")

    while True:
        time.sleep(10)
        while len(release_buffer) > 0:
            time.sleep(INTERVAL)
            send_release_messages()

# Flask stuff
webview = Flask(__name__)

def safe_globals():
    # Only show non-private, non-module, non-callable globals
    return {k: repr(v) for k, v in globals().items()
            if not k.startswith('__') and
               not callable(v) and
               not isinstance(v, type(webview)) and
               k not in ('webview', 'request', 'Response')}

@webview.route('/inspect', methods=['GET'])
def inspect():
    # For easier reading, return as plain text
    import pprint
    return Response(pprint.pformat(safe_globals()), mimetype='text/plain')

@webview.route('/inspectgame', methods=['GET'])
def get_game():
    return jsonify(game.to_dict())

@webview.route('/locations/checkable/', methods=['GET'], defaults={'found': False})
@webview.route('/locations/checkable/found', methods=['GET'], defaults={'found': True})
def get_checkable_locations(found: bool = False):
    locationtable = {}
    for player_name, player in game.players.items():
        if player.game not in locationtable:
            locationtable[player.game] = {}
        for location_name, location in player.locations.items():
            if found:
                locationtable[player.game][location_name] = [location.found, location.is_location_checkable]
            else:
                locationtable[player.game][location_name] = location.is_location_checkable
    return jsonify(locationtable)

def run_flask():
    # Listen only on localhost by default for safety
    webview.run(host='127.0.0.1', port=42069, debug=False, use_reloader=False)

if __name__ == "__main__":

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    logger.info(f"logging messages from AP Room ID {room_id}")

    release_thread = threading.Thread(target=process_releases)
    release_thread.start()

    watch_log(log_url, INTERVAL)
