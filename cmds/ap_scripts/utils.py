import datetime
import time
import re
import fnmatch
import math
import psycopg2 as psql
import logging
import yaml
import discord

from typing import Iterable, Any

from cmds.ap_scripts.emitter import event_emitter
# from cmds.ap_scripts.name_translations import gzDoomMapNames
from zoneinfo import ZoneInfo

# setup logging
logger = logging.getLogger('ap_itemlog')

with open('config.yaml', 'r', encoding='UTF-8') as file:
    cfg = yaml.safe_load(file)


sqlcfg = cfg['bot']['psql']
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


classification_cache = {}
cache_timeout = 1*60*60 # 1 hour(s)

item_table = {}

# def push_to_database(cursor: psql.cursor, game: Game, database: str, column: str, payload):
#     try:
#             cursor.execute(f"UPDATE {database} set {column} = %s WHERE room_id = %s", (payload, room_id))
#     except Exception as e:
#         logger.error(f"Error pushing to database: {e}")


class Game(dict):
    seed = None
    room_id = None
    version_generator = None
    version_server = None
    running: bool = False
    world_settings = {}
    spoiler_log = {}
    players = {}
    collected_locations: int = 0
    total_locations: int = 0
    collection_percentage: float = 0.0
    milestones = set()
    start_timestamp: float = None

    # This is a cache for Item instances, so we don't have to create new ones every time
    # Unique by (sender, location)
    # Store it in the Game class to keep duplicate instances minimal
    item_instance_cache = {}

    def init_db(self):
        cursor = sqlcon.cursor()

        # DBs to do:
        # {room_id}
        # {room_id}_locations
        # {room_id}_items
        # {room_id}_p_{player}

        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS games.{self.room_id} (setting bpchar, value bpchar, classification varchar(32))")

    def update_locations(self):
        self.collected_locations = sum([p.collected_locations for p in self.players.values()])
        self.total_locations = sum([p.total_locations for p in self.players.values()])
        self.collection_percentage = (self.collected_locations / self.total_locations) * 100 if self.total_locations > 0 else 0.0

        self.check_milestones()

    def check_milestones(self):
        milestones = [25, 50, 75, 100]  # Define milestones
        for milestone in milestones:
            if self.collection_percentage >= milestone and milestone not in self.milestones:
                self.milestones.add(milestone)
                logger.info(f"Game reached {milestone}% completion!")
                message = f"**The game has reached {milestone}% completion!**"
                event_emitter.emit("milestone", message)  # Emit the milestone message

    def to_dict(self):
        return {
            "seed": self.seed,
            "room_id": self.room_id,
            "version_generator": self.version_generator,
            "version_server": self.version_server,
            "world_settings": self.world_settings,
            "start_timestamp": self.start_timestamp,
            "running": self.running,
            "spoiler_log": {k: {lk: lv.to_dict() for lk, lv in v.items()} for k, v in self.spoiler_log.items()},
            "players": {k: v.to_dict() for k, v in self.players.items()},
            "collected_locations": self.collected_locations,
            "total_locations": self.total_locations,
            "collection_percentage": self.collection_percentage,
        }
    
    def get_or_create_item(self, sender, receiver, itemname, location, entrance=None, received_timestamp: float = None):
        key = (str(sender), location, itemname)
        if key in self.item_instance_cache:
            item = self.item_instance_cache[key]
            if received_timestamp is not None:
                item.received_timestamp = received_timestamp
            return item
        obj = Item(sender, receiver, itemname, location, entrance, received_timestamp)
        self.item_instance_cache[key] = obj
        return obj

    def pushdb(self, cursor, database: str, column: str, payload):
        try:
            cursor.execute(f"UPDATE {database} set {column} = %s WHERE room_id = %s", (payload, self.room_id))
        except Exception as e:
            logger.error(f"Error pushing to database: {e}")

    def pulldb(self, cursor, database: str, column: str):
        """Pull a value from the database for this game."""
        try:
            cursor.execute(f"SELECT {column} FROM {database} WHERE room_id = %s", (self.room_id,))
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Error pulling from database: {e}")
            return None
        
    def refresh_classifications(self):
        """Refresh the item classifications for all items in the game.
        Useful if classifications have been changed during runtime and need to be applied."""

        logger.info("Refreshing item classifications.")
        for item in self.item_instance_cache.values():
            item.set_item_classification()
        logger.info("Item classifications refreshed.")
        

def handle_hint_update(self):
    pass


