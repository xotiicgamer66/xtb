import json
import uuid
import asyncio
import random
import time
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="XTRIALS API", version="7.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY STORES
# ─────────────────────────────────────────────────────────────────────────────

SESSIONS: Dict[str, Dict] = {}        # session_id -> game state
USERS: Dict[str, Dict] = {}           # username -> user record
CONNECTIONS: Dict[str, WebSocket] = {} # session_id -> websocket
USER_SESSIONS: Dict[str, str] = {}    # username -> session_id
CHAT_MESSAGES: List[Dict] = []        # global chat log (last 500)
ADMIN_TOKENS: set = set()             # validated admin session tokens

OWNER_ID = "xotiic"

# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class SaveRequest(BaseModel):
    session_id: str
    username: str
    state: Dict[str, Any]

class ActionRequest(BaseModel):
    session_id: str
    username: str
    action_type: str
    payload: Dict[str, Any] = {}

class DeathRequest(BaseModel):
    session_id: str
    username: str
    cause: str = "unknown"

class ChatRequest(BaseModel):
    username: str
    token: str
    text: str = ""
    msg_type: str = "text"  # text | image | gif | voice | system
    file_data: str = ""     # base64 for images/voice
    gif_url: str = ""

class AdminActionRequest(BaseModel):
    admin_username: str
    token: str
    target_session: str
    action: str
    payload: Dict[str, Any] = {}

class QuestRequest(BaseModel):
    session_id: str
    username: str
    quest_type: str
    quest_id: str
    data: Dict[str, Any]

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def make_token(username: str) -> str:
    raw = f"{username}:{time.time()}:{random.random()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

def is_admin(username: str) -> bool:
    return username == OWNER_ID

def tick_clock(state: Dict, minutes: int = 1) -> Dict:
    state["world"]["minutes"] = (state["world"]["minutes"] + minutes) % (24 * 60)
    h = state["world"]["minutes"] // 60
    m = state["world"]["minutes"] % 60
    state["world"]["time"] = f"{h:02d}:{m:02d}"
    if h == 3 and m == 33:
        state["flags"]["floor3"] = True
    return state

def apply_sanity(state: Dict, delta: int, source: str = "unknown") -> Dict:
    p = state["player"]
    p["sanity"] = max(0, min(p["maxSanity"], p["sanity"] + delta))
    sanity = p["sanity"]
    if sanity < 20:
        state["flags"]["hallucination"] = "severe"
    elif sanity < 50:
        state["flags"]["hallucination"] = "moderate"
    elif sanity < 75:
        state["flags"]["hallucination"] = "mild"
    else:
        state["flags"]["hallucination"] = "none"
    return state

def apply_health(state: Dict, delta: int) -> Dict:
    p = state["player"]
    p["health"] = max(0, min(p["maxHealth"], p["health"] + delta))
    return state

def jumpscare_eligible(state: Dict) -> bool:
    now = time.time()
    cooldown = state["flags"].get("jumpscare_cooldown", 0)
    if now > cooldown:
        state["flags"]["jumpscare_cooldown"] = now + random.uniform(25, 75)
        return True
    return False

def awareness_modifier(state: Dict) -> float:
    aw = state["flags"].get("entity_awareness", 0)
    return 1.0 + (aw / 100.0) * 2.0

def default_state(session_id: str, username: str) -> Dict:
    return {
        "session_id": session_id,
        "username": username,
        "created": datetime.utcnow().isoformat(),
        "player": {
            "health": 100,
            "maxHealth": 100,
            "sanity": 100,
            "maxSanity": 100,
            "inventory": [],
            "journal": [],
            "deaths": 0,
            "catCount": 0,
            "memFragments": 0,
            "position": {"x": 3, "y": 5, "map": "station_k7"},
            "facing": "down",
        },
        "world": {
            "time": "23:47",
            "minutes": 1427,
            "maps": ["station_k7"],
            "blood_decals": [],
            "npc_states": {
                "ellis": {"met": False, "tutorial": False},
                "helena": {"met": False, "complete": False},
                "ferryman": {"met": False},
                "forgotten": {"met": False, "memory_restored": False},
                "mimic": {"spawned": False},
            },
            "door_states": {},
            "item_states": {},
        },
        "quests": {
            "main": {
                "investigate": {"active": True, "tapes": 0, "complete": False},
                "find_helena": {"active": False, "fragments": 0, "complete": False},
                "true_name": {"active": False, "runes": 0, "complete": False},
                "seal_or_confront": {"active": False, "choice": None, "complete": False},
            },
            "side": {
                "cat_posters": {"found": 0, "total": 7},
                "vhs_tapes": {"watched": [], "total": 7},
                "memory_restore": {"count": 0},
                "radio_puzzle": {"complete": False},
                "rune_puzzle": {"complete": False},
                "crafting": {"items_crafted": []},
                "anomaly_photos": {"captured": [], "total": 12},
            },
        },
        "flags": {
            "entity_awareness": 0,
            "hallucination": "none",
            "floor3": False,
            "radio_tuned": False,
            "true_name_known": False,
            "cat_count": 0,
            "vhs_watched": [],
            "jumpscare_cooldown": 0,
            "mimic_warned": False,
            "ending": None,
            "lore_discovered": [],
            "npc": {
                "ellis": {"met": False},
                "helena": {"met": False, "complete": False},
                "ferryman": {"met": False},
                "forgotten": {"met": False},
            },
        },
    }

# ─────────────────────────────────────────────────────────────────────────────
# GAME LOGIC
# ─────────────────────────────────────────────────────────────────────────────

