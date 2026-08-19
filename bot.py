import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# ============================================================
# Configuration
# ============================================================

load_dotenv()

# IMPORTANT:
# The actual bot token goes in .env, NOT in this file.
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()


def parse_id(value: str) -> Optional[int]:
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


GUILD_ID = parse_id(
    os.getenv("1538069562522603571", "").strip()
)

TICKET_CATEGORY_ID = parse_id(
    os.getenv("TICKET_CATEGORY_ID", "").strip()
)

STAFF_ROLE_ID = parse_id(
    os.getenv("STAFF_ROLE_ID", "").strip()
)

SERVER_INFO_CHANNEL_ID = parse_id(
    os.getenv("SERVER_INFO_CHANNEL_ID", "").strip()
)========
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("discord-ticket-bot")


# ============================================================
# Intents / Bot
# ============================================================

intents = discord.Intents.default()
intents.guilds = True


class TicketBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            reconnect=True,
        )

    async def setup_hook(self):
        # Register persistent dropdown.
        self.add_view(TicketPanelView())

        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)

                # Copy global commands to the development guild.
                self.tree.copy_global_to(guild=guild)

                synced = await self.tree.sync(guild=guild)

                logger.info(
                    "Slash-command synchronization successful: "
                    "%d command(s) synced to guild %s.",
                    len(synced),
                    GUILD_ID,
                )
            else:
                synced = await self.tree.sync()

                logger.info(
                    "Slash-command synchronization successful: "
                    "%d global command(s) synced.",
                    len(synced),
                )

        except discord.HTTPException as exc:
            logger.error(
                "Discord API error during slash-command synchronization: %s",
                exc,
            )

        except Exception:
            logger.exception(
                "Unexpected slash-command synchronization error."
            )

    async def on_ready(self):
        if self.user is None:
            return

        logger.info("Bot is online!")
        logger.info("Bot username: %s", self.user)
        logger.info("Bot ID: %s", self.user.id)
        logger.info("Guild count: %d", len(self.guilds))


bot = TicketBot()


# ============================================================
# Helpers
# ============================================================

def clean_username(username: str) -> str:
    """
    Convert a Discord username into a safe Discord channel-name
    component.
    """
    username = username.lower()

    username = re.sub(
        r"[^a-z0-9_-]",
        "-",
        username,
    )

    username = re.sub(
        r"-+",
        "-",
        username,
    )

    username = username.strip("-_")

    if not username:
        username = "user"

    return username[:70]


def get_ticket_type_label(ticket_type: str) -> str:
    labels = {
        "general": "General Support",
        "bug": "Bug Report",
        "report": "Player Report",
    }

    return labels.get(
        ticket_type,
        ticket_type.title(),
    )


def make_error_embed(
    title: str,
    description: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_footer(
        text="Powered by Python Discord Bot"
    )

    return embed


def make_ticket_topic(
    user_id: int,
    ticket_type: str,
) -> str:
    return (
        f"ticket_owner={user_id};"
        f"ticket_type={ticket_type}"
    )


async def find_existing_ticket(
    guild: discord.Guild,
    user_id: int,
) -> Optional[discord.TextChannel]:
    """
    Search the ticket category for an existing ticket belonging
    to this user.

    No database is required because ticket ownership is stored
    in the channel topic.
    """
    if TICKET_CATEGORY_ID is None:
        return None

    category = guild.get_channel(
        TICKET_CATEGORY_ID
    )

    if not isinstance(
        category,
        discord.CategoryChannel,
    ):
        return None

    owner_marker = f"ticket_owner={user_id};"

    for channel in category.text_channels:
        if (
            channel.topic
            and owner_marker in channel.topic
        ):
            return channel

    return None


# ============================================================
# Ticket Dropdown
# ============================================================

class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="General Support",
                description="Open a ticket for general assistance.",
                value="general",
                emoji="🎫",
            ),
            discord.SelectOption(
                label="Bug Report",
                description="Report a bug or technical issue.",
                value="bug",
                emoji="🐛",
            ),
            discord.SelectOption(
                label="Player Report",
                description="Report a player to the staff team.",
                value="report",
                emoji="🚨",
            ),
        ]

        super().__init__(
            placeholder="Select a ticket category...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="support_ticket_select",
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Server Only",
                    "Tickets can only be created inside a server.",
                ),
                ephemeral=True,
            )
            return

        # Acknowledge the interaction immediately so Discord
        # does not display "Interaction Failed".
        await interaction.response.defer(
            ephemeral=True
        )

        ticket_type = self.values[0]

        try:
            await create_ticket(
                interaction,
                ticket_type,
            )

        except discord.Forbidden:
            logger.warning(
                "Missing permissions while creating ticket "
                "for user %s in guild %s.",
                interaction.user.id,
                interaction.guild.id,
            )

            await interaction.followup.send(
                embed=make_error_embed(
                    "Permission Error",
                    (
                        "I do not have enough permissions to create "
                        "the ticket channel. Make sure the bot has "
                        "**Manage Channels** permission."
                    ),
                ),
                ephemeral=True,
            )

        except discord.HTTPException as exc:
            logger.error(
                "Discord API error while creating ticket: %s",
                exc,
            )

            await interaction.followup.send(
                embed=make_error_embed(
                    "Discord API Error",
                    (
                        "Discord returned an error while creating "
                        "your ticket. Please try again."
                    ),
                ),
                ephemeral=True,
            )

        except Exception:
            logger.exception(
                "Unexpected ticket creation error."
            )

            await interaction.followup.send(
                embed=make_error_embed(
                    "Ticket Creation Failed",
                    (
                        "Something went wrong while creating your "
                        "ticket. Please contact the staff team."
                    ),
                ),
                ephemeral=True,
            )


