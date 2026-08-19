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
# Environment configuration
# ============================================================

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
TICKET_CATEGORY_ID_RAW = os.getenv("TICKET_CATEGORY_ID", "").strip()
STAFF_ROLE_ID_RAW = os.getenv("STAFF_ROLE_ID", "").strip()
SERVER_INFO_CHANNEL_ID_RAW = os.getenv("SERVER_INFO_CHANNEL_ID", "").strip()


def parse_id(value: str) -> Optional[int]:
    """Convert an environment variable to an integer Discord ID."""
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


GUILD_ID = parse_id(GUILD_ID_RAW)
TICKET_CATEGORY_ID = parse_id(TICKET_CATEGORY_ID_RAW)
STAFF_ROLE_ID = parse_id(STAFF_ROLE_ID_RAW)
SERVER_INFO_CHANNEL_ID = parse_id(SERVER_INFO_CHANNEL_ID_RAW)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("discord-ticket-bot")


# ============================================================
# Bot configuration
# ============================================================

intents = discord.Intents.default()
intents.guilds = True


class TicketBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=intents,
            reconnect=True,
        )

    async def setup_hook(self) -> None:
        """
        Runs once when the bot is preparing to connect.

        The ticket dropdown is registered as a persistent view so
        existing dropdown panels continue working after a restart.
        """
        self.add_view(TicketPanelView())

        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)

                # Copy the commands to the development guild.
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
                "Discord API error while synchronizing slash commands: %s",
                exc,
            )
        except Exception:
            logger.exception(
                "Unexpected error while synchronizing slash commands."
            )

    async def on_ready(self) -> None:
        if self.user is None:
            return

        logger.info("Bot is online!")
        logger.info("Bot username: %s", self.user)
        logger.info("Bot ID: %s", self.user.id)
        logger.info("Guild count: %d", len(self.guilds))


bot = TicketBot()


# ============================================================
# Utility functions
# ============================================================

def clean_username(username: str) -> str:
    """
    Convert a Discord username into a safe channel-name component.
    """
    username = username.lower()

    # Keep letters, numbers, hyphens and underscores.
    username = re.sub(r"[^a-z0-9_-]", "-", username)

    # Collapse repeated hyphens.
    username = re.sub(r"-+", "-", username)

    username = username.strip("-_")

    if not username:
        username = "user"

    # Discord channel names have a practical 100-character limit.
    return username[:70]


def ticket_topic(user_id: int, ticket_type: str) -> str:
    """
    Topic format used to identify ticket ownership and type.

    This avoids requiring a database or additional file.
    """
    return f"ticket_owner={user_id};ticket_type={ticket_type}"


def get_ticket_type_label(ticket_type: str) -> str:
    labels = {
        "general": "General Support",
        "bug": "Bug Report",
        "report": "Player Report",
    }

    return labels.get(ticket_type, ticket_type.title())


async def find_existing_ticket(
    guild: discord.Guild,
    user_id: int,
) -> Optional[discord.TextChannel]:
    """
    Search the configured ticket category for an existing ticket
    belonging to the user.

    No database is required because ownership is stored in the
    channel topic.
    """
    if TICKET_CATEGORY_ID is None:
        return None

    category = guild.get_channel(TICKET_CATEGORY_ID)

    if not isinstance(category, discord.CategoryChannel):
        return None

    owner_marker = f"ticket_owner={user_id};"

    for channel in category.text_channels:
        if channel.topic and owner_marker in channel.topic:
            return channel

    return None


def make_error_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Python Discord Bot")
    return embed


# ============================================================
# Ticket dropdown
# ============================================================

