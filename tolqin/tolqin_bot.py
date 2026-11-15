"""
Mening To'lqinim — Telegram musiqa tavsiya boti.
Qo'shiqqa 👍 bossang, Last.fm shunga o'xshash treklarni topib, to'lqin qilib davom ettiradi.
Har trek uchun to'g'ridan-to'g'ri YouTube link yuboradi (bosib eshitasan).
Windows 7 / Python 3.8 da ham ishlaydi — faqat telethon, requests, python-dotenv kerak.
Layklar lokal bazada (tolqin.db) saqlanadi.
"""

import asyncio
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse

import requests
from dotenv import load_dotenv
from telethon import Button, TelegramClient, connection, events

# Windows + Telethon "attached to a different loop" xatosini oldini oladi
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------- Sozlamalar (.env fayldan o'qiladi) ----------
load_dotenv()
API_ID = int(os.environ.get('API_ID', '0').strip() or 0)
API_HASH = os.environ.get('API_HASH', '').strip()
BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
LASTFM_KEY = os.environ.get('LASTFM_KEY', '').strip()

if not all([API_ID, API_HASH, BOT_TOKEN, LASTFM_KEY]):
    raise SystemExit(
        ".env faylida API_ID, API_HASH, BOT_TOKEN yoki LASTFM_KEY to'ldirilmagan!\n"
        "Namuna uchun .env.example fayliga qarang."
    )

LASTFM_URL = 'https://ws.audioscrobbler.com/2.0/'
DB_PATH = os.environ.get('TOLQIN_DB', 'tolqin.db')
QUEUE_MIN = 2
QUEUE_TARGET = 6
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# Ixtiyoriy MTProto proxy (.env da MTPROXY=server:port:secret). VPN bo'lsa shart emas.
MTPROXY = os.environ.get('MTPROXY', '').strip()
if MTPROXY:
    try:
        _host, _port, _secret = MTPROXY.split(':')
        client = TelegramClient(
            'tolqin', API_ID, API_HASH,
            connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
            proxy=(_host, int(_port), _secret),
        )
        print(f'[Info] MTProto proxy orqali ulanmoqda: {_host}:{_port}')
    except Exception as e:
        print(f"[Ogohlantirish] MTPROXY formati noto'g'ri (server:port:secret bo'lsin): {e}")
        client = TelegramClient('tolqin', API_ID, API_HASH)
else:
    client = TelegramClient('tolqin', API_ID, API_HASH)

BUTTONS = [
    [Button.inline('👍 Yoqdi', b'like'), Button.inline('👎 Yoqmadi', b'dislike')],
    [Button.inline('⏭ Keyingisi', b'next'), Button.inline('⏹ Toʻxtat', b'stop')],
]

sessions = {}  # user_id -> holat


async def run_sync(func, *args):
    # Python 3.8 da asyncio.to_thread yo'q — run_in_executor ishlatamiz
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


# ---------- Baza ----------
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        'CREATE TABLE IF NOT EXISTS likes ('
        'user_id INTEGER, artist TEXT, title TEXT, ts INTEGER, '
        'UNIQUE(user_id, artist, title))'
    )
    con.commit()
    con.close()


def db_add_like(user_id, artist, title):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        'INSERT OR IGNORE INTO likes(user_id, artist, title, ts) VALUES(?,?,?,?)',
        (user_id, artist, title, int(time.time())),
    )
    con.commit()
    con.close()


def db_get_likes(user_id, limit=50):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        'SELECT artist, title FROM likes WHERE user_id=? ORDER BY ts DESC LIMIT ?',
        (user_id, limit),
    ).fetchall()
    con.close()
    return rows


# ---------- Last.fm ----------
def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _lastfm(params):
    params.update(api_key=LASTFM_KEY, format='json')
    resp = requests.get(LASTFM_URL, params=params, timeout=25, headers={'User-Agent': 'TolqinBot/1.0'})
    resp.raise_for_status()
    data = resp.json()
    if 'error' in data:
        raise RuntimeError(f"Last.fm xato {data['error']}: {data.get('message')}")
    return data


def lastfm_search_track(query):
    data = _lastfm({'method': 'track.search', 'track': query, 'limit': '1'})
    hits = _as_list(data.get('results', {}).get('trackmatches', {}).get('track'))
    if not hits:
        return None
    return (hits[0]['artist'], hits[0]['name'])


