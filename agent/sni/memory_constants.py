"""
Memory address constants for ALttPR (WRAM offsets from 0x7E0000).
Used by qusb2snes_tracker and SNI plugin.
"""

# Link position addresses
LINK_Y_LOW = 0x0020
LINK_Y_HIGH = 0x0021
LINK_X_LOW = 0x0022
LINK_X_HIGH = 0x0023

# Sprite table constants for enemy spawning
SPRITE_AI_BASE = 0x0DD0
SPRITE_TYPE_BASE = 0x0E20
SPRITE_AUX_BASE = 0x0E30
SPRITE_Y_LOW_BASE = 0x0D00
SPRITE_Y_HIGH_BASE = 0x0D20
SPRITE_X_LOW_BASE = 0x0D10
SPRITE_X_HIGH_BASE = 0x0D30
SPRITE_SPAWN_BASE = 0x0D80
SPRITE_DA0_BASE = 0x0DA0
SPRITE_DB0_BASE = 0x0DB0
SPRITE_HP_BASE = 0x0E50
SPRITE_DC0_BASE = 0x0DC0

# AI states for sprites
AI_INERT = 0x00
AI_INIT = 0x08
AI_ALIVE = 0x09

MEMORY_ADDRESSES = {
    'game_mode': 0x0010,
    'building_flag': 0x001B,
    'death_count': 0xF449,
    'selected_item_index': 0xF33F,
    'sword': 0xF359,
    'shield': 0xF35A,
    'armor': 0xF35B,
    'gloves': 0xF354,
    'half_magic': 0xF37B,
    'rupees': 0xF360,
    'arrows': 0xF377,
    'bombs': 0xF343,
    'max_hp': 0xF36C,
    'current_hp': 0xF36D,
    'visibility': 0x004B,
    'invincibility': 0x037B,
    'speed_setting': 0x005E,
    'speed_modifier': 0x0057,
    'damage_queue': 0x0373,
    'movement': 0x0049,
    'button_a': 0x003B,
    'button_b': 0x003A,
    'cucco_storm': 0xF3C5,
    'crystals': 0xF37A,
    'items_inventory': {
        0xF340: 'bow', 0xF341: 'boomerang', 0xF342: 'hookshot', 0xF343: 'bombs',
        0xF344: 'mushroom', 0xF345: 'firerod', 0xF346: 'icerod', 0xF347: 'bombos',
        0xF348: 'ether', 0xF349: 'quake', 0xF34A: 'lamp', 0xF34B: 'hammer',
        0xF34C: 'shovel', 0xF34D: 'bug_net', 0xF34E: 'book', 0xF350: 'somaria',
        0xF351: 'byrna', 0xF352: 'cape', 0xF353: 'mirror', 0xF354: 'gloves',
        0xF355: 'boots', 0xF356: 'flippers', 0xF357: 'moon_pearl',
        0xF35C: 'bottle1', 0xF35D: 'bottle2', 0xF35E: 'bottle3', 0xF35F: 'bottle4',
    },
    'equipment': {
        0xF359: 'sword_level', 0xF35A: 'shield_level', 0xF35B: 'armor_level',
    },
}

PLAYABLE_MODES = {0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0E}
OUTDOOR_MODES = {0x08, 0x09, 0x0A, 0x0B}