DIALOGUE_TREES = {
    "ellis": [
        {
            "text": "You... finally made it. I've been sending the signal for three weeks. The others are gone. All of them. Do NOT go into town at night. Do you understand me?",
            "journal": "Ellis is the radio operator at Station K-7. Terrified. The others are 'gone.'",
            "sanity": -5,
            "give_item": {"id": "flashlight", "name": "Flashlight", "icon": "[FL]", "type": "tool"},
        },
        {
            "text": "The radio started picking up something. Not static. Not a carrier signal. It was breathing. Thirty-seven hours continuous. Then the first one vanished.",
            "journal": "Entity breathes through radio signal. 87.6 MHz. 37 hours.",
            "sanity": -8,
        },
        {
            "text": "The flashlight is yours. Battery won't last forever but it won't approach light directly. Not yet.",
            "journal": "Flashlight repels direct approach. Keep it on.",
            "sanity": 5,
            "awareness": 3,
        },
        {
            "text": "I found Helena's notes in the archive room. She was close to something. Too close. You should find them before... before it notices you looking.",
            "journal": "Helena Vance left research notes in the archive. Find them.",
            "sanity": -3,
            "unlock_quest": "find_helena",
        },
    ],
    "helena": [
        {
            "text": "You can see me. Interesting. The others couldn't — or wouldn't. My name is Helena Vance. I was a researcher. I found something in the well. It found me back.",
            "journal": "Helena Vance — ghost. Former researcher. Connected to the Well.",
            "sanity": -15,
            "awareness": 5,
        },
        {
            "text": "The entity has no true name. Names are cages for concepts. But it has syllables. Components. Arranged correctly they contain it. Arranged wrong... I have seen what wrong looks like.",
            "journal": "Entity's name has four syllables. Order is critical. Wrong order invites it in.",
            "sanity": -20,
            "awareness": 8,
        },
        {
            "text": "The four components are WR, TH, AZ, EH. The correct sequence — I cannot tell you. That is the trap. You must discover it through the rune stones. Third altar in Marrow.",
            "journal": "Syllables: WR TH AZ EH. Correct order unknown. Find the rune altar in Marrow Town.",
            "sanity": -12,
            "awareness": 10,
            "complete": True,
        },
    ],
    "ferryman": [
        {
            "text": "...You have the look of someone who doesn't know what they've walked into. Good. Ignorance is the only armor that works here.",
            "journal": "The Ferryman. Ancient. Knows more than he says.",
            "sanity": -8,
        },
        {
            "text": "I have been ferrying passengers for longer than this island has had a name. Some go back. Most do not. The difference is not luck.",
            "journal": "Ferryman has run this route for 127+ years. Does not age.",
            "sanity": -10,
            "awareness": 5,
        },
        {
            "text": "Want passage? Come back when you understand what you're carrying. You will know when.",
            "journal": "Must understand what I'm 'carrying' before the Ferryman will help. What does that mean?",
            "sanity": -5,
        },
    ],
    "forgotten": [
        {
            "text": "Have you seen... no. I had a face once. I reach up and there's something there but when I try to remember what it looks like I just... there's nothing.",
            "journal": "The Forgotten forgot their own face. Entity takes most-valued memories first.",
            "sanity": -12,
        },
        {
            "text": "It takes what you value most. I valued knowing myself. That was the first thing to go. Think about what you value before you learn too much.",
            "journal": "Warning: learning about the entity increases its awareness of you. The trap of research.",
            "sanity": -8,
            "awareness": 3,
        },
    ],
}

VHS_TAPES = {
    "1": {
        "title": "STATION K-7 ORIENTATION — 1963",
        "text": "A woman in a white coat stands at a blackboard covered in equations. She pauses mid-sentence. Looks at the camera. 'The signal is not random. It never was.' She reaches for the camera. Static.",
        "sanity_hit": 5,
        "awareness": 4,
        "journal": "Tape 1: Signal is intentional. Not random. The entity broadcasts.",
    },
    "2": {
        "title": "MARROW TOWN COMMUNITY MEETING — 1987",
        "text": "Town hall. A man at the podium: 'The well has been sealed for good reason. We do not discuss what lives below.' A woman in the back: 'Helena found a way to talk to it.' Chaos. Tape cuts.",
        "sanity_hit": 10,
        "awareness": 6,
        "journal": "Tape 2: The well was sealed for a reason. Helena spoke to the entity.",
    },
    "3": {
        "title": "[UNLABELED]",
        "text": "Your own face stares back at you from the screen. You are sitting in this exact location, watching this tape. The timestamp reads three minutes from now. The version of you on screen reaches toward the camera. Power cut.",
        "sanity_hit": 25,
        "awareness": 15,
        "journal": "Tape 3: Showed me watching the tape before I watched it. Impossible.",
    },
    "4": {
        "title": "INTERVIEW: THE FERRYMAN",
        "text": "Old man. Interviewer (Helena): 'How many have you carried?' Ferryman: 'Carried, or ferried back?' Long silence. 'The living don't know about the return trip.' He looks at his hands. 'Neither do most of the dead.'",
        "sanity_hit": 15,
        "awareness": 8,
        "journal": "Tape 4: There is a return trip. For the dead.",
    },
    "5": {
        "title": "THE WELL — DOCUMENTARY FRAGMENT",
        "text": "Handheld footage, crew descending stone stairs. Counter: 89 feet. Steps continue. At 120 feet, tape shows footage of Marrow Town, sunny afternoon. Everyone is standing completely still.",
        "sanity_hit": 20,
        "awareness": 10,
        "journal": "Tape 5: Well depth is non-Euclidean. Entry points to somewhere else.",
    },
    "6": {
        "title": "DR VANCE — PERSONAL LOG DAY 134",
        "text": "Helena, visibly worn: 'I have found references to this entity in 12 distinct cultures. Phoenicians called it The Sleeper Below Frequencies. Medieval monks: The Static Saint. It has always been here.'",
        "sanity_hit": 30,
        "awareness": 12,
        "journal": "Tape 6: Entity predates all known civilization. Called by many names.",
    },
    "7": {
        "title": "[RECORDING DATE: UNKNOWN]",
        "text": "60 minutes of an empty hallway. Your hallway. At 58:32, something moves at the edge of frame. It looks exactly like you. Where your face should be: the static of an untuned television set. It walks toward the camera.",
        "sanity_hit": 40,
        "awareness": 20,
        "journal": "Tape 7: There is a version of me here that is not me.",
    },
}

