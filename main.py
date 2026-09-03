import os
import sys
import json
import gc
import asyncio
import shutil
from urllib.parse import parse_qs, urlparse

# Paksa stdout UTF-8 agar emoji log tidak crash di console Windows (cp1252)
try:
    if sys.stdout and sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =========================================================
# 1. KONFIGURASI (ENV dulu, fallback config.json lokal)
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "/")

config = {}
if os.path.exists("config.json"):
    try:
        with open("config.json", "r") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[Config] gagal baca config.json: {e}")

if not DISCORD_TOKEN:
    DISCORD_TOKEN = config.get("discord_token")
if PREFIX == "/" and config.get("prefix"):
    PREFIX = config.get("prefix", "/")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN kosong. Isi .env atau config.json lalu regenerate token lama yang bocor.")

# FFMPEG: env -> PATH -> Winget fallback (Windows) -> "ffmpeg" (Linux DisCloud)
def _find_ffmpeg():
    if os.getenv("FFMPEG_PATH"):
        return os.getenv("FFMPEG_PATH")
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.name == "nt":
        import glob as _glob
        base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*")
        for hit in sorted(_glob.glob(os.path.join(base, "ffmpeg-*", "bin", "ffmpeg.exe"))):
            if os.path.isfile(hit):
                return hit
    return "ffmpeg"


FFMPEG_PATH = _find_ffmpeg()

# =========================================================
# 2. BOT + INTENTS MINIMAL (diet RAM 100MB, slash-only)
# =========================================================
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    member_cache_flags=discord.MemberCacheFlags.none(),
    chunk_guilds_at_startup=False,
    max_messages=None,
)

# Server pribadi untuk sync slash instan (global butuh s.d. 1 jam)
GUILD_ID = int(os.getenv("GUILD_ID", "938470036862029945"))

# =========================================================
# 3. YT-DLP ANTI BOT (cookies + rotasi client + retry)
# =========================================================
COOKIE_FILE = "cookies.txt"
HAS_COOKIES = os.path.exists(COOKIE_FILE)

YDL_BASE = {
    "format": "bestaudio[abr<=96]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "force_ipv4": True,
    "retries": 2,
    "fragment_retries": 2,
    "socket_timeout": 12,
    "sleep_interval": 1,
    "max_sleep_interval": 3,
}

# Urutan sesuai hasil tes lokal 2026: web_safari+cookies terbukti mengembalikan
# audio (m3u8), tv tanpa cookies kena SABR-only, android tidak mendukung cookies.
CLIENT_ROTATION = ["web_safari", "tv", "android"]

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -sn -dn",
}

queues = {}
MAX_QUEUE = 5
IDLE_TIMEOUT = 120
_idle_tasks = {}


