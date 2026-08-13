"""
Kingshot Governor Profile OCR

Liest Governor-Profile aus Screenshots und erzeugt:

    ID,Name,Alliance,Kingdom

Phase 1:
    Nur OCR + Erkennung.
    Keine Änderung an users.sqlite.
"""

import logging
import os
import re
import sqlite3

import discord
from discord.ext import commands
from discord import app_commands

from . import bear_track


logger = logging.getLogger("bot")


class ProfileOCR(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.log_file_path = "profiles_log.txt"

        self.id_pattern = re.compile(
            r"\bID\s*[:=-]?\s*(\d{7,10})\b",
            re.IGNORECASE,
        )
        self.kingdom_pattern = re.compile(
            r"\bKingdom\s*[:=-]?\s*#?\s*(\d{1,6})\b",
            re.IGNORECASE,
        )
        self.alliance_pattern = re.compile(
            r"\bAlliance\s*[:=-]?\s*(.+?)(?=\s+\b(?:Kingdom|ID|Kills|Power)\b|$)",
            re.IGNORECASE,
        )
        self.profile_name_pattern = re.compile(
            r"^\s*\[([^\]]{1,20})\]\s*(.+?)\s*$"
        )
        self.inline_profile_pattern = re.compile(
            r"(?:Governor\s+Profile(?:\s+Gear)?\s*)?"
            r"\[([^\]]{1,20})\]\s*"
            r"(.+?)\s+"
            r"(?=ID\s*[:=-]?)",
            re.IGNORECASE,
        )

        if not os.path.exists(self.log_file_path):
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                f.write("ID,Name,Alliance,Kingdom\n")

    # =============================================================
    # CHANNEL CONFIGURATION
    # =============================================================

    @staticmethod
    def _ensure_settings_table(conn):
        """Keep Profile OCR settings separate from alliance OCR settings."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_ocr_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            )
            """
        )

    def get_configured_channel_id(self, guild_id):
        try:
            with sqlite3.connect("db/settings.sqlite", timeout=10) as conn:
                self._ensure_settings_table(conn)
                row = conn.execute(
                    """
                    SELECT channel_id
                    FROM profile_ocr_settings
                    WHERE guild_id = ?
                    LIMIT 1
                    """,
                    (guild_id,),
                ).fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.warning("ProfileOCR: channel lookup failed: %s", e)
            return None

    @app_commands.command(
        name="set_profile_channel",
        description="Legt den Kanal für Governor-Profil-Screenshots fest.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def set_profile_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        try:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "❌ Dieser Befehl kann nur auf einem Server verwendet werden.",
                    ephemeral=True,
                )
                return

            with sqlite3.connect("db/settings.sqlite", timeout=10) as conn:
                self._ensure_settings_table(conn)
                conn.execute(
                    """
                    INSERT INTO profile_ocr_settings (guild_id, channel_id)
                    VALUES (?, ?)
                    ON CONFLICT(guild_id)
                    DO UPDATE SET channel_id = excluded.channel_id
                    """,
                    (interaction.guild_id, channel.id),
                )
                conn.commit()

            await interaction.response.send_message(
                "📑 **Governor-Profil-OCR aktiviert**\n"
                f"Kanal: {channel.mention}",
                ephemeral=True,
            )
        except Exception as e:
            logger.exception("ProfileOCR: failed to configure channel")
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: `{e}`", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"❌ Fehler: `{e}`",
                    ephemeral=True,
                )

    # =============================================================
    # TEXT NORMALIZATION
    # =============================================================

    @staticmethod
    def normalize_text(text: str) -> str:
        if not text:
            return ""
        try:
            text = bear_track.repair_ocr_digits(text)
        except Exception:
            pass
        text = text.replace("\u00a0", " ")
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    # =============================================================
    # PROFILE PARSER
    # =============================================================

    def parse_profile(self, text: str):
        text = self.normalize_text(text)
        if not text:
            return None

        # Extract the labelled fields from the complete OCR string first.
        # RapidOCR often returns the whole profile as one line.
        id_match = self.id_pattern.search(text)
        if not id_match:
            return None

        detected_id = id_match.group(1)
        detected_name = None
        detected_alliance = None
        detected_kingdom = None

        alliance_match = self.alliance_pattern.search(text)
        if alliance_match:
            detected_alliance = alliance_match.group(1).strip().strip("[](){}#,:; ")

        kingdom_match = self.kingdom_pattern.search(text)
        if kingdom_match:
            detected_kingdom = kingdom_match.group(1)

        # Kingshot profile header commonly appears as:
        # Governor Profile Gear [PAX]Newlifeyv13 ID: 129255884 ...
        # Read [Alliance]Name immediately before the ID label.
        prefix = text[:id_match.start()].strip()
        inline_match = self.inline_profile_pattern.search(prefix + " ID:")
        if inline_match:
            header_alliance = inline_match.group(1).strip()
            header_name = inline_match.group(2).strip()
            header_name = re.sub(
                r"^(?:Governor\s+Profile(?:\s+Gear)?\s*)+",
                "",
                header_name,
                flags=re.IGNORECASE,
            ).strip()
            if header_name:
                detected_name = header_name
            if not detected_alliance and header_alliance:
                detected_alliance = header_alliance

        # Fallback for OCR output containing line breaks.
        if not detected_name:
            lines = [
                line.strip()
                for line in re.split(r"[\r\n]+", text)
                if line.strip()
            ]
            for line in lines:
                match = self.profile_name_pattern.match(line)
                if match:
                    detected_alliance = detected_alliance or match.group(1).strip()
                    detected_name = match.group(2).strip()
                    break

        detected_name = (detected_name or "Unbekannt").replace(",", " ").strip()
        detected_alliance = (detected_alliance or "Unbekannt").replace(",", " ").strip()
        detected_kingdom = detected_kingdom or "Unbekannt"

        return {
            "id": detected_id,
            "name": detected_name,
            "alliance": detected_alliance,
            "kingdom": detected_kingdom,
        }

    # =============================================================
    # MESSAGE LISTENER
    # =============================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.attachments:
            return
        if message.guild is None:
            return

        channel_id = self.get_configured_channel_id(message.guild.id)
        if not channel_id or message.channel.id != channel_id:
            return

        image = None
        for attachment in message.attachments:
            content_type = (attachment.content_type or "").lower()
            filename = (attachment.filename or "").lower()
            if content_type.startswith("image/") or filename.endswith(
                (".png", ".jpg", ".jpeg", ".webp")
            ):
                image = attachment
                break

        if image is None:
            return

        status = await message.channel.send(
            "🔎 **Governor-Profil wird analysiert...**"
        )

        try:
            image_bytes = await image.read()
            extracted_text = await bear_track.ocr_bytes(
                image_bytes,
                lang=bear_track.DEFAULT_OCR_LANG,
            )

            logger.info("ProfileOCR raw text: %r", extracted_text)

            if not extracted_text:
                await status.edit(
                    content=(
                        "❌ **OCR konnte keinen Text erkennen.**\n"
                        "Bitte einen klaren Kingshot-Profil-Screenshot hochladen."
                    )
                )
                return

            profile = self.parse_profile(extracted_text)
            if not profile:
                await status.edit(
                    content=(
                        "❌ **Governor-Profil konnte nicht erkannt werden.**\n\n"
                        "OCR-Ausgabe:\n"
                        "```text\n"
                        f"{extracted_text[:1500]}\n"
                        "```"
                    )
                )
                return

            result_string = (
                f"{profile['id']},{profile['name']},"
                f"{profile['alliance']},{profile['kingdom']}"
            )

            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(result_string + "\n")

            logger.info("ProfileOCR result: %s", result_string)

            await status.edit(
                content=(
                    "✅ **Governor-Profil erkannt**\n\n"
                    "```text\n"
                    "ID,Name,Alliance,Kingdom\n"
                    f"{result_string}\n"
                    "```"
                )
            )
        except Exception as e:
            logger.exception("ProfileOCR failed")
            await status.edit(
                content=(
                    "❌ **Fehler bei der Profil-OCR:**\n"
                    f"`{e}`"
                )
            )


async def setup(bot):
    await bot.add_cog(ProfileOCR(bot))