class TicketSelect(discord.ui.Select):
    def __init__(self) -> None:
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

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Server Only",
                    "Tickets can only be created inside a server.",
                ),
                ephemeral=True,
            )
            return

        # Acknowledge immediately so Discord does not display
        # "Interaction Failed" while the channel is being created.
        await interaction.response.defer(ephemeral=True)

        ticket_type = self.values[0]

        try:
            await create_ticket(
                interaction=interaction,
                ticket_type=ticket_type,
            )

        except discord.Forbidden:
            logger.warning(
                "Missing Discord permissions while creating a ticket "
                "for user %s in guild %s.",
                interaction.user.id,
                interaction.guild.id,
            )

            await interaction.followup.send(
                embed=make_error_embed(
                    "Permission Error",
                    (
                        "I do not have enough permissions to create the "
                        "ticket channel. Make sure the bot has **Manage "
                        "Channels** permission."
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
                        "Discord returned an error while creating your "
                        "ticket. Please try again in a moment."
                    ),
                ),
                ephemeral=True,
            )

        except Exception:
            logger.exception("Unexpected ticket creation error.")

            await interaction.followup.send(
                embed=make_error_embed(
                    "Ticket Creation Failed",
                    (
                        "Something went wrong while creating your ticket. "
                        "Please contact the staff team."
                    ),
                ),
                ephemeral=True,
            )