class TicketPanelView(discord.ui.View):
    """
    Persistent ticket dropdown.

    timeout=None and a fixed custom_id allow the dropdown to
    continue working after bot restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


# ============================================================
# Ticket Creation
# ============================================================

async def create_ticket(
    interaction: discord.Interaction,
    ticket_type: str,
):
    guild = interaction.guild

    if guild is None:
        return

    # --------------------------------------------------------
    # Configuration checks
    # --------------------------------------------------------

    if TICKET_CATEGORY_ID is None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Configuration Error",
                (
                    "The TICKET_CATEGORY_ID environment variable "
                    "is missing or invalid."
                ),
            ),
            ephemeral=True,
        )
        return

    if STAFF_ROLE_ID is None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Configuration Error",
                (
                    "The STAFF_ROLE_ID environment variable "
                    "is missing or invalid."
                ),
            ),
            ephemeral=True,
        )
        return

    category = guild.get_channel(
        TICKET_CATEGORY_ID
    )

    if not isinstance(
        category,
        discord.CategoryChannel,
    ):
        await interaction.followup.send(
            embed=make_error_embed(
                "Ticket Category Missing",
                (
                    "The configured ticket category could not "
                    "be found. Please contact the server administrator."
                ),
            ),
            ephemeral=True,
        )
        return

    staff_role = guild.get_role(
        STAFF_ROLE_ID
    )

    if staff_role is None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Staff Role Missing",
                (
                    "The configured staff role could not be found. "
                    "Please contact the server administrator."
                ),
            ),
            ephemeral=True,
        )
        return

    # --------------------------------------------------------
    # Duplicate prevention
    # --------------------------------------------------------

    existing_ticket = await find_existing_ticket(
        guild,
        interaction.user.id,
    )

    if existing_ticket is not None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Ticket Already Open",
                (
                    f"You already have an open ticket: "
                    f"{existing_ticket.mention}\n\n"
                    "Please use your existing ticket instead."
                ),
            ),
            ephemeral=True,
        )
        return

    # --------------------------------------------------------
    # Channel name
    # --------------------------------------------------------

    username = clean_username(
        interaction.user.name
    )

    prefixes = {
        "general": "support",
        "bug": "bug",
        "report": "report",
    }

    prefix = prefixes.get(
        ticket_type,
        "ticket",
    )

    channel_name = (
        f"{prefix}-{username}"
    )[:100]

    # --------------------------------------------------------
    # Permissions
    # --------------------------------------------------------

    everyone_role = guild.default_role

    overwrites = {
        everyone_role: discord.PermissionOverwrite(
            view_channel=False,
        ),

        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        ),

        staff_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            manage_messages=True,
        ),
    }

    # --------------------------------------------------------
    # Create ticket channel
    # --------------------------------------------------------

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        topic=make_ticket_topic(
            interaction.user.id,
            ticket_type,
        ),
        reason=(
            f"Support ticket created by "
            f"{interaction.user}"
        ),
    )

    # --------------------------------------------------------
    # Welcome embed
    # --------------------------------------------------------

    ticket_label = get_ticket_type_label(
        ticket_type
    )

    welcome_embed = discord.Embed(
        title="Support Ticket Created",
        description=(
            "Thank you for contacting the staff team.\n\n"
            "A staff member will respond shortly.\n\n"
            "Please provide as much relevant information "
            "as possible so we can assist you quickly."
        ),
        color=discord.Color.cyan(),
        timestamp=datetime.now(timezone.utc),
    )

    welcome_embed.add_field(
        name="Created By",
        value=interaction.user.mention,
        inline=True,
    )

    welcome_embed.add_field(
        name="Ticket Type",
        value=ticket_label,
        inline=True,
    )

    welcome_embed.set_footer(
        text="Powered by Python Discord Bot"
    )

    try:
        await channel.send(
            content=(
                f"{interaction.user.mention} "
                f"{staff_role.mention}"
            ),
            embed=welcome_embed,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
            ),
        )

    except discord.HTTPException:
        logger.exception(
            "Ticket channel %s was created, but "
            "the welcome message failed.",
            channel.id,
        )

    # --------------------------------------------------------
    # Confirm ticket creation
    # --------------------------------------------------------

    success_embed = discord.Embed(
        title="Ticket Created",
        description=(
            f"Your {ticket_label.lower()} ticket has been created:\n"
            f"{channel.mention}"
        ),
        color=discord.Color.green(),
    )

    await interaction.followup.send(
        embed=success_embed,
        ephemeral=True,
    )

    logger.info(
        "Created %s ticket #%s for %s (%s) in guild %s.",
        ticket_label,
        channel.id,
        interaction.user,
        interaction.user.id,
        guild.id,
    )


# ============================================================
# /setticketpanel
# ============================================================

@bot.tree.command(
    name="setticketpanel",
    description="Post the support ticket dropdown panel",
)
async def setticketpanel(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "Server Only",
                "This command can only be used inside a server.",
            ),
            ephemeral=True,
        )
        return

    panel_embed = discord.Embed(
        title="Support Ticket Center",
        description=(
            "Select a category from the dropdown menu below to "
            "open a private support ticket with our staff team."
        ),
        color=discord.Color.cyan(),
    )

    panel_embed.set_footer(
        text="Powered by Python Discord Bot"
    )

    try:
        if interaction.channel is None:
            raise discord.HTTPException(
                response=None,
                message="Command channel is unavailable.",
            )

        await interaction.channel.send(
            embed=panel_embed,
            view=TicketPanelView(),
        )

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Ticket Panel Posted",
                description=(
                    "The support ticket panel has been posted."
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

        logger.info(
            "Ticket panel posted by %s (%s) in guild %s.",
            interaction.user,
            interaction.user.id,
            interaction.guild.id,
        )

    except discord.Forbidden:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Permission Error",
                    (
                        "I cannot send messages in this channel. "
                        "Please check my channel permissions."
                    ),
                ),
                ephemeral=True,
            )

    except discord.HTTPException as exc:
        logger.error(
            "Discord API error while posting ticket panel: %s",
            exc,
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Discord API Error",
                    (
                        "The ticket panel could not be posted. "
                        "Please try again."
                    ),
                ),
                ephemeral=True,
            )

    except Exception:
        logger.exception(
            "Unexpected /setticketpanel error."
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Error",
                    "An unexpected error occurred.",
                ),
                ephemeral=True,
            )


# ============================================================
# /serverinfo
# ============================================================

@bot.tree.command(
    name="serverinfo",
    description="Display information about the server",
)
async def serverinfo(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "Server Only",
                "This command can only be used inside a server.",
            ),
            ephemeral=True,
        )
        return

    if SERVER_INFO_CHANNEL_ID is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "Configuration Error",
                (
                    "The SERVER_INFO_CHANNEL_ID environment "
                    "variable is missing or invalid."
                ),
            ),
            ephemeral=True,
        )
        return

    target_channel = interaction.guild.get_channel(
        SERVER_INFO_CHANNEL_ID
    )

    if not isinstance(
        target_channel,
        discord.TextChannel,
    ):
        await interaction.response.send_message(
            embed=make_error_embed(
                "Channel Missing",
                (
                    "The configured server information channel "
                    "could not be found."
                ),
            ),
            ephemeral=True,
        )
        return

    guild = interaction.guild

    text_channel_count = len(
        guild.text_channels
    )

    voice_channel_count = len(
        guild.voice_channels
    )

    owner_text = (
        guild.owner.mention
        if guild.owner is not None
        else f"User ID: {guild.owner_id}"
    )

    created_timestamp = int(
        guild.created_at.timestamp()
    )

    info_embed = discord.Embed(
        title="Server Information",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )

    info_embed.add_field(
        name="Server Name",
        value=guild.name,
        inline=True,
    )

    info_embed.add_field(
        name="Server ID",
        value=str(guild.id),
        inline=True,
    )

    info_embed.add_field(
        name="Member Count",
        value=str(guild.member_count),
        inline=True,
    )

    info_embed.add_field(
        name="Server Owner",
        value=owner_text,
        inline=True,
    )

    info_embed.add_field(
        name="Text Channels",
        value=str(text_channel_count),
        inline=True,
    )

    info_embed.add_field(
        name="Voice Channels",
        value=str(voice_channel_count),
        inline=True,
    )

    info_embed.add_field(
        name="Server Created",
        value=f"<t:{created_timestamp}:F>",
        inline=False,
    )

    info_embed.set_footer(
        text="Powered by Python Discord Bot"
    )

    try:
        await target_channel.send(
            embed=info_embed
        )

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Server Information Sent",
                description=(
                    f"The server information was posted in "
                    f"{target_channel.mention}."
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    except discord.Forbidden:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Permission Error",
                    (
                        f"I cannot send messages in "
                        f"{target_channel.mention}. Please check "
                        "my channel permissions."
                    ),
                ),
                ephemeral=True,
            )

    except discord.HTTPException as exc:
        logger.error(
            "Discord API error while sending server info: %s",
            exc,
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Discord API Error",
                    (
                        "Discord rejected the server information "
                        "message. Please try again later."
                    ),
                ),
                ephemeral=True,
            )

    except Exception:
        logger.exception(
            "Unexpected /serverinfo error."
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Error",
                    "An unexpected error occurred.",
                ),
                ephemeral=True,
            )


# ============================================================
# Slash-command error handling
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    logger.error(
        "Application command error: %s",
        error,
        exc_info=True,
    )

    message = (
        "An unexpected error occurred while processing "
        "the command. Please try again later."
    )

    if isinstance(
        error,
        app_commands.CommandInvokeError,
    ):
        original = error.original

        if isinstance(
            original,
            discord.Forbidden,
        ):
            message = (
                "I do not have the required Discord permissions "
                "to complete this command."
            )

        elif isinstance(
            original,
            discord.HTTPException,
        ):
            message = (
                "Discord returned an API error while processing "
                "the command. Please try again."
            )

    try:
        error_embed = make_error_embed(
            "Command Error",
            message,
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                embed=error_embed,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=error_embed,
                ephemeral=True,
            )

    except discord.HTTPException:
        logger.exception(
            "Could not send application-command error response."
        )


# ============================================================
# Startup validation
# ============================================================

def validate_configuration() -> bool:
    if not DISCORD_TOKEN:
        logger.critical(
            "DISCORD_TOKEN is missing. "
            "Set it in your environment or .env file."
        )
        return False

    if GUILD_ID is None and os.getenv(
        "GUILD_ID",
        "",
    ).strip():
        logger.warning(
            "GUILD_ID is not a valid integer."
        )

    if TICKET_CATEGORY_ID is None and os.getenv(
        "TICKET_CATEGORY_ID",
        "",
    ).strip():
        logger.warning(
            "TICKET_CATEGORY_ID is not a valid integer."
        )

    if STAFF_ROLE_ID is None and os.getenv(
        "STAFF_ROLE_ID",
        "",
    ).strip():
        logger.warning(
            "STAFF_ROLE_ID is not a valid integer."
        )

    if SERVER_INFO_CHANNEL_ID is None and os.getenv(
        "SERVER_INFO_CHANNEL_ID",
        "",
    ).strip():
        logger.warning(
            "SERVER_INFO_CHANNEL_ID is not a valid integer."
        )

    return True


# ============================================================
# Run bot
# ============================================================

if __name__ == "__main__":
    if not validate_configuration():
        raise SystemExit(1)

    try:
        bot.run(
            DISCORD_TOKEN,
            reconnect=True,
        )

    except discord.LoginFailure:
        logger.critical(
            "Discord login failed. The bot token may be invalid. "
            "If the token was previously exposed, regenerate it "
            "in the Discord Developer Portal."
        )

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")

    except Exception:
        logger.exception(
            "Fatal bot error."
        )