LORE_FRAGMENTS = {
    "station_founding": [
        "Station K-7 established 1963 for classified signal monitoring. Staff: 12. After 6 months: 8. After 1 year: 3.",
        "Station bedrock predates the island. Core samples showed structural inconsistencies. The brass ordered the well capped. They never said why.",
        "Duty logs, 1965: 'Not static. Breathing. 37 hours continuous. Then Markov disappeared.'",
    ],
    "helena_notes": [
        "DR VANCE, DAY 1: Signal follows patterns suggesting non-human intelligence. Something that thinks in frequencies we cannot produce.",
        "DR VANCE, DAY 89: I believe this is sleep-state breathing. It is not fully awake. We must ensure it never wakes.",
        "DR VANCE, DAY 201: The entity has no true name. Arranged correctly, the syllables contain it. Arranged wrong — I have seen what wrong looks like.",
        "DR VANCE, FINAL: If you find this, you have heard the signal. You are marked. Three ways this ends: forget, remember, or become. I chose to remember.",
    ],
    "well_origin": [
        "ARCHAEOLOGICAL REPORT: The well predates all known settlements by 4000 years. Tool marks match no known civilization.",
        "SONAR LOG: At 200m, movement. At 300m, breathing. At 400m, equipment failure. Probe returned to surface 3 days later. No record of how.",
        "BONE ANALYSIS: 38 human remains. DNA shows extra base pairs. Cause of death: 'transition.' No further classification.",
    ],
    "entity_nature": [
        "RESEARCH NOTE: The entity navigates by attention. Thinking about it creates a path it can follow.",
        "BEHAVIORAL NOTES: Stage 1 (0-30%): you feel watched. Stage 2 (30-60%): it learns your patterns. Stage 3 (60-100%): it can find you anywhere.",
        "CONCLUSION: Cannot be killed. Cannot be banished. Can only be contained, forgotten, or joined.",
    ],
    "marrow_history": [
        "TOWN RECORDS: Marrow founded 1847 by whalers who found the island by accident. Leaving 'felt wrong.' They stopped trying to explain this.",
        "ANNUAL REPORT: Every 47 years: Festival of Silence. 24 hours no speech. No one knows why. Predates written records. The entity is quietest during this time.",
        "FERRYMAN DEPOSITION: Has served 127 years. Does not age. Ferried 847 to the island. 12 back.",
    ],
    "forgotten_files": [
        "CASE FILE 1: Male, ~40, arrived 1991. Within 6 months forgot his wife's name. Within 2 years forgot his face. Now: The Forgotten.",
        "CASE FILE 2: Female, age unknown, island 30+ years. Remembers everything except her identity. States it was 'taken as payment.'",
        "THEORY: The entity takes what you value most. The Forgotten are not victims. They are receipts.",
    ],
}

EPITAPHS = {
    "static_walker": "The static remembered your frequency.",
    "the_remembered": "You looked away. It was waiting.",
    "well_spawn": "The well always gets what it reaches for.",
    "sanity": "You stopped believing in yourself before you stopped existing.",
    "signal": "The signal found you before you found it.",
    "unknown": "Something remembered you. Now you cannot forget.",
    "entity": "It knew your name before you did.",
}

CRAFTING_RECIPES = {
    "salt_circle": {"needs": ["salt", "chalk"], "name": "Salt Circle", "type": "ward", "icon": "[O]"},
    "signal_disruptor": {"needs": ["radio", "battery", "wire"], "name": "Signal Disruptor", "type": "weapon", "icon": "[D]"},
    "spirit_camera": {"needs": ["camera", "film"], "name": "Spirit Camera", "type": "tool", "icon": "[P]"},
    "purified_water": {"needs": ["water_bottle", "salt"], "name": "Purified Water", "type": "consumable", "icon": "[W]"},
    "tuning_fork": {"needs": ["metal_rod", "salt"], "name": "Tuning Fork", "type": "weapon", "icon": "[T]"},
}

ENDINGS = {
    "forget": {
        "name": "THE FORGETTING",
        "epilogue": "You wake on the ferry. The town behind you looks normal. The lighthouse is silent. You remember something important, but it slips away like water through open fingers. Perhaps that is for the best.",
        "reqs": lambda s: len(s["flags"].get("vhs_watched", [])) >= 5 and s["flags"].get("entity_awareness", 0) <= 30,
    },
    "remember": {
        "name": "I REMEMBER",
        "epilogue": "The entity has a name. You know it. It knows you know. This changes everything. The island doesn't release those who truly see it. You are not trapped here. You are part of here now. Helena greets you at the lighthouse. 'Welcome home.'",
        "reqs": lambda s: s["flags"].get("true_name_known") and len(s["flags"].get("lore_discovered", [])) >= 10 and len(s["player"].get("journal", [])) >= 8,
    },
    "become": {
        "name": "BECOME SIGNAL",
        "epilogue": "The static clears. A new figure arrives on the ferry. They look determined. Confused. They hold a radio. You reach out through the signal. You try to warn them. But all they hear is breathing.",
        "reqs": lambda s: s["player"].get("deaths", 0) >= 3 and s["flags"].get("entity_awareness", 0) >= 90 and s["player"].get("sanity", 100) <= 5,
    },
    "collector": {
        "name": "THE COLLECTOR",
        "epilogue": "In the deepest part of the well, the entity pauses its eternal hunger. It considers your priorities. It finds the cat posters... oddly comforting. Some things transcend even cosmic horror.",
        "reqs": lambda s: s["flags"].get("cat_count", 0) >= 7,
    },
}

def check_endings(state: Dict) -> Optional[str]:
    for eid, ending in ENDINGS.items():
        try:
            if ending["reqs"](state):
                return eid
        except Exception:
            pass
    return None

def process_combat(state: Dict, monster_type: str, action: str) -> Dict:
    """Balanced combat — reduced player damage, tactical choices matter."""
    result = {
        "damage_dealt": 0, "damage_taken": 0,
        "sanity_lost": 0, "blood_splatter": False,
        "monster_killed": False, "monster_hp": 50,
        "special_effect": None,
    }
    p = state["player"]
    inv_ids = [i["id"] for i in p.get("inventory", [])]

    # Weapon bonuses
    has_disruptor = "signal_disruptor" in inv_ids
    has_fork = "tuning_fork" in inv_ids
    has_salt = "salt" in inv_ids

    # Monster HP tracking (approximated server-side)
    m_hp = state.setdefault("_combat", {}).setdefault(monster_type, random.randint(40, 60))

    if action == "attack":
        base = 15 + (12 if any(t in inv_ids for t in ["signal_disruptor", "tuning_fork"]) else 0)
        crit = 1.5 if random.random() < 0.18 else 1.0
        dmg = int(base * crit)
        m_hp = max(0, m_hp - dmg)
        result["damage_dealt"] = dmg

        # Counter-damage (balanced — 2-6 range)
        if m_hp > 0:
            cdmg = random.randint(2, 6)
            result["damage_taken"] = cdmg
            result["sanity_lost"] = random.randint(2, 5)
        result["blood_splatter"] = True

    elif action == "special":
        if has_disruptor and monster_type == "static_walker":
            m_hp = 0
            result["damage_dealt"] = 999
            result["special_effect"] = "signal_disruptor_kill"
        elif has_fork and monster_type == "the_remembered":
            m_hp = 0
            result["damage_dealt"] = 999
            result["special_effect"] = "tuning_fork_shatter"
            result["blood_splatter"] = True
        elif has_salt:
            m_hp = max(0, m_hp - 20)
            result["damage_dealt"] = 20
            result["special_effect"] = "salt_burn"
        else:
            dmg = random.randint(8, 14)
            m_hp = max(0, m_hp - dmg)
            result["damage_dealt"] = dmg
            cdmg = random.randint(3, 7)
            result["damage_taken"] = cdmg

    elif action == "item":
        # Use consumables
        pw_idx = next((i for i, x in enumerate(p.get("inventory", [])) if x["id"] == "purified_water"), None)
        sc_idx = next((i for i, x in enumerate(p.get("inventory", [])) if x["id"] == "salt_circle"), None)
        if pw_idx is not None:
            state = apply_sanity(state, 15, "purified_water")
            p["inventory"].pop(pw_idx)
            result["special_effect"] = "sanity_restored"
            # Still take light hit
            result["damage_taken"] = random.randint(1, 4)
        elif sc_idx is not None:
            m_hp = max(0, m_hp - 15)
            result["damage_dealt"] = 15
            result["special_effect"] = "salt_circle"
        else:
            result["damage_taken"] = random.randint(3, 6)
            result["special_effect"] = "no_item"

    elif action == "run":
        escape = random.random() < 0.45
        if escape:
            result["special_effect"] = "escaped"
        else:
            result["damage_taken"] = random.randint(3, 6)
            result["special_effect"] = "escape_failed"

    elif action == "take_hit":
        # Monster auto-attacked
        dmg_ranges = {"static_walker": (2, 6), "the_remembered": (3, 8), "well_spawn": (4, 10)}
        lo, hi = dmg_ranges.get(monster_type, (2, 5))
        cdmg = max(1, random.randint(lo, hi) - 3)  # defense reduction
        result["damage_taken"] = cdmg
        result["sanity_lost"] = random.randint(1, 4)

    # Apply results to state
    state = apply_health(state, -result["damage_taken"])
    state = apply_sanity(state, -result["sanity_lost"], "combat")

    state["_combat"][monster_type] = m_hp
    result["monster_hp"] = m_hp
    result["monster_killed"] = m_hp <= 0

    if result["monster_killed"]:
        del state["_combat"][monster_type]
        # Blood decal
        state["world"]["blood_decals"].append({
            "x": random.randint(0, 20), "y": random.randint(0, 15),
            "map": state["world"].get("current_map", "station_k7"),
            "size": random.uniform(1.0, 2.5), "type": "pool",
        })
        state["world"]["blood_decals"] = state["world"]["blood_decals"][-50:]
        p["memFragments"] = p.get("memFragments", 0) + 1

    return result, state

