"""
Kingshot Governor Profile OCR

Liest Governor-Profile aus Screenshots und erzeugt:

    ID,Name,,Kingdom

Alliance wird nur intern zur Erkennung verwendet. Mehrere Screenshots in einer
Nachricht werden nacheinander verarbeitet.
"""

import asyncio
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
        self._guild_locks = {}
        self.id_pattern = re.compile(r"\bID\s*[:=-]?\s*(\d{7,10})\b", re.IGNORECASE)
        self.kingdom_pattern = re.compile(r"\bKingdom\s*[:=-]?\s*#?\s*(\d{1,6})\b", re.IGNORECASE)
        self.alliance_pattern = re.compile(r"\bAlliance\s*[:=-]?\s*(.+?)(?=\s+\b(?:Kingdom|ID|Kills|Power)\b|$)", re.IGNORECASE)
        self.profile_name_pattern = re.compile(r"^\s*\[([^\]]{1,20})\]\s*(.+?)\s*$")
        self.inline_profile_pattern = re.compile(r"(?:Governor\s+Profile(?:\s+Gear)?\s*)?\[([^\]]{1,20})\]\s*(.+?)\s+(?=ID\s*[:=-]?)", re.IGNORECASE)
        if not os.path.exists(self.log_file_path):
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                f.write("ID,Name,,Kingdom\n")
        logger.warning("ProfileOCR DEBUG: cog initialized")

    @staticmethod
    def _ensure_settings_table(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS profile_ocr_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)")

    def get_configured_channel_id(self, guild_id):
        try:
            with sqlite3.connect("db/settings.sqlite", timeout=10) as conn:
                self._ensure_settings_table(conn)
                row = conn.execute("SELECT channel_id FROM profile_ocr_settings WHERE guild_id = ? LIMIT 1", (guild_id,)).fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.warning("ProfileOCR: channel lookup failed: %s", e)
            return None

    @app_commands.command(name="set_profile_channel", description="Legt den Kanal für Governor-Profil-Screenshots fest.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_profile_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        try:
            if interaction.guild_id is None:
                await interaction.response.send_message("❌ Dieser Befehl kann nur auf einem Server verwendet werden.", ephemeral=True)
                return
            with sqlite3.connect("db/settings.sqlite", timeout=10) as conn:
                self._ensure_settings_table(conn)
                conn.execute("INSERT INTO profile_ocr_settings (guild_id, channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id", (interaction.guild_id, channel.id))
                conn.commit()
            await interaction.response.send_message("📑 **Governor-Profil-OCR aktiviert**\n" f"Kanal: {channel.mention}", ephemeral=True)
        except Exception as e:
            logger.exception("ProfileOCR: failed to configure channel")
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: `{e}`", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler: `{e}`", ephemeral=True)

    @staticmethod
    def normalize_text(text: str) -> str:
        if not text:
            return ""
        # Do not run Bear Track's generic digit repair here: profile IDs can be
        # turned into comma-separated numbers and then fail the ID regex.
        text = text.replace("\u00a0", " ")
        return re.sub(r"[ \t]+", " ", text).strip()

    def parse_profile(self, text: str):
        text = self.normalize_text(text)
        if not text:
            return None
        id_match = self.id_pattern.search(text)
        if not id_match:
            return None
        detected_id = id_match.group(1)
        detected_name = None
        detected_alliance = None
        kingdom_match = self.kingdom_pattern.search(text)
        detected_kingdom = kingdom_match.group(1) if kingdom_match else None
        alliance_match = self.alliance_pattern.search(text)
        if alliance_match:
            detected_alliance = alliance_match.group(1).strip().strip("[](){}#,:; ")
        prefix = text[:id_match.start()].strip()
        inline_match = self.inline_profile_pattern.search(prefix + " ID:")
        if inline_match:
            header_alliance = inline_match.group(1).strip()
            header_name = re.sub(r"^(?:Governor\s+Profile(?:\s+Gear)?\s*)+", "", inline_match.group(2).strip(), flags=re.IGNORECASE).strip()
            if header_name:
                detected_name = header_name
            if not detected_alliance and header_alliance:
                detected_alliance = header_alliance
        if not detected_name:
            for line in [x.strip() for x in re.split(r"[\r\n]+", text) if x.strip()]:
                match = self.profile_name_pattern.match(line)
                if match:
                    detected_alliance = detected_alliance or match.group(1).strip()
                    detected_name = match.group(2).strip()
                    break
        return {
            "id": detected_id,
            "name": (detected_name or "Unbekannt").replace(",", " ").strip(),
            "alliance": (detected_alliance or "Unbekannt").replace(",", " ").strip(),
            "kingdom": detected_kingdom or "Unbekannt",
        }

    @staticmethod
    def _is_image(attachment):
        content_type = (attachment.content_type or "").lower()
        filename = (attachment.filename or "").lower()
        return content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp"))

    def _guild_lock(self, guild_id):
        if guild_id not in self._guild_locks:
            self._guild_locks[guild_id] = asyncio.Lock()
        return self._guild_locks[guild_id]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot:
                return
            if message.guild is None:
                return

            # WARNING level is intentional while debugging because main.py
            # temporarily disables INFO logs during cog loading and some bot
            # deployments filter INFO at runtime.
            logger.warning(
                "ProfileOCR DEBUG: message guild=%s channel=%s attachments=%s author=%s",
                message.guild.id,
                message.channel.id,
                len(message.attachments),
                message.author.id,
            )

            if not message.attachments:
                return

            channel_id = self.get_configured_channel_id(message.guild.id)
            logger.warning(
                "ProfileOCR DEBUG: configured_channel=%s actual_channel=%s",
                channel_id,
                message.channel.id,
            )
            if not channel_id or message.channel.id != channel_id:
                return

            images = [a for a in message.attachments if self._is_image(a)]
            logger.warning(
                "ProfileOCR DEBUG: attachments=%s images=%s files=%s",
                len(message.attachments),
                len(images),
                [a.filename for a in message.attachments],
            )
            if not images:
                return

            status = await message.channel.send(f"🔎 **{len(images)} Governor-Profil{'e' if len(images) != 1 else ''} werden analysiert...**")
            async with self._guild_lock(message.guild.id):
                successes = []
                failures = []
                for image in images:
                    try:
                        logger.warning("ProfileOCR DEBUG: reading image %s", image.filename)
                        image_bytes = await image.read()
                        logger.warning("ProfileOCR DEBUG: image %s bytes=%s; starting OCR", image.filename, len(image_bytes))
                        extracted_text = await bear_track.ocr_bytes(image_bytes, lang=bear_track.DEFAULT_OCR_LANG)
                        logger.warning("ProfileOCR DEBUG: raw text (%s): %r", image.filename, extracted_text)
                        if not extracted_text:
                            failures.append(f"{image.filename}: kein Text erkannt")
                            continue
                        profile = self.parse_profile(extracted_text)
                        logger.warning("ProfileOCR DEBUG: parsed %s -> %r", image.filename, profile)
                        if not profile:
                            failures.append(f"{image.filename}: Profil nicht erkannt")
                            continue
                        result = f"{profile['id']},{profile['name']},,{profile['kingdom']}"
                        successes.append(result)
                        logger.warning("ProfileOCR result: %s", result)
                    except Exception as e:
                        logger.exception("ProfileOCR failed for %s", image.filename)
                        failures.append(f"{image.filename}: {e}")

                if successes:
                    with open(self.log_file_path, "a", encoding="utf-8") as f:
                        for result in successes:
                            f.write(result + "\n")

                parts = []
                if successes:
                    parts.append(f"✅ **{len(successes)}/{len(images)} Governor-Profile erkannt**\n\n```text\nID,Name,,Kingdom\n" + "\n".join(successes) + "\n```")
                if failures:
                    parts.append("⚠️ **Nicht erkannt:**\n" + "\n".join(f"• {x}" for x in failures[:10]))
                await status.edit(content="\n\n".join(parts) or "❌ Kein Governor-Profil erkannt.")
        except Exception:
            logger.exception("ProfileOCR DEBUG: unhandled on_message failure")


async def setup(bot):
    await bot.add_cog(ProfileOCR(bot))
