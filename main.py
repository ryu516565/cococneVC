import os
import random
import sqlite3
import tempfile
import time
import asyncio
import shutil
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import edge_tts

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DB_NAME = "rank_system.db"
TTS_VOICE = "ja-JP-NanamiNeural"
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"
ROLE_REWARDS = {5: "ブロンズ", 10: "シルバー", 20: "ゴールド"}


def xp_for_level(level: int) -> int:
    return level * 100


conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    PRIMARY KEY (guild_id, user_id)
)
""")
conn.commit()
db_lock = asyncio.Lock()
msg_cooldowns = {}
vc_auto_guilds = set()
vc_locks = {}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def add_xp(member: discord.Member, xp_gained: int, channel=None):
    async with db_lock:
        cursor.execute("SELECT xp, level FROM users WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
        row = cursor.fetchone()
        xp, level = row if row else (0, 1)
        if row is None:
            cursor.execute("INSERT INTO users(guild_id,user_id,xp,level) VALUES(?,?,?,?)", (member.guild.id, member.id, 0, 1))
        new_xp = xp + xp_gained
        leveled = False
        while new_xp >= xp_for_level(level):
            new_xp -= xp_for_level(level)
            level += 1
            leveled = True
        cursor.execute("UPDATE users SET xp=?, level=? WHERE guild_id=? AND user_id=?", (new_xp, level, member.guild.id, member.id))
        conn.commit()
    if leveled and channel:
        await channel.send(f"🎉 {member.mention} がレベルアップ！ **Lv.{level}**")
        role_name = ROLE_REWARDS.get(level)
        if role_name:
            role = discord.utils.get(member.guild.roles, name=role_name)
            if role:
                try:
                    await member.add_roles(role)
                except discord.Forbidden:
                    pass


async def get_voice_client(guild):
    return discord.utils.get(bot.voice_clients, guild=guild)


async def tts_to_file(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="discord_tts_", suffix=".mp3")
    os.close(fd)
    try:
        communicate = edge_tts.Communicate(text=text, voice=TTS_VOICE, rate=TTS_RATE, volume=TTS_VOLUME)
        await communicate.save(path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        print(f"[TTS] file={path} size={size}")
        if size <= 0:
            raise RuntimeError("edge-ttsが空の音声ファイルを作りました")
        return path
    except Exception:
        try: os.remove(path)
        except OSError: pass
        raise


async def play_tts(guild: discord.Guild, text: str):
    if not text.strip():
        raise ValueError("読み上げる文章が空です")
    vc = await get_voice_client(guild)
    if not vc or not vc.is_connected():
        raise RuntimeError("BotがVCに接続していません。先に /vc_join を実行してください。")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpegが見つかりません。Chromebookなら `sudo apt install -y ffmpeg` を実行してください。")

    me = guild.me
    if me and vc.channel:
        perms = vc.channel.permissions_for(me)
        if not perms.speak:
            raise RuntimeError("BotにVCの「発言(Speak)」権限がありません。")

    lock = vc_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        path = await tts_to_file(text[:2000])
        source = None
        callback_error = []
        done = asyncio.Event()
        try:
            source = discord.FFmpegPCMAudio(
                path,
                executable=ffmpeg,
                before_options="-nostdin",
                options="-vn -loglevel error",
            )

            def after(error):
                if error:
                    callback_error.append(error)
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_soon_threadsafe(done.set)
                except RuntimeError:
                    pass

            print(f"[VC] playing in {vc.channel} with ffmpeg={ffmpeg}")
            vc.play(source, after=after)

            # afterコールバックだけに依存せず、実際の再生状態も監視する
            started = time.monotonic()
            while vc.is_playing() or not done.is_set():
                if callback_error:
                    raise RuntimeError(f"FFmpeg/Discord再生エラー: {callback_error[0]}")
                if time.monotonic() - started > 90:
                    vc.stop()
                    raise RuntimeError("音声再生が90秒を超えました。FFmpegまたはVC音声経路を確認してください。")
                if done.is_set():
                    break
                await asyncio.sleep(0.2)

            if callback_error:
                raise RuntimeError(f"FFmpeg/Discord再生エラー: {callback_error[0]}")
        finally:
            if vc.is_playing():
                vc.stop()
            if source:
                try: source.cleanup()
                except Exception: pass
            try: os.remove(path)
            except OSError: pass


@bot.event
async def on_ready():
    print(f"ログインしました: {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} 個のスラッシュコマンドを同期しました")
    except Exception as e:
        print(f"コマンド同期エラー: {e}")
    if not vc_xp_loop.is_running():
        vc_xp_loop.start()


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - msg_cooldowns.get(key, 0) >= 60:
        msg_cooldowns[key] = now
        await add_xp(message.author, random.randint(10, 20), message.channel)

    if message.guild.id in vc_auto_guilds and message.content.strip() and not message.content.startswith("/"):
        try:
            await play_tts(message.guild, message.content.strip())
        except Exception as e:
            print(f"[AUTO TTS ERROR] {e}")
    await bot.process_commands(message)


@tasks.loop(minutes=1)
async def vc_xp_loop():
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            members = [m for m in channel.members if not m.bot and m.voice and not m.voice.self_deaf and not m.voice.deaf]
            if len(members) >= 2:
                for member in members:
                    await add_xp(member, random.randint(10, 20))


@bot.tree.command(name="rank", description="現在のランクと経験値を確認")
async def rank(interaction: discord.Interaction):
    async with db_lock:
        cursor.execute("SELECT xp,level FROM users WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
        row = cursor.fetchone()
    xp, level = row if row else (0, 1)
    await interaction.response.send_message(f"📈 **Lv.{level}** / ✨ {xp} / {xp_for_level(level)} XP")


@bot.tree.command(name="leaderboard", description="サーバー内ランキングを表示")
async def leaderboard(interaction: discord.Interaction):
    async with db_lock:
        cursor.execute("SELECT user_id,level,xp FROM users WHERE guild_id=? ORDER BY level DESC,xp DESC LIMIT 10", (interaction.guild.id,))
        rows = cursor.fetchall()
    if not rows:
        await interaction.response.send_message("まだデータがありません。")
        return
    lines = []
    for i, (uid, level, xp) in enumerate(rows, 1):
        m = interaction.guild.get_member(uid)
        lines.append(f"**{i}.** {m.display_name if m else uid} — Lv.{level} ({xp} XP)")
    await interaction.response.send_message("🏆 **ランキング**\n" + "\n".join(lines))


@bot.tree.command(name="reset_rank", description="指定ユーザーのランクをリセット（管理者のみ）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def reset_rank(interaction: discord.Interaction, member: discord.Member):
    async with db_lock:
        cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        conn.commit()
    await interaction.response.send_message(f"✅ {member.mention} のランクをリセットしました。")


@bot.tree.command(name="vc_join", description="自分がいるVCにBotを接続")
async def vc_join(interaction: discord.Interaction):
    if not interaction.guild or not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("先に自分がボイスチャンネルに入ってください。", ephemeral=True)
        return
    await interaction.response.defer()
    channel = interaction.user.voice.channel
    try:
        me = interaction.guild.me
        if me:
            perms = channel.permissions_for(me)
            missing = []
            if not perms.connect: missing.append("接続")
            if not perms.speak: missing.append("発言(Speak)")
            if missing:
                raise RuntimeError("Botに必要な権限がありません: " + ", ".join(missing))
        vc = await get_voice_client(interaction.guild)
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            await channel.connect()
        await interaction.followup.send(f"🔊 **{channel.name}** に接続しました。`/vc_test` を実行して音声を確認してください。")
    except Exception as e:
        await interaction.followup.send(f"❌ VC接続エラー: `{e}`")


@bot.tree.command(name="vc_leave", description="BotをVCから切断")
async def vc_leave(interaction: discord.Interaction):
    vc = await get_voice_client(interaction.guild)
    if not vc:
        await interaction.response.send_message("BotはVCに接続していません。", ephemeral=True)
        return
    await vc.disconnect(force=True)
    await interaction.response.send_message("👋 VCから切断しました。")


@bot.tree.command(name="vc_say", description="入力した文章をVCで読み上げ")
async def vc_say(interaction: discord.Interaction, text: str):
    await interaction.response.defer()
    try:
        await play_tts(interaction.guild, text)
        await interaction.followup.send("🔊 読み上げ完了！")
    except Exception as e:
        print(f"[VC SAY ERROR] {repr(e)}")
        await interaction.followup.send(f"❌ 読み上げ失敗: `{e}`")


@bot.tree.command(name="vc_test", description="VCでテスト音声を再生")
async def vc_test(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        await play_tts(interaction.guild, "これはVC読み上げのテストです。聞こえれば成功です。")
        await interaction.followup.send("✅ テスト音声の再生が完了しました。")
    except Exception as e:
        print(f"[VC TEST ERROR] {repr(e)}")
        await interaction.followup.send(f"❌ テスト再生失敗: `{e}`")


@bot.tree.command(name="vc_auto", description="チャットをVCで自動読み上げ（管理者のみ）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def vc_auto(interaction: discord.Interaction, enabled: bool):
    if enabled:
        vc_auto_guilds.add(interaction.guild.id)
        await interaction.response.send_message("🔊 自動読み上げON。BotがVCに接続中なら通常メッセージを読み上げます。")
    else:
        vc_auto_guilds.discard(interaction.guild.id)
        await interaction.response.send_message("🔇 自動読み上げOFF。")


@bot.tree.command(name="vc_status", description="VC読み上げの状態を確認")
async def vc_status(interaction: discord.Interaction):
    vc = await get_voice_client(interaction.guild)
    ffmpeg = shutil.which("ffmpeg") or "見つかりません"
    channel = vc.channel.name if vc and vc.is_connected() else "未接続"
    await interaction.response.send_message(f"🔎 VC: **{channel}**\nFFmpeg: `{ffmpeg}`\n音声: `{TTS_VOICE}`")


@reset_rank.error
async def reset_rank_error(interaction, error):
    if isinstance(error, discord.app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ 管理者のみ使用できます。", ephemeral=True)


if not TOKEN:
    raise RuntimeError(".env に DISCORD_BOT_TOKEN=BotToken を設定してください。")

bot.run(TOKEN)