def process_dialogue(state: Dict, npc_id: str, step: int) -> Dict:
    lines = DIALOGUE_TREES.get(npc_id, [])
    if not lines:
        return {"text": "...", "sanity": 0}, state

    idx = min(step, len(lines) - 1)
    line = lines[idx]

    state = apply_sanity(state, line.get("sanity", 0), "dialogue")
    aw = line.get("awareness", 0)
    state["flags"]["entity_awareness"] = min(100, state["flags"].get("entity_awareness", 0) + aw)

    if line.get("journal"):
        state["player"]["journal"].append({
            "title": npc_id.upper(),
            "text": line["journal"],
            "ts": state["world"]["time"],
            "loc": state["world"].get("current_map", ""),
        })

    if "give_item" in line:
        inv = state["player"].get("inventory", [])
        if len(inv) < 12 and not any(i["id"] == line["give_item"]["id"] for i in inv):
            inv.append(line["give_item"])
            state["player"]["inventory"] = inv

    if line.get("unlock_quest"):
        qid = line["unlock_quest"]
        if qid in state["quests"]["main"]:
            state["quests"]["main"][qid]["active"] = True

    if line.get("complete"):
        npc_state = state["flags"]["npc"].setdefault(npc_id, {})
        npc_state["complete"] = True

    npc_state = state["flags"]["npc"].setdefault(npc_id, {})
    npc_state["met"] = True

    # Mimic warning at 50%
    if state["flags"]["entity_awareness"] >= 50 and not state["flags"].get("mimic_warned"):
        state["flags"]["mimic_warned"] = True
        state["world"]["npc_states"]["mimic"]["spawned"] = True

    return line, state

def process_lore(state: Dict, category: str, idx: int) -> Optional[str]:
    key = f"{category}_{idx}"
    if key in state["flags"].get("lore_discovered", []):
        return None
    fragments = LORE_FRAGMENTS.get(category, [])
    if idx >= len(fragments):
        return None
    text = fragments[idx]
    discovered = state["flags"].setdefault("lore_discovered", [])
    discovered.append(key)
    # Lore RESTORES sanity (learning empowers)
    state = apply_sanity(state, 4, "lore")
    state["flags"]["entity_awareness"] = min(100, state["flags"].get("entity_awareness", 0) + 3)
    state["player"]["journal"].append({
        "title": category.replace("_", " ").upper(),
        "text": text,
        "ts": state["world"]["time"],
        "loc": state["world"].get("current_map", ""),
    })
    return text

def get_random_lore(state: Dict, location: str = None) -> Optional[str]:
    all_keys = []
    for cat, frags in LORE_FRAGMENTS.items():
        for i in range(len(frags)):
            key = f"{cat}_{i}"
            if key not in state["flags"].get("lore_discovered", []):
                all_keys.append((cat, i))
    if not all_keys:
        return None
    cat, idx = random.choice(all_keys)
    return process_lore(state, cat, idx)

# ─────────────────────────────────────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/auth/register")
async def register(req: RegisterRequest):
    u = req.username.strip()
    if len(u) < 2:
        raise HTTPException(400, "Username too short")
    if len(req.password) < 4:
        raise HTTPException(400, "Password too short")
    if u in USERS:
        raise HTTPException(409, "Username taken")
    token = make_token(u)
    USERS[u] = {
        "pw": hash_password(req.password),
        "created": datetime.utcnow().isoformat(),
        "deaths": 0,
        "completions": [],
        "token": token,
        "is_admin": u == OWNER_ID,
    }
    return {
        "success": True,
        "username": u,
        "token": token,
        "is_admin": u == OWNER_ID,
        "message": "Operator registered. God help you.",
    }

@app.post("/auth/login")
async def login(req: LoginRequest):
    u = req.username.strip()
    user = USERS.get(u)
    if not user or user["pw"] != hash_password(req.password):
        raise HTTPException(401, "Invalid credentials")
    # Rotate token on login
    token = make_token(u)
    user["token"] = token
    return {
        "success": True,
        "username": u,
        "token": token,
        "is_admin": u == OWNER_ID,
        "deaths": user.get("deaths", 0),
        "completions": user.get("completions", []),
    }

@app.get("/auth/check/{username}")
async def check_user(username: str):
    return {"exists": username in USERS}

# ─────────────────────────────────────────────────────────────────────────────
# GAME ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "game": "XTRIALS",
        "version": "7.3.0",
        "status": "online",
        "sessions": len(SESSIONS),
        "operators": len(USERS),
    }

