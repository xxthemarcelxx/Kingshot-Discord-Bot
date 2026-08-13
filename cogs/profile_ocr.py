import discord
from discord.ext import commands
from discord import app_commands
import re
import os
import sqlite3
import logging

from . import bear_track

logger = logging.getLogger("bot")


class ProfileOCR(commands.Cog):
    """
    Erkennt Kingshot-Governor-Profile aus Screenshots.

    Erwartetes Format:
        ID,Name,Alliance,Kingdom

    Beispiel:
        129255884,Newlifeyv13,PAX,856
    """

    def __init__(self, bot):
        self.bot = bot

        # ID: 129255884
        self.id_pattern = re.compile(
            r"\bID\s*[:=-]?\s*(\d{7,10})\b",
            re.IGNORECASE
        )

        # Kingdom: #856
        self.kingdom_pattern = re.compile(
            r"\bKingdom\s*[:=-]?\s*#?\s*(\d{1,6})\b",
            re.IGNORECASE
        )

        # Alliance: PAX
        # Etwas toleranter gegenüber OCR-Fehlern:
        # Alliance / Alllance / Alli ance etc.
        self.alliance_pattern = re.compile(
            r"\bAll[i1l]{1,2}ance\s*[:=-]?\s*(.+?)(?=\s+\b(?:Kingdom|ID|Kills|Power)\b|$)",
            re.IGNORECASE
        )

        # [PAX]Newlifeyv13
        self.tag_pattern = re.compile(
            r"^\s*\[[^\]]{1,10}\]\s*"
        )

        self.log_file_path = "profiles_log.txt"

        if not os.path.exists(self.log_file_path):
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                f.write("ID,Name,Alliance,Kingdom\n")

    # ---------------------------------------------------------
    # CHANNEL CONFIG
    # ---------------------------------------------------------

    def get_configured_channel_id(self):
        try:
            with sqlite3.connect("db/settings.sqlite") as conn:
                row = conn.execute(
                    """
                    SELECT channel_id
                    FROM ocr_channel_settings
                    WHERE alliance_id = 9999
                    LIMIT 1
                    """
                ).fetchone()

            return row[0] if row else None

        except Exception as e:
            logger.warning(
                f"ProfileOCR: could not read OCR channel: {e}"
            )
            return None

    @app_commands.command(
        name="set_profile_channel",
        description="Legt den Kanal für automatische Profil-Screenshot-Analysen fest."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def set_profile_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        try:
            with sqlite3.connect("db/settings.sqlite") as conn:

                conn.execute(
                    "DELETE FROM ocr_channel_settings WHERE alliance_id = 9999"
                )

                conn.execute(
                    """
                    INSERT INTO ocr_channel_settings
                    (
                        channel_id,
                        alliance_id,
                        auto_delete_screenshots
                    )
                    VALUES (?, 9999, 0)
                    """,
                    (channel.id,)
                )

                conn.commit()

            await interaction.response.send_message(
                f"📑 **Profil-OCR aktiviert für:** {channel.mention}",
                ephemeral=True
            )

        except Exception as e:
            logger.exception("ProfileOCR: failed to configure channel")

            await interaction.response.send_message(
                f"❌ Fehler beim Konfigurieren: `{e}`",
                ephemeral=True
            )

    # ---------------------------------------------------------
    # PROFILE PARSER
    # ---------------------------------------------------------

    def parse_profile(self, text: str):
        """
        Parsed OCR text into:

            {
                "id": "...",
                "name": "...",
                "alliance": "...",
                "kingdom": "..."
            }

        """

        if not text:
            return None

        # OCR-Zahlen reparieren.
        try:
            text = bear_track.repair_ocr_digits(text)
        except Exception:
            pass

        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in text.splitlines()
            if line.strip()
        ]

        detected_id = None
        detected_name = None
        detected_alliance = None
        detected_kingdom = None

        # -----------------------------------------------------
        # ID
        # -----------------------------------------------------

        for line in lines:
            match = self.id_pattern.search(line)

            if match:
                detected_id = match.group(1)

                # Name steht im Kingshot-Profil normalerweise
                # direkt über der ID.
                index = lines.index(line)

                if index > 0:
                    candidate = lines[index - 1].strip()

                    # [PAX]Newlifeyv13 -> Newlifeyv13
                    candidate = self.tag_pattern.sub("", candidate).strip()

                    # Nur sinnvolle Namen übernehmen.
                    if (
                        candidate
                        and not re.search(
                            r"\b(?:ID|Kills|Alliance|Kingdom|Power)\b",
                            candidate,
                            re.IGNORECASE
                        )
                    ):
                        detected_name = candidate

                break

        # -----------------------------------------------------
        # KINGDOM
        # -----------------------------------------------------

        for line in lines:
            match = self.kingdom_pattern.search(line)

            if match:
                detected_kingdom = match.group(1)
                break

        # -----------------------------------------------------
        # ALLIANCE
        # -----------------------------------------------------

        for line in lines:
            match = self.alliance_pattern.search(line)

            if match:
                detected_alliance = match.group(1).strip()

                # OCR kann gelegentlich # oder [] anhängen.
                detected_alliance = detected_alliance.strip(
                    "[](){}# :,-"
                )

                break

        # -----------------------------------------------------
        # FALLBACK: Alliance aus [PAX] extrahieren
        # -----------------------------------------------------

        if not detected_alliance:
            for line in lines:
                match = re.match(
                    r"\s*\[([A-Za-z0-9]{1,10})\]",
                    line
                )

                if match:
                    detected_alliance = match.group(1)
                    break

        # -----------------------------------------------------
        # FALLBACK: Name suchen
        # -----------------------------------------------------

        if not detected_name:

            for line in lines:

                if re.search(
                    r"\b(?:ID|Kills|Alliance|Kingdom|Power)\b",
                    line,
                    re.IGNORECASE
                ):
                    continue

                match = re.match(
                    r"\s*\[([^\]]+)\]\s*(.+)",
                    line
                )

                if match:
                    detected_name = match.group(2).strip()

                    if not detected_alliance:
                        detected_alliance = match.group(1).strip()

                    break

        # -----------------------------------------------------
        # VALIDIERUNG
        # -----------------------------------------------------

        if not detected_id:
            return None

        if not detected_name:
            detected_name = "Unbekannt"

        if not detected_alliance:
            detected_alliance = "Unbekannt"

        if not detected_kingdom:
            detected_kingdom = "Unbekannt"

        # CSV nicht durch Kommas im Spielernamen kaputtmachen.
        detected_name = detected_name.replace(",", " ")

        return {
            "id": detected_id,
            "name": detected_name,
            "alliance": detected_alliance,
            "kingdom": detected_kingdom,
        }

    # ---------------------------------------------------------
    # DISCORD MESSAGE HANDLER
    # ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        if message.author.bot:
            return

        if not message.attachments:
            return

        configured_channel = self.get_configured_channel_id()

        if not configured_channel:
            return

        if message.channel.id != configured_channel:
            return

        # Nur Bilder
        image = None

        for attachment in message.attachments:

            content_type = attachment.content_type or ""

            if (
                content_type.startswith("image/")
                or attachment.filename.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp")
                )
            ):
                image = attachment
                break

        if image is None:
            return

        status = await message.channel.send(
            "🔎 **Profil-Screenshot wird analysiert...**"
        )

        try:

            image_bytes = await image.read()

            # -------------------------------------------------
            # Zentrale OCR des Bots verwenden
            # -------------------------------------------------

            extracted_text = await bear_track.ocr_bytes(
                image_bytes,
                lang=bear_track.DEFAULT_OCR_LANG
            )

            logger.info(
                f"ProfileOCR raw OCR: {extracted_text!r}"
            )

            if not extracted_text:

                await status.edit(
                    content=(
                        "❌ **OCR konnte keinen Text erkennen.**\n"
                        "Bitte einen schärferen Screenshot hochladen."
                    )
                )
                return

            # -------------------------------------------------
            # Profil parsen
            # -------------------------------------------------

            profile = self.parse_profile(extracted_text)

            if not profile:

                await status.edit(
                    content=(
                        "❌ **Spieler-ID konnte nicht erkannt werden.**\n\n"
                        f"```text\n{extracted_text[:1500]}\n```"
                    )
                )
                return

            player_id = profile["id"]
            name = profile["name"]
            alliance = profile["alliance"]
            kingdom = profile["kingdom"]

            # -------------------------------------------------
            # Datenbank aktualisieren
            # -------------------------------------------------

            try:

                with sqlite3.connect(
                    "db/users.sqlite",
                    timeout=30
                ) as conn:

                    conn.execute(
                        """
                        INSERT INTO users
                        (
                            fid,
                            nickname,
                            kid,
                            alliance
                        )
                        VALUES (?, ?, ?, ?)

                        ON CONFLICT(fid)
                        DO UPDATE SET

                            nickname = excluded.nickname,

                            kid = CASE
                                WHEN excluded.kid IS NOT NULL
                                THEN excluded.kid
                                ELSE users.kid
                            END,

                            alliance = CASE
                                WHEN excluded.alliance IS NOT NULL
                                THEN excluded.alliance
                                ELSE users.alliance
                            END
                        """,
                        (
                            int(player_id),
                            name,
                            (
                                int(kingdom)
                                if kingdom.isdigit()
                                else None
                            ),
                            alliance
                        )
                    )

                    conn.commit()

            except Exception:
                logger.exception(
                    "ProfileOCR: database update failed"
                )

            # -------------------------------------------------
            # Gewünschtes Format
            # -------------------------------------------------

            result_string = (
                f"{player_id},"
                f"{name},"
                f"{alliance},"
                f"{kingdom}"
            )

            # Log
            with open(
                self.log_file_path,
                "a",
                encoding="utf-8"
            ) as f:
                f.write(result_string + "\n")

            # -------------------------------------------------
            # Discord Ergebnis
            # -------------------------------------------------

            await status.edit(
                content=(
                    "✅ **Profil erkannt**\n\n"
                    "```text\n"
                    "ID,Name,Alliance,Kingdom\n"
                    f"{result_string}\n"
                    "```"
                )
            )

            logger.info(
                f"ProfileOCR result: {result_string}"
            )

        except Exception as e:

            logger.exception(
                "ProfileOCR: image processing failed"
            )

            await status.edit(
                content=(
                    "❌ **Fehler bei der Profil-OCR:**\n"
                    f"`{e}`"
                )
            )


async def setup(bot):
    await bot.add_cog(ProfileOCR(bot))
