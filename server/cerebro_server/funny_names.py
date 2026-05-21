"""Wandb-style random adjective-noun name generator for new sessions."""
import random

_ADJECTIVES = [
    "happy", "fluffy", "swift", "lazy", "noisy", "silent", "tiny", "giant",
    "brave", "shy", "wild", "calm", "bouncy", "sleepy", "snappy", "fancy",
    "jolly", "moody", "spicy", "sweet", "bitter", "salty", "fuzzy", "spiky",
    "shiny", "dusty", "muddy", "frosty", "sunny", "stormy", "misty", "rainy",
    "cosmic", "lunar", "neon", "rusty", "golden", "silver", "crimson", "azure",
    "wandering", "curious", "feral", "stoic", "drowsy", "glowing", "humble",
    "puzzled", "mighty", "sneaky", "loud", "groovy",
]

_NOUNS = [
    "otter", "falcon", "panda", "tiger", "yak", "moose", "platypus", "newt",
    "raven", "fox", "wolf", "lemur", "kraken", "phoenix", "dragon", "narwhal",
    "marmot", "axolotl", "okapi", "tapir", "puffin", "ibis", "heron", "owl",
    "cloud", "comet", "meteor", "nebula", "galaxy", "quasar", "pulsar",
    "harbor", "summit", "canyon", "tundra", "geyser", "delta", "fjord",
    "gizmo", "widget", "sprocket", "lantern", "kettle", "pebble", "anvil",
    "muffin", "bagel", "pickle", "mango", "lychee", "papaya",
]


def random_name() -> str:
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"
