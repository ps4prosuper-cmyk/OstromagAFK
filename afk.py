"""Ostromag RPG AFK automation; scheduled, bounded Telethon execution.

Designed for the reply keyboard visible in @ostromag_game_bot. No secrets
are stored in this file. GitHub Actions provides credentials as environment variables.
"""

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

LOG = logging.getLogger("ostromag_afk")
STATE_PATH = Path("state.json")
MAX_ACTIONS = 18
MAX_RUNTIME_SECONDS = 160
ACTION_DELAY_SECONDS = 2
RESPONSE_TIMEOUT_SECONDS = 18

# These are REAL reply-keyboard texts from the screenshots. Their purpose is
# only to replace a missing historic reply-keyboard message, never a callback.
EXPLORE_FALLBACK = "🗺 Досліджувати (⚡ 1)"
ATTACK_FALLBACK = "⚔️ Атака"

HP_RE = re.compile(r"\bВи\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)", re.I)
MINUTES_RE = re.compile(r"через\s+(\d+)\s*хв", re.I)
ENERGY_WORDS = ("недостатньо енергі", "не вистачає енергі")
DEATH_WORDS = ("ви загинули", "ви померли", "ваша смерть", "моя смерть")
WIN_WORDS = ("смерть ворога", "ви перемогли", "ворога переможено")
BATTLE_WORDS = (
    "параметри ворога", "vs ⚔", "раунд ", "з'явився", "з’явився",
    # The game sometimes offers a special move during combat.
    # The user wants to IGNORE it and keep pressing ordinary Attack.
    "прийом", "прийоми",
)


def utc_now():
    return datetime.now(timezone.utc)


def normalize(value):
    return unicodedata.normalize("NFKC", value or "").lower().strip()


