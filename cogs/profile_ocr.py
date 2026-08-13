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
from .alliance_member_edit import apply_member_edit, enqueue_catchups

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

    @staticmethod
    def _sync_existing_member(profile):
        """
        Synchronisiert ein erkanntes Governor-Profil nur mit einem
        bereits vorhandenen Benutzer.

        Geändert werden ausschließlich:
        - nickname
        - kid (Kingdom)

        Alliance, Furnace-Level, Power, Discord-Daten usw.
        bleiben unverändert.
        """

        fid = int(profile["id"])
        nickname = profile["name"]
        kid = int(profile["kingdom"])

        with sqlite3.connect(
            "db/users.sqlite",
            timeout=30.0,
        ) as conn:

            row = conn.execute(
                """
                SELECT nickname, kid
                FROM users
                WHERE fid = ?
                """,
                (fid,),
            ).fetchone()

        if row is None:
            # Governor wurde sicher erkannt, existiert aber noch nicht.
            # Nur die Daten eintragen, die direkt aus dem Profil stammen.
            # Die Alliance wird später manuell zugewiesen.
            with sqlite3.connect(
                "db/users.sqlite",
                timeout=30.0,
            ) as conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO users (
                            fid,
                            nickname,
                            furnace_lv,
                            kid,
                            alliance
                        )
                        VALUES (?, ?, 0, ?, NULL)
                        """,
                        (fid, nickname, kid),
                    )
                    conn.commit()
                    return "added", False, None
                except sqlite3.IntegrityError:
                    # Falls die ID zwischen SELECT und INSERT von einem
                    # anderen Prozess angelegt wurde, nicht überschreiben.
                    return "unchanged", False, None

        old_nickname, old_kid = row

        changed = apply_member_edit(
            fid,
            nickname=nickname,
            kid=kid,
            alliance_id=None,
        )

        if changed:
            details = {
                "old_name": old_nickname,
                "new_name": nickname,
                "old_kingdom": old_kid,
                "new_kingdom": kid,
            }
            return "updated", ("state" in changed), details

        return "unchanged", False, None

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

                db_updated = 0
                db_unchanged = 0
                db_added = 0
                state_fids = []

                db_updated_profiles = []
                db_unchanged_profiles = []
                db_added_profiles = []

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

                        # Nur sicher erkannte Profile mit der bestehenden
                        # Mitgliederdatenbank synchronisieren.
                        if (
                            profile["name"] != "Unbekannt"
                            and profile["kingdom"] != "Unbekannt"
                        ):
                            try:
                                sync_status, state_changed, change_details = (
                                    await asyncio.to_thread(
                                        self._sync_existing_member,
                                        profile,
                                    )
                                )

                                profile_label = (
                                    f"{profile['id']} — {profile['name']}"
                                )

                                if sync_status == "updated":
                                    db_updated += 1

                                    change_lines = [
                                        f"{profile['id']}"
                                    ]

                                    if (
                                        change_details
                                        and change_details["old_name"]
                                        != change_details["new_name"]
                                    ):
                                        change_lines.append(
                                            "Name: "
                                            f"{change_details['old_name']} "
                                            "→ "
                                            f"{change_details['new_name']}"
                                        )

                                    if (
                                        change_details
                                        and change_details["old_kingdom"]
                                        != change_details["new_kingdom"]
                                    ):
                                        change_lines.append(
                                            "Kingdom: "
                                            f"{change_details['old_kingdom']} "
                                            "→ "
                                            f"{change_details['new_kingdom']}"
                                        )

                                    db_updated_profiles.append(
                                        " | ".join(change_lines)
                                    )

                                elif sync_status == "unchanged":
                                    db_unchanged += 1
                                    db_unchanged_profiles.append(profile_label)

                                elif sync_status == "added":
                                    db_added += 1
                                    db_added_profiles.append(profile_label)

                                if state_changed:
                                    state_fids.append(
                                        int(profile["id"])
                                    )

                            except Exception as db_error:
                                logger.exception(
                                    "ProfileOCR DB sync failed for %s",
                                    profile["id"],
                                )

                                failures.append(
                                    f"{image.filename}: "
                                    f"Profil erkannt, aber "
                                    f"DB-Update fehlgeschlagen "
                                    f"({db_error})"
                                )
                    except Exception as e:
                        logger.exception("ProfileOCR failed for %s", image.filename)
                        failures.append(f"{image.filename}: {e}")

                if successes:
                    with open(self.log_file_path, "a", encoding="utf-8") as f:
                        for result in successes:
                            f.write(result + "\n")

                # Wenn sich das Kingdom eines bestehenden Spielers
                # geändert hat, bestehenden Catch-up-Mechanismus verwenden.
                caught = (
                    enqueue_catchups(self.bot, state_fids)
                    if state_fids
                    else 0
                )

                parts = []

                if successes:
                    parts.append(
                        f"✅ **{len(successes)}/{len(images)} "
                        f"Governor-Profile erkannt**\n\n"
                        f"```text\n"
                        f"ID,Name,,Kingdom\n"
                        + "\n".join(successes)
                        + "\n```"
                    )

                    db_lines = [
                        "💾 **Mitgliederdatenbank**",
                        f"🔄 Aktualisiert: **{db_updated}**",
                    ]

                    db_lines.extend(
                        f"  • {x}" for x in db_updated_profiles
                    )

                    db_lines.append(
                        f"➖ Unverändert: **{db_unchanged}**"
                    )
                    db_lines.extend(
                        f"  • {x}" for x in db_unchanged_profiles
                    )

                    db_lines.append(
                        f"➕ Neu hinzugefügt: **{db_added}**"
                    )
                    db_lines.extend(
                        f"  • {x}" for x in db_added_profiles
                    )

                    db_text = "\n".join(db_lines)

                    if caught:
                        db_text += (
                            f"\n🎁 Gift-Code Catch-up: **{caught}**"
                        )

                    parts.append(db_text)

                if failures:
                    parts.append("⚠️ **Nicht erkannt:**\n" + "\n".join(f"• {x}" for x in failures[:10]))
                await status.edit(content="\n\n".join(parts) or "❌ Kein Governor-Profil erkannt.")
        except Exception:
            logger.exception("ProfileOCR DEBUG: unhandled on_message failure")


async def setup(bot):
    await bot.add_cog(ProfileOCR(bot))