class Player(dict):
    name = None
    game = None
    inventory: list = []
    locations = {}
    hints = {}
    spoilers = {"items": [], "locations": {}}
    online = False
    last_online: datetime.datetime = None
    tags = []
    settings = None
    stats: 'PlayerState' # Game-specific stats
    goaled = False
    released = False
    collected_locations: int = 0
    total_locations: int = 0
    collection_percentage: float = 0.0
    finished_percentage: float = 0.0

    class PlayerState(dict):
        """A class to hold the player's state in the game.
        Uses the player's inventory to calculate stats base on the game and required goal."""

        goal_str: str = None
        stats: dict = {}

        def __init__(self):
            super().__init__()
            self.stats = {}

        def to_dict(self):
            
            dict_stats = self.stats.copy()
            dict_stats['goal_str'] = self.goal_str if self.goal_str else None

            return dict_stats

        def set_stat(self, stat_name: str, value: Any):
            """Update a game-specific stat for the player."""
            if stat_name not in self.stats:
                self.stats[stat_name] = value
            elif isinstance(self.stats[stat_name], (int, float)) and isinstance(value, (int, float)):
                self.stats[stat_name] += value
            else:
                self.stats[stat_name] = value


    def __init__(self,name,game):
        self.name = name
        self.game = game
        self.inventory = []
        self.locations = {}
        self.hints = {
            "sending": [],
            "receiving": []
        }
        self.settings = PlayerSettings()
        self.goaled = False
        self.released = False
        self.milestones = set()
        self.stats = Player.PlayerState()

    def __str__(self):
        return self.name

    def to_dict(self):
        return {
            "name": self.name,
            "game": self.game,
            "inventory": [i.to_dict() for i in self.inventory],
            "locations": {k: v.to_dict() for k, v in self.locations.items()},
            "hints": {k: [i.to_dict() for i in v] for k, v in self.hints.items()},
            "spoilers": self.spoilers,
            "online": self.online,
            "last_online": self.last_online.timestamp() if self.last_online else None,
            "tags": self.tags,
            "stats": self.stats.to_dict(),
            "settings": dict(self.settings) if self.settings else {},
            "goaled": self.goaled,
            "released": self.released,
            "collected_locations": self.collected_locations,
            "total_locations": self.total_locations,
            "collection_percentage": self.collection_percentage,
        }

    def is_finished(self) -> bool:
        return self.goaled or self.released

    def is_goaled(self) -> bool:
        return self.goaled

    def set_online(self, online: bool, timestamp: datetime.datetime):
        self.online = online
        self.last_online = timestamp

    def last_seen(self):
        if self.online is True:
            return time.time()
        else:
            return self.last_online

    def update_locations(self, game: Game):
        self.locations = {l.location: l for l in game.spoiler_log[self.name].values()}

        location_count = len(self.locations)
        checkable_location_count = len([l for l in self.locations.values() if l.is_location_checkable is True])

        if (checkable_location_count / location_count) < 0.95:
            # If the amount of checkable locations does not pass a certain threshold,
            # The world has likely not been fully played through to determine checkability
            # In this case just use the unfiltered total location count
            self.total_locations = location_count
        else:
            self.total_locations = checkable_location_count

        if not (self.goaled or self.released):
            self.collected_locations = len([l for l in self.locations.values() if l.found is True])
            self.collection_percentage = (self.collected_locations / self.total_locations) * 100 if self.total_locations > 0 else 0.0

        self.check_milestones()

    def check_milestones(self):
        milestones = [50, 75, 100]  # Define milestones
        if self.goaled or self.released:
            return # Don't process milestones if the player is already finished
        for milestone in milestones:
            if self.collection_percentage >= milestone and milestone not in self.milestones:
                self.milestones.add(milestone)
                message = f"**{self.name} has reached {milestone}% completion!**"
                event_emitter.emit("milestone", message)  # Emit the milestone message

    def add_hint(self, hint_type: str, item):
        if hint_type not in self.hints:
            self.hints[hint_type] = list()
        self.hints[hint_type].append(item)
        self.on_hints_updated()

    def on_hints_updated(self):
        # This method will be called whenever hints are updated
        logger.debug(f"Hints for player {self.name} have been updated.")
        handle_hint_update(self)

    def collect_item(self, item):
        """Collect an item and add it to the player's inventory."""
        if isinstance(item, Item):
            item.found = True
            item.collect()
            # Item.collect already adds itself to the inventory
            self.on_item_collected(item)
        else:
            logger.error(f"Attempted to collect a non-Item object: {item}")

    def on_item_collected(self, item):
        if item is not None:
            pass # TODO: Handle item collection logic here, e.g., updating stats, notifying other players, etc.
        handle_state_tracking(self)

    def get_item_count(self, item_name: str) -> int:
        """Get the count of a specific item in the player's inventory."""
        return sum(1 for item in self.inventory if item.name == item_name)
    
    def has_item(self, item_name: str) -> bool:
        """Check if the player has at least one of the specified item in their inventory."""
        return any(item.name == item_name for item in self.inventory)
    
    def get_collected_items(self, items: Iterable[Any]) -> list:
        """For a list of items requested, return the items that are present in the inventory."""
        collected_items = []

        for collected_item in self.inventory:
            if collected_item.name in items:
                collected_items.append(collected_item)
        
        return collected_items
    
    def add_spoiler(self, item: 'Item'):
        """Add an item to the player's spoiler log."""
        if self.name == str(item.sender) or self.name == str(item.receiver):
            if item.receiver == self.name:
                self.spoilers['items'].append(item)
            if item.sender == self.name:
                self.spoilers['locations'].update({item.location: item})
            logger.debug(f"Spoiler added for player {self.name}: {item.name} at {item.location}")
            return True
        else:
            logger.error(f"Attempted to add spoiler for player {self.name} but they are not involved with the item: {item.name} at {item.sender}: {item.location} for {item.receiver}")
            return False

