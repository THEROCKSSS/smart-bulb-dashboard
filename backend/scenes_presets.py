PRESET_COLORS = [
    {"id": "red", "name": "Red", "rgb": [255, 0, 0]},
    {"id": "orange", "name": "Orange", "rgb": [255, 100, 0]},
    {"id": "amber", "name": "Amber", "rgb": [255, 170, 0]},
    {"id": "yellow", "name": "Yellow", "rgb": [255, 255, 0]},
    {"id": "lime", "name": "Lime", "rgb": [170, 255, 0]},
    {"id": "green", "name": "Green", "rgb": [0, 255, 0]},
    {"id": "emerald", "name": "Emerald", "rgb": [0, 255, 130]},
    {"id": "teal", "name": "Teal", "rgb": [0, 255, 200]},
    {"id": "cyan", "name": "Cyan", "rgb": [0, 255, 255]},
    {"id": "sky", "name": "Sky Blue", "rgb": [0, 170, 255]},
    {"id": "blue", "name": "Blue", "rgb": [0, 0, 255]},
    {"id": "indigo", "name": "Indigo", "rgb": [75, 0, 255]},
    {"id": "violet", "name": "Violet", "rgb": [140, 0, 255]},
    {"id": "purple", "name": "Purple", "rgb": [180, 0, 200]},
    {"id": "magenta", "name": "Magenta", "rgb": [255, 0, 255]},
    {"id": "pink", "name": "Pink", "rgb": [255, 0, 130]},
    {"id": "rose", "name": "Rose", "rgb": [255, 50, 90]},
    {"id": "hot_pink", "name": "Hot Pink", "rgb": [255, 20, 147]},
    {"id": "coral", "name": "Coral", "rgb": [255, 90, 60]},
    {"id": "gold", "name": "Gold", "rgb": [255, 200, 0]},
    {"id": "mint", "name": "Mint", "rgb": [100, 255, 180]},
    {"id": "turquoise", "name": "Turquoise", "rgb": [50, 220, 220]},
    {"id": "lavender", "name": "Lavender", "rgb": [200, 160, 255]},
    {"id": "white_warm", "name": "Warm White", "rgb": [255, 214, 170]},
    {"id": "white_cool", "name": "Cool White", "rgb": [220, 235, 255]},
]

SCENES = [
    {
        "id": "movie_night",
        "name": "Movie Night",
        "description": "Dim warm blue for watching films",
        "mode": "colour",
        "rgb": [40, 50, 120],
        "brightness": 25,
    },
    {
        "id": "reading",
        "name": "Reading",
        "description": "Bright neutral white for focus reading",
        "mode": "white",
        "color_temp": 60,
        "brightness": 90,
    },
    {
        "id": "party",
        "name": "Party",
        "description": "Vivid magenta, full brightness",
        "mode": "colour",
        "rgb": [255, 0, 200],
        "brightness": 100,
    },
    {
        "id": "sunset",
        "name": "Sunset",
        "description": "Warm orange glow",
        "mode": "colour",
        "rgb": [255, 90, 20],
        "brightness": 60,
    },
    {
        "id": "ocean",
        "name": "Ocean",
        "description": "Cool teal calm",
        "mode": "colour",
        "rgb": [0, 150, 180],
        "brightness": 55,
    },
    {
        "id": "forest",
        "name": "Forest",
        "description": "Deep green ambiance",
        "mode": "colour",
        "rgb": [20, 120, 40],
        "brightness": 50,
    },
    {
        "id": "romantic",
        "name": "Romantic",
        "description": "Soft rose, low brightness",
        "mode": "colour",
        "rgb": [255, 40, 90],
        "brightness": 30,
    },
    {
        "id": "focus",
        "name": "Focus / Work",
        "description": "Crisp cool white, high brightness",
        "mode": "white",
        "color_temp": 85,
        "brightness": 100,
    },
    {
        "id": "relax",
        "name": "Relax",
        "description": "Warm dim white",
        "mode": "white",
        "color_temp": 20,
        "brightness": 35,
    },
    {
        "id": "night_light",
        "name": "Night Light",
        "description": "Very dim warm amber",
        "mode": "colour",
        "rgb": [255, 140, 40],
        "brightness": 5,
    },
    {
        "id": "wake_up",
        "name": "Wake Up",
        "description": "Bright daylight white to help you wake",
        "mode": "white",
        "color_temp": 70,
        "brightness": 100,
    },
    {
        "id": "holiday",
        "name": "Holiday Red",
        "description": "Festive red",
        "mode": "colour",
        "rgb": [200, 0, 0],
        "brightness": 70,
    },
    {
        "id": "halloween",
        "name": "Halloween",
        "description": "Eerie orange-purple",
        "mode": "colour",
        "rgb": [255, 80, 0],
        "brightness": 65,
    },
    {
        "id": "valentine",
        "name": "Valentine",
        "description": "Soft pink romance",
        "mode": "colour",
        "rgb": [255, 90, 150],
        "brightness": 45,
    },
    {
        "id": "gaming",
        "name": "Gaming",
        "description": "Saturated purple ambiance",
        "mode": "colour",
        "rgb": [130, 0, 255],
        "brightness": 60,
    },
]

EFFECTS = [
    {"id": "rainbow", "name": "Rainbow Cycle", "description": "Smoothly cycles through the full hue spectrum"},
    {"id": "pulse", "name": "Pulse / Breathe", "description": "Fades brightness up and down on the current color"},
    {"id": "strobe", "name": "Strobe", "description": "Rapid on/off flashing"},
    {"id": "candle", "name": "Candle Flicker", "description": "Randomized warm flicker simulating a flame"},
    {"id": "fade", "name": "Two-Color Fade", "description": "Smoothly alternates between two chosen colors"},
    {"id": "color_loop", "name": "Preset Color Loop", "description": "Steps through the preset color swatches in sequence"},
    {"id": "random", "name": "Random Color Jump", "description": "Jumps to a new random vivid color at an interval"},
]


def find_preset(preset_id):
    return next((p for p in PRESET_COLORS if p["id"] == preset_id), None)


def find_scene(scene_id):
    return next((s for s in SCENES if s["id"] == scene_id), None)


def find_effect(effect_id):
    return next((e for e in EFFECTS if e["id"] == effect_id), None)
