"""The names the game shows for what a replay header records as codes.

The header says "spaces/49_Shoreside", "PJSC008-Myoko" and "pvp"; the
results screen says Scandinavia, Myoko and Standard. Only maps already
played in live battles are listed. A map that is not falls back to its
code, cleaned ("62_New_Map" -> "New Map"), so a new one still reads.
"""
import re

MAPS = {
    "00_CO_OCEAN": "Ocean",
    "01_SOLOMON_ISLANDS": "Solomon Islands",
    "04_ARCHIPELAGO": "Archipelago",
    "05_RING": "Ring",
    "08_NE_PASSAGE": "Strait",
    "10_NE_BIG_RACE": "Big Race",
    "13_OC_NEW_DAWN": "New Dawn",
    "14_ATLANTIC": "The Atlantic",
    "15_NE_NORTH": "North",
    "16_OC_BEES_TO_HONEY": "Hotspot",
    "17_NA_FAULT_LINE": "Fault Line",
    "18_NE_ICE_ISLANDS": "Islands of Ice",
    "19_OC_PREY": "Trap",
    "20_NE_TWO_BROTHERS": "Two Brothers",
    "22_TIERRA_DEL_FUEGO": "Land of Fire",
    "23_SHARDS": "Shards",
    "25_SEA_HOPE": "Sea of Fortune",
    "28_NAVAL_MISSION": "Tears of the Desert",
    "33_NEW_TIERRA": "Polar",
    "34_OC_ISLANDS": "Islands",
    "37_RIDGE": "Mountain Range",
    "38_CANADA": "Shatter",
    "41_CONQUEST": "Trident",
    "42_NEIGHBORS": "Neighbors",
    "44_PATH_WARRIOR": "Warrior's Path",
    "45_ZIGZAG": "Loop",
    "46_ESTUARY": "Estuary",
    "47_SLEEPING_GIANT": "Sleeping Giant",
    "49_SHORESIDE": "Scandinavia",
    "50_GOLD_HARBOR": "Haven",
    "51_GREECE": "Greece",
    "52_BRITAIN": "Crash Zone",
    "53_SHORESIDE": "Northern Waters",
    "61_SHARDS_REWORKED": "Shards",
    "HW26_CONCEPT": "Soul Spire",
    "R01_MILITARY_NAVIGATION": "Riposte",
}

# matchGroup -> the battle type the results screen leads with
BATTLE_TYPES = {
    "pvp": "Standard",
    "ranked": "Ranked",
    "cooperative": "Co-op",
    "pve": "PvE",
    "clan": "Clan Battle",
    "clan_elimination_cup": "Clan Battle",
    "training_room": "Training Room",
}


def map_name(raw):
    """'spaces/49_Shoreside' -> 'Scandinavia'; '' for nothing."""
    code = (raw or "").split("/")[-1]
    if not code:
        return ""
    known = MAPS.get(code.upper())
    if known:
        return known
    return re.sub(r"^\d+_", "", code).replace("_", " ").strip() or code


def ship_name(raw):
    """'PJSC008-Myoko' -> 'Myoko'; 'PJSB013-Amagi-1942' -> 'Amagi 1942'."""
    return (raw or "").split("-", 1)[-1].replace("-", " ")


def battle_type(group):
    """'pvp' -> 'Standard'; an unknown group reads as itself, tidied."""
    g = (group or "").lower()
    if not g:
        return ""
    return BATTLE_TYPES.get(g) or g.replace("_", " ").capitalize()