@app.get("/api/new_game/{username}")
async def new_game(username: str, token: str):
    user = USERS.get(username)
    if not user or user.get("token") != token:
        raise HTTPException(401, "Invalid token")

    session_id = str(uuid.uuid4())
    state = default_state(session_id, username)
    state["player"]["deaths"] = user.get("deaths", 0)
    SESSIONS[session_id] = state
    USER_SESSIONS[username] = session_id

    return {
        "session_id": session_id,
        "state": state,
        "message": "A new transmission has begun. God help you.",
    }

@app.post("/api/save")
async def save_game(req: SaveRequest):
    user = USERS.get(req.username)
    if not user:
        raise HTTPException(401, "Unknown operator")

    # Validate session ownership
    if req.session_id in SESSIONS and SESSIONS[req.session_id].get("username") != req.username:
        raise HTTPException(403, "Session belongs to another operator")

    SESSIONS[req.session_id] = {**req.state, "session_id": req.session_id, "username": req.username}
    SESSIONS[req.session_id]["last_saved"] = datetime.utcnow().isoformat()
    USER_SESSIONS[req.username] = req.session_id

    return {"success": True, "saved_at": SESSIONS[req.session_id]["last_saved"]}

@app.get("/api/load/{username}")
async def load_game(username: str, token: str):
    user = USERS.get(username)
    if not user or user.get("token") != token:
        raise HTTPException(401, "Invalid token")

    session_id = USER_SESSIONS.get(username)
    if not session_id or session_id not in SESSIONS:
        raise HTTPException(404, "No save found. The signal was lost.")

    state = SESSIONS[session_id]
    state = tick_clock(state, random.randint(1, 5))
    SESSIONS[session_id] = state

    return {"state": state, "session_id": session_id, "loaded": True}

@app.get("/api/state/{session_id}")
async def get_state(session_id: str):
    if session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")
    s = SESSIONS[session_id]
    return {
        "player": s["player"],
        "world": s["world"],
        "quests": s["quests"],
        "flags": s["flags"],
        "time": s["world"]["time"],
        "entity_awareness": s["flags"].get("entity_awareness", 0),
    }