def lastfm_similar_tracks(artist, title, limit=20):
    data = _lastfm({'method': 'track.getSimilar', 'artist': artist,
                    'track': title, 'limit': str(limit), 'autocorrect': '1'})
    out = []
    for t in _as_list(data.get('similartracks', {}).get('track')):
        name = t.get('name')
        art = (t.get('artist') or {}).get('name')
        if name and art:
            out.append((art, name))
    return out


def lastfm_similar_artists(artist, limit=5):
    data = _lastfm({'method': 'artist.getSimilar', 'artist': artist,
                    'limit': str(limit), 'autocorrect': '1'})
    return [a['name'] for a in _as_list(data.get('similarartists', {}).get('artist')) if a.get('name')]


def lastfm_top_tracks(artist, limit=5):
    data = _lastfm({'method': 'artist.getTopTracks', 'artist': artist,
                    'limit': str(limit), 'autocorrect': '1'})
    out = []
    for t in _as_list(data.get('toptracks', {}).get('track')):
        name = t.get('name')
        if name:
            out.append(((t.get('artist') or {}).get('name') or artist, name))
    return out


# ---------- YouTube link (yuklamasdan, faqat havola) ----------
def youtube_link(artist, title):
    query = f'{artist} {title}'
    search_url = 'https://www.youtube.com/results?search_query=' + urllib.parse.quote(query)
    try:
        r = requests.get(search_url, timeout=15,
                         headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
        m = re.search(r'"videoId":"([\w-]{11})"', r.text)
        if m:
            return 'https://youtu.be/' + m.group(1)
    except Exception as e:
        print(f'[YouTube link xato]: {e}')
    return search_url  # topolmasa — qidiruv havolasi


# ---------- Sessiya ----------
def get_session(user_id):
    s = sessions.get(user_id)
    if s is None:
        s = {'queue': [], 'played': set(), 'current': None, 'lock': asyncio.Lock()}
        sessions[user_id] = s
    return s


def norm(track):
    return (track[0].lower().strip(), track[1].lower().strip())


async def ensure_queue(user_id):
    s = get_session(user_id)
    if len(s['queue']) >= QUEUE_MIN:
        return

    seeds = []
    if s['current']:
        seeds.append(s['current'])
    likes = db_get_likes(user_id, 10)
    if likes:
        seeds.append(random.choice(likes))

    for artist, title in seeds:
        try:
            sims = await run_sync(lastfm_similar_tracks, artist, title, 25)
        except Exception as e:
            print(f'[Last.fm similar xato]: {e}')
            sims = []
        random.shuffle(sims)
        for cand in sims:
            if norm(cand) in s['played'] or cand in s['queue']:
                continue
            s['queue'].append(cand)
        if len(s['queue']) >= QUEUE_TARGET:
            return

    if not s['queue'] and s['current']:
        try:
            artists = await run_sync(lastfm_similar_artists, s['current'][0], 5)
            for art in artists:
                tops = await run_sync(lastfm_top_tracks, art, 3)
                for cand in tops:
                    if norm(cand) not in s['played'] and cand not in s['queue']:
                        s['queue'].append(cand)
                if len(s['queue']) >= QUEUE_TARGET:
                    break
        except Exception as e:
            print(f'[Last.fm zaxira xato]: {e}')


async def play_track(event, user_id, artist, title):
    s = get_session(user_id)
    s['current'] = (artist, title)
    s['played'].add(norm((artist, title)))
    async with client.action(event.chat_id, 'typing'):
        link = await run_sync(youtube_link, artist, title)
    await event.respond(f'🎵 {artist} — {title}\n▶️ {link}', buttons=BUTTONS)


async def play_next(event, user_id):
    s = get_session(user_id)
    await ensure_queue(user_id)
    while s['queue']:
        cand = s['queue'].pop(0)
        if norm(cand) in s['played']:
            continue
        await play_track(event, user_id, cand[0], cand[1])
        return
    await event.respond(
        '🌊 Toʻlqin tugadi — oʻxshash trek topilmadi. '
        'Boshqa qoʻshiq nomini yozib, yangi toʻlqin boshlang.'
    )


# ---------- Buyruqlar ----------
@client.on(events.NewMessage(pattern=r'^/start'))
async def start_handler(event):
    await event.respond(
        '🌊 Mening Toʻlqinimga xush kelibsiz!\n\n'
        'Yoqtirgan qoʻshigʻingiz nomini yozing (masalan: The Weeknd Blinding Lights).\n'
        'Men uni topib, oʻxshashlaridan toʻlqin yasayman.\n\n'
        '👍 — shu yoʻnalishda davom etaman\n'
        '⏭ — keyingi trek\n'
        '⏹ — toʻxtatish\n\n'
        '/tolqin — yoqtirganlaringizdan toʻlqin\n'
        '/yoqqanlar — yoqtirgan treklaringiz'
    )


@client.on(events.NewMessage(pattern=r'^/yoqqanlar'))
async def likes_handler(event):
    rows = db_get_likes(event.sender_id, 30)
    if not rows:
        await event.respond('Hali hech narsa yoqtirmagansiz. Qoʻshiq nomini yozib boshlang.')
        return
    text = '❤️ Yoqtirgan treklaringiz:\n\n' + '\n'.join(
        f'{i}. {a} — {t}' for i, (a, t) in enumerate(rows, 1))
    await event.respond(text[:4000])


@client.on(events.NewMessage(pattern=r'^/tolqin'))
async def wave_handler(event):
    user_id = event.sender_id
    likes = db_get_likes(user_id, 20)
    if not likes:
        await event.respond('Avval biror qoʻshiq nomini yozib, bir nechtasini 👍 qiling. '
                            'Keyin /tolqin shular asosida toʻlqin yasaydi.')
        return
    s = get_session(user_id)
    s['queue'].clear()
    s['current'] = random.choice(likes)
    await event.respond('🌊 Yoqtirganlaringiz asosida toʻlqin boshlanmoqda...')
    await play_next(event, user_id)


@client.on(events.NewMessage(func=lambda e: e.is_private and e.text and not e.text.startswith('/')))
async def seed_handler(event):
    user_id = event.sender_id
    query = event.text.strip()
    s = get_session(user_id)
    if s['lock'].locked():
        await event.respond('⏳ Avvalgisi tayyor boʻlsin, biroz kuting.')
        return
    async with s['lock']:
        status = await event.respond(f'🔎 "{query}" qidirilmoqda...')
        try:
            found = await run_sync(lastfm_search_track, query)
        except Exception as e:
            print(f'[Last.fm qidiruv xato]: {e}')
            await status.edit('❌ Qidiruvda xatolik. Birozdan soʻng qayta urining.')
            return
        if not found:
            await status.edit('❌ Bunday qoʻshiq topilmadi. Nomini boshqacha yozib koʻring.')
            return
        await status.delete()
        s['queue'].clear()
        s['current'] = found
        await play_track(event, user_id, found[0], found[1])
        await ensure_queue(user_id)


# ---------- Tugmalar ----------
@client.on(events.CallbackQuery)
async def button_handler(event):
    user_id = event.sender_id
    s = get_session(user_id)
    data = event.data

    if data == b'stop':
        s['queue'].clear()
        s['current'] = None
        await event.answer('Toʻxtatildi')
        await event.respond('⏹ Toʻlqin toʻxtatildi. Yangisini boshlash uchun qoʻshiq nomini yozing.')
        return

    if s['lock'].locked():
        await event.answer('⏳ Biroz kuting', alert=False)
        return

    async with s['lock']:
        if data == b'like':
            if s['current']:
                await run_sync(db_add_like, user_id, s['current'][0], s['current'][1])
                await event.answer('👍 Saqlandi!')
            await play_next(event, user_id)
        elif data == b'dislike':
            if s['current']:
                s['played'].add(norm(s['current']))
            await event.answer('👎 Oʻtkazildi')
            await play_next(event, user_id)
        elif data == b'next':
            await event.answer('⏭')
            await play_next(event, user_id)


# ---------- Render uchun kichik web-server (port ochiq tursin) ----------
async def start_web_server():
    from aiohttp import web  # faqat Render'da kerak (PORT bo'lsa)

    async def ping(request):
        return web.Response(text='Tolqin bot ishlayapti!')

    app = web.Application()
    app.router.add_get('/', ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    print(f'[Info] Web server {port}-portda ishga tushdi')


async def main():
    db_init()
    if os.environ.get('PORT'):      # Render PORT ni o'zi beradi; lokalda bo'lmaydi
        await start_web_server()
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f'🌊 Toʻlqin bot ishga tushdi: @{me.username}')
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
