import discord
from discord.ext import commands
from discord import app_commands
import re
import io
import os
import sqlite3
from datetime import datetime

try:
    from rapidocr_onnxruntime import RapidOCR
    ocr_engine = RapidOCR()
except ImportError:
    ocr_engine = None

class ProfileOCR(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.id_pattern = re.compile(r'ID\s*[:=-]?\s*(\d{7,9})', re.IGNORECASE)
        self.kid_pattern = re.compile(r'Kingdom\s*[:=-]?\s*#?(\d+)', re.IGNORECASE)
        self.alliance_pattern = re.compile(r'Alliance\s*[:=-]?\s*(.+)', re.IGNORECASE)
        self.clan_tag_pattern = re.compile(r'^\[[^\]]+\]\s*')
        self.log_file_path = "profiles_log.txt"
        
        if not os.path.exists(self.log_file_path):
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                f.write("Zeitstempel,ID,Name,Alliance,Kingdom\n")

    def get_configured_channel_id(self):
        try:
            conn = sqlite3.connect("db/settings.sqlite")
            cursor = conn.cursor()
            cursor.execute("SELECT channel_id FROM ocr_channel_settings WHERE alliance_id = 9999")
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None
        except:
            return None

    @app_commands.command(name="set_profile_channel", description="Legt den Kanal für automatische Profil-Bildanalysen fest.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_profile_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        conn = sqlite3.connect("db/settings.sqlite")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ocr_channel_settings WHERE alliance_id = 9999")
        cursor.execute("""
            INSERT INTO ocr_channel_settings (channel_id, alliance_id, auto_delete_screenshots)
            VALUES (?, 9999, 0)
        """, (channel.id,))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"📑 **Profil-OCR erfolgreich für Kanal aktiviert:** {channel.mention}", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.attachments:
            return
            
        if message.channel.id != self.get_configured_channel_id():
            return

        status_msg = await message.channel.send("Analysiere Profil-Screenshot... 📑")

        try:
            image_bytes = await message.attachments[0].read()
            extracted_text = ""

            if ocr_engine:
                result, _ = ocr_engine(image_bytes)
                if result: extracted_text = "\n".join([line[1] for line in result])
            else:
                from PIL import Image
                import pytesseract
                extracted_text = pytesseract.image_to_string(Image.open(io.BytesIO(image_bytes)), lang='eng+deu')

            lines = [line.strip() for line in extracted_text.split('\n') if line.strip()]
            detected_id, detected_kid, detected_name, detected_alliance = "Unbekannt", "Unbekannt", "Unbekannt", "None"

            for i, line in enumerate(lines):
                if self.id_pattern.search(line):
                    detected_id = self.id_pattern.search(line).group(1)
                    if i > 0: detected_name = self.clan_tag_pattern.sub('', lines[i - 1]).strip()
                if self.kid_pattern.search(line): detected_kid = self.kid_pattern.search(line).group(1)
                if self.alliance_pattern.search(line): detected_alliance = self.alliance_pattern.search(line).group(1).strip()

            if detected_id == "Unbekannt":
                await status_msg.edit(content="❌ **Fehler**: Spieler-ID konnte im Bild nicht erkannt werden.")
                return

            conn = sqlite3.connect("db/users.sqlite")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (fid, nickname, kid, alliance) VALUES (?, ?, ?, ?)
                ON CONFLICT(fid) DO UPDATE SET nickname=excluded.nickname, kid=COALESCE(excluded.kid, users.kid), alliance=COALESCE(excluded.alliance, users.alliance)
            """, (int(detected_id), detected_name, int(detected_kid) if detected_kid.isdigit() else None, detected_alliance))
            conn.commit()
            conn.close()

            result_string = f"{detected_id},{detected_name},{detected_alliance},{detected_kid}"
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                f.write(f"{timestamp},{result_string}\n")

            await status_msg.edit(content=f"✅ **Profil erkannt und protokolliert!**\n```\n{result_string}\n```")

        except Exception as e:
            await status_msg.edit(content=f"❌ Fehler bei der Bildverarbeitung: {str(e)}")

async def setup(bot):
    await bot.add_cog(ProfileOCR(bot))