def get_hp(text):
    found = HP_RE.search(text or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


def get_energy_wait(text):
    value = normalize(text)
    if not any(word in value for word in ENERGY_WORDS):
        return None
    found = MINUTES_RE.search(value)
    return max(1, int(found.group(1))) if found else 5


def classify(text):
    value = normalize(text)
    hp = get_hp(text)
    # Death must win over a simultaneous kill of the enemy.
    if (hp and hp[0] == 0) or any(word in value for word in DEATH_WORDS):
        return "death", hp
    if get_energy_wait(text) is not None:
        return "energy", hp
    if any(word in value for word in WIN_WORDS):
        return "victory", hp
    # A combat-round result that shows the opponent with 0 HP is also a win.
    if "раунд " in value and "vs" in value:
        # Look only at HP not preceded by "Ви"; in normal reports the
        # character's HP comes first and the enemy's HP comes second.
        all_hp = re.findall(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)", text or "")
        if len(all_hp) >= 2 and int(all_hp[1][0]) == 0:
            return "victory", hp
    if any(word in value for word in BATTLE_WORDS):
        return "combat", hp
    return "other", hp


def read_state():
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {"next_action_at": None, "reason": "", "last_hp": None,
            "combat_active": False}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def pause(state, minutes, reason, message_id=None, hp=None):
    state["next_action_at"] = (utc_now() + timedelta(minutes=minutes)).isoformat()
    state["reason"] = reason
    state["last_hp"] = hp
    state["pause_message_id"] = message_id
    save_state(state)
    LOG.info("Pause: %s minute(s), reason=%s", minutes, reason)


def keyboard_texts(message):
    return [button.text for row in (message.buttons or []) for button in row]


def find_button(messages, fragment):
    # Newest keyboard containing the desired option, if available.
    for message in messages:
        for text in keyboard_texts(message):
            if fragment in normalize(text):
                return message, text
    return None, None


def reply_text_from_history(messages, fragment, fallback):
    # An earlier user click on a Telegram reply keyboard is an outgoing message.
    for message in messages:
        if message.out and fragment in normalize(message.raw_text):
            return message.raw_text
    return fallback


def incoming_signature(messages):
    """Ignore outgoing messages: sending a command is NOT a bot response."""
    return tuple(
        (message.id,
         str(message.edit_date) if message.edit_date else "",
         message.raw_text or "",
         tuple(keyboard_texts(message)))
        for message in messages if not message.out
    )


async def messages_from_bot(client, bot):
    return await client.get_messages(bot, limit=35)


async def wait_for_bot_response(client, bot, signature):
    deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        messages = await messages_from_bot(client, bot)
        if incoming_signature(messages) != signature:
            # Allow multiple bot messages from one game action to arrive.
            await asyncio.sleep(1)
            return True
    return False


async def take_action(client, bot, messages, fragment, fallback):
    reply_msg, reply_label = find_button(messages, fragment)
    if reply_msg:
        LOG.info("Button: %s", reply_label)
        await reply_msg.click(text=reply_label)
        return reply_label

    # The active Telegram REPLY keyboard can persist on screen even after
    # its message disappears from the 35 most recent messages. For this game,
    # reply-keyboard buttons submit plain messages, so use the exact text of
    # a previous human click if possible; otherwise use the screenshot text.
    label = reply_text_from_history(messages, fragment, fallback)
    LOG.info("Reply keyboard not in history; sending text: %s", label)
    await client.send_message(bot, label)
    return label


async def main():
    needed = ("TG_API_ID", "TG_API_HASH", "TG_SESSION", "GAME_BOT")
    missing = [name for name in needed if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing Secrets / variables: " + ", ".join(missing))

    state = read_state()
    old_pause_message_id = None
    if state.get("next_action_at"):
        end = datetime.fromisoformat(state["next_action_at"])
        if utc_now() < end:
            LOG.info("Waiting until %s (%s)", end, state.get("reason"))
            return
        # The bot's old "energy=0" or "player died" message might still
        # be the newest message after the pause. Do not pause again on it.
        old_pause_message_id = state.get("pause_message_id")
        state["next_action_at"] = None
        state["reason"] = ""
        save_state(state)

    game_bot = os.environ["GAME_BOT"].lstrip("@")
    client = TelegramClient(
        StringSession(os.environ["TG_SESSION"]),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Expired session: create a new TG_SESSION locally")
        bot = await client.get_entity(game_bot)
        start = time.monotonic()
        last_action_signature = None

        for index in range(MAX_ACTIONS):
            if time.monotonic() - start >= MAX_RUNTIME_SECONDS:
                LOG.info("Run time budget reached")
                break

            messages = await messages_from_bot(client, bot)
            latest = next((msg for msg in messages if not msg.out), None)
            if latest is None:
                LOG.warning("No incoming messages: open and start the game manually")
                break

            status, hp = classify(latest.raw_text)
            # Some combat prompts contain only a "Прийом" button rather than
            # combat text; never press it or go back to exploring in that case.
            current_buttons = [normalize(text) for text in keyboard_texts(latest)]
            if status == "other" and any("прийом" in text for text in current_buttons):
                status = "combat"

            # Ignore a stale pause-trigger message after its wait has finished.
            if latest.id == old_pause_message_id and status in ("death", "energy", "victory"):
                LOG.info("Ignoring stale result after completed cooldown")
                status = "other"
            old_pause_message_id = None

            # Fight mode survives bot messages that only show a special move,
            # a combat notification, or some other unrelated intermediate text.
            # It also survives separate GitHub Actions runs via state.json.
            if status == "combat":
                if not state.get("combat_active", False):
                    state["combat_active"] = True
                    save_state(state)
            elif status in ("victory", "death"):
                if state.get("combat_active", False):
                    state["combat_active"] = False
                    save_state(state)
            elif status == "other" and state.get("combat_active", False):
                LOG.info("Ignoring intermediate prompt; continue ordinary Attack")
                status = "combat"

            LOG.info("Action %s: %s, HP=%s, message=%s", index + 1, status, hp, latest.id)

            if status == "death":
                pause(state, 10, "death", latest.id, hp[0] if hp else 0)
                break
            if status == "energy":
                pause(state, get_energy_wait(latest.raw_text), "energy", latest.id,
                      hp[0] if hp else None)
                break
            if status == "victory" and hp and 0 < hp[0] < 120:
                pause(state, 5, "low_hp", latest.id, hp[0])
                break

            combat = status == "combat"
            fragment = "атака" if combat else "досліджувати"
            fallback = ATTACK_FALLBACK if combat else EXPLORE_FALLBACK
            action_key = (latest.id, str(latest.edit_date), latest.raw_text, fragment)
            if action_key == last_action_signature:
                LOG.warning("Unchanged bot message; refusing duplicate click")
                break
            last_action_signature = action_key

            previous_bot_messages = incoming_signature(messages)
            await take_action(client, bot, messages, fragment, fallback)
            await asyncio.sleep(ACTION_DELAY_SECONDS)
            if not await wait_for_bot_response(client, bot, previous_bot_messages):
                LOG.warning("No new bot response; stopping to prevent repeated commands")
                break
        else:
            LOG.info("Reached action limit for this run")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except FloodWaitError as error:
        LOG.warning("Telegram FloodWait: %s seconds", error.seconds)
        pause(read_state(), max(5, (error.seconds + 59) // 60), "telegram_flood_wait")
    except RPCError:
        LOG.exception("Telegram API error")
        raise
