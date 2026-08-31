import os
import json
import asyncio
import discord
from discord.ext import commands
from urllib.parse import parse_qs, urlparse
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from flask import Flask
from threading import Thread

# =========================================================
# 0. FLASK WEB SERVER FOR KEEP-ALIVE (RENDER COMPATIBILITY)
# =========================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot Online 24/7!"

def run():
    # Render secara otomatis menyediakan variabel port di environment
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# =========================================================
# 1. LOAD KONFIGURASI DARI CONFIG.JSON
# =========================================================
with open('config.json', 'r') as f:
    config = json.load(f)

DISCORD_TOKEN = config['discord_token']
PREFIX = config.get('prefix', '!')

# JALUR FFMPEG (Auto-detect Linux/Render vs Windows)
if os.name == 'nt':  # Windows (Laptop Lokal)
    FFMPEG_PATH = r"C:\Users\MARS\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
else:  # Linux (Render.com / Cloud Hosting)
    FFMPEG_PATH = "ffmpeg"

# =========================================================
# 2. INISIALISASI BOT DISCORD & SPOTIFY CLIENT
# =========================================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

spotify_id = config.get('spotify_client_id')
spotify_secret = config.get('spotify_client_secret')

if spotify_id and spotify_secret:
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=spotify_id,
        client_secret=spotify_secret
    ))
else:
    sp = None

# =========================================================
# 3. KONFIGURASI AUDIO (ANDROID/MWEB PLAYER CLIENT)
# =========================================================
YDL_OPTIONS = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'source_address': '0.0.0.0',
    'skip_download': True,
    'no_warnings': True,
    # Menggunakan emulasi android/mweb untuk bypass proteksi bot YouTube
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'mweb']
        }
    }
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -sn -dn'
}

queues = {}

# =========================================================
# 4. HELPER FUNCTIONS
# =========================================================
def parse_spotify_url(url):
    """Mengekstrak nama lagu dan artis dari link Spotify."""
    try:
        if 'track' in url and sp:
            track = sp.track(url)
            track_name = track['name']
            artist_name = track['artists'][0]['name']
            return f"{track_name} {artist_name}"
    except Exception as e:
        print(f"[Error Spotify]: {e}")
    return None

def extract_clean_url_or_query(query):
    """Mengubah link YouTube menjadi URL bersih tanpa playlist."""
    try:
        parsed_url = urlparse(query)
        if "youtube.com" in parsed_url.netloc:
            query_params = parse_qs(parsed_url.query)
            if 'v' in query_params:
                return f"https://www.youtube.com/watch?v={query_params['v'][0]}"
        elif "youtu.be" in parsed_url.netloc:
            video_id = parsed_url.path.lstrip('/')
            return f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        print(f"[Error Parsing URL]: {e}")
    return query

def get_stream_info(query):
    """Mengekstrak direct stream URL dan Judul lagu secara presisi."""
    target = extract_clean_url_or_query(query)
    is_url = target.startswith("http://") or target.startswith("https://")

    if not is_url:
        target = f"ytsearch1:{target}"

    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(target, download=False)
            
            if is_url and info:
                return {'source': info['url'], 'title': info.get('title', 'Audio')}
            elif not is_url and info and 'entries' in info and len(info['entries']) > 0:
                video = info['entries'][0]
                return {'source': video['url'], 'title': video['title']}

        except Exception as e:
            print(f"[Error yt-dlp]: {e}")
    return None

def play_next(ctx):
    """Memutar lagu berikutnya yang ada di antrean."""
    guild_id = ctx.guild.id
    if guild_id in queues and len(queues[guild_id]) > 0:
        next_query = queues[guild_id].pop(0)
        song_info = get_stream_info(next_query)

        if song_info:
            source = discord.FFmpegPCMAudio(song_info['source'], executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
            ctx.voice_client.play(source, after=lambda e: play_next(ctx))
            asyncio.run_coroutine_threadsafe(
                ctx.send(f"🎵 Memutar berikutnya: **{song_info['title']}**"),
                bot.loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                ctx.send("❌ Gagal mengekstrak lagu berikutnya."),
                bot.loop
            )
            play_next(ctx)

# =========================================================
# 5. BOT EVENTS & COMMANDS
# =========================================================
@bot.event
async def on_ready():
    print("==========================================")
    print(f"✅ Bot telah online sebagai: {bot.user.name}")
    print("==========================================")

@bot.command(name='play', aliases=['p'], help='Memutar lagu dari YouTube atau link Spotify')
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        return await ctx.send("❌ Kamu harus bergabung ke Voice Channel terlebih dahulu!")

    channel = ctx.author.voice.channel
    if not ctx.voice_client:
        await channel.connect()

    guild_id = ctx.guild.id
    if guild_id not in queues:
        queues[guild_id] = []

    async with ctx.typing():
        # Handle Spotify
        if "open.spotify.com/track" in query:
            if not sp:
                return await ctx.send("❌ Kunci Spotify API belum terpasang di `config.json`!")
            
            search_term = parse_spotify_url(query)
            if not search_term:
                return await ctx.send("❌ Gagal membaca metadata dari link Spotify.")
            query = search_term

        # Jika bot sedang memutar lagu, simpan ke antrean
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            clean_query = extract_clean_url_or_query(query)
            queues[guild_id].append(clean_query)
            return await ctx.send(f"📝 Ditambahkan ke antrean: <{clean_query}>")

        # Dapatkan stream dan putar
        song_info = get_stream_info(query)
        if not song_info:
            return await ctx.send("❌ Gagal memutar URL tersebut.")

        source = discord.FFmpegPCMAudio(song_info['source'], executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
        ctx.voice_client.play(source, after=lambda e: play_next(ctx))
        await ctx.send(f"🎵 Sedang memutar: **{song_info['title']}**")

@bot.command(name='skip', aliases=['s'], help='Melompati lagu yang sedang diputar')
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Lagu dilompati.")
    else:
        await ctx.send("❌ Tidak ada lagu yang sedang diputar.")

@bot.command(name='stop', help='Menghentikan musik dan mengeluarkan bot')
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in queues:
        queues[guild_id].clear()

    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Pemutaran dihentikan dan bot keluar.")
    else:
        await ctx.send("❌ Bot tidak sedang berada di Voice Channel.")

# =========================================================
# 6. JALANKAN WEB SERVER & BOT DISCORD
# =========================================================
keep_alive()
bot.run(DISCORD_TOKEN)