@app.post("/api/action")
async def process_action(req: ActionRequest):
    if req.session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")

    state = SESSIONS[req.session_id]

    # Verify ownership
    if state.get("username") != req.username:
        raise HTTPException(403, "Not your session")

    result = {"success": True, "events": [], "state": None}
    state = tick_clock(state, 1)

    # ── COMBAT ──────────────────────────────────────────────────────────────
    if req.action_type == "combat":
        monster_type = req.payload.get("monster_type", "static_walker")
        action = req.payload.get("action", "attack")
        combat_result, state = process_combat(state, monster_type, action)
        result["combat"] = combat_result

        if state["player"]["health"] <= 0:
            result["events"].append("PLAYER_DEAD")
        if combat_result.get("monster_killed") and jumpscare_eligible(state):
            result["events"].append("JUMPSCARE_COMBAT")

    # ── DIALOGUE ─────────────────────────────────────────────────────────────
    elif req.action_type == "dialogue":
        npc_id = req.payload.get("npc_id", "")
        step = req.payload.get("step", 0)
        dialogue_result, state = process_dialogue(state, npc_id, step)
        result["dialogue"] = dialogue_result

        if state["flags"]["entity_awareness"] >= 50 and not state["flags"].get("mimic_warned"):
            result["events"].append("MIMIC_SPAWNED")

    # ── VHS TAPE ─────────────────────────────────────────────────────────────
    elif req.action_type == "vhs":
        tape_id = req.payload.get("tape_id", "1")
        tape = VHS_TAPES.get(tape_id, VHS_TAPES["1"])
        state = apply_sanity(state, -tape["sanity_hit"], "vhs")
        state["flags"]["entity_awareness"] = min(100, state["flags"].get("entity_awareness", 0) + tape.get("awareness", 5))
        watched = state["flags"].setdefault("vhs_watched", [])
        if tape_id not in watched:
            watched.append(tape_id)
            if tape.get("journal"):
                state["player"]["journal"].append({
                    "title": tape["title"],
                    "text": tape["journal"],
                    "ts": state["world"]["time"],
                    "loc": "vhs_player",
                })
        result["vhs"] = tape
        result["events"].append("VHS_WATCHED")
        ending = check_endings(state)
        if ending:
            result["events"].append(f"ENDING_{ending.upper()}")
            result["ending"] = ending

    # ── LORE / EXAMINE ───────────────────────────────────────────────────────
    elif req.action_type == "examine":
        item_id = req.payload.get("item_id", "")
        if "mirror" in item_id.lower() and random.random() < 0.3:
            state = apply_sanity(state, -15, "mirror")
            result["events"].append("JUMPSCARE_MIRROR")
        elif "journal" in item_id.lower() or "note" in item_id.lower():
            text = get_random_lore(state, state["world"].get("current_map"))
            result["lore"] = text
        elif jumpscare_eligible(state) and random.random() < 0.15:
            result["events"].append("JUMPSCARE_EXAMINE")

    # ── ITEM PICKUP ───────────────────────────────────────────────────────────
    elif req.action_type == "pick_up":
        item = req.payload.get("item", {})
        item_id = item.get("id", "")
        inv = state["player"].get("inventory", [])

        cursed = ["the_hand", "black_phone", "music_box"]
        if item_id in cursed and jumpscare_eligible(state):
            state = apply_sanity(state, -20, "cursed")
            result["events"].append("JUMPSCARE_CURSED")

        if len(inv) < 12:
            inv.append(item)
            state["player"]["inventory"] = inv
            result["item_added"] = item

            if item_id.startswith("cat_"):
                state["flags"]["cat_count"] = state["flags"].get("cat_count", 0) + 1
                if state["flags"]["cat_count"] >= 7:
                    result["events"].append("ENDING_COLLECTOR")

            # Lore on pickup
            text = get_random_lore(state, state["world"].get("current_map"))
            if text:
                result["lore"] = text
        else:
            result["success"] = False
            result["message"] = "Inventory full"

    # ── USE ITEM ──────────────────────────────────────────────────────────────
    elif req.action_type == "use_item":
        item_id = req.payload.get("item_id", "")
        inv = state["player"].get("inventory", [])

        if item_id == "purified_water":
            state = apply_sanity(state, 15, "purified_water")
            state["player"]["inventory"] = [i for i in inv if i["id"] != item_id]
            result["special"] = "sanity_restored"

        elif item_id == "radio":
            freq = state["world"].get("radio_frequency", 87.6)
            aw = state["flags"].get("entity_awareness", 0)
            result["radio"] = {
                "frequency": freq,
                "signal": "breathing" if aw > 30 else "static",
                "monster_nearby": random.random() < (aw / 100),
            }

        elif item_id == "camera":
            anomalies = ["static_figure", "floating_orb", "blood_writing", "impossible_geometry", "reflected_face"]
            if random.random() < 0.4:
                anom = random.choice(anomalies)
                captured = state["quests"]["side"]["anomaly_photos"].setdefault("captured", [])
                if anom not in captured:
                    captured.append(anom)
                result["photo"] = {"anomaly": anom}
                result["events"].append("ANOMALY_PHOTOGRAPHED")

    # ── PUZZLE ───────────────────────────────────────────────────────────────
    elif req.action_type == "puzzle":
        puzzle_id = req.payload.get("puzzle_id", "")
        answer = req.payload.get("answer")

        if puzzle_id == "radio_tuning":
            target = 87.6
            val = float(req.payload.get("value", 87.0))
            if abs(val - target) <= 0.15:
                state["flags"]["radio_tuned"] = True
                state["quests"]["side"]["radio_puzzle"]["complete"] = True
                result["puzzle_solved"] = True
                result["events"].append("RADIO_TUNED")
                get_random_lore(state)
            else:
                result["puzzle_solved"] = False

        elif puzzle_id == "rune_sequence":
            # Correct: WR TH AZ EH
            correct = ["WR", "TH", "AZ", "EH"]
            attempt = req.payload.get("sequence", [])
            if attempt == correct:
                state["flags"]["true_name_known"] = True
                state["flags"]["entity_awareness"] = min(100, state["flags"].get("entity_awareness", 0) + 30)
                state["quests"]["side"]["rune_puzzle"]["complete"] = True
                result["puzzle_solved"] = True
                result["events"].append("TRUE_NAME_KNOWN")
                ending = check_endings(state)
                if ending:
                    result["ending"] = ending
            else:
                state = apply_sanity(state, -18, "wrong_runes")
                result["puzzle_solved"] = False
                result["events"].append("JUMPSCARE_RUNES")

    # ── CRAFTING ──────────────────────────────────────────────────────────────
    elif req.action_type == "craft":
        recipe_id = req.payload.get("recipe_id", "")
        recipe = CRAFTING_RECIPES.get(recipe_id)
        if not recipe:
            result["success"] = False
            result["message"] = "Unknown recipe"
        else:
            inv = state["player"].get("inventory", [])
            inv_ids = [i["id"] for i in inv]
            has_all = all(n in inv_ids for n in recipe["needs"])
            if has_all:
                for need in recipe["needs"]:
                    idx = next((i for i, x in enumerate(inv) if x["id"] == need), None)
                    if idx is not None:
                        inv.pop(idx)
                new_item = {
                    "id": recipe_id,
                    "name": recipe["name"],
                    "icon": recipe["icon"],
                    "type": recipe["type"],
                }
                inv.append(new_item)
                state["player"]["inventory"] = inv
                crafted = state["quests"]["side"]["crafting"].setdefault("items_crafted", [])
                if recipe_id not in crafted:
                    crafted.append(recipe_id)
                result["crafted"] = new_item
                result["events"].append("ITEM_CRAFTED")
            else:
                result["success"] = False
                result["message"] = f"Missing: {', '.join(n for n in recipe['needs'] if n not in inv_ids)}"

    # ── MAP MOVE ──────────────────────────────────────────────────────────────
    elif req.action_type == "move":
        new_map = req.payload.get("map")
        pos = req.payload.get("position", {})
        if new_map:
            state["world"]["current_map"] = new_map
            if new_map not in state["world"].get("maps", []):
                state["world"]["maps"].append(new_map)
        if pos:
            state["player"]["position"] = pos
        if state["flags"].get("entity_awareness", 0) > 20 and jumpscare_eligible(state) and random.random() < 0.2:
            result["events"].append("JUMPSCARE_DOOR")
        # Random lore on map enter
        if new_map and random.random() < 0.3:
            get_random_lore(state, new_map)

    # ── SANITY EFFECTS ────────────────────────────────────────────────────────
    san = state["player"]["sanity"]
    if san < 25 and random.random() < 0.08:
        effects = ["WALLS_BLEED", "FAKE_MONSTER", "AUDIO_HALLUCINATION", "TEXT_DISTORT"]
        result["events"].append(random.choice(effects))
    if san < 10 and random.random() < 0.05:
        result["events"].append("SEVERE_HALLUCINATION")

    # Check endings after every action
    ending = check_endings(state)
    if ending and state["flags"].get("ending") != ending:
        state["flags"]["ending"] = ending
        result["events"].append(f"ENDING_{ending.upper()}")
        result["ending"] = ending

    SESSIONS[req.session_id] = state
    result["state"] = {
        "player": state["player"],
        "flags": state["flags"],
        "quests": state["quests"],
        "world_time": state["world"]["time"],
        "entity_awareness": state["flags"].get("entity_awareness", 0),
    }

    # Broadcast to WebSocket
    if req.session_id in CONNECTIONS:
        try:
            await CONNECTIONS[req.session_id].send_json({
                "type": "state_update",
                "events": result["events"],
                "sanity": state["player"]["sanity"],
                "health": state["player"]["health"],
                "entity_awareness": state["flags"].get("entity_awareness", 0),
            })
        except Exception:
            pass

    return result

@app.post("/api/death")
async def handle_death(req: DeathRequest):
    if req.session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")

    state = SESSIONS[req.session_id]
    if state.get("username") != req.username:
        raise HTTPException(403, "Not your session")

    stats = {
        "username": req.username,
        "cause": req.cause,
        "sanity_at_death": state["player"].get("sanity", 0),
        "health_at_death": state["player"].get("health", 0),
        "maps_explored": len(state["world"].get("maps", [])),
        "mem_fragments": state["player"].get("memFragments", 0),
        "lore_found": len(state["flags"].get("lore_discovered", [])),
        "vhs_watched": len(state["flags"].get("vhs_watched", [])),
    }

    # Update user death count
    user = USERS.get(req.username)
    if user:
        user["deaths"] = user.get("deaths", 0) + 1

    # Broadcast death to WebSocket
    if req.session_id in CONNECTIONS:
        try:
            await CONNECTIONS[req.session_id].send_json({
                "type": "permadeath",
                "stats": stats,
                "epitaph": EPITAPHS.get(req.cause, EPITAPHS["unknown"]),
            })
        except Exception:
            pass

    # Delete save (permadeath)
    del SESSIONS[req.session_id]
    if req.username in USER_SESSIONS and USER_SESSIONS[req.username] == req.session_id:
        del USER_SESSIONS[req.username]
    if req.session_id in CONNECTIONS:
        del CONNECTIONS[req.session_id]

    return {
        "deleted": True,
        "epitaph": EPITAPHS.get(req.cause, EPITAPHS["unknown"]),
        "stats": stats,
    }

