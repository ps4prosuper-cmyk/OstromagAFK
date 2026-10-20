"""Telegram RPG AFK runner. One bounded execution per GitHub Actions invocation."""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

LOG = logging.getLogger("afk")
STATE_PATH = Path("state.json")
UTC = timezone.utc
MAX_ACTIONS = 18
MAX_SECONDS = 160
ACTION_DELAY = 3
WAIT_RESPONSE = 12

HP_RE = re.compile(r"(?:👤\s*)?Ви\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)", re.I)
ENERGY_RE = re.compile(r"(?:у вас\s*)?(\d+)\s*/\s*(\d+)\s*очок\s+енерг", re.I)
MINUTES_RE = re.compile(r"через\s+(\d+)\s*хв", re.I)
ENEMY_HP_RE = re.compile(r"(?:🐍|🦅|🐺|👹|🧟|🐉|🦇|🕷|🦂|🐻|🐗|💀)?[^\n()]{2,65}\(\s*(\d+)\s*/\s*(\d+)\s*\)")
BATTLE_WORDS = ("параметри ворога", "vs ⚔", "раунд ", "з'явився", "з’явився")
ENERGY_WORDS = ("недостатньо енергі", "не вистачає енергі")
DEATH_WORDS = ("ви загинули", "ви померли", "ваша смерть", "моя смерть")
VICTORY_WORDS = ("смерть ворога", "ви перемогли", "ворога переможено")


def now():
    return datetime.now(UTC)


def parse_hp(text):
    match = HP_RE.search(text or "")
    return (int(match[1]), int(match[2])) if match else None


def parse_energy_delay(text):
    text = (text or "").lower()
    if not any(s in text for s in ENERGY_WORDS):
        return None
    m = MINUTES_RE.search(text)
    return max(1, int(m[1])) if m else 5


def classify(text, buttons=()):
    t = (text or "").lower()
    hp = parse_hp(text)
    if hp and hp[0] == 0:
        return "death", hp
    if any(w in t for w in DEATH_WORDS):
        return "death", hp
    if parse_energy_delay(text) is not None:
        return "energy", hp
    if any(w in t for w in VICTORY_WORDS):
        return "victory", hp
    # For combat logs, enemy's HP=0 signals victory even if wording changes.
    if "раунд " in t and "vs" in t:
        matches = ENEMY_HP_RE.findall(text or "")
        if matches and int(matches[-1][0]) == 0:
            return "victory", hp
    if any(w in t for w in BATTLE_WORDS) or (
        any("атака" in normalize(b) for b in buttons) and "vs" in t
    ):
        return "combat", hp
    return "other", hp


def normalize(text):
    return unicodedata.normalize("NFKC", text or "").lower().strip()


def keyboard_buttons(msg):
    return [b.text for row in (msg.buttons or []) for b in row]


def button_match(msg, term):
    for row in (msg.buttons or []):
        for b in row:
            if term in normalize(b.text):
                return b.text
    return None


def read_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"next_action_at": None, "reason": "", "last_hp": None}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def pause(state, minutes, reason, hp=None):
    state.update(next_action_at=(now() + timedelta(minutes=minutes)).isoformat(),
                 reason=reason, last_hp=hp)
    save_state(state)
    LOG.info("Pause %s minutes: %s", minutes, reason)


def clear_pause(state):
    state.update(next_action_at=None, reason="")
    save_state(state)


def fingerprint(messages):
    return tuple((m.id, m.edit_date.isoformat() if m.edit_date else "",
                  m.raw_text or "", tuple(keyboard_buttons(m))) for m in messages[:5])


async def fetch(client, bot):
    # Newest first, includes incoming and our outgoing commands.
    return await client.get_messages(bot, limit=18)