class Item(dict):
    """An Archipelago item in the multiworld"""

    sender = None
    receiver = None
    name = None
    game = None
    location = None
    location_costs: list[str] = []
    location_info: str = None
    location_entrance = None
    is_location_checkable = None
    classification = None
    count = 1
    found = False
    hinted = False
    spoiled = False
    received_timestamp: datetime.datetime = None

    def __init__(self, sender: Player|str, receiver: Player, item: str, location: str, entrance: str = None, received_timestamp: float = None):
        self.sender = sender
        self.receiver = receiver
        self.name = item
        self.game = receiver.game
        self.location = location
        self.is_location_checkable = self.get_location_checkable()
        self.location_entrance = entrance
        self.location_costs, self.location_info = handle_location_hinting(self.receiver, self)
        self.classification = self.set_item_classification(self)
        self.count: int = 1
        self.found = False
        self.hinted = False
        self.spoiled = False
        self.received_timestamp = received_timestamp

        if self.game is None:
            logger.warning(f"Item object for {self.name} has no game associated with it?")

        if self.game not in item_table:
            item_table[self.game] = {}
        if self.name not in item_table[self.game]:
            item_table[self.game][self.name] = self

    def __str__(self):
        return self.name

    def to_dict(self):
        return {
            "sender": str(self.sender) if hasattr(self.sender, 'name') else self.sender,
            "receiver": str(self.receiver) if hasattr(self.receiver, 'name') else self.receiver,
            "name": self.name,
            "game": self.game,
            "location": self.location,
            "location_entrance": self.location_entrance,
            "location_costs": self.location_costs,
            "location_info": self.location_info,
            "is_location_checkable": self.is_location_checkable,
            "classification": self.classification,
            "count": self.count,
            "found": self.found,
            "hinted": self.hinted,
            "spoiled": self.spoiled,
            "received_timestamp": self.received_timestamp.timestamp() if self.received_timestamp else None
        }

    def collect(self):
        """Mark this item as collected and add it to the receiver's inventory."""
        self.found = True
        self.receiver.inventory.append(self)

    def hint(self):
        self.hinted = True

    def spoil(self):
        self.spoiled = True

    def get_location_checkable(self) -> bool:
        if not isinstance(self.sender, Player):
            return False # Archipelago starting items, etc
        match self.game:
            case "SlotLock"|"APBingo":
                return True # Metagames are always checkable
            case "Jigsaw"|"Simon Tatham's Portable Puzzle Collection":
                return True # AP-specific games are simple enough that all their locations are checkable
            case "gzDoom":
                # Locations are dynamically generated by the selected wad
                # So let's assume they they are all checkable
                return True 
            case _:
                with sqlcon.cursor() as cursor:
                    cursor.execute("SELECT is_checkable FROM archipelago.game_locations WHERE game = %s AND location = %s;", (self.sender.game, self.location))
                    response = cursor.fetchone()
                    # logger.info(f"locationsdb: {self.sender.game}: {self.location} is checkable: {response[0]}") # debugging in info, yes i know
                    return response[0] if response else False

    def db_add_location(self, is_check: bool = False):
        """Add this item's location to the database if it doesn't already exist.
        If a location shows up in a playthrough, it is a checkable location.
        If it doesn't (only appears in spoiler log), it is *likely* an event.

        If the location already exists, but the 'checkable' value is wrong,
        this function will update the value in the database.

        This should help to establish accurate location counts when we start tracking those."""
        cursor = sqlcon.cursor()

        cursor.execute("CREATE TABLE IF NOT EXISTS archipelago.game_locations (game bpchar, location bpchar, is_checkable boolean)")

        is_checkable: bool = None

        try:
            cursor.execute("SELECT * FROM archipelago.game_locations WHERE game = %s AND location = %s;", (self.sender.game, self.location))
            game, location, is_checkable = cursor.fetchone()
            if is_checkable != is_check and is_check == True:
                logger.debug(f"Request to update checkable status for {self.sender.game}: {self.location} (to: {str(is_check)})")
                cursor.execute("UPDATE archipelago.game_locations set is_checkable = %s WHERE game = %s AND location = %s;", (str(is_check), game, location))
        except TypeError:
            logger.debug("Nothing found for this location, likely")
            logger.info(f"locationsdb: adding {self.sender.game}: {self.location} to the db")
            cursor.execute("INSERT INTO archipelago.game_locations VALUES (%s, %s, %s)", (self.sender.game, self.location, str(is_check)))
        finally:
            sqlcon.commit()
        logger.debug(f"locationsdb: classified {self.sender.game}: {self.location} as {is_checkable}")
        self.is_location_checkable = self.get_location_checkable()


    def set_item_classification(self, player: Player = None):
        """Refer to the itemdb and see whether the provided Item has a classification.
        If it doesn't, creates a new entry for that item with no classification.

        We can pass this through an intermediate step to assume things about
        some common items, but not everything.
        """

        permitted_values = [
            "progression", # Unlocks new checks
            "conditional progression", # Progression overall, but maybe only in certain settings or certain qualities
            "useful", # Good to have but doesn't unlock anything new
            "currency", # Filler, but specifically currency
            "filler", # Filler - not really necessary
            "trap" # Negative effect upon the player
            ]
        response = None # What we will ultimately return

        player = self.receiver if player is None else player

        if self.game is None:
            return None

        if self.game in classification_cache and self.name in classification_cache[self.game]:
            if bool(classification_cache[self.game][self.name][1]) and (time.time() - classification_cache[self.game][self.name][1] > cache_timeout):
                if classification_cache[self.game][self.name][0] is None:
                    logger.warning(f"Invalidating cache for {self.game}: {self.name}")
                    del classification_cache[self.game][self.name]
            else:
                classification = classification_cache[self.game][self.name][0]
                if classification != "conditional progression": return classification

        # Some games are 'simple' enough that everything (or near everything) is progression
        match self.game:
            case "Simon Tatham's Portable Puzzle Collection":
                if self.name == "Filler": response = "filler"
                else: response = "progression"
            case "SlotLock"|"APBingo": response = "progression" # metagames are generally always progression
            case _:
                cursor = sqlcon.cursor()

                cursor.execute("CREATE TABLE IF NOT EXISTS archipelago.item_classifications (game bpchar, item bpchar, classification varchar(32))")

                try:
                    cursor.execute("SELECT classification FROM archipelago.item_classifications WHERE game = %s AND item = %s;", (self.game, self.name))
                    response = cursor.fetchone()[0]
                except TypeError:
                    logger.debug("Nothing found for this item, likely")
                    logger.info(f"itemsdb: adding {self.game}: {self.name} to the db")
                    cursor.execute("INSERT INTO archipelago.item_classifications VALUES (%s, %s, %s)", (self.game, self.name, None))
                finally:
                    sqlcon.commit()
        logger.debug(f"itemsdb: classified {self.game}: {self.name} as {response}")
        if self.game not in classification_cache:
            classification_cache[self.game] = {}
        classification_cache[self.game][self.name] = (response.lower(), time.time()) if bool(response) else (None, time.time())
        return classification_cache[self.game][self.name][0]
        # return response


    def update_item_classification(self, classification: str) -> bool:
        # Abort if already set
        if classification == self.classification: return True

        permitted_values = [
            "progression", # Unlocks new checks
            "conditional progression", # Progression overall, but maybe only in certain settings or certain qualities
            "useful", # Good to have but doesn't unlock anything new
            "currency", # Filler, but specifically currency
            "filler", # Filler - not really necessary
            "trap" # Negative effect upon the player
            ]
        if classification not in permitted_values:
            logger.error(f"Tried to update classification for {self.game}: {self.name} (value '{classification}' not permitted)")
            return False

        logger.info(f"Request to update classification for {self.game}: {self.name} (to: {classification})")
        cursor = sqlcon.cursor()

        cursor.execute("CREATE TABLE IF NOT EXISTS archipelago.item_classifications (game bpchar, item bpchar, classification varchar(32))")

        try:
            cursor.execute("UPDATE archipelago.item_classifications set classification = %s where game = %s and item = %s;", (classification, self.game, self.name))
        finally:
            sqlcon.commit()
            self.set_item_classification(self.receiver)
        return True

    def is_found(self):
        return self.found


    def is_filler(self):
        return self.classification == "filler"
    def is_currency(self):
        return self.classification == "currency"

class PlayerSettings(dict):
    def __init__(self):
        pass