@app.post("/api/quest")
async def update_quest(req: QuestRequest):
    if req.session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")
    state = SESSIONS[req.session_id]
    if state.get("username") != req.username:
        raise HTTPException(403, "Not your session")

    qt = state["quests"].get(req.quest_type, {})
    if req.quest_id in qt:
        qt[req.quest_id].update(req.data)

    ending = check_endings(state)
    SESSIONS[req.session_id] = state
    return {"success": True, "quests": state["quests"], "ending": ending}

# ─────────────────────────────────────────────────────────────────────────────
# CHAT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/chat/send")
async def chat_send(req: ChatRequest):
    user = USERS.get(req.username)
    if not user or user.get("token") != req.token:
        raise HTTPException(401, "Invalid token")

    msg = {
        "id": f"{time.time()}_{uuid.uuid4().hex[:8]}",
        "username": req.username,
        "is_admin": req.username == OWNER_ID,
        "time": datetime.utcnow().strftime("%H:%M"),
        "text": req.text[:500],
        "msg_type": req.msg_type,
        "file_data": req.file_data if req.msg_type in ("image", "voice") else "",
        "gif_url": req.gif_url if req.msg_type == "gif" else "",
    }

    CHAT_MESSAGES.append(msg)
    if len(CHAT_MESSAGES) > 500:
        CHAT_MESSAGES.pop(0)

    # Broadcast to all connected WebSocket sessions
    dead = []
    for sid, ws in list(CONNECTIONS.items()):
        try:
            await ws.send_json({"type": "chat_message", "message": msg})
        except Exception:
            dead.append(sid)
    for sid in dead:
        CONNECTIONS.pop(sid, None)

    return {"success": True, "message": msg}

@app.get("/chat/history")
async def chat_history(limit: int = 100):
    return {"messages": CHAT_MESSAGES[-limit:]}

@app.post("/chat/system")
async def chat_system(text: str, secret: str = ""):
    """Internal system message endpoint."""
    if secret != "xtrials_internal_7":
        raise HTTPException(403, "Forbidden")
    msg = {
        "id": f"{time.time()}_sys",
        "username": "SYSTEM",
        "is_admin": False,
        "time": datetime.utcnow().strftime("%H:%M"),
        "text": text,
        "msg_type": "system",
        "file_data": "",
        "gif_url": "",
    }
    CHAT_MESSAGES.append(msg)
    for sid, ws in list(CONNECTIONS.items()):
        try:
            await ws.send_json({"type": "chat_message", "message": msg})
        except Exception:
            pass
    return {"success": True}

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin/sessions")
async def admin_sessions(admin_username: str, token: str):
    user = USERS.get(admin_username)
    if not user or user.get("token") != token or admin_username != OWNER_ID:
        raise HTTPException(403, "Admin access required")

    sessions_summary = []
    for sid, state in SESSIONS.items():
        sessions_summary.append({
            "session_id": sid,
            "username": state.get("username", "unknown"),
            "map": state["world"].get("current_map", "?"),
            "health": state["player"].get("health", 0),
            "sanity": state["player"].get("sanity", 0),
            "awareness": state["flags"].get("entity_awareness", 0),
            "time": state["world"].get("time", "?"),
            "connected_ws": sid in CONNECTIONS,
        })

    return {
        "sessions": sessions_summary,
        "total_users": len(USERS),
        "active_sessions": len(SESSIONS),
        "ws_connections": len(CONNECTIONS),
        "chat_messages": len(CHAT_MESSAGES),
    }

@app.post("/admin/action")
async def admin_action(req: AdminActionRequest):
    admin_user = USERS.get(req.admin_username)
    if not admin_user or admin_user.get("token") != req.token or req.admin_username != OWNER_ID:
        raise HTTPException(403, "Admin access required")

    if req.target_session not in SESSIONS:
        raise HTTPException(404, "Target session not found")

    state = SESSIONS[req.target_session]
    target_user = state.get("username", "unknown")
    result = {"action": req.action, "target": target_user, "success": True}
    ws_msg = None

    if req.action == "jumpscare":
        scare = req.payload.get("type", "mirror_reflection")
        state = apply_sanity(state, -12, "admin_scare")
        ws_msg = {
            "type": "jumpscare",
            "scare_type": scare,
            "source": "[ADMIN]",
            "intensity": req.payload.get("intensity", "medium"),
        }
        result["message"] = f"Triggered {scare} on {target_user}"

    elif req.action == "heal":
        state = apply_health(state, 50)
        state = apply_sanity(state, 25, "admin_heal")
        ws_msg = {
            "type": "state_update",
            "events": ["ADMIN_HEAL"],
            "sanity": state["player"]["sanity"],
            "health": state["player"]["health"],
            "entity_awareness": state["flags"].get("entity_awareness", 0),
        }
        result["message"] = f"Healed {target_user}"

    elif req.action == "drain":
        amount = req.payload.get("amount", 25)
        state = apply_sanity(state, -amount, "admin_drain")
        ws_msg = {
            "type": "state_update",
            "events": ["ADMIN_DRAIN"],
            "sanity": state["player"]["sanity"],
            "health": state["player"]["health"],
            "entity_awareness": state["flags"].get("entity_awareness", 0),
        }
        result["message"] = f"Drained {amount} sanity from {target_user}"

    elif req.action == "spawn_monster":
        ws_msg = {
            "type": "spawn_monster",
            "monster_type": req.payload.get("type", "static_walker"),
            "source": "[ADMIN]",
        }
        result["message"] = f"Spawned monster near {target_user}"

    elif req.action == "give_item":
        item = req.payload.get("item", {"id": "flashlight", "name": "Flashlight", "icon": "[FL]", "type": "tool"})
        inv = state["player"].get("inventory", [])
        if len(inv) < 12:
            inv.append(item)
            state["player"]["inventory"] = inv
        ws_msg = {
            "type": "state_update",
            "events": ["ADMIN_ITEM"],
            "sanity": state["player"]["sanity"],
            "health": state["player"]["health"],
            "entity_awareness": state["flags"].get("entity_awareness", 0),
        }
        result["message"] = f"Gave {item.get('name')} to {target_user}"

    elif req.action == "message":
        text = req.payload.get("text", "The entity watches.")
        ws_msg = {
            "type": "admin_message",
            "text": text,
            "source": "[ENTITY BROADCAST — ADMIN VISIBLE]",
        }
        # Also post to chat so it's transparent
        chat_msg = {
            "id": f"{time.time()}_admin",
            "username": "ADMIN",
            "is_admin": True,
            "time": datetime.utcnow().strftime("%H:%M"),
            "text": f"[BROADCAST TO {target_user}]: {text}",
            "msg_type": "system",
            "file_data": "",
            "gif_url": "",
        }
        CHAT_MESSAGES.append(chat_msg)
        result["message"] = f"Message sent to {target_user}"

    elif req.action == "set_awareness":
        val = int(req.payload.get("value", 50))
        state["flags"]["entity_awareness"] = min(100, max(0, val))
        ws_msg = {
            "type": "state_update",
            "events": [],
            "sanity": state["player"]["sanity"],
            "health": state["player"]["health"],
            "entity_awareness": state["flags"]["entity_awareness"],
        }
        result["message"] = f"Set awareness to {val} for {target_user}"

    SESSIONS[req.target_session] = state

    if ws_msg and req.target_session in CONNECTIONS:
        try:
            await CONNECTIONS[req.target_session].send_json(ws_msg)
        except Exception:
            result["ws_delivered"] = False
    
    result["ws_delivered"] = req.target_session in CONNECTIONS
    return result

