import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import tasks

import config
import database as db
from zcash_client import zcash
from web import start_web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tipbot")

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

pending_ops: dict[str, dict] = {}


# ── Events ──────────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    await db.init_db()
    guild = discord.Object(id=1429785830389448749)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    check_shielded_ops.start()
    await start_web(port=3000)
    log.info(f"Tip bot online as {bot.user}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != config.TIP_EMOJI:
        return
    if payload.user_id == bot.user.id:
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    if message.author.bot:
        return

    tipper = await db.get_user(payload.user_id)
    if not tipper:
        try:
            user = await bot.fetch_user(payload.user_id)
            await user.send("You need an account first! Use `/deposit` to get started.")
        except discord.Forbidden:
            pass
        return

    tip_amount = tipper["default_tip"]

    if tipper["balance"] < tip_amount:
        try:
            user = await bot.fetch_user(payload.user_id)
            await user.send(
                f"Insufficient balance ({tipper['balance']:.4f} ZEC). Use `/deposit` to add funds."
            )
        except discord.Forbidden:
            pass
        return

    recipient = await db.get_user(message.author.id)
    if not recipient:
        deposit_addr = await zcash.get_new_shielded_address()
        await db.get_or_create_user(message.author.id, deposit_addr)

    success = await db.transfer_balance(payload.user_id, message.author.id, tip_amount)
    if not success:
        return

    try:
        tipper_user = await bot.fetch_user(payload.user_id)
        recipient_user = await bot.fetch_user(message.author.id)
        await recipient_user.send(
            f"**{tipper_user.display_name}** tipped you!\n"
            f"Use `/balance` to check your funds."
        )
    except discord.Forbidden:
        pass

    try:
        tipper_user = await bot.fetch_user(payload.user_id)
        await channel.send(
            f"**{tipper_user.display_name}** tipped **{message.author.display_name}**!",
            delete_after=10,
        )
    except discord.Forbidden:
        pass


# ── Slash Commands ──────────────────────────────────────────────────────


@tree.command(name="deposit", description="Get your ZEC deposit address")
async def deposit(interaction: discord.Interaction):
    user = await db.get_user(interaction.user.id)
    if not user:
        addr = await zcash.get_new_shielded_address()
        user = await db.get_or_create_user(interaction.user.id, addr)

    await interaction.response.send_message(
        f"Send ZEC to your deposit address (shielded):\n"
        f"```\n{user['deposit_address']}\n```\n"
        f"Your balance will update after 1 confirmation.",
        ephemeral=True,
    )


@tree.command(name="balance", description="Check your ZEC tip balance")
async def balance(interaction: discord.Interaction):
    user = await db.get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message(
            "No account yet. Use `/deposit` to get started.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"**Balance:** {user['balance']:.4f} ZEC\n"
        f"**Default tip:** {user['default_tip']:.4f} ZEC",
        ephemeral=True,
    )


@tree.command(name="withdraw", description="Withdraw ZEC to your wallet")
@app_commands.describe(amount="Amount of ZEC to withdraw", address="Destination Zcash address")
async def withdraw(interaction: discord.Interaction, amount: float, address: str):
    await interaction.response.defer(ephemeral=True)

    user = await db.get_user(interaction.user.id)
    if not user:
        await interaction.followup.send("No account found.", ephemeral=True)
        return

    if amount < config.MIN_WITHDRAW:
        await interaction.followup.send(f"Minimum withdrawal: {config.MIN_WITHDRAW} ZEC", ephemeral=True)
        return

    if user["balance"] < amount:
        await interaction.followup.send(f"Insufficient balance ({user['balance']:.4f} ZEC).", ephemeral=True)
        return

    try:
        valid = await zcash.validate_address(address)
    except Exception:
        valid = False
    if not valid:
        await interaction.followup.send("Invalid Zcash address.", ephemeral=True)
        return

    await db.update_balance(interaction.user.id, -amount)

    try:
        bot_z_addr = await zcash.get_new_shielded_address()
        opid = await zcash.send_shielded(bot_z_addr, address, amount)
        pending_ops[opid] = {"from_user": interaction.user.id, "amount": amount}
        await interaction.followup.send(
            f"Withdrawal of {amount:.4f} ZEC initiated via shielded pool.", ephemeral=True
        )
    except Exception as e:
        log.error(f"Withdraw failed: {e}")
        await db.update_balance(interaction.user.id, amount)
        await interaction.followup.send(f"Withdrawal failed: {e}", ephemeral=True)


@tree.command(name="settip", description="Set your default tip amount")
@app_commands.describe(amount="Default ZEC amount per reaction tip")
async def settip(interaction: discord.Interaction, amount: float):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return

    user = await db.get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("No account yet. Use `/deposit` first.", ephemeral=True)
        return

    await db.set_default_tip(interaction.user.id, amount)
    await interaction.response.send_message(f"Default tip set to {amount:.4f} ZEC.", ephemeral=True)


@tree.command(name="tip", description="Tip a user (works in DMs and servers)")
@app_commands.describe(user="The user to tip", amount="Amount of ZEC to send")
async def tip(interaction: discord.Interaction, user: discord.User, amount: float):
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("Can't tip bots.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("Can't tip yourself.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    sender = await db.get_user(interaction.user.id)
    if not sender:
        await interaction.followup.send("No account yet. Use `/deposit` first.", ephemeral=True)
        return

    if sender["balance"] < amount:
        await interaction.followup.send(f"Insufficient balance ({sender['balance']:.4f} ZEC).", ephemeral=True)
        return

    recipient = await db.get_user(user.id)
    if not recipient:
        addr = await zcash.get_new_shielded_address()
        await db.get_or_create_user(user.id, addr)

    success = await db.transfer_balance(interaction.user.id, user.id, amount)
    if not success:
        await interaction.followup.send("Tip failed.", ephemeral=True)
        return

    try:
        await user.send(
            f"**{interaction.user.display_name}** tipped you!\n"
            f"Use `/balance` to check your funds."
        )
    except discord.Forbidden:
        pass

    await interaction.followup.send(f"Tipped **{user.display_name}**.", ephemeral=True)


# ── Anonymous Tip ──────────────────────────────────────────────────────


@tree.command(name="setaddress", description="Share your Zcash address for anonymous tips")
@app_commands.describe(address="Your personal Zcash z-address")
async def setaddress(interaction: discord.Interaction, address: str):
    user = await db.get_user(interaction.user.id)
    if not user:
        addr = await zcash.get_new_shielded_address()
        user = await db.get_or_create_user(interaction.user.id, addr)

    await db.set_zaddress(interaction.user.id, address)
    await interaction.response.send_message(
        "Address saved. Others can use `/anontip @you` to tip you directly.", ephemeral=True
    )


@tree.command(name="anontip", description="Get a Zcash payment link to tip someone anonymously")
@app_commands.describe(user="The user you want to tip anonymously")
async def anontip(interaction: discord.Interaction, user: discord.User):
    recipient = await db.get_user(user.id)
    if not recipient or not recipient.get("zaddress"):
        await interaction.response.send_message(
            f"**{user.display_name}** hasn't shared their Zcash address yet.\n"
            f"Ask them to use `/setaddress` first.",
            ephemeral=True,
        )
        return

    from urllib.parse import quote
    label = quote(user.display_name)
    zcash_uri = f"zcash:{recipient['zaddress']}?label={label}"

    await interaction.response.send_message(
        f"Tip **{user.display_name}** anonymously:\n\n"
        f"```\n{zcash_uri}\n```\n"
        f"Open in any Zcash wallet. The bot never sees this transaction.",
        ephemeral=True,
    )


# ── Leaderboard ────────────────────────────────────────────────────────


@tree.command(name="leaderboard", description="See the most generous and most appreciated users")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    top_tippers = await db.get_top_tippers_by_count(5)
    top_receivers = await db.get_top_receivers_by_count(5)

    embed = discord.Embed(title="DiscordCash Leaderboard", color=0xF4B728)

    if top_tippers:
        lines = []
        for i, (user_id, count) in enumerate(top_tippers, 1):
            try:
                u = await bot.fetch_user(user_id)
                name = u.display_name
            except Exception:
                name = f"User {user_id}"
            medal = ["\U0001f947", "\U0001f948", "\U0001f949"][i - 1] if i <= 3 else f"**{i}.**"
            lines.append(f"{medal} {name} — {count} tips")
        embed.add_field(name="Most Generous", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Most Generous", value="No tips yet!", inline=False)

    if top_receivers:
        lines = []
        for i, (user_id, count) in enumerate(top_receivers, 1):
            try:
                u = await bot.fetch_user(user_id)
                name = u.display_name
            except Exception:
                name = f"User {user_id}"
            medal = ["\U0001f947", "\U0001f948", "\U0001f949"][i - 1] if i <= 3 else f"**{i}.**"
            lines.append(f"{medal} {name} — {count} tips")
        embed.add_field(name="Most Appreciated", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Most Appreciated", value="No tips yet!", inline=False)

    await interaction.followup.send(embed=embed)


# ── Rain ───────────────────────────────────────────────────────────────

RAIN_EMOJI = "\U0001f327\ufe0f"
RAIN_DURATION = 7200
active_rains: dict[int, dict] = {}


@tree.command(name="rain", description="Start a rain — react to claim! Splits ZEC after 2 hours")
@app_commands.describe(amount="Total ZEC to distribute")
async def rain(interaction: discord.Interaction, amount: float):
    if not interaction.guild:
        await interaction.response.send_message("Rain only works in servers.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return

    sender = await db.get_user(interaction.user.id)
    if not sender:
        await interaction.response.send_message("No account yet. Use `/deposit` first.", ephemeral=True)
        return

    if sender["balance"] < amount:
        await interaction.response.send_message(
            f"Insufficient balance ({sender['balance']:.4f} ZEC).", ephemeral=True
        )
        return

    await db.update_balance(interaction.user.id, -amount)

    await interaction.response.send_message(
        f"\U0001f327\ufe0f **{interaction.user.display_name}** is making it rain!\n\n"
        f"React with \U0001f327\ufe0f to claim your share!\n"
        f"Rain ends in **2 hours**."
    )
    rain_msg = await interaction.original_response()
    await rain_msg.add_reaction(RAIN_EMOJI)

    active_rains[rain_msg.id] = {
        "sender_id": interaction.user.id,
        "amount": amount,
        "channel_id": interaction.channel_id,
    }
    bot.loop.create_task(finalize_rain(rain_msg.id, RAIN_DURATION))


async def finalize_rain(message_id: int, delay: float):
    await asyncio.sleep(delay)
    rain_info = active_rains.pop(message_id, None)
    if not rain_info:
        return

    channel = bot.get_channel(rain_info["channel_id"])
    if not channel:
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await db.update_balance(rain_info["sender_id"], rain_info["amount"])
        return

    participants = set()
    for reaction in message.reactions:
        if str(reaction.emoji) == RAIN_EMOJI:
            async for user in reaction.users():
                if not user.bot and user.id != rain_info["sender_id"]:
                    participants.add(user.id)

    if not participants:
        await db.update_balance(rain_info["sender_id"], rain_info["amount"])
        await channel.send("\U0001f327\ufe0f Rain ended — nobody joined! Funds refunded.", delete_after=30)
        return

    per_user = rain_info["amount"] / len(participants)
    recipient_names = []
    for user_id in participants:
        recipient = await db.get_user(user_id)
        if not recipient:
            addr = await zcash.get_new_shielded_address()
            await db.get_or_create_user(user_id, addr)
        await db.update_balance(user_id, per_user)
        await db.record_tip_count(rain_info["sender_id"], user_id)
        try:
            u = await bot.fetch_user(user_id)
            recipient_names.append(u.display_name)
        except Exception:
            recipient_names.append(f"User {user_id}")

    await channel.send(
        f"\U0001f327\ufe0f **Rain complete!** Split across **{len(participants)}** users!\n\n"
        f"Recipients: {', '.join(recipient_names)}"
    )


# ── Mock Testing ───────────────────────────────────────────────────────


if config.MOCK_MODE:

    @tree.command(name="mockdeposit", description="[TEST] Give yourself fake ZEC")
    @app_commands.describe(amount="Amount of fake ZEC to credit")
    async def mockdeposit(interaction: discord.Interaction, amount: float = 1.0):
        user = await db.get_user(interaction.user.id)
        if not user:
            addr = await zcash.get_new_shielded_address()
            user = await db.get_or_create_user(interaction.user.id, addr)

        await db.update_balance(interaction.user.id, amount)
        new_bal = user["balance"] + amount
        await interaction.response.send_message(
            f"Credited {amount:.4f} ZEC.\nBalance: {new_bal:.4f} ZEC", ephemeral=True
        )


# ── Background Tasks ───────────────────────────────────────────────────


@tasks.loop(seconds=15)
async def check_shielded_ops():
    completed = []
    for opid, info in pending_ops.items():
        try:
            status = await zcash.get_operation_status(opid)
            if not status:
                continue
            if status["status"] == "success":
                log.info(f"Withdrawal {opid} succeeded")
                completed.append(opid)
            elif status["status"] == "failed":
                log.error(f"Withdrawal {opid} failed")
                await db.update_balance(info["from_user"], info["amount"])
                completed.append(opid)
        except Exception as e:
            log.error(f"Op check failed for {opid}: {e}")
    for opid in completed:
        del pending_ops[opid]


if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)