class TicketPanelView(discord.ui.View):
    """
    Persistent ticket panel.

    timeout=None and a fixed custom_id allow the dropdown to continue
    functioning after bot restarts.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


# ============================================================
# Ticket creation
# ============================================================

async def create_ticket(
    interaction: discord.Interaction,
    ticket_type: str,
) -> None:
    guild = interaction.guild

    if guild is None:
        return

    # --------------------------------------------------------
    # Configuration validation
    # --------------------------------------------------------

    if TICKET_CATEGORY_ID is None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Configuration Error",
                "The `TICKET_CATEGORY_ID` environment variable is missing or invalid.",
            ),
            ephemeral=True,
        )
        return

    if STAFF_ROLE_ID is None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Configuration Error",
                "The `STAFF_ROLE_ID` environment variable is missing or invalid.",
            ),
            ephemeral=True,
        )
        return

    category = guild.get_channel(TICKET_CATEGORY_ID)

    if not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send(
            embed=make_error_embed(
                "Ticket Category Missing",
                (
                    "The configured ticket category could not be found. "
                    "Please contact the server administrator."
                ),
            ),
            ephemeral=True,
        )
        return

    staff_role = guild.get_role(STAFF_ROLE_ID)

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
    # Duplicate ticket prevention
    # --------------------------------------------------------

    existing_ticket = await find_existing_ticket(
        guild=guild,
        user_id=interaction.user.id,
    )

    if existing_ticket is not None:
        await interaction.followup.send(
            embed=make_error_embed(
                "Ticket Already Open",
                (
                    f"You already have an open ticket: "
                    f"{existing_ticket.mention}\n\n"
                    "Please use your existing ticket instead of creating "
                    "another one."
                ),
            ),
            ephemeral=True,
        )
        return

    # --------------------------------------------------------
    # Channel name
    # --------------------------------------------------------

    username = clean_username(interaction.user.name)

    prefixes = {
        "general": "support",
        "bug": "bug",
        "report": "report",
    }

    prefix = prefixes.get(ticket_type, "ticket")
    channel_name = f"{prefix}-{username}"[:100]

    # --------------------------------------------------------
    # Permission overwrites
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
    # Create channel
    # --------------------------------------------------------

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        topic=ticket_topic(
            user_id=interaction.user.id,
            ticket_type=ticket_type,
        ),
        reason=f"Support ticket created by {interaction.user}",
    )

    # --------------------------------------------------------
    # Welcome embed
    # --------------------------------------------------------

    ticket_label = get_ticket_type_label(ticket_type)

    welcome_embed = discord.Embed(
        title="Support Ticket Created",
        description=(
            "Thank you for contacting the staff team. "
            "A staff member will respond shortly.\n\n"
            "**Please provide as much relevant information as possible "
            "so we can assist you quickly.**"
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
            content=f"{interaction.user.mention} <@&{staff_role.id}>",
            embed=welcome_embed,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
            ),
        )

    except discord.HTTPException:
        # The channel was created successfully, so do not report the
        # entire ticket creation as a failure.
        logger.exception(
            "Ticket channel %s was created, but the welcome message failed.",
            channel.id,
        )

    # --------------------------------------------------------
    # Final interaction response
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
        "Created %s ticket #%s for user %s (%s) in guild %s.",
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
async def setticketpanel(interaction: discord.Interaction) -> None:
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
            "Select a category from the dropdown menu below to open "
            "a private support ticket with our staff team."
        ),
        color=discord.Color.cyan(),
    )

    panel_embed.set_footer(
        text="Powered by Python Discord Bot"
    )

    try:
        await interaction.channel.send(
            embed=panel_embed,
            view=TicketPanelView(),
        )

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Ticket Panel Posted",
                description="The support ticket panel has been posted.",
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

        await interaction.response.send_message(
            embed=make_error_embed(
                "Discord API Error",
                "The ticket panel could not be posted. Please try again.",
            ),
            ephemeral=True,
        )

    except Exception:
        logger.exception("Unexpected /setticketpanel error.")

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
async def serverinfo(interaction: discord.Interaction) -> None:
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
                    "The `SERVER_INFO_CHANNEL_ID` environment variable "
                    "is missing or invalid."
                ),
            ),
            ephemeral=True,
        )
        return

    target_channel = interaction.guild.get_channel(
        SERVER_INFO_CHANNEL_ID
    )

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            embed=make_error_embed(
                "Channel Missing",
                (
                    "The configured server information channel could not "
                    "be found."
                ),
            ),
            ephemeral=True,
        )
        return

    guild = interaction.guild

    text_channel_count = len(guild.text_channels)
    voice_channel_count = len(guild.voice_channels)

    owner_text = (
        guild.owner.mention
        if guild.owner is not None
        else f"User ID: {guild.owner_id}"
    )

    created_timestamp = int(guild.created_at.timestamp())

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
        await target_channel.send(embed=info_embed)

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
        await interaction.response.send_message(
            embed=make_error_embed(
                "Permission Error",
                (
                    f"I cannot send messages in {target_channel.mention}. "
                    "Please check my channel permissions."
                ),
            ),
            ephemeral=True,
        )

    except discord.HTTPException as exc:
        logger.error(
            "Discord API error while sending server information: %s",
            exc,
        )

        await interaction.response.send_message(
            embed=make_error_embed(
                "Discord API Error",
                (
                    "Discord rejected the server information message. "
                    "Please try again later."
                ),
            ),
            ephemeral=True,
        )

    except Exception:
        logger.exception("Unexpected /serverinfo error.")

        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Error",
                    "An unexpected error occurred.",
                ),
                ephemeral=True,
            )


# ============================================================
# Global application-command error handler
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.error(
        "Application command error: %s",
        error,
        exc_info=True,
    )

    message = (
        "An unexpected error occurred while processing the command. "
        "Please try again later."
    )

    if isinstance(error, app_commands.CommandInvokeError):
        original = error.original

        if isinstance(original, discord.Forbidden):
            message = (
                "I do not have the required Discord permissions to "
                "complete this command."
            )

        elif isinstance(original, discord.HTTPException):
            message = (
                "Discord returned an API error while processing the "
                "command. Please try again."
            )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                embed=make_error_embed(
                    "Command Error",
                    message,
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=make_error_embed(
                    "Command Error",
                    message,
                ),
                ephemeral=True,
            )

    except discord.HTTPException:
        logger.exception(
            "Could not send application-command error response."
        )


# ============================================================
# Startup
# ============================================================

def validate_configuration() -> bool:
    """
    Validate required configuration before starting the bot.
    """
    if not DISCORD_TOKEN:
        logger.critical(
            "DISCORD_TOKEN is missing. "
            "Set it in your environment or .env file."
        )
        return False

    if GUILD_ID_RAW and GUILD_ID is None:
        logger.warning("GUILD_ID is not a valid integer.")

    if TICKET_CATEGORY_ID_RAW and TICKET_CATEGORY_ID is None:
        logger.warning("TICKET_CATEGORY_ID is not a valid integer.")

    if STAFF_ROLE_ID_RAW and STAFF_ROLE_ID is None:
        logger.warning("STAFF_ROLE_ID is not a valid integer.")

    if SERVER_INFO_CHANNEL_ID_RAW and SERVER_INFO_CHANNEL_ID is None:
        logger.warning("SERVER_INFO_CHANNEL_ID is not a valid integer.")

    return True


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
            "If the token was previously exposed, regenerate it in "
            "the Discord Developer Portal."
        )

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")

    except Exception:
        logger.exception("Fatal bot error.")
