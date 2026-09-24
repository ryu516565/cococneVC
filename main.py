import os
import random
import sqlite3
import tempfile
import time
import asyncio
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import edge_tts

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

ROLE_REWARDS = {5: "ブロンズ", 10: "シルバー", 20: "ゴールド"}
DB_NAME = "rank_system.db"
TTS_VOICE = "ja-JP-NanamiNeural"
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"


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
vc_auto_channels = set()  # guild IDs where the current text channel is used for auto reading
vc_locks = {}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def add_xp(member: discord.Member, xp_gained: int, channel=None):
    async with db_lock:
        cursor.execute(
            "SELECT xp, level FROM users WHERE guild_id = ? AND user_id = ?",
            (member.guild.id, member.id),
        )
        result = cursor.fetchone()
        if result is None:
            xp, level = 0, 1
            cursor.execute(
                "INSERT INTO users (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)",
                (member.guild.id, member.id, xp, level),
            )
        else:
            xp, level = result

        new_xp = xp + xp_gained
        leveled_up = False
        while new_xp >= xp_for_level(level):
            new_xp -= xp_for_level(level)
            level += 1
            leveled_up = True

        cursor.execute(
            "UPDATE users SET xp = ?, level = ? WHERE guild_id = ? AND user_id = ?",
            (new_xp, level, member.guild.id, member.id),
        )
        conn.commit()

    if leveled_up and channel:
        await channel.send(f"🎉 {member.mention} がレベルアップしました！ **Lv.{level}**")
        if level in ROLE_REWARDS:
            role_name = ROLE_REWARDS[level]
            role = discord.utils.get(member.guild.roles, name=role_name)
            if role:
                try:
                    await member.add_roles(role)
                    await channel.send(f"🎖️ ランク報酬役職 **【{role_name}】** を付与しました！")
                except discord.Forbidden:
                    await channel.send(f"⚠️ 役職【{role_name}】の付与に失敗しました。Botの役職順位を確認してください。")


async def get_voice_client(guild: discord.Guild):
    return discord.utils.get(bot.voice_clients, guild=guild)