def extract_clean_url_or_query(query):
    try:
        parsed_url = urlparse(query)
        if "youtube.com" in parsed_url.netloc:
            query_params = parse_qs(parsed_url.query)
            if "v" in query_params:
                return f"https://www.youtube.com/watch?v={query_params['v'][0]}"
        elif "youtu.be" in parsed_url.netloc:
            video_id = parsed_url.path.lstrip("/").split("?")[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        print(f"[Error Parsing URL]: {e}")
    return query


def _extract_sync(target, is_url, client, use_cookies):
    """Blocking yt-dlp, dijalankan via asyncio.to_thread."""
    opts = dict(YDL_BASE)
    if use_cookies and HAS_COOKIES:
        opts["cookiefile"] = COOKIE_FILE
    opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if is_url and info:
            if info.get("_type") == "playlist" and info.get("entries"):
                video = info["entries"][0]
                if video.get("url"):
                    return {"source": video["url"], "title": video.get("title", "Audio")}
                return None
            if info.get("url"):
                return {"source": info["url"], "title": info.get("title", "Audio")}
            return None
        elif not is_url and info and info.get("entries"):
            video = info["entries"][0]
            if video.get("url"):
                return {"source": video["url"], "title": video.get("title", "Audio")}
    return None


async def get_stream_info(query):
    target = extract_clean_url_or_query(query)
    is_url = target.startswith("http://") or target.startswith("https://")
    if not is_url:
        target = f"ytsearch1:{target}"

    last_err = None
    # Search (ytsearch1) lambat per attempt: cukup 1x web_safari+cookies.
    # URL langsung: rotasi penuh sebagai fallback.
    if not is_url and HAS_COOKIES:
        attempts = [("web_safari", True)]
    else:
        attempts = [
            ("web_safari", True),
            ("tv", False),
            ("android", False),
        ]
    for client, use_cookies in attempts:
        if use_cookies and not HAS_COOKIES:
            continue
        try:
            result = await asyncio.to_thread(_extract_sync, target, is_url, client, use_cookies)
            if result:
                gc.collect()
                return result
        except Exception as e:
            last_err = e
            msg = str(e)
            print(f"[yt-dlp:{client}] {msg[:300]}")
            if "Sign in to confirm" in msg or "not a bot" in msg:
                continue  # coba client berikutnya
            await asyncio.sleep(1)
            continue
    if last_err:
        print(f"[yt-dlp] semua client gagal: {str(last_err)[:300]}")
    gc.collect()
    return None


async def play_next_async(ctx):
    guild_id = ctx.guild.id
    if guild_id not in queues or not queues[guild_id]:
        return
    next_query = queues[guild_id].pop(0)
    song_info = await get_stream_info(next_query)
    if not song_info:
        try:
            await ctx.send("❌ Gagal mengekstrak lagu berikutnya (kemungkinan bot-check YouTube / cookies basi).")
        except Exception:
            pass
        if queues[guild_id]:
            await play_next_async(ctx)
        return
    try:
        source = discord.FFmpegPCMAudio(song_info["source"], executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
        ctx.voice_client.play(source, after=lambda e: schedule_next(ctx, e))
        await ctx.send(f"🎵 Memutar berikutnya: **{song_info['title']}**")
    except Exception as e:
        print(f"[play_next] {e}")
        try:
            await ctx.send("❌ Gagal memutar lagu berikutnya.")
        except Exception:
            pass


def schedule_next(ctx, error=None):
    if error:
        print(f"[voice after] {error}")
    if ctx.guild.id in queues and queues[ctx.guild.id]:
        asyncio.run_coroutine_threadsafe(play_next_async(ctx), bot.loop)
    else:
        schedule_idle_leave(ctx)


def schedule_idle_leave(ctx):
    async def _leave():
        await asyncio.sleep(IDLE_TIMEOUT)
        try:
            vc = ctx.guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused():
                await vc.disconnect()
                queues.get(ctx.guild.id, []).clear()
        except Exception:
            pass
        finally:
            _idle_tasks.pop(ctx.guild.id, None)

    old = _idle_tasks.get(ctx.guild.id)
    if old:
        old.cancel()
    _idle_tasks[ctx.guild.id] = asyncio.run_coroutine_threadsafe(_leave(), bot.loop)


def cancel_idle_leave(guild_id):
    t = _idle_tasks.pop(guild_id, None)
    if t:
        t.cancel()


# =========================================================
# 4. EVENTS & SLASH COMMANDS (YouTube only)
# =========================================================
class _InteractionCtx:
    """Adapter agar helper voice (ctx.guild/voice_client/send) bisa dipakai dari slash."""

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.guild = interaction.guild
        self.author = interaction.user

    @property
    def voice_client(self):
        return self.guild.voice_client if self.guild else None

    async def send(self, *args, **kwargs):
        try:
            return await self.interaction.followup.send(*args, **kwargs)
        except Exception:
            pass
        try:  # fallback kalau token interaction basi (>15 mnt): kirim ke channel
            channel = getattr(self.interaction, "channel", None)
            if channel:
                return await channel.send(*args, **kwargs)
        except Exception:
            pass


@bot.event
async def on_ready():
    print("==========================================")
    print(f"✅ Bot online sebagai: {bot.user.name}")
    print(f"   FFMPEG: {FFMPEG_PATH} | Cookies: {HAS_COOKIES} | Guild: {GUILD_ID}")
    print("==========================================")
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"   Slash sync guild {GUILD_ID}: {len(synced)} command")
    except Exception as e:
        print(f"[tree sync guild] {e}")
    try:
        glob_synced = await bot.tree.sync()
        print(f"   Slash sync global: {len(glob_synced)} command")
    except Exception as e:
        print(f"[tree sync global] {e}")


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: Exception):
    print(f"[slash error] {interaction.command.name if interaction.command else '?'}: {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Error: `{str(error)[:200]}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Error: `{str(error)[:200]}`", ephemeral=True)
    except Exception:
        pass