@app.get("/admin/users")
async def admin_users(admin_username: str, token: str):
    user = USERS.get(admin_username)
    if not user or user.get("token") != token or admin_username != OWNER_ID:
        raise HTTPException(403, "Admin access required")

    return {
        "users": [
            {
                "username": u,
                "deaths": data.get("deaths", 0),
                "completions": data.get("completions", []),
                "created": data.get("created"),
                "has_active_session": u in USER_SESSIONS,
            }
            for u, data in USERS.items()
        ]
    }

# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/game/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    CONNECTIONS[session_id] = websocket

    await websocket.send_json({
        "type": "connected",
        "message": "Signal established. Protocol 7.3 active.",
        "session_id": session_id,
    })

    # Send full chat history on connect
    await websocket.send_json({
        "type": "chat_history",
        "messages": CHAT_MESSAGES[-50:],
    })

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
                msg_type = data.get("type", "")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong", "time": datetime.utcnow().isoformat()})

                elif msg_type == "report_idle":
                    state = SESSIONS.get(session_id)
                    if state:
                        idle_sec = data.get("seconds", 0)
                        if idle_sec > 12 and jumpscare_eligible(state):
                            state = apply_sanity(state, -5, "idle_darkness")
                            SESSIONS[session_id] = state
                            await websocket.send_json({
                                "type": "jumpscare",
                                "scare_type": "idle_darkness",
                                "audio": "whisper_then_loud",
                            })

                elif msg_type == "blood_sync":
                    state = SESSIONS.get(session_id)
                    if state:
                        decals = data.get("decals", [])
                        state["world"]["blood_decals"] = decals[-50:]
                        SESSIONS[session_id] = state
                        await websocket.send_json({"type": "blood_ack", "count": len(decals)})

                elif msg_type == "monster_sync":
                    state = SESSIONS.get(session_id)
                    if state:
                        mod = awareness_modifier(state)
                        await websocket.send_json({
                            "type": "monster_state",
                            "spawn_rate_modifier": mod,
                            "mimic_active": state["world"]["npc_states"]["mimic"]["spawned"],
                            "time": state["world"]["time"],
                            "entity_awareness": state["flags"].get("entity_awareness", 0),
                        })

                elif msg_type == "chat":
                    # Handle chat over WS
                    username = data.get("username", "UNKNOWN")
                    token = data.get("token", "")
                    user = USERS.get(username)
                    if user and user.get("token") == token:
                        msg = {
                            "id": f"{time.time()}_{uuid.uuid4().hex[:8]}",
                            "username": username,
                            "is_admin": username == OWNER_ID,
                            "time": datetime.utcnow().strftime("%H:%M"),
                            "text": data.get("text", "")[:500],
                            "msg_type": data.get("msg_type", "text"),
                            "file_data": data.get("file_data", ""),
                            "gif_url": data.get("gif_url", ""),
                        }
                        CHAT_MESSAGES.append(msg)
                        if len(CHAT_MESSAGES) > 500:
                            CHAT_MESSAGES.pop(0)
                        # Broadcast to all
                        dead = []
                        for sid, ws in list(CONNECTIONS.items()):
                            try:
                                await ws.send_json({"type": "chat_message", "message": msg})
                            except Exception:
                                dead.append(sid)
                        for sid in dead:
                            CONNECTIONS.pop(sid, None)

                elif msg_type == "fav_chat":
                    # Just acknowledge — favs are client-side
                    await websocket.send_json({"type": "fav_ack", "msg_id": data.get("msg_id")})

            except asyncio.TimeoutError:
                # Periodic ambient events
                state = SESSIONS.get(session_id)
                if state:
                    aw = state["flags"].get("entity_awareness", 0)
                    # Ambient events scale with awareness
                    event_chance = 0.04 + (aw / 100) * 0.08
                    if random.random() < event_chance:
                        events = [
                            "distant_scream", "radio_burst", "flicker",
                            "footsteps", "door_creak", "breathing", "static_surge"
                        ]
                        drain = 2 if state["player"]["sanity"] < 50 else 0
                        await websocket.send_json({
                            "type": "ambient_event",
                            "event": random.choice(events),
                            "sanity_drain": drain,
                        })
                        if drain:
                            state = apply_sanity(state, -drain, "ambient")
                            SESSIONS[session_id] = state

                    # Random jumpscare at high awareness
                    if aw >= 70 and jumpscare_eligible(state) and random.random() < 0.02:
                        SESSIONS[session_id] = state
                        scares = ["mirror_reflection", "idle_darkness", "door_entity"]
                        await websocket.send_json({
                            "type": "jumpscare",
                            "scare_type": random.choice(scares),
                        })

                await websocket.send_json({"type": "heartbeat"})

    except WebSocketDisconnect:
        CONNECTIONS.pop(session_id, None)

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH & STATS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "online",
        "game": "XTRIALS",
        "version": "7.3.0",
        "sessions": len(SESSIONS),
        "operators": len(USERS),
        "ws_connections": len(CONNECTIONS),
        "chat_messages": len(CHAT_MESSAGES),
        "uptime": "active",
    }

@app.get("/leaderboard")
async def leaderboard():
    """Public leaderboard — sorted by deaths desc."""
    board = [
        {
            "rank": i + 1,
            "username": u,
            "deaths": data.get("deaths", 0),
            "completions": len(data.get("completions", [])),
        }
        for i, (u, data) in enumerate(
            sorted(USERS.items(), key=lambda x: x[1].get("deaths", 0), reverse=True)[:20]
        )
    ]
    return {"leaderboard": board}
