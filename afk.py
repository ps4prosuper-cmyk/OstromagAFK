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
            # Ignore a stale pause-trigger message after its wait has finished.
            if latest.id == old_pause_message_id and status in ("death", "energy", "victory"):
                LOG.info("Ignoring stale result after completed cooldown")
                status = "other"
            old_pause_message_id = None
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