async def wait_for_change(client, bot, old_fp):
    until = time.monotonic() + WAIT_RESPONSE
    while time.monotonic() < until:
        await asyncio.sleep(1.5)
        messages = await fetch(client, bot)
        if fingerprint(messages) != old_fp:
            # Telegram sometimes sends the combat result as a second message.
            await asyncio.sleep(1)
            return await fetch(client, bot)
    return None


async def click_or_send(client, bot, msg, label):
    # Message.click handles both inline and reply-keyboard buttons.
    # Fail closed: do not send a guessed text command on callback failure.
    await msg.click(text=label)


async def run():
    required = ["TG_API_ID", "TG_API_HASH", "TG_SESSION", "GAME_BOT"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("Missing GitHub Secrets/Variables: " + ", ".join(missing))
    bot = os.environ["GAME_BOT"].lstrip("@")
    state = read_state()
    if state.get("next_action_at"):
        deadline = datetime.fromisoformat(state["next_action_at"])
        if now() < deadline:
            LOG.info("Cooldown active until %s (%s)", deadline, state.get("reason"))
            return
        clear_pause(state)

    client = TelegramClient(StringSession(os.environ["TG_SESSION"]),
                            int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session expired. Generate a new TG_SESSION.")
        bot_entity = await client.get_entity(bot)
        start = time.monotonic()
        last_command = None
        for action in range(MAX_ACTIONS):
            if time.monotonic() - start > MAX_SECONDS:
                LOG.info("Time budget reached")
                break
            messages = await fetch(client, bot_entity)
            if not messages:
                LOG.warning("No messages yet. Open the game bot and send /start manually.")
                break
            # Analyze the latest *incoming* result, not our outgoing button text.
            incoming = [m for m in messages if not m.out]
            if not incoming:
                LOG.warning("No incoming messages from game")
                break
            current = incoming[0]
            status, hp = classify(current.raw_text, keyboard_buttons(current))
            LOG.info("Action %s: %s, HP=%s, message=%s", action + 1, status, hp, current.id)

            if status == "death":
                pause(state, 10, "death", hp[0] if hp else 0)
                break
            if status == "energy":
                pause(state, parse_energy_delay(current.raw_text), "energy", hp[0] if hp else None)
                break
            if status == "victory":
                if hp and 0 < hp[0] < 120:
                    pause(state, 5, "low_hp", hp[0])
                    break
            # If a new noncombat message lacks HP, do not assume a prior HP is still valid.
            # For a battle, prefer an attack keyboard attached to the newest relevant message.
            combat = status == "combat"
            keyboard_msgs = [m for m in messages if m.buttons]
            chosen = None
            label = None
            if combat:
                for m in keyboard_msgs:
                    label = button_match(m, "атака")
                    if label:
                        chosen = m
                        break
                if chosen is None:
                    LOG.warning("Combat detected but attack button missing; stopping safely")
                    break
            else:
                for m in keyboard_msgs:
                    label = button_match(m, "досліджувати")
                    if label:
                        chosen = m
                        break
                if chosen is None:
                    LOG.warning("Explore button missing; stopping safely")
                    break

            # Never repeat an action when a prior command produced no new response.
            command_key = (current.id, current.edit_date, status, label)
            if command_key == last_command:
                LOG.warning("Unchanged response; not repeating the command")
                break
            last_command = command_key
            old_fp = fingerprint(messages)
            LOG.info("Click: %s", label)
            await click_or_send(client, bot_entity, chosen, label)
            await asyncio.sleep(ACTION_DELAY)
            changed = await wait_for_change(client, bot_entity, old_fp)
            if changed is None:
                LOG.warning("No new bot response; stopping to avoid duplicate actions")
                break
        else:
            LOG.info("Maximum actions reached")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run())
    except FloodWaitError as exc:
        logging.error("Telegram rate limit: wait %s seconds", exc.seconds)
        state = read_state()
        pause(state, max(5, (exc.seconds + 59) // 60), "telegram_flood_wait")
    except RPCError:
        LOG.exception("Telegram API error; stopping without retries")
        raise
