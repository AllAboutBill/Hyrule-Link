"""Silly auto-generated room names like 'Tasty-GoKart-Muffins'."""
import random

ADJECTIVES = [
    "Tasty", "Sneaky", "Mighty", "Soggy", "Turbo", "Spicy", "Cosmic", "Grumpy",
    "Wobbly", "Fancy", "Sleepy", "Zesty", "Bouncy", "Cranky", "Dizzy", "Funky",
    "Jolly", "Lucky", "Nifty", "Plucky", "Rowdy", "Sassy", "Velvet", "Witty",
    "Zippy", "Brave", "Chunky", "Dapper", "Feisty", "Glowing", "Hyper", "Quirky",
    "Rusty", "Snazzy", "Wild", "Cheeky", "Frosty", "Gnarly", "Loopy", "Mellow",
]
NOUNS = [
    "GoKart", "Muffin", "Goblin", "Wizard", "Pickle", "Noodle", "Cactus", "Penguin",
    "Raccoon", "Waffle", "Dragon", "Llama", "Hamster", "Burrito", "Pretzel", "Banjo",
    "Comet", "Yeti", "Walrus", "Gnome", "Otter", "Mango", "Taco", "Biscuit", "Turnip",
    "Wombat", "Narwhal", "Pancake", "Marble", "Pyramid",
    # a little Zelda flavor
    "Triforce", "Hookshot", "Boomerang", "Mushroom", "Rupee", "Moblin", "Octorok",
    "Korok", "Zora", "Goron", "Deku", "Bombchu", "Cucco", "Chuchu", "Keese",
]


def random_room_name() -> str:
    a = random.choice(ADJECTIVES)
    n1, n2 = random.sample(NOUNS, 2)
    return f"{a}-{n1}-{n2}"