@bot.tree.command(name="play", description="Putar lagu dari YouTube (link / judul)")
@app_commands.describe(query="Judul lagu atau link YouTube")
async def play_slash(interaction: discord.Interaction, query: str):
    if not interaction.guild:
        return await interaction.response.send_message("❌ Hanya bisa dipakai di server.", ephemeral=True)
    voice_state = getattr(interaction.user, "voice", None)
    if not voice_state or not voice_state.channel:
        return await interaction.response.send_message(
            "❌ Kamu harus join Voice Channel dulu!", ephemeral=True
        )
    await interaction.response.defer()
    ctx = _InteractionCtx(interaction)

    channel = voice_state.channel
    vc = interaction.guild.voice_client
    if not vc:
        await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    cancel_idle_leave(interaction.guild.id)
    guild_id = interaction.guild.id
    queues.setdefault(guild_id, [])

    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        if len(queues[guild_id]) >= MAX_QUEUE:
            return await ctx.send(f"❌ Antrean penuh (max {MAX_QUEUE}). Skip dulu dengan `/skip`.")
        clean_query = extract_clean_url_or_query(query)
        queues[guild_id].append(clean_query)
        return await ctx.send(f"📝 Antrean #{len(queues[guild_id])}: <{clean_query}>")

    song_info = await get_stream_info(query)
    if not song_info:
        if not HAS_COOKIES:
            return await ctx.send("❌ Gagal memutar. `cookies.txt` tidak ditemukan di server — upload dulu.")
        return await ctx.send(
            "❌ Gagal memutar (YouTube bot-check / cookies basi). "
            "Coba link langsung atau refresh `cookies.txt`."
        )
    try:
        source = discord.FFmpegPCMAudio(song_info["source"], executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
        ctx.voice_client.play(source, after=lambda e: schedule_next(ctx, e))
        await ctx.send(f"🎵 Sedang memutar: **{song_info['title']}**")
    except Exception as e:
        print(f"[play] {e}")
        await ctx.send("❌ Gagal memutar audio (FFmpeg error).")


@bot.tree.command(name="skip", description="Skip lagu yang sedang diputar")
async def skip_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # after-callback akan memutar antrean
        await interaction.response.send_message("⏭️ Dilompati.")
    else:
        await interaction.response.send_message("❌ Tidak ada lagu yang diputar.", ephemeral=True)


@bot.tree.command(name="queue", description="Lihat antrean lagu")
async def queue_slash(interaction: discord.Interaction):
    q = queues.get(interaction.guild.id, []) if interaction.guild else []
    if not q:
        return await interaction.response.send_message("📭 Antrean kosong.", ephemeral=True)
    lines = [f"{i+1}. <{url}>" for i, url in enumerate(q[:MAX_QUEUE])]
    await interaction.response.send_message("📝 **Antrean:**\n" + "\n".join(lines))


@bot.tree.command(name="stop", description="Stop musik dan keluarkan bot dari VC")
async def stop_slash(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in queues:
        queues[guild_id].clear()
    cancel_idle_leave(guild_id)
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("🛑 Berhenti dan keluar.")
    else:
        await interaction.response.send_message("❌ Bot tidak di Voice Channel.", ephemeral=True)


# =========================================================
# 5. RUN
# =========================================================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