async def tts_to_file(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="discord_tts_")
    os.close(fd)
    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=TTS_VOICE,
            rate=TTS_RATE,
            volume=TTS_VOLUME,
        )
        await communicate.save(path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError("TTS音声ファイルが作成されませんでした")
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


async def play_tts(guild: discord.Guild, text: str):
    vc = await get_voice_client(guild)
    if not vc or not vc.is_connected():
        raise RuntimeError("BotがVCに接続していません。先に /vc_join を実行してください。")
    if not text.strip():
        raise ValueError("読み上げる文章を入力してください。")

    lock = vc_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        path = await tts_to_file(text[:2000])
        try:
            if vc.is_playing():
                vc.stop()
            source = discord.FFmpegPCMAudio(path, options="-vn")
            finished = asyncio.Event()
            error_box = []

            def after(error):
                if error:
                    error_box.append(error)
                bot.loop.call_soon_threadsafe(finished.set)

            vc.play(source, after=after)
            try:
                await asyncio.wait_for(finished.wait(), timeout=120)
            except asyncio.TimeoutError:
                vc.stop()
                raise RuntimeError("音声再生が120秒でタイムアウトしました。FFmpegまたは音声接続を確認してください。")
            if error_box:
                raise RuntimeError(f"Discord音声再生エラー: {error_box[0]}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


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
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    user_id = message.author.id
    now = time.time()
    key = (message.guild.id, user_id)
    if now - msg_cooldowns.get(key, 0) >= 60:
        msg_cooldowns[key] = now
        await add_xp(message.author, random.randint(10, 20), message.channel)

    # /vc_auto をONにしたサーバーでは通常メッセージも読み上げる
    if message.guild.id in vc_auto_channels and not message.content.startswith("/"):
        text = message.content.strip()
        if text and len(text) <= 2000:
            try:
                await play_tts(message.guild, text)
            except Exception as e:
                print(f"自動読み上げエラー: {e}")

    await bot.process_commands(message)


@tasks.loop(minutes=1)
async def vc_xp_loop():
    for guild in bot.guilds:
        for vc_channel in guild.voice_channels:
            active_members = [
                m for m in vc_channel.members
                if not m.bot and m.voice and not m.voice.self_deaf and not m.voice.deaf
            ]
            if len(active_members) >= 2:
                for member in active_members:
                    await add_xp(member, random.randint(10, 20))


@bot.tree.command(name="rank", description="現在のランクと経験値を確認します")
async def rank(interaction: discord.Interaction):
    async with db_lock:
        cursor.execute(
            "SELECT xp, level FROM users WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, interaction.user.id),
        )
        result = cursor.fetchone()
    xp, level = result if result else (0, 1)
    embed = discord.Embed(title=f"{interaction.user.display_name} のステータス", color=discord.Color.blue())
    embed.add_field(name="レベル", value=f"📈 **Lv.{level}**", inline=False)
    embed.add_field(name="経験値 (XP)", value=f"✨ {xp} / {xp_for_level(level)} XP", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="サーバー内のランクランキングを表示します")
async def leaderboard(interaction: discord.Interaction):
    async with db_lock:
        cursor.execute(
            "SELECT user_id, level, xp FROM users WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 10",
            (interaction.guild.id,),
        )
        rows = cursor.fetchall()
    if not rows:
        await interaction.response.send_message("まだデータがありません。")
        return
    embed = discord.Embed(title="🏆 ランクリーダーボード", color=discord.Color.gold())
    for index, (user_id, level, xp) in enumerate(rows, start=1):
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else f"ユーザー({user_id})"
        embed.add_field(name=f"{index}位: {name}", value=f"Lv.{level} ({xp} XP)", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="reset_rank", description="指定ユーザーのランクをリセットします（管理者のみ）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def reset_rank(interaction: discord.Interaction, member: discord.Member):
    async with db_lock:
        cursor.execute("DELETE FROM users WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id))
        conn.commit()
    await interaction.response.send_message(f"✅ {member.mention} のランクをリセットしました。")


@bot.tree.command(name="vc_join", description="あなたがいるボイスチャンネルにBotを接続します")
async def vc_join(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使用してください。", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("先に自分がボイスチャンネルに入ってください。", ephemeral=True)
        return
    await interaction.response.defer()
    channel = interaction.user.voice.channel
    try:
        vc = await get_voice_client(interaction.guild)
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            await channel.connect()
        await interaction.followup.send(f"🔊 **{channel.name}** に接続しました！\n`/vc_say` で文章を読み上げられます。")
    except Exception as e:
        await interaction.followup.send(f"❌ VC接続に失敗しました: `{e}`")


@bot.tree.command(name="vc_leave", description="Botをボイスチャンネルから切断します")
async def vc_leave(interaction: discord.Interaction):
    vc = await get_voice_client(interaction.guild)
    if not vc:
        await interaction.response.send_message("BotはVCに接続していません。", ephemeral=True)
        return
    await vc.disconnect(force=True)
    await interaction.response.send_message("👋 VCから切断しました。")


@bot.tree.command(name="vc_say", description="入力した文章をVCで日本語読み上げします")
async def vc_say(interaction: discord.Interaction, text: str):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使用してください。", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        await play_tts(interaction.guild, text)
        await interaction.followup.send("🔊 読み上げました！")
    except Exception as e:
        await interaction.followup.send(f"❌ 読み上げに失敗しました: `{e}`")


@bot.tree.command(name="vc_auto", description="通常のチャットをVCで自動読み上げします（管理者のみ）")
@discord.app_commands.checks.has_permissions(administrator=True)
async def vc_auto(interaction: discord.Interaction, enabled: bool):
    if enabled:
        vc_auto_channels.add(interaction.guild.id)
        await interaction.response.send_message("🔊 自動読み上げをONにしました。BotがVCに接続している間、メッセージを読み上げます。")
    else:
        vc_auto_channels.discard(interaction.guild.id)
        await interaction.response.send_message("🔇 自動読み上げをOFFにしました。")


@bot.tree.command(name="vc_test", description="VCでテスト音声を再生します")
async def vc_test(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        await play_tts(interaction.guild, "これはVC読み上げのテストです。正常に聞こえれば設定完了です。")
        await interaction.followup.send("✅ テスト音声を再生しました。")
    except Exception as e:
        await interaction.followup.send(f"❌ テスト再生に失敗しました: `{e}`")


@reset_rank.error
async def reset_rank_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ このコマンドはDiscord管理者のみ使用できます。", ephemeral=True)
    else:
        print(f"reset_rank error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)


@vc_auto.error
async def vc_auto_error(interaction: discord.Interaction, error):
    if isinstance(error, discord.app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ このコマンドはDiscord管理者のみ使用できます。", ephemeral=True)
    else:
        print(f"vc_auto error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)


if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("DISCORD_BOT_TOKEN を .env に設定してください。")

bot.run(TOKEN)