class APEvent:
    def __init__(self, event_type: str, timestamp: str, sender: str, receiver: str = None, location: str = None, item: str = None, extra: str = None):
        self.type = event_type
        self.sender = sender
        self.receiver = receiver if receiver else None
        self.location = location if location else None
        self.item = item if item else None
        self.extra = extra if extra else None

        try:
            self.timestamp = time.mktime(datetime.datetime(tzinfo=ZoneInfo()).strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f"))
        except ValueError as e:
            raise

        match self.type:
            case "item_send"|"hint":
                if not all([bool(criteria) for criteria in [self.sender, self.receiver, self.location, self.item]]):
                    raise ValueError(f"Invalid {self.type} event! Requires a sender, receiver, location, and item.")

def handle_item_tracking(game: Game, player: Player, item: Item):
    """If an item is an important collectable of some kind, we should put some extra info in the item name for the logs."""
    global item_table

    ItemObject = item
    item = item.name

    if bool(player.settings):
        settings = player.settings
        game = player.game
        count = player.get_item_count(item)

        try:
            match game:
                case "A Hat in Time":
                    if item == "Time Piece" and not settings['Death Wish Only']:
                        required = 0
                        match settings['End Goal']:
                            case 'Finale':
                                required = settings['Chapter 5 Cost']
                            case 'Rush Hour':
                                required = settings['Chapter 7 Cost']
                        return f"{item} (*{count}/{required}*)"
                    if item == "Progressive Painting Unlock":
                        required = 3
                        return f"{item} ({count}/{required})"
                    if item.startswith("Metro Ticket"):
                        required = 4
                        tickets = ["Yellow", "Green", "Blue", "Pink"]
                        collected = [player.get_collected_items([f"Metro Ticket - {ticket}" for ticket in tickets])]
                        logger.debug(f"Collected tickets: {collected}")
                        return f"{item} ({len(collected)}/{required})"
                    if item.startswith("Relic"):
                        relics = {
                            "Burger": [
                                "Relic (Burger Cushion)",
                                "Relic (Burger Patty)"
                            ],
                            "Cake": [
                                "Relic (Cake Stand)",
                                "Relic (Chocolate Cake Slice)",
                                "Relic (Chocolate Cake)",
                                "Relic (Shortcake)"
                            ],
                            "Crayon": [
                                "Relic (Blue Crayon)",
                                "Relic (Crayon Box)",
                                "Relic (Green Crayon)",
                                "Relic (Red Crayon)"
                            ],
                            "Necklace": [
                                "Relic (Necklace Bust)",
                                "Relic (Necklace)"
                            ],
                            "Train": [
                                "Relic (Mountain Set)",
                                "Relic (Train)"
                            ],
                            "UFO": [
                                "Relic (Cool Cow)",
                                "Relic (Cow)",
                                "Relic (Tin-foil Hat Cow)",
                                "Relic (UFO)"
                            ]
                        }
                        for relic, parts in relics.items():
                            if any(part == item for part in parts):
                                required = len(parts)
                                count = len(player.get_collected_items(parts))
                                return f"{item} ({relic} {count}/{required})"
                case "A Short Hike":
                    if item == "Seashell":
                        return f"{item} ({count})"
                case "Archipela-Go!":
                    if settings['Goal'] == "Long Macguffin" and len(item) == 1:
                        items = list("Archipela-Go!")
                        collected = [player.get_collected_items(items)]
                        collected_string = ""
                        for i in items:
                            if i in collected: collected_string += i
                            else: collected_string += "_"
                        return f"{item} ({collected_string})"
                case "Celeste (Open World)":
                    if item == 'Strawberry':
                        total = settings['Total Strawberries']
                        required = round(total * (settings['Strawberries Required Percentage'] / 100))
                        return f"{item} *({count}/{required})*"
                case "Donkey Kong 64":
                    kongs = ["Donkey", "Diddy", "Lanky", "Tiny", "Chunky"]
                    shopkeepers = ["Candy", "Cranky", "Funky", "Snide"]
                    moves = { # Translate rando names to full names for convenience
                        "Barrels": "Barrel Throwing",
                        "Bongos": "Bongo Blast",
                        "Coconut": "Coconut Shooter",
                        "Feather": "Feather Bow",
                        "Grape": "Grape Shooter",
                        "Guitar": "Guitar Gazump",
                        "Oranges": "Orange Throwing",
                        "Peanut": "Peanut Popguns",
                        "Triangle": "Triangle Trample",
                        "Trombone": "Trombone Tremor",
                        "Vines": "Vine Swinging",
                    }
                    if item == "Banana Fairy":
                        required = settings['Rareware GB Requirement']
                        total = 20
                        return f"{item} (*{count}/{required}*/{total})"
                    if item == "Golden Banana":
                        max_gbs = max([settings[f"Level {num} B. Locker"] for num in range(1,9)])
                        total = 201
                        return f"{item} (*{count}/{max_gbs}*/{total})"
                    if item.startswith("Key "):
                        keys = 8
                        collected_string = ""
                        collected_keys = player.get_collected_items([f"Key {k+1}" for k in range(keys)])
                        for k in range(keys):
                                if f"Key {k+1}" in collected_keys: collected_string += str(k+1)
                                else: collected_string += "_"
                        return f"{item} ({collected_string})"
                    if item in kongs:
                        collected_string = ""
                        collected_kongs = player.get_collected_items(kongs)
                        for kong in kongs:
                            if kong in collected_kongs: collected_string += kong[0:1]
                            else: collected_string += "__"
                        return f"{item} Kong ({collected_string})"
                    if item in moves.keys():
                        return moves[item]
                case "Donkey Kong Country 2":
                    if item == "Kremcoin":
                        # Not sure if the vanilla use (Klubba Kiosk) is intact,
                        # but we still need to count these
                        return f"{item} ({count})"
                case "Donkey Kong Country 3":
                    if item == "DK Coin":
                        required = settings['Dk Coins For Gyrocopter']
                        return f"{item} ({count}/{required})"
                case "DOOM 1993":
                    if item.endswith(" - Complete"):
                        count = len([i for i in player.inventory if str(i).endswith(" - Complete")])
                        required = 0
                        for episode in 1, 2, 3, 4:
                            if settings[f"Episode {episode}"] is True:
                                required = required + (1 if settings['Goal'] == "Complete Boss Levels" else 9)
                        return f"{item} ({count}/{required})"
                case "DOOM II":
                    if item.endswith(" - Complete"):
                        count = len([i for i in player.inventory if str(i).endswith(" - Complete")])
                        required = 0
                        if settings["Episode 1"] is True:
                            required = required + 11 # MAP01-MAP11
                        if settings["Episode 2"] is True:
                            required = required + 9 # MAP12-MAP20
                        if settings["Episode 3"] is True:
                            required = required + 10 #  MAP21-MAP30
                        if settings["Secret Levels"] is True:
                            required = required + 2 # Wolfenstein/Grosse
                        return f"{item} ({count}/{required})"
                case "Final Fantasy Mystic Quest":
                    if item == "Sky Fragment":
                        return f"{item} ({count})"
                case "gzDoom":
                    item_regex = re.compile(r"^([a-zA-Z]+) \((\S+)\)$")
                    if item.startswith("Level Access"):
                        count = len([i for i in player.inventory if str(i).startswith("Level Access")])
                        total = len(settings['Included levels'])
                        return f"{item} ({count}/{total})"
                    if item.startswith("Level Clear"):
                        count = len([i for i in player.inventory if str(i).startswith("Level Clear")])
                        required_num = 0
                        required_maps = []
                        req_maps_formatted = []
                        if settings['Win conditions']['nrof-maps'] == "all":
                            required_num = len(settings['Included levels'])
                        else:
                            required_num = int(settings['Win conditions']['nrof-maps'])
                            required_maps = list(settings['Win conditions']['specific-maps'])

                        if len(required_maps) > 0:
                            for map in required_maps:
                                if player.has_item(f"Level Clear ({map})"):
                                    req_maps_formatted.append(f"~~{map}~~")
                                else:
                                    req_maps_formatted.append(map)

                        return f"{item} ({count}/{required_num}{f"+{",".join(req_maps_formatted)}" if len(required_maps) > 0 else ""})"
                    if any([str(item).startswith(color) for color in ["Blue","Yellow","Red"]]) and not str(item) == "BlueArmor":
                        item_match = item_regex.match(item)
                        subitem,map = item_match.groups()
                        collected_string = str()
                        keys = [f"{color}{key}" for color in ["Blue","Yellow","Red"] for key in ["Skull", "Card"]]
                        map_keys = sorted([i for i in item_table['gzDoom'].keys() if (i.endswith(f"({map})") and any([key in i for key in keys]))])
                        for i in map_keys:
                            if player.has_item(i): collected_string += i[0]
                            else: collected_string += "_"
                        if not player.has_item(f"Level Access ({map})"):
                            collected_string = f"~~{collected_string}~~" # Strikethrough keys if not found
                        return f"{item} ({collected_string})"
                case "Here Comes Niko!":
                    if item == "Cassette":
                        required = max({k: v for k, v in settings.items() if "Cassette Cost" in k}.values())
                        return f"{item} ({count}/{required})"
                    if item.endswith("Cassette") and settings['Cassette Logic'] == "Level Based":
                        total = 10
                        return f"{item} ({count}/{total})"
                    if item == "Coin":
                        required = 76 if settings['Completion Goal'] == "Employee" else settings['Elevator Cost']
                        return f"{item} (*{count}/{required}*)"
                    if item in ["Hairball City Fish", "Turbine Town Fish", "Salmon Creek Forest Fish", "Public Pool Fish", "Bathhouse Fish", "Tadpole HQ Fish"] and settings['Fishsanity'] == "Insanity":
                        required = 5
                        return f"{item} ({count}/{required})"
                case "Hollow Knight":
                    if item == "Grub":
                        total = 46
                        return f"{item} ({count}/{total})"
                    
                    return item.replace("_", " ").replace("-"," - ")
                case "Jigsaw":
                    if item.endswith("Puzzle Pieces"):
                        starting_pieces: int = int(settings['Precollected pieces']) if settings['Precollected pieces'] else 0
                        pieces_per_item: int = int(item.split()[0])
                        item_count: int = player.get_item_count(item)

                        total_pieces = starting_pieces + (pieces_per_item * item_count)
                        return f"{item} ({total_pieces} Available)"
                case "Kingdom Hearts 2":
                    if item == "Bounty" and settings["Goal"] == "Hitlist":
                        required = settings['Bounties Required']
                        return f"{item} (*{count}/{required}*)"
                case "A Link to the Past":
                    if item == "Triforce Piece" and "Triforce Hunt" in settings['Goal']:
                        required = settings['Triforce Pieces Required']
                        return f"{item} (*{count}/{required}*)"
                    if item == "Piece of Heart":
                        if count % 4 == 0:
                            return f"{item} (+1 Heart Container)"
                        else: return f"{item} ({count % 4}/4)"
                case "Mega Man 2":
                    if item.endswith("Access Codes"):
                        total = 8
                        count = len([i for i in player.inventory if str(i).endswith("Access Codes")])
                        return f"{item} ({count}/{total})"
                case "Muse Dash":
                    if item == "Music Sheet":
                        song_count = settings['Starting Song Count'] + settings['Additional Song Count']
                        total = round(song_count * (settings['Music Sheet Percentage'] / 100))
                        required = round(total / (settings['Music Sheets Needed to Win'] / 100))
                        return f"{item} ({count}/{required})"
                case "Ocarina of Time":
                    if item == "Triforce Piece" and settings['Triforce Hunt'] is True:
                        required = settings['Required Triforce Pieces']
                        return f"{item} ({count}/{required})"
                    if item == "Gold Skulltula Token":
                        required = 50
                        return f"{item} ({count}/{required})"
                    if item == "Progressive Wallet":
                        capacities = ["99", "200", "500", "999"]
                        return f"{item} ({capacities[player.get_item_count(item)]} Capacity)"
                    if item == "Piece of Heart":
                        if count % 4 == 0:
                            return f"{item} (+1 Heart Container)"
                        else: return f"{item} ({count % 4}/4)"
                case "Pokemon Mystery Dungeon Explorers of Sky":
                    sky_peaks = [ "1st Station Pass", "2nd Station Pass", "3rd Station Pass", "4th Station Pass",
                        "5th Station Pass", "6th Station Pass", "7th Station Pass", "8th Station Pass",
                        "9th Station Pass", "Sky Peak Summit Pass" ]
                    instruments = ["Icy Flute", "Fiery Drum", "Terra Cymbal", "Aqua-Monica", "Rock Horn", "Grass Corner",
                                   "Sky Melodica", "Stellar Symphony", "Null Bagpipes", "Glimmer Harp", "Toxic Sax",
                                   "Biting Bass", "Knockout Bell", "Spectral Chimes", "Liar's Lyre", "Charge Synth",
                                   "Norma-ccordion", "Psychic Cello", "Dragu-teki", "Steel Guitar"]
                    seal_unlocks = [ "Ice Aegis Cave", "Rock Aegis Cave", "Steel Aegis Cave", "Aegis Cave Pit" ]

                    if item == "Progressive Sky Peak":
                        return f"{item} ({sky_peaks[count-1]})"
                case "Powerwash Simulator":
                    if item == "A Job Well Done" and settings['Goal Type'] == "Mcguffin":
                        # I need to figure out how to calculate the amount required
                        # so for now
                        pass
                case "Pizza Tower":
                    if item == "Toppin":
                        total = settings['Toppin Count']
                        required = max([settings[f'Floor {num} Boss Toppins'] for num in range(1, 6)])
                        return f"{item} ({count}/{required})"
                case "Simon Tatham's Portable Puzzle Collection":
                    # Tracking total access to puzzles instead of completion percentage
                    # that's for the locations
                    total = settings['puzzle count']
                    count = len(player.inventory)
                    return f"{item} ({count}/{total})"
                case "Sonic Adventure 2 Battle":
                    if item == "Emblem":
                        required = round(settings['Max Emblem Cap'] * (settings["Emblem Percentage for Cannon's Core"] / 100))
                        return f"{item} ({count}/{required})"
                case "Super Cat Planet":
                    if item == "Cat":
                        total = 169
                        return f"{item} ({count}/{total})"
                    if item == "Strange Cat":
                        total = 17
                        return f"{item} ({count}/{total})"
                case "Super Mario 64":
                    if item == "Power Star":
                        required = round(
                            settings['Total Power Stars']
                            * (settings['Endless Stairs Star %'] / 100)
                        )
                        return f"{item} ({count}/{required})"
                case "Super Mario World":
                    if item == "Progressive Powerup":
                        prog_powerup = ["Super Mushroom", "Fire Flower", "Cape Feather"]
                        return f"{item} ({prog_powerup[count-1]})"
                    if item == "Yoshi Egg" and settings['Goal'] == "Yoshi Egg Hunt":
                        required = round(
                            settings['Max Number of Yoshi Eggs']
                            * (settings['Required Percentage of Yoshi Eggs'] / 100))
                        return f"{item} ({count}/{required})"
                    if item == "Boss Token" and settings['Goal'] == "Bowser":
                        required = settings['Bosses Required']
                        return f"{item} ({count}/{required})"
                case "Trackmania":
                    medals = ["Bronze Medal", "Silver Medal", "Gold Medal", "Author Medal"]
                    # From TMAP docs: 
                    # "The quicket medal equal to or below target difficulty is made the progression medal."
                    target_difficulty = settings['Target Time Difficulty']
                    progression_medal_lookup = target_difficulty // 100
                    progression_medal = medals[progression_medal_lookup]

                    if item == progression_medal:
                        total = len([l for l in player.locations.values() if l.location.endswith("Target Time")])
                        required = math.ceil(total * (settings['Series Medal Percentage'] / 100))
                        return f"{item} ({count}/{required})"
                case "TUNIC":
                    treasures = {
                        "DEF": ["Secret Legend", "Phonomath"],
                        "POTION": ["Spring Falls", "Just Some Pals", "Back To Work"],
                        "SP": ["Forever Friend", "Mr Mayor", "Power Up", "Regal Weasel"],
                        "MP": ["Sacred Geometry", "Vintage", "Dusty"]
                    }
                    if item == "Flask Shard":
                        flask_progress = player.get_item_count(item) % 3
                        return f"{item} ({"Gained Flask!" if flask_progress == 0 else f"{flask_progress}/3"})"
                    if item == "Fairy":
                        required = 20
                        if count < 10: required = 10
                        return f"{item} ({count}/{required})"
                    if item == "Gold Questagon":
                        required = settings['Gold Hexagons Required']
                        return f"{item} (*{count}/{required}*)"
                    if item == "Golden Coin":
                        required = [3,6,10,15,20]
                        next_req = 0
                        for check in required:
                            if count >= check: continue
                            if count < check:
                                next_req = check
                                break
                        if next_req == 0: next_req = 20

                        return f"{item} ({count}/{next_req})"
                    if item in ["Blue Questagon", "Red Questagon", "Green Questagon"]:
                        count = len(i for i in ["Blue Questagon", "Red Questagon", "Green Questagon"] if i in player.inventory)
                        required = 3
                        return f"{item} (*{count}/{required}*)"
                    if item == "Sword Upgrade":
                        upgrades = ["Stick", "Ruin Seeker's Sword", "Librarian's Sword", "Heir's Sword"]
                        upgrade = upgrades[count-1]
                        return f"{item} (LV{count}: {upgrade})"
                    # Treasures
                    for stat, stat_items in treasures.items():
                        if item in stat_items:
                            return f"{item} (+1 {stat})"
                case "Twilight Princess":
                    if item == "Poe Soul":
                        required = 60
                        if count < 20: required = 20
                        return f"{item} ({count}/{required})"
                case "Void Stranger":
                    if item == "Greed Coin":
                        required = 15
                        return f"{item} ({count}/{required})"
                    if item == "Locust Idol" or item == "Lucky Locust Idol":
                        OneCount = player.get_item_count("Locust Idol")
                        ThreeCount = Player.get_item_count("Lucky Locust Idol")
                        count = OneCount + (ThreeCount * 3)
                        return f"{item} ({count})"
                case "Wario Land 4":
                    if item.endswith("Piece"):
                        # Gather up all the jewels
                        jewels = ["Emerald", "Entry Jewel", "Golden Jewel", "Ruby", "Sapphire", "Topaz"]
                        parts = ["Bottom Left", "Bottom Right", "Top Left", "Top Right"]
                        jewel = next(j for j in jewels if j in item) # Get jewel name matching the item

                        # Count how many pieces of this jewel we have
                        jewel_count = len(player.get_collected_items([f"{part} {jewel} Piece" for part in parts]))
                        jewel_pieces = 4 # This is static

                        def determine_complete_jewels() -> int:
                            """Helper function for WL4 jewels. Returns how many jewels have all pieces collected."""
                            completed = 0
                            for j in jewels:
                                bottom_left = player.get_item_count(f"Bottom Left {j} Piece")
                                bottom_right = player.get_item_count(f"Bottom Right {j} Piece")
                                top_left = player.get_item_count(f"Top Left {j} Piece")
                                top_right = player.get_item_count(f"Top Right {j} Piece")

                                # If we have at least one of each piece, we have at least one completed jewel
                                # Get the minimum of all counts to determine how many
                                if all([bottom_left > 0, bottom_right > 0, top_left > 0, top_right > 0]):
                                    completed += min([bottom_left, bottom_right, top_left, top_right])

                            return completed


                        jewels_complete = determine_complete_jewels()
                        jewels_required = settings['Required Jewels']

                        # return f"{item} ({jewel_count}/{jewel_pieces}P|{jewels_complete}/{jewels_required}C)"
                        return f"{item} ({jewels_complete}/{jewels_required}C)"
                case _:
                    return item
        except Exception as e:
            logger.error(f"Error while parsing tracking info for item {item} in game {game}:", e, exc_info=True)
            # If we can't parse the item, just return the name
            # This is to prevent the bot from crashing if something goes wrong
            # with the settings or the item name.
            return item
    # If the item is not in the settings, return the name as is

    # Return the same name if nothing matched (or no settings available)
    return item

def handle_location_tracking(game: Game, player: Player, item: Item):
    """If checking a location is an indicator of progress, we should track that in the location name."""

    ItemObject = item
    location = item.location

    if bool(player.settings):
        settings = player.settings
        game = player.game

        match game:
            case "A Hat in Time":
                if location.startswith("Tasksanity") and settings['Tasksanity'] is True:
                    total = settings['Tasksanity Check Count']
                    return f"{location}/{total}"
            case "Hollow Knight":
                # There'll probably be something here later
                return location.replace("_", " ").replace("-"," - ")
            case "Jigsaw":
                if location.startswith("Merge"):
                    count = player.collected_locations
                    dimensions = settings['Puzzle dimension'].split("×")
                    required = int(dimensions[0]) * int(dimensions[1])
                    return f"{location} (of {required})"
            case "Mega Man 2":
                if location.endswith(" - Defeated"):
                    count = len([l for l in player.locations.values() if l.location.endswith(" - Defeated") and l.found is True])
                    required = 8
                    return f"{location} ({count}/{required})"
            case "Simon Tatham's Portable Puzzle Collection":
                required = round(settings['puzzle count']
                                 * (settings['Target Completion Percentage'] / 100))
                count = player.collected_locations
                return f"{location} ({count}/{required})"
            # case "Trackmania":
            #     if location.endswith("Target Time"):
            #         total = len([l for l in player.locations.values() if l.location.endswith("Target Time")])
            #         required = round(total * (settings['Series Medal Percentage'] / 100))
            #         count = len([l for l in player.locations.values() if l.location.endswith("Target Time") and l.found is True])
            #         return f"{location} ({count}/{required})"
            case _:
                return location
    return location

def handle_location_hinting(player: Player, item: Item) -> tuple[list[str], str]:
    """Some locations have a cost or extra info associated with it.
    If an item that's hinted is on this location, go through similar steps to
    the tracking functions to provide info on costs etc."""

    location = item.location

    requirements = []
    extra_info = ""

    if bool(player.settings):
        settings = player.settings
        game = item.game

        match game:
            case "Here Comes Niko!":
                contact_lists = {
                    "1": [
                        f"Hairball City - {npc}" for npc in ["Mitch", "Mai", "Moomy", "Blippy Dog", "Nina"]
                    ] + [
                        f"Turbine Town - {npc}" for npc in ["Mitch", "Mai", "Blippy Dog"]
                    ] + [
                        f"Salmon Creek Forest - {npc}" for npc in (["SPORTVIVAL", "Mai"]
                        + ["Fish with Fischer", "Bass", "Catfish", "Pike", "Salmon", "Trout"])
                    ],
                    "2": [
                        f"Hairball City - {npc}" for npc in ["Game Kid", "Blippy", "Serschel & Louist"]
                    ] + [
                        f"Turbine Town - {npc}" for npc in ["Blippy", "Serschel & Louist"]
                    ] + [
                        f"Salmon Creek Forest - {npc}" for npc in ["Game Kid", "Blippy", "Serschel & Louist"]
                    ] + [
                        f"Public Pool - {npc}" for npc in (["Mitch", "SPORTVIVAL VOLLEY", "Blessley"]
                        + ["Little Gabi's Flowers"] + [f"Flowerbed {num+1}" for num in range(3)])
                    ] + [
                        f"Bathhouse - {npc}" for npc in (["Blessley", "Blippy", "Blippy Dog"]
                        + ["Little Gabi's Flowers"] + [f"Flowerbed {num+1}" for num in range(3)]
                        + ["Fish with Fischer", "Anglerfish", "Clione", "Jellyfish", "Little Wiggly Guy", "Pufferfish"])
                    ]
                }

                level = None
                npc = None
                try:
                    level, npc = location.split(" - ")
                except ValueError:
                    level = location
                    npc = None

                if f"{location} Cassette Cost" in settings:
                    # Get the cassette cost
                    cost = settings[f"{location} Cassette Cost"]

                    # Cassette Requirements
                    if settings['Cassette Logic'] == "Level Based":
                        requirements.append(f"{cost} {level} Cassettes")
                    else:
                        requirements.append(f"{cost} Cassettes")

                if f"Kiosk {level} Cost" in settings and location == f"{level} - Kiosk":
                    # Get the kiosk cost
                    cost = settings[f"Kiosk {level} Cost"]

                    requirements.append(f"{cost} Coins")

                # Contact List Requirements
                if location in contact_lists["1"]:
                    requirements.append("Contact List 1")
                if location in contact_lists["2"]:
                    requirements.append("Contact List 2")

                if "Chatsanity" in location and settings['Textbox'] is True:
                    requirements.append("Textbox")


    if bool(requirements):
        logger.info(f"Updating item's location {item.location} with requirements: {requirements}")
    return (requirements, extra_info)

def handle_state_tracking(player: Player):
    """Use the tracked game state to build a summary of the player's progress."""

    player_game = player.game
    settings = player.settings

    goal: str = ""
    goal_str: str = ""

    match player_game:
        case "A Hat in Time":
            hats = ["Sprint Hat", "Brewing Hat", "Ice Hat", "Dweller Mask", "Time Stop Hat"]
            collected_hats = player.get_collected_items(hats)
            time_pieces = player.get_item_count("Time Piece")
            time_pieces_required: int

            goal = settings['End Goal']
            match goal:
                case "Finale":
                    goal_str = "Defeat Mustache Girl"
                    time_pieces_required = settings['Chapter 5 Cost']
                case "Rush Hour":
                    goal_str = "Escape Nyakuza Metro's Rush Hour"
                    time_pieces_required = settings['Chapter 7 Cost']
                case "Seal The Deal":
                    goal_str = "Seal the Deal with Snatcher"
                case _:
                    goal_str = goal
            if goal == "Finale" or goal == "Rush Hour":
                player.stats.set_stat("time_pieces", time_pieces)
                player.stats.set_stat("time_pieces_required", time_pieces_required)
            player.stats.set_stat("found_hats", [hat.name for hat in collected_hats])

            if goal == "Rush Hour":
                metro_tickets = ["Yellow", "Pink", "Green", "Blue"]
                player.stats.set_stat("collected_tickets", [item.name for item in player.get_collected_items([f"Metro Ticket - {color}" for color in metro_tickets]) ])

            world_costs = {
                "Kitchen": settings['Chapter 1 Cost'],
                "Machine Room": settings['Chapter 2 Cost'],
                "Bedroom": settings['Chapter 3 Cost'],
                "Boiler Room": settings['Chapter 4 Cost'],
                "Attic": settings['Chapter 5 Cost'],
                "Laundry": settings['Chapter 6 Cost'],
                "Lab": settings['Chapter 7 Cost'],
            }
            player.stats.set_stat("accessible_worlds",[k for k, v in world_costs.items() if time_pieces >= v])

        case "Celeste (Open World)":
            goal = settings['Goal Area']

            required_strawberries = settings['Total Strawberries'] * (settings['Strawberries Required Percentage'] / 100)
            goal_with_strawbs = lambda string: string + f" (with {int(required_strawberries)} Strawberries)"

            match goal:
                case "The Summit A":
                    goal_str = goal_with_strawbs("Reach the Summit of Mount Celeste")
                case "The Summit B":
                    goal_str = goal_with_strawbs("Take a Harder Path to Mount Celeste's Summit")
                case "The Summit C":
                    goal_str = goal_with_strawbs("Reach Celeste's Hardest Peak")
                case "Core A":
                    goal_str = goal_with_strawbs("Reach the Heart of the Mountain")
                case "Core B":
                    goal_str = goal_with_strawbs("Understand the Heart of the Mountain")
                case "Core C":
                    goal_str = goal_with_strawbs("Conquer the Heart of the Mountain")
                case "Empty Space":
                    goal_str = goal_with_strawbs("Reach Acceptance?")
                case "Farewell":
                    goal_str = goal_with_strawbs("Bid Farewell")
                case "Farewell Golden":
                    goal_str = goal_with_strawbs("Conquer Farewell's Hardest Challenge")
        
        case "Here Comes Niko!":
            coins = player.get_item_count("Coin")
            coins_required: int

            goal = settings['Completion Goal']
            match goal:
                case "Hired":
                    goal_str = "Get Hired as a Professional Friend"
                    coins_required = settings['Elevator Cost']
                case "Employee":
                    goal_str = "Become Employee of the Month"
                    coins_required = 76
                case _:
                    goal_str = goal

            movement_abilities = [
                "Textbox",
                "Swim Course",
                "Apple Basket",
                "Safety Helmet",
                "Bug Net",
                "Soda Repair",
                "Parasol Repair",
                "AC Repair",
            ]

            player.stats.set_stat("coins", coins)
            player.stats.set_stat("coins_required", coins_required)
            player.stats.set_stat("movement_abilities", [ability.name for ability in player.get_collected_items(movement_abilities)])

        case "Kingdom Hearts 2":
            match settings['Goal']:
                case "Three Proofs":
                    goal_str = "Collect the Three Proofs of Connection, Nonexistence and Peace"
                case "Hitlist":
                    required = settings['Bounties Required']
                    goal_str = f"Collect {required} Bounties"
                case _:
                    goal_str = settings['Goal']

        case "Jigsaw":
            dimensions = settings['Puzzle dimension'].split("×")
            required = int(dimensions[0]) * int(dimensions[1])
            goal_str = f"Complete a {settings['Puzzle dimension']} ({required} piece) Puzzle"

        case "Link to the Past":
            # Goal matching
            if settings['Goal'].endswith("Triforce Hunt"):
                required_pieces = settings['Triforce Pieces Required']
                goal_str = f"Collect {required_pieces} Triforce Pieces"
            if "Ganon" in settings['Goal'] and settings['Goal'] != "Ganon":
                goal_str += ", then Defeat Ganon"
            if settings['Goal'] == "Ganon":
                goal_str = "Reach Ganon and Defeat Him"

        case "Ocarina of Time":
            max_hearts = 20
            starting_hearts = 3
            heart_containers = player.get_item_count("Heart Container")
            heart_pieces = player.get_item_count("Piece of Heart")
            completed_heart_pieces = heart_pieces // 4
            partial_hearts = heart_pieces % 4

            current_hearts = starting_hearts + heart_containers + completed_heart_pieces
            player.stats.set_stat("current_hearts", current_hearts)

            match settings['Triforce Hunt']:
                case True:
                    goal_pieces = settings['Required Triforce Pieces']
                    goal_str = f"Collect {goal_pieces} Triforce Pieces from around Hyrule"

                    triforce_pieces = player.get_item_count("Triforce Piece")
                case False:
                    goal_str = "Defeat Ganon and Save Hyrule"

            # TODO Get main inventory

        case "Pokemon Emerald":
            match settings['Goal']:
                case "Champion":
                    goal_str = "Become Champion of the Hoenn League"

        case "Simon Tatham's Portable Puzzle Collection":
            required = round(settings['puzzle count']
                                * (settings['Target Completion Percentage'] / 100))
            count = player.collected_locations
            goal_str = f"Solve {required} puzzles"

        case "Super Cat Planet":
            match settings['Goal Ending']:
                case "Crows":
                    goal_str = "Evade Crows and Rescue the King of the Cats"
                case "Final Boss":
                    goal_str = "Best the Dark Angel"

        case "Super Mario World":
            match settings['Goal']:
                case "Yoshi Egg Hunt":
                    eggs = player.get_item_count("Yoshi Egg")
                    required = round(settings['Max Number of Yoshi Eggs'] * (settings['Required Percentage of Yoshi Eggs'] / 100))
                    goal_str = f"Return {required} Yoshi Eggs to Yoshi's House"

                    player.stats.set_stat("collected_eggs", eggs)
                    player.stats.set_stat("required_eggs", required)
                case "Bowser":
                    boss_tokens = player.get_item_count("Boss Token")
                    required = settings['Bosses Required']
                    player.stats.set_stat("collected_boss_tokens", boss_tokens)
                    player.stats.set_stat("required_boss_tokens", required)

                    goal_str = f"Defeat {required} Bosses, and then Bowser"

            movement_abilities = [
                "Climb",
                "Swim",
                "Progressive Powerup",
            ] + [f"{color} Switch Palace" for color in ["Red", "Green", "Yellow", "Blue"]]
        
            player.stats.set_stat("movement_abilities", 
                                  [ability.name for ability in player.get_collected_items(movement_abilities)])
        
        case "TUNIC":
            if settings['Hexagon Quest'] is True:
                required = settings['Gold Hexagons Required']
                gold_questagons = player.get_item_count("Gold Questagon")
                goal_str = f"Collect {required} Hexagons and Return to the Heir"
            else:
                seal_questagons = player.get_collected_items(["Red Questagon", "Green Questagon", "Blue Questagon"]) 
                player.stats.set_stat("collected_seal_questagons", [q.name for q in seal_questagons])
                goal_str = "Claim Your Rightful Place"

            treasures = {
                "DEF": ["Secret Legend", "Phonomath"],
                "POTION": ["Spring Falls", "Just Some Pals", "Back To Work"],
                "SP": ["Forever Friend", "Mr Mayor", "Power Up", "Regal Weasel"],
                "MP": ["Sacred Geometry", "Vintage", "Dusty"]
            }

            for stat in ["ATT", "DEF", "HP", "SP", "MP", "POTION"]:
                player.stats.set_stat(
                    f"logical_{stat.lower()}", 
                    player.get_item_count(f"{stat} Offering") + 
                    (len(player.get_collected_items(treasures[stat])) if stat in treasures else 0)
                )

        # MANUAL GAMES
        case "Manual_PokemonPlatinum_Linneus":
            match settings['goal']:
                case "Pokemon League - Become Champion":
                    goal_str = "Become Champion of the Sinnoh League"
                case _:
                    goal_str = settings['goal']

        case _:
            pass

    player.stats.goal_str = goal_str
