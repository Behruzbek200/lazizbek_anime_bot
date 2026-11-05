# ANIME UZ PRO - one-file Telegram anime catalogue bot
# Python 3.11+, pyTelegramBotAPI 4.37.1
import os, re, io, csv, json, time, math, sqlite3, logging, threading, unicodedata, hashlib, hmac
from datetime import datetime
from html import escape
from pathlib import Path
from collections import defaultdict
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

TOKEN = os.getenv('BOT_TOKEN', 'TOKEN_BU_YERGA')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
DB_PATH = os.getenv('DB_PATH', 'animeuz.sqlite3')
Path(DB_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)
if TOKEN == 'TOKEN_BU_YERGA' or not OWNER_ID:
    raise SystemExit('bot.py boshida TOKEN va OWNER_ID ni kiriting yoki BOT_TOKEN va OWNER_ID environment variables o‘rnating.')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('animeuz')
bot = telebot.TeleBot(TOKEN, parse_mode='HTML', threaded=True, num_threads=8)
BOT_USERNAME = None
DB_LOCK = threading.RLock()
STATES = {}
STATE_LOCK = threading.RLock()
BROADCAST_LOCK = threading.Lock()
UPLOAD_LOCK = threading.RLock()
PAGE = 8


def db(sql, params=(), one=False, all=False):
    with DB_LOCK:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA busy_timeout=5000')
            cur = con.execute(sql, params)
            rows = cur.fetchall() if all else (cur.fetchone() if one else None)
            con.commit()
            return (rows if all else rows) if (one or all) else cur.lastrowid
        finally:
            con.close()


def init_db():
    with DB_LOCK:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined INTEGER, last_seen INTEGER, blocked INTEGER DEFAULT 0, banned INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS anime(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, title TEXT NOT NULL, alt TEXT DEFAULT '', description TEXT DEFAULT '', genres TEXT DEFAULT '', year TEXT DEFAULT '', rating TEXT DEFAULT '', language TEXT DEFAULT 'O‘zbekcha', status TEXT DEFAULT 'Davom etmoqda', poster TEXT, created INTEGER, views INTEGER DEFAULT 0, visible INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY AUTOINCREMENT, anime_id INTEGER NOT NULL, season INTEGER NOT NULL DEFAULT 1, episode INTEGER NOT NULL, quality TEXT DEFAULT '720p', file_id TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT 'video', added INTEGER, UNIQUE(anime_id,season,episode,quality));
        CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER, anime_id INTEGER, PRIMARY KEY(user_id, anime_id));
        CREATE TABLE IF NOT EXISTS watches(user_id INTEGER, anime_id INTEGER, season INTEGER, episode INTEGER, watched INTEGER, PRIMARY KEY(user_id,anime_id));
        CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, anime_id INTEGER, rating INTEGER, PRIMARY KEY(user_id,anime_id));
        CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, anime_id INTEGER, text TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, title TEXT, url TEXT, mode TEXT, enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS requests(user_id INTEGER, channel_id INTEGER, created INTEGER, status TEXT DEFAULT 'pending', PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS notices(user_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, created INTEGER);
        ''')
        # Mavjud bazani o'chirmasdan eski loyihadagi qism jadvalini yangilash.
        columns = {r[1] for r in con.execute('PRAGMA table_info(episodes)')}
        if 'media_type' not in columns:
            con.execute("ALTER TABLE episodes ADD COLUMN media_type TEXT NOT NULL DEFAULT 'video'")
        con.execute("INSERT OR IGNORE INTO admins(id,role) VALUES(?, 'owner')", (OWNER_ID,))
        for key, val in [('welcome','🎌 <b>ANIME UZ PRO</b>\n\nAnime nomi yoki kodini yuboring va tomosha qiling!'),('maintenance','0'),('force_sub','1'),('support','')]:
            con.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(key,val))
        # Old default value '@admin' was only a placeholder, not a verified contact.
        con.execute("UPDATE settings SET value='' WHERE key='support' AND value='@admin'")
        con.commit(); con.close()


def permanently_delete_anime(aid, admin_uid):
    """Anime va barcha unga bog‘liq yozuvlarni BIR tranzaksiyada o‘chiradi.

    Qaytaradi: {'title', 'code', 'episodes', 'favorites', 'watches',
                'ratings', 'comments'} yoki anime topilmasa None.
    """
    if not can_manage(admin_uid):
        raise PermissionError('Admin ruxsati yetarli emas')
    with DB_LOCK:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('BEGIN IMMEDIATE')
            a = con.execute('SELECT id, title, code FROM anime WHERE id=?', (aid,)).fetchone()
            if a is None:
                con.rollback()
                return None
            result = {'title': a['title'], 'code': a['code']}
            # SQLite eski jadvallarida ON DELETE CASCADE bo'lmaganligi uchun
            # barcha bog'liq ma'lumotlarni qo'lda, atomar usulda tozalaymiz.
            for table in ('episodes', 'favorites', 'watches', 'ratings', 'comments'):
                result[table] = con.execute(
                    f'DELETE FROM {table} WHERE anime_id=?', (aid,)
                ).rowcount
            deleted = con.execute('DELETE FROM anime WHERE id=?', (aid,)).rowcount
            if deleted != 1:
                raise RuntimeError('Anime o‘chirish tranzaksiyasi yakunlanmadi')
            con.execute('INSERT INTO logs(user_id,action,created) VALUES(?,?,?)',
                        (admin_uid, f"Anime BUTUNLAY O'CHIRILDI id={aid}, code={a['code']}, qismlar={result['episodes']}", now()))
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def delete_anime_confirmation(uid, aid):
    a = db('SELECT id, title, code FROM anime WHERE id=?', (aid,), one=True)
    if a is None:
        send(uid, '❌ Anime topilmadi yoki allaqachon o‘chirilgan.')
        return
    n = db('SELECT COUNT(*) AS n FROM episodes WHERE anime_id=?', (aid,), one=True)['n']
    send(uid,
         f"⚠️ <b>ANIMENI BUTUNLAY O‘CHIRISH</b>\n\n"
         f"🎌 {h(a['title'])}\n🆔 Kod: <code>{h(a['code'])}</code>\n"
         f"📺 Qismlar: {n} ta\n\n"
         'Tasdiqlasangiz anime, uning barcha qismlari, sevimlilar, '
         'ko‘rish tarixi, reytinglar va izohlar bazadan butunlay o‘chadi.\n'
         '<b>Bu amalni qaytarib bo‘lmaydi!</b>',
         kb([[('🗑 Ha, butunlay o‘chirish', f'ad:animeremoveconfirm:{aid}')],
             [('❌ Bekor qilish', 'ad:animeremovecancel')]]))


def setting(k):
    r=db('SELECT value FROM settings WHERE key=?',(k,),one=True)
    return r['value'] if r else ''

def set_setting(k,v): db('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v)))
def parse_support_username(value):
    """Return a safe public Telegram username (without @), or None."""
    raw = str(value or '').strip()
    raw = re.sub(r'^(?:https?://)?(?:www\.)?t\.me/', '', raw, flags=re.I)
    raw = raw.lstrip('@')
    # Telegram public username: 5-32 letters, digits or underscores.
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', raw):
        return None
    return raw


def contact_markup():
    username = parse_support_username(setting('support'))
    if not username:
        return None
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('✉️ Adminga yozish', url=f'https://t.me/{username}'))
    return markup


def show_contact(uid):
    username = parse_support_username(setting('support'))
    if not username:
        send(uid, '📞 Admin bilan aloqa hali sozlanmagan. Administrator /admin → ⚙️ Sozlamalar → 📞 Aloqa username bo‘limidan @username kiritishi kerak.')
        return
    send(uid, f'📞 <b>Admin bilan bog‘lanish</b>\n\n👤 <b>@{h(username)}</b>\n\nQuyidagi tugmani bosing — Telegram administratorning shaxsiy chatini ochadi.', contact_markup())


def now(): return int(time.time())
def h(v): return escape(str(v or ''))
def norm(s):
    s=unicodedata.normalize('NFKC',str(s or '')).casefold().replace('ʻ',"'").replace('‘',"'").replace('’',"'").replace('`',"'")
    return re.sub(r'\s+',' ',s).strip()
def state(uid,kind,**data):
    with STATE_LOCK: STATES[uid]={'kind':kind,**data}
def getstate(uid):
    with STATE_LOCK: return STATES.get(uid,{}).copy()
def clearstate(uid):
    with STATE_LOCK: STATES.pop(uid,None)
def role(uid):
    if uid==OWNER_ID:return 'owner'
    r=db('SELECT role FROM admins WHERE id=?',(uid,),one=True)
    return r['role'] if r else None
def isadmin(uid): return bool(role(uid))
def can_manage(uid): return role(uid) in ('owner','super','moderator')
def can_broadcast(uid): return role(uid) in ('owner','super','advertiser')
def log_action(uid,action): db('INSERT INTO logs(user_id,action,created) VALUES(?,?,?)',(uid,action,now()))
def ensure_user(u):
    if not u:return
    db('INSERT INTO users(id,name,username,joined,last_seen) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username,last_seen=excluded.last_seen', (u.id, ((u.first_name or '')+' '+(u.last_name or '')).strip(),u.username or '',now(),now()))

def kb(rows):
    markup=types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        markup.row(*[types.InlineKeyboardButton(label, callback_data=data) for label,data in row])
    return markup

def mainkb(uid):
    """Asosiy menyuda FAQAT uchta tugma. Admin aloqa — to'g'ridan-to'g'ri URL."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.row(types.InlineKeyboardButton('🔎 Anime qidirish', callback_data='menu:search'))
    markup.row(types.InlineKeyboardButton('❤️ Sevimlilarim', callback_data='menu:favorites'))
    username = parse_support_username(setting('support'))
    if username:
        markup.row(types.InlineKeyboardButton('📞 Adminga murojaat', url=f'https://t.me/{username}'))
    else:
        markup.row(types.InlineKeyboardButton('📞 Adminga murojaat', callback_data='menu:contact_unset'))
    if isadmin(uid):
        markup.row(types.InlineKeyboardButton('👑 Admin panel', callback_data='menu:admin'))
    return markup


def show_main_menu(uid, *, remove_old=False, text=None):
    """Eski ReplyKeyboard menyusini o'chiradi va 3 tugmali inline menyuni ko'rsatadi."""
    if remove_old:
        # Telegram ReplyKeyboard tugmasi havolani ocholmaydi; eski pastki klaviaturani yopamiz.
        send(uid, '✅ Menyu yangilandi.', types.ReplyKeyboardRemove())
    send(uid, text if text is not None else setting('welcome'), mainkb(uid))


def show_favorites(uid):
    rr = db('SELECT a.* FROM anime a JOIN favorites f ON f.anime_id=a.id WHERE f.user_id=? AND a.visible=1 ORDER BY a.title', (uid,), all=True)
    rows = [[(f"❤️ {a['title'][:35]}", f'anime:{a["id"]}')] for a in rr[:60]]
    rows.append([('🏠 Bosh menyu', 'menu:home')])
    send(uid, '❤️ <b>Sevimlilarim</b>' if rr else '❤️ Sevimlilar ro‘yxati hozircha bo‘sh.', kb(rows))

def send(cid,txt,markup=None,**kw):
    return bot.send_message(cid,txt,reply_markup=markup,**kw)

def edit_or_send(call, txt, markup=None):
    try:
        if call.message and call.message.content_type=='text':
            bot.edit_message_text(txt,call.message.chat.id,call.message.message_id,reply_markup=markup)
            return
    except ApiTelegramException: pass
    send(call.from_user.id,txt,markup)

def sub_missing(uid):
    if isadmin(uid) or setting('force_sub')!='1':return []
    missing=[]
    for ch in db('SELECT * FROM channels WHERE enabled=1',all=True):
        try:
            m=bot.get_chat_member(ch['id'],uid)
            if m.status in ('creator','administrator','member') or (m.status=='restricted' and getattr(m,'is_member',False)):
                continue
        except Exception as e:
            log.warning('Obuna tekshirib bo‘lmadi %s: %s',ch['id'],e)
            missing.append(ch);continue
        if ch['mode']=='request':
            r=db('SELECT status FROM requests WHERE user_id=? AND channel_id=?',(uid,ch['id']),one=True)
            if r and r['status']=='pending': continue
        missing.append(ch)
    return missing

def gate(uid):
    u=db('SELECT banned FROM users WHERE id=?',(uid,),one=True)
    if u and u['banned'] and not isadmin(uid):send(uid,'🚫 Sizga botdan foydalanish cheklangan.');return False
    if setting('maintenance')=='1' and not isadmin(uid):send(uid,'🛠 Texnik ishlar olib borilmoqda.');return False
    missing=sub_missing(uid)
    if not missing:return True
    rows=[]
    for ch in missing:
        if not ch['url']:
            rows.append([(f"⚠️ {ch['title'][:25]} (havola yo‘q)",'noop')]);continue
        rows.append([(f"📢 {ch['title'][:35]}", 'url:'+ch['url'])])
    markup=types.InlineKeyboardMarkup()
    for row in rows:
        _,url=row[0]
        if url.startswith('url:'): markup.row(types.InlineKeyboardButton(row[0][0],url=url[4:]))
    markup.row(types.InlineKeyboardButton('✅ Obunani tekshirish',callback_data='sub:check'))
    send(uid,'🔐 <b>Obuna kerak</b>\n\nOddiy kanalga obuna bo‘ling. So‘rovli kanalga esa qo‘shilish <b>so‘rovi yuborishingiz kifoya</b>. So‘rov botga kelgach ruxsat ochiladi.',markup)
    return False

def anime_card(a):
    n=db('SELECT COUNT(*) n FROM episodes WHERE anime_id=?',(a['id'],),one=True)['n']
    avg=db('SELECT ROUND(AVG(rating),1) avg,COUNT(*) n FROM ratings WHERE anime_id=?',(a['id'],),one=True)
    return (f"🎌 <b>{h(a['title'])}</b>\n🆔 Kod: <code>{h(a['code'])}</code>\n🎭 Janr: {h(a['genres'] or '—')}\n📅 Yil: {h(a['year'] or '—')}\n⭐ Reyting: {h(avg['avg'] or a['rating'] or '—')} ({avg['n']} baho)\n📺 Yuklangan video: {n}\n🎙 Til: {h(a['language'])}\n📌 Holati: {h(a['status'])}\n👁 Ko‘rishlar: {a['views']}\n\n📖 {h((a['description'] or '')[:900])}")

def anime_buttons(uid,a):
    fav=db('SELECT 1 FROM favorites WHERE user_id=? AND anime_id=?',(uid,a['id']),one=True)
    deep=f'https://t.me/{BOT_USERNAME}?start=a{a["id"]}' if BOT_USERNAME else ''
    rows=[[('▶️ Tomosha qilish',f'seasons:{a["id"]}')], [('💔 Sevimlidan olish' if fav else '❤️ Sevimliga qo‘shish',f'fav:{a["id"]}')], [('⭐ Baholash',f'rate:{a["id"]}'),('💬 Izohlar',f'comments:{a["id"]}')]]
    if deep: rows.append([('📤 Ulashish',f'share:{a["id"]}')])
    if can_manage(uid):rows.append([('📤 Video yuklash (admin)',f'ad:quickupload:{a["id"]}')])
    rows.append([('🏠 Bosh menyu', 'menu:home')])
    return kb(rows)

def show_anime(uid,aid):
    a=db('SELECT * FROM anime WHERE id=? AND visible=1',(aid,),one=True)
    if not a:send(uid,'❌ Anime topilmadi.');return
    text=anime_card(a); markup=anime_buttons(uid,a)
    if a['poster']:
        try:bot.send_photo(uid,a['poster'],caption=text[:1024],reply_markup=markup);return
        except Exception:log.exception('Poster yuborilmadi')
    send(uid,text,markup)

def list_anime(uid,kind='all',page=0,query=''):
    rows=db('SELECT * FROM anime WHERE visible=1',all=True)
    if kind=='top':rows.sort(key=lambda x:(-x['views'],-x['id']))
    elif kind=='new':rows.sort(key=lambda x:-x['id'])
    elif kind=='az':rows.sort(key=lambda x:norm(x['title']))
    elif kind=='search':
        q=norm(query)
        rows=[r for r in rows if q in norm(r['title']+' '+r['alt']+' '+r['code'])]
        rows.sort(key=lambda r:(0 if norm(r['code'])==q else 1 if norm(r['title']).startswith(q) else 2,norm(r['title'])))
    elif kind.startswith('genre:'):
        q=norm(kind[6:]); rows=[r for r in rows if q in norm(r['genres'])]
    else: rows.sort(key=lambda x:-x['id'])
    total=len(rows); page=max(0,min(page,max(0,(total-1)//PAGE)))
    subset=rows[page*PAGE:(page+1)*PAGE]
    title={'top':'🔥 TOP animelar','new':'🆕 Yangi animelar','all':'🎬 Barcha animelar','az':'🔤 A–Z','search':'🔎 Qidiruv'}.get(kind,'🎭 Janr bo‘yicha')
    output=f'<b>{title}</b>\nJami: {total} | Sahifa: {page+1}/{max(1,math.ceil(total/PAGE))}\n\n'
    for a in subset: output+=f"🎌 {h(a['title'])} — <code>{h(a['code'])}</code>\n"
    token=f'{kind}|{page}|{query}'
    # callbackData Telegram limit 64 bytes; search navigation stores query in a short in-memory state
    if kind=='search':
        state(uid,'searchpage',query=query)
        token=f'search|{page}'
    nav=[]
    if page>0:nav.append(('◀️',f'page:{kind}:{page-1}' if kind!='search' else f'page:search:{page-1}'))
    if (page+1)*PAGE<total:nav.append(('▶️',f'page:{kind}:{page+1}' if kind!='search' else f'page:search:{page+1}'))
    buttons=[[(f"🎌 {a['title'][:38]}",f'anime:{a["id"]}')] for a in subset]
    if nav:buttons.append(nav)
    if kind=='all':buttons.append([('🔤 A–Z','page:az:0'),('🎭 Janrlar','genres')])
    buttons.append([('🏠 Bosh menyu', 'menu:home')])
    send(uid,output if total else 'Hech qanday anime topilmadi.',kb(buttons))

def seasons(uid,aid):
    """Bitta video bo'lsa videoni bevosita ko'rsatadi."""
    a=db('SELECT * FROM anime WHERE id=?',(aid,),one=True)
    if not a:return
    one=db('SELECT * FROM episodes WHERE anime_id=? ORDER BY season,episode,id LIMIT 2',(aid,),all=True)
    if not one:
        send(uid,'📭 Bu animega hali video biriktirilmagan. Agar siz admin bo‘lsangiz, anime ostidagi «📤 Video yuklash» tugmasini bosing.')
        return
    if len(one)==1:
        send_video(uid,one[0])
        return
    rows=db('SELECT season, COUNT(DISTINCT episode) n FROM episodes WHERE anime_id=? GROUP BY season ORDER BY season',(aid,),all=True)
    if len(rows)==1:
        episodes(uid,aid,rows[0]['season'])
        return
    send(uid,f"🎌 <b>{h(a['title'])}</b>\n📺 Faslni tanlang:",kb([[(f"📺 {r['season']}-fasl ({r['n']} qism)",f'eps:{aid}:{r["season"]}:0')] for r in rows]+[[('🔙 Anime',f'anime:{aid}')]]))


def episodes(uid,aid,s,p=0):
    a=db('SELECT * FROM anime WHERE id=?',(aid,),one=True)
    if not a:return
    rs=db('SELECT DISTINCT episode FROM episodes WHERE anime_id=? AND season=? ORDER BY episode',(aid,s),all=True)
    nums=[r['episode'] for r in rs];p=max(0,min(p,max(0,(len(nums)-1)//20)))
    buttons=[]
    selection=nums[p*20:(p+1)*20]
    for i in range(0,len(selection),4):buttons.append([(f'{e}-qism',f'play:{aid}:{s}:{e}') for e in selection[i:i+4]])
    nav=[]
    if p:nav.append(('◀️',f'eps:{aid}:{s}:{p-1}'))
    if (p+1)*20<len(nums):nav.append(('▶️',f'eps:{aid}:{s}:{p+1}'))
    if nav:buttons.append(nav)
    buttons.append([('🔙 Fasllar',f'seasons:{aid}')])
    buttons.append([('🏠 Bosh menyu', 'menu:home')])
    send(uid,f"🎌 {h(a['title'])}\n📺 {s}-fasl | Qismni tanlang:",kb(buttons))

def play(uid,aid,s,e):
    a=db('SELECT * FROM anime WHERE id=?',(aid,),one=True)
    vs=db('SELECT * FROM episodes WHERE anime_id=? AND season=? AND episode=? ORDER BY quality',(aid,s,e),all=True)
    if not a or not vs:send(uid,'Video topilmadi.');return
    if len(vs)>1:
        send(uid,'📹 Video sifatini tanlang:',kb([[(f"📹 {v['quality']}",f'video:{v["id"]}')] for v in vs]));return
    send_video(uid,vs[0])

def send_video(uid,v):
    """Telegram video, yoki video sifatida yuklangan faylni yuborish."""
    a=db('SELECT * FROM anime WHERE id=?',(v['anime_id'],),one=True)
    if not a:return
    eps=db('SELECT DISTINCT episode FROM episodes WHERE anime_id=? AND season=? ORDER BY episode',(a['id'],v['season']),all=True)
    nums=[x['episode'] for x in eps];index=nums.index(v['episode']) if v['episode'] in nums else 0
    nav=[]
    if index>0:nav.append(('⏮ Oldingi',f'play:{a["id"]}:{v["season"]}:{nums[index-1]}'))
    if index+1<len(nums):nav.append(('Keyingi ⏭',f'play:{a["id"]}:{v["season"]}:{nums[index+1]}'))
    markup=kb(([nav] if nav else [])+[[('📑 Barcha qismlar',f'eps:{a["id"]}:{v["season"]}:0')], [('🎌 Anime ma’lumoti',f'anime:{a["id"]}')], [('🏠 Bosh menyu','menu:home')]])
    caption=f"🎌 <b>{h(a['title'])}</b>\n📺 {v['season']}-fasl | {v['episode']}-qism\n📹 {h(v['quality'])}\n🍿 Yoqimli tomosha!"
    try:
        if 'media_type' in v.keys() and v['media_type']=='document':
            # Telegramning document file_id qiymati send_video uchun ishlamaydi.
            bot.send_document(uid,v['file_id'],caption=caption,reply_markup=markup)
        else:
            bot.send_video(uid,v['file_id'],caption=caption,reply_markup=markup,supports_streaming=True)
    except Exception:
        log.exception('Video yuborish xatosi (qism id=%s)',v['id'])
        send(uid,'❌ Video yuborilmadi. Admin videoni qayta yuklasin. Video to‘g‘ridan-to‘g‘ri ijro etilishi uchun faylni Telegramda «Video» ko‘rinishida yuboring.')
        return
    db('INSERT INTO watches(user_id,anime_id,season,episode,watched) VALUES(?,?,?,?,?) ON CONFLICT(user_id,anime_id) DO UPDATE SET season=excluded.season,episode=excluded.episode,watched=excluded.watched',(uid,a['id'],v['season'],v['episode'],now()))
    db('UPDATE anime SET views=views+1 WHERE id=?',(a['id'],))


def uploaded_media(m):
    """Faqat video va video-mime hujjatlar; qayta yuborish uchun Telegram file_id."""
    if m.content_type=='video' and m.video:
        return (m.video.file_id,'video')
    if m.content_type=='document' and m.document:
        name=(m.document.file_name or '').lower()
        mime=(m.document.mime_type or '').lower()
        if mime.startswith('video/') or name.endswith(('.mp4','.mkv','.mov','.avi','.webm','.m4v')):
            return (m.document.file_id,'document')
    return None


def save_episode(aid, season, ep, quality, file_id, media_type):
    """Videoni saqlaydi; o'chirilgan animega orphan qism biriktirishga yo'l qo'ymaydi."""
    with DB_LOCK:
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            con.execute('PRAGMA busy_timeout=5000')
            con.execute('BEGIN IMMEDIATE')
            if not con.execute('SELECT 1 FROM anime WHERE id=?', (aid,)).fetchone():
                raise ValueError('Anime bazadan o‘chirilgan. Videoni qo‘shib bo‘lmaydi.')
            con.execute('INSERT INTO episodes(anime_id,season,episode,quality,file_id,media_type,added) VALUES(?,?,?,?,?,?,?) ON CONFLICT(anime_id,season,episode,quality) DO UPDATE SET file_id=excluded.file_id,media_type=excluded.media_type,added=excluded.added', (aid,season,ep,quality,file_id,media_type,now()))
            row = con.execute('SELECT id FROM episodes WHERE anime_id=? AND season=? AND episode=? AND quality=?', (aid,season,ep,quality)).fetchone()
            con.commit()
            return row[0]
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def append_episode(aid, file_id, media_type, season=1):
    # Bitta anime uchun yangi tartib raqamini avtomatik beradi.
    with UPLOAD_LOCK:
        last=db('SELECT COALESCE(MAX(episode),0) n FROM episodes WHERE anime_id=? AND season=?',(aid,season),one=True)['n']
        ep=last+1
        vid=save_episode(aid,season,ep,'Asl sifat',file_id,media_type)
        return ep,vid


def admin_menu(uid):
    if not isadmin(uid):return
    buttons=[[('🎬 Anime boshqaruvi','adm:anime'),('📺 Qismlar boshqaruvi','adm:episodes')], [('👥 Foydalanuvchilar','adm:users'),('📊 Statistika','adm:stats')], [('📢 Xabar yuborish','adm:broadcast'),('📡 Kanallar','adm:channels')], [('🛡 Adminlar','adm:admins'),('⚙️ Sozlamalar','adm:settings')], [('📋 Loglar','adm:logs'),('💾 Backup','adm:backup')]]
    buttons.append([('🏠 Asosiy menyu', 'menu:home')])
    send(uid,'👑 <b>ANIME UZ PRO | ADMIN PANEL</b>',kb(buttons))

def menu(uid,section):
    menus={
      'anime': [('➕ Anime qo‘shish','act:addanime'),('✏️ Tahrirlash','act:editanime'),('🗑 Butunlay o‘chirish','act:deleteanime'),('📚 Barcha animelar','act:adminlist'),('🎭 Janrlar','genres')],
      'episodes':[('🎞 Oddiy video biriktirish','act:quickcode'),('➕ Qism qo‘shish','act:addepisode'),('📤 Ommaviy yuklash','act:bulk'),('🗑 Qism o‘chirish','act:delepisode'),('📚 Qismlarni ko‘rish','act:epbrowse')],
      'users':[('🔎 ID orqali qidirish','act:finduser'),('🚫 Ban / unban','act:ban'),('📥 CSV userlar','act:userscsv')],
      'broadcast':[('👥 Hammaga yuborish','act:broadcast'),('📢 Anime e’loni','act:announce')],
      'channels':[('➕ Kanal qo‘shish','act:addchannel'),('📚 Kanallar','act:channels'),('🔐 Majburiy obuna yoq/o‘chir','act:forcesub')],
      'admins':[('➕ Admin qo‘shish','act:addadmin'),('🗑 Admin o‘chirish','act:removeadmin'),('📚 Adminlar ro‘yxati','act:listadmins')],
      'settings':[('📝 Start matni','act:welcome'),('📞 Aloqa username','act:support'),('🔧 Texnik rejim','act:maintenance'),('🔔 Bildirishnoma','act:notify')]
    }
    if section=='admins' and role(uid)!='owner':send(uid,'Faqat OWNER.');return
    items=menus.get(section,[])
    send(uid,f'🛠 <b>{h(section.upper())}</b>',kb([[(lab,dat)] for lab,dat in items]+[[('🔙 Admin panel','adm:home')]]))

def ask(uid,kind,text,**args):state(uid,kind,**args);send(uid,text+'\n\nBekor qilish: /cancel')

def notify_users(text,only_subs=False):
    users=db('SELECT id FROM users WHERE blocked=0 AND banned=0',all=True)
    ok=bad=0
    for u in users:
        if only_subs:
            n=db('SELECT enabled FROM notices WHERE user_id=?',(u['id'],),one=True)
            if n and not n['enabled']:continue
        try:send(u['id'],text);ok+=1
        except Exception:bad+=1;db('UPDATE users SET blocked=1 WHERE id=?',(u['id'],))
        time.sleep(0.06)
    return ok,bad

def bulk_broadcast(message,admin):
    if not BROADCAST_LOCK.acquire(blocking=False):send(admin,'⏳ Boshqa tarqatma davom etmoqda.');return
    try:
        users=db('SELECT id FROM users WHERE blocked=0 AND banned=0',all=True)
        ok=bad=0
        for u in users:
            try:bot.copy_message(u['id'],message.chat.id,message.message_id);ok+=1
            except Exception:bad+=1
            time.sleep(0.06)
        send(admin,f'✅ Yuborildi: {ok}\n❌ Yuborilmadi: {bad}')
    finally:BROADCAST_LOCK.release()

@bot.chat_join_request_handler(func=lambda req:True)
def joined_request(req):
    channel=db('SELECT * FROM channels WHERE id=? AND enabled=1',(req.chat.id,),one=True)
    if not channel or channel['mode']!='request':return
    uid=req.from_user.id
    db("INSERT INTO requests(user_id,channel_id,created,status) VALUES(?,?,?,'pending') ON CONFLICT(user_id,channel_id) DO UPDATE SET created=excluded.created,status='pending'",(uid,req.chat.id,now()))
    try:send(uid,f"✅ <b>{h(channel['title'])}</b> kanaliga so‘rovingiz olindi. Botdan hoziroq foydalanishingiz mumkin!",mainkb(uid))
    except Exception:pass

@bot.chat_member_handler(func=lambda cm:True)
def member_changed(cm):
    ch=db('SELECT mode FROM channels WHERE id=?',(cm.chat.id,),one=True)
    if not ch or ch['mode']!='request':return
    status=cm.new_chat_member.status
    if status in ('left','kicked'):
        # Pending request could have been declined: clear cached request on explicit removal.
        db('DELETE FROM requests WHERE user_id=? AND channel_id=?',(cm.new_chat_member.user.id,cm.chat.id))

@bot.message_handler(commands=['start','menu','help','admin','cancel'])
def commands(m):
    if m.chat.type != 'private': return
    uid = m.from_user.id
    ensure_user(m.from_user)
    cmd = m.text.split()[0].split('@')[0]
    if cmd == '/cancel':
        clearstate(uid)
        show_main_menu(uid, remove_old=True, text='✅ Amal bekor qilindi.\n\n🎌 Asosiy menyu:')
        return
    if cmd == '/admin':
        admin_menu(uid)
        return
    if not gate(uid): return
    clearstate(uid)
    if cmd == '/help':
        show_contact(uid)
        return
    parts = m.text.split(maxsplit=1)
    if cmd == '/start' and len(parts)>1 and parts[1].startswith('a') and parts[1][1:].isdigit():
        send(uid, '✅ Yangi asosiy menyu uchun /menu ni bosing.', types.ReplyKeyboardRemove())
        show_anime(uid, int(parts[1][1:]))
        return
    show_main_menu(uid, remove_old=True)

@bot.message_handler(content_types=['text','video','photo','document'])
def messages(m):
    if m.chat.type!='private':return
    uid=m.from_user.id;ensure_user(m.from_user)
    st=getstate(uid)
    if st and isadmin(uid) and st['kind'].startswith('admin:'):
        admin_input(m,st);return
    if st and st['kind']=='comment' and m.content_type=='text':
        if not gate(uid):return
        txt=m.text.strip()
        if 3<=len(txt)<=500:
            if not db('SELECT 1 FROM anime WHERE id=? AND visible=1',(st['aid'],),one=True):
                clearstate(uid);send(uid,'❌ Bu anime o‘chirilgan.');return
            db('INSERT INTO comments(user_id,anime_id,text,created) VALUES(?,?,?,?)',(uid,st['aid'],txt,now()))
            clearstate(uid);send(uid,'✅ Izoh qo‘shildi.')
        else:send(uid,'3–500 belgili izoh yozing.')
        return
    # Admin videoni hech qaysi menyuga kirmasdan yuborsa ham qabul qilish.
    if isadmin(uid) and m.content_type in ('video','document'):
        media=uploaded_media(m)
        if media:
            state(uid,'admin:uploaded_choose_anime',file_id=media[0],media_type=media[1])
            send(uid,'✅ <b>Video qabul qilindi!</b>\n\nQaysi animega qo‘shamiz? <b>Anime kodini</b> yuboring.\n\nBot avtomatik 1-faslga keyingi qism qilib biriktiradi.\nBekor qilish: /cancel')
        else:
            send(uid,'❌ Bu fayl video emas. MP4 yoki videofayl yuboring.')
        return
    if m.content_type!='text':return
    t=m.text.strip()
    if t=='🛠 Admin panel' and isadmin(uid):admin_menu(uid);return
    if not gate(uid):return
    # Yangi interfeys inline, ammo foydalanuvchi eski tugmalarini bosgan bo'lsa ham ishlaydi.
    if t in ('🔎 Anime qidirish',):
        send(uid, '🔎 Anime nomi yoki kodini yuboring.\n\n🏠 /menu — bosh menyu.')
    elif t in ('❤️ Sevimlilarim',):
        show_favorites(uid)
    elif t in ('📞 Adminga murojaat', '📞 Admin bilan bog‘lanish', '📞 Yordam'):
        # Eski pastki keyboard URL ni bevosita ocholmaydi. Yangi /menu inline URL beradi.
        show_main_menu(uid, remove_old=True, text='📞 Admin bilan bog‘lanish uchun quyidagi tugmani bosing:')
    elif t in ('🛠 Admin panel',) and isadmin(uid):
        admin_menu(uid)
    else:
        a = db('SELECT * FROM anime WHERE visible=1 AND code=?', (t,), one=True)
        if a: show_anime(uid, a['id'])
        else: list_anime(uid, 'search', query=t)

@bot.callback_query_handler(func=lambda c:True)
def callbacks(c):
    uid=c.from_user.id;ensure_user(c.from_user)
    data=c.data or ''
    try:bot.answer_callback_query(c.id)
    except Exception:pass
    if data=='sub:check':
        if gate(uid):show_main_menu(uid, text='✅ Obuna tekshirildi!\n\n🎌 Asosiy menyu:')
        return
    if data.startswith(('adm:','act:','ad:')):
        if not isadmin(uid):return
        admin_callbacks(c,data);return
    if not gate(uid): return
    if data.startswith('menu:'):
        action = data.split(':',1)[1]
        if action == 'home': show_main_menu(uid)
        elif action == 'admin' and isadmin(uid): admin_menu(uid)
        elif action == 'search': send(uid, '🔎 Anime nomi yoki kodini yuboring.\n\n🏠 /menu — bosh menyu.')
        elif action == 'favorites': show_favorites(uid)
        elif action == 'contact_unset': send(uid, '📞 Admin aloqa username hali sozlanmagan. Administrator /admin → ⚙️ Sozlamalar → 📞 Aloqa username bo‘limida sozlashi mumkin.')
        return
    try:
        x=data.split(':');tag=x[0]
        if tag=='anime':show_anime(uid,int(x[1]))
        elif tag=='seasons':seasons(uid,int(x[1]))
        elif tag=='eps':episodes(uid,int(x[1]),int(x[2]),int(x[3]))
        elif tag=='play':play(uid,int(x[1]),int(x[2]),int(x[3]))
        elif tag=='video':
            v=db('SELECT * FROM episodes WHERE id=?',(int(x[1]),),one=True)
            if v:send_video(uid,v)
        elif tag=='fav':
            aid=int(x[1])
            if not db('SELECT 1 FROM anime WHERE id=? AND visible=1',(aid,),one=True):
                send(uid,'❌ Bu anime o‘chirilgan.');return
            r=db('SELECT 1 FROM favorites WHERE user_id=? AND anime_id=?',(uid,aid),one=True)
            if r:db('DELETE FROM favorites WHERE user_id=? AND anime_id=?',(uid,aid));send(uid,'💔 Sevimlilardan olib tashlandi.')
            else:db('INSERT OR IGNORE INTO favorites(user_id,anime_id) VALUES(?,?)',(uid,aid));send(uid,'❤️ Sevimlilarga qo‘shildi.')
        elif tag=='rate':send(uid,'⭐ Baholang:',kb([[('⭐'*i,f'vote:{x[1]}:{i}')] for i in range(1,6)]))
        elif tag=='vote':
            aid=int(x[1])
            if not db('SELECT 1 FROM anime WHERE id=? AND visible=1',(aid,),one=True):
                send(uid,'❌ Bu anime o‘chirilgan.');return
            value=int(x[2]);db('INSERT INTO ratings(user_id,anime_id,rating) VALUES(?,?,?) ON CONFLICT(user_id,anime_id) DO UPDATE SET rating=excluded.rating',(uid,aid,value));send(uid,'✅ Bahongiz saqlandi.')
        elif tag=='comments':
            rr=db('SELECT c.text,u.name FROM comments c LEFT JOIN users u ON u.id=c.user_id WHERE c.anime_id=? ORDER BY c.id DESC LIMIT 10',(int(x[1]),),all=True)
            txt='💬 <b>So‘nggi izohlar</b>\n\n'+('\n\n'.join(f"👤 {h(r['name'])}: {h(r['text'])}" for r in rr) if rr else 'Hali izoh yo‘q.')
            send(uid,txt[:3800],kb([[('✍️ Izoh yozish',f'comment:{x[1]}')]]))
        elif tag=='comment':
            aid=int(x[1])
            if not db('SELECT 1 FROM anime WHERE id=? AND visible=1',(aid,),one=True):
                send(uid,'❌ Bu anime o‘chirilgan.');return
            state(uid,'comment',aid=aid);send(uid,'✍️ 3–500 belgili izohingizni yozing. /cancel — bekor qilish.')
        elif tag=='share':send(uid,f"🔗 https://t.me/{BOT_USERNAME}?start=a{x[1]}")
        elif tag=='notify':
            cur=db('SELECT enabled FROM notices WHERE user_id=?',(uid,),one=True)
            val=0 if not cur or cur['enabled'] else 1
            db('INSERT INTO notices(user_id,enabled) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled',(uid,val));send(uid,'✅ Bildirishnoma '+('yoqildi' if val else 'o‘chirildi'))
        elif tag=='genres':
            names=set()
            for a in db('SELECT genres FROM anime WHERE visible=1',all=True):names.update(x.strip() for x in (a['genres'] or '').split(',') if x.strip())
            send(uid,'🎭 Janrni tanlang:',kb([[(g[:35],f'page:genre:{g[:32]}:0')] for g in sorted(names)][:50]))
        elif tag=='page':
            if x[1]=='search':
                st=getstate(uid);list_anime(uid,'search',int(x[2]),st.get('query',''))
            elif x[1]=='genre':list_anime(uid,'genre:'+x[2],int(x[3]))
            else:list_anime(uid,x[1],int(x[2]))
    except Exception:log.exception('Callback xatosi');send(uid,'❌ Amal bajarilmadi, qayta urinib ko‘ring.')


def admin_callbacks(c,data):
    uid=c.from_user.id;parts=data.split(':');op=parts[0]
    if op=='adm':
        if parts[1]=='home':admin_menu(uid)
        elif parts[1]=='stats':
            counts={}
            for label,sql in [('👥 Foydalanuvchi','SELECT COUNT(*) n FROM users'),('🟢 Bugun faol',f'SELECT COUNT(*) n FROM users WHERE last_seen>={now()-86400}'),('🎬 Anime','SELECT COUNT(*) n FROM anime'),('📺 Qismlar','SELECT COUNT(*) n FROM episodes'),('👁 Tomoshalar','SELECT COALESCE(SUM(views),0) n FROM anime')]:counts[label]=db(sql,one=True)['n']
            send(uid,'📊 <b>STATISTIKA</b>\n\n'+'\n'.join(f'{k}: {v}' for k,v in counts.items()))
        elif parts[1]=='logs':
            rr=db('SELECT * FROM logs ORDER BY id DESC LIMIT 20',all=True)
            send(uid,'📋 <b>LOG</b>\n'+'\n'.join(f"{h(r['user_id'])}: {h(r['action'])[:90]}" for r in rr))
        elif parts[1]=='backup':
            if role(uid)!='owner':return
            with DB_LOCK:
                src=sqlite3.connect(DB_PATH);mem=io.BytesIO();dst=sqlite3.connect(':memory:');src.backup(dst)
                # portable temporary DB, then send file
                tmp=Path(DB_PATH).with_name('animeuz_backup.sqlite3');disk=sqlite3.connect(str(tmp));dst.backup(disk);disk.close();dst.close();src.close()
                with tmp.open('rb') as f:bot.send_document(uid,f,visible_file_name='animeuz_backup.sqlite3')
                tmp.unlink(missing_ok=True)
        else:menu(uid,parts[1])
        return
    if op=='ad':
        action=parts[1]
        if action=='channeldelete' and role(uid) in ('owner','super'):
            db('DELETE FROM channels WHERE id=?',(int(parts[2]),));db('DELETE FROM requests WHERE channel_id=?',(int(parts[2]),));send(uid,'✅ Kanal o‘chirildi.')
        elif action=='channeltoggle' and role(uid) in ('owner','super'):
            db('UPDATE channels SET enabled=1-enabled WHERE id=?',(int(parts[2]),));send(uid,'✅ Holat o‘zgartirildi.')
        elif action in ('animehide','animeremove') and can_manage(uid):
            delete_anime_confirmation(uid, int(parts[2]))
        elif action=='animeremoveconfirm' and can_manage(uid):
            try:
                result = permanently_delete_anime(int(parts[2]), uid)
                if result is None:
                    send(uid,'❌ Anime allaqachon o‘chirilgan.')
                else:
                    send(uid, f"✅ <b>ANIME BUTUNLAY O‘CHIRILDI!</b>\n🎌 {h(result['title'])}\n🆔 Kod: {h(result['code'])}\n\n"
                         f"📺 Qismlar: {result['episodes']}\n❤️ Sevimlilar: {result['favorites']}\n"
                         f"📺 Ko‘rish tarixi: {result['watches']}\n⭐ Reytinglar: {result['ratings']}\n"
                         f"💬 Izohlar: {result['comments']}",
                         kb([[('👑 Admin panel','adm:home')]]))
            except Exception:
                log.exception('Anime butunlay o‘chirishda xato')
                send(uid,'❌ O‘chirish amalga oshmadi, bazadagi ma’lumotlar saqlab qolindi. Logni tekshiring.')
        elif action=='animeremovecancel' and can_manage(uid):
            send(uid,'✅ O‘chirish bekor qilindi.',kb([[('👑 Admin panel','adm:home')]]))
        elif action=='epdelete' and can_manage(uid):db('DELETE FROM episodes WHERE id=?',(int(parts[2]),));send(uid,'✅ Video o‘chirildi.')
        elif action in ('bulkfinish','quickfinish') and can_manage(uid):
            st=getstate(uid)
            aid=st.get('aid')
            clearstate(uid)
            send(uid,'✅ Video yuklash yakunlandi.')
            if aid:show_anime(uid,aid)
        elif action=='quickupload' and can_manage(uid):
            aid=int(parts[2]);a=db('SELECT id,title FROM anime WHERE id=?',(aid,),one=True)
            if not a:send(uid,'❌ Anime topilmadi.');return
            ask(uid,'admin:quick_video',f'🎞 <b>{h(a["title"])}</b>\n\nVideo yoki MP4 faylni yuboring. Bot uni avtomatik keyingi qism sifatida biriktiradi. Bir nechta video yuborishingiz mumkin.\n\nTugatish: ✅ Yakunlash',aid=aid,count=0)
        elif action=='confirmbroadcast' and can_broadcast(uid):
            st=getstate(uid)
            if st.get('kind')=='admin:broadcast_confirm':
                clearstate(uid);send(uid,'📢 Tarqatma boshlandi.')
                threading.Thread(target=bulk_broadcast,args=(st['message'],uid),daemon=True).start()
        elif action=='announce' and can_manage(uid):
            aid=int(parts[2]);a=db('SELECT * FROM anime WHERE id=?',(aid,),one=True)
            if a:
                link=f'https://t.me/{BOT_USERNAME}?start=a{aid}'
                threading.Thread(target=lambda:send(uid,'✅ Natija: %s / %s'%notify_users(f"🆕 <b>Yangi anime!</b>\n🎌 {h(a['title'])}\n🆔 Kod: {h(a['code'])}\n🔗 {link}",True)),daemon=True).start()
                send(uid,'📢 E’lon navbatga qo‘yildi.')
        return
    act=parts[1]
    if act in ('addanime','editanime','deleteanime','adminlist','addepisode','bulk','delepisode','epbrowse','announce','quickcode') and not can_manage(uid):return
    if act in ('addchannel','forcesub','channels') and role(uid) not in ('owner','super'):return
    if act in ('addadmin','removeadmin','listadmins') and role(uid)!='owner':return
    if act=='addanime':ask(uid,'admin:add_title','🎌 Anime nomini yozing:')
    elif act=='editanime':ask(uid,'admin:edit_code','✏️ Anime kodi yoki ID sini yuboring:')
    elif act=='deleteanime':ask(uid,'admin:delete_code','🗑 Butunlay o‘chiriladigan anime kodini yuboring (tasdiqlash oynasi chiqadi):')
    elif act=='adminlist':list_anime(uid)
    elif act=='quickcode':ask(uid,'admin:quick_code','🎞 Anime kodini yuboring. Keyin video yoki MP4 faylni yuborishingiz mumkin:')
    elif act=='addepisode':ask(uid,'admin:ep_code','📺 Qaysi anime? Kodini yuboring:')
    elif act=='bulk':ask(uid,'admin:bulk_code','📤 Ommaviy yuklash uchun anime kodini yuboring:')
    elif act=='delepisode':ask(uid,'admin:delete_ep_code','🗑 Anime kodini yuboring:')
    elif act=='epbrowse':ask(uid,'admin:epbrowse','📚 Anime kodini yuboring:')
    elif act=='finduser':ask(uid,'admin:finduser','👤 User ID sini yuboring:')
    elif act=='ban':ask(uid,'admin:ban','🚫 Ban/unban uchun user ID sini yuboring:')
    elif act=='userscsv':
        rr=db('SELECT id,name,username,joined,last_seen,banned FROM users',all=True)
        out=io.StringIO();w=csv.writer(out);w.writerow(['id','name','username','joined_timestamp','last_seen_timestamp','banned'])
        for r in rr:w.writerow(list(r))
        data=io.BytesIO(out.getvalue().encode('utf-8-sig'));data.name='users.csv';bot.send_document(uid,data)
    elif act=='broadcast':
        if can_broadcast(uid):ask(uid,'admin:broadcast','📢 Yuboriladigan xabar, rasm yoki videoni yuboring:')
    elif act=='announce':ask(uid,'admin:announce','🎬 E’lon qilinadigan anime kodini yuboring:')
    elif act=='addchannel':ask(uid,'admin:channel_id','📡 Kanal ID sini (-100...) yoki @usernamesini yuboring. YOKI kanaldan bitta postni forward qiling (forward manbasi yashirilmagan bo‘lsin).\n\nXUSUSIY kanal uchun: botni administrator qiling, keyin ID yoki forward post yuboring.\nSo‘rovli kanalda botga «Add subscribers / Invite users» huquqini bering.')
    elif act=='channels':
        rr=db('SELECT * FROM channels',all=True)
        for ch in rr:
            send(uid,f"📡 {h(ch['title'])}\nID: <code>{ch['id']}</code>\nRejim: {ch['mode']}\nHolat: {'✅' if ch['enabled'] else '❌'}\n{h(ch['url'])}",kb([[('🔁 Faollik',f'ad:channeltoggle:{ch["id"]}'),('🗑 O‘chirish',f'ad:channeldelete:{ch["id"]}')]]))
        if not rr:send(uid,'Kanal yo‘q.')
    elif act=='forcesub':
        set_setting('force_sub','0' if setting('force_sub')=='1' else '1');send(uid,'🔐 Majburiy obuna: '+('ON' if setting('force_sub')=='1' else 'OFF'))
    elif act=='addadmin':ask(uid,'admin:addadmin','🛡 Yangi admin ID sini yuboring:')
    elif act=='removeadmin':ask(uid,'admin:removeadmin','🛡 O‘chiriladigan admin ID sini yuboring:')
    elif act=='listadmins':
        rr=db('SELECT * FROM admins',all=True);send(uid,'🛡 ADMINLAR\n'+'\n'.join(f"<code>{r['id']}</code> — {r['role']}" for r in rr))
    elif act=='welcome':ask(uid,'admin:setting:welcome','📝 Start uchun yangi matnni yuboring:')
    elif act=='support':
        if role(uid) not in ('owner','super'):
            send(uid, '❌ Kontaktni faqat OWNER yoki SUPER ADMIN o‘zgartira oladi.')
            return
        current = parse_support_username(setting('support'))
        ask(uid, 'admin:setting:support',
            '📞 <b>Administratorning Telegram username qiymatini yuboring.</b>\n\n'
            'Masalan: <code>@BehruzbekAdmin</code> yoki <code>https://t.me/BehruzbekAdmin</code>\n'
            f'Hozirgi: <b>{"@" + h(current) if current else "sozlanmagan"}</b>\n\n'
            'ℹ️ Username Telegram → Sozlamalar → Username bo‘limida ommaviy bo‘lishi kerak.')
    elif act=='maintenance':set_setting('maintenance','0' if setting('maintenance')=='1' else '1');send(uid,'🛠 Texnik rejim: '+setting('maintenance'))
    elif act=='notify':send(uid,'🔔 Anime qo‘shilganda admin paneldan «📢 Anime e’loni» funksiyasini tanlang.')

def admin_input(m,st):
    uid=m.from_user.id;k=st['kind'];t=(m.text or '').strip()
    try:
        if k=='admin:add_title':ask(uid,'admin:add_code','🆔 Anime kodini yuboring. Takrorlanmas bo‘lsin:',title=t[:150]);return
        if k=='admin:add_code':
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,25}',t):send(uid,'Kod 1–25 ta lotin harfi, raqam, _ yoki - dan iborat bo‘lsin.');return
            if db('SELECT 1 FROM anime WHERE code=?',(t,),one=True):send(uid,'❌ Bu kod band. Boshqasini yuboring.');return
            ask(uid,'admin:add_alt','🔠 Qo‘shimcha nomlar (ruscha/inglizcha) vergul bilan yoki - yuboring:',title=st['title'],code=t);return
        if k=='admin:add_alt':ask(uid,'admin:add_desc','📖 Anime tavsifini yuboring:',title=st['title'],code=st['code'],alt='' if t=='-' else t[:250]);return
        if k=='admin:add_desc':ask(uid,'admin:add_genres','🎭 Janrlar (vergul bilan):',**{x:st[x] for x in ('title','code','alt')},description=t[:1500]);return
        if k=='admin:add_genres':ask(uid,'admin:add_year','📅 Yili (masalan 2025):',**{x:st[x] for x in ('title','code','alt','description')},genres=t[:150]);return
        if k=='admin:add_year':ask(uid,'admin:add_rating','⭐ Reyting (masalan 8.5):',**{x:st[x] for x in ('title','code','alt','description','genres')},year=t[:20]);return
        if k=='admin:add_rating':ask(uid,'admin:add_status','📌 Holati: Yakunlangan yoki Davom etmoqda:',**{x:st[x] for x in ('title','code','alt','description','genres','year')},rating=t[:20]);return
        if k=='admin:add_status':ask(uid,'admin:add_poster','🖼 Poster rasmini yuboring yoki - deb yozing:',**{x:st[x] for x in ('title','code','alt','description','genres','year','rating')},status=t[:50]);return
        if k=='admin:add_poster':
            if m.content_type!='photo' and t!='-':send(uid,'Rasm yuboring yoki - yozing.');return
            poster=m.photo[-1].file_id if m.content_type=='photo' else None
            aid=db('INSERT INTO anime(code,title,alt,description,genres,year,rating,status,poster,created) VALUES(?,?,?,?,?,?,?,?,?,?)',(st['code'],st['title'],st['alt'],st['description'],st['genres'],st['year'],st['rating'],st['status'],poster,now()))
            clearstate(uid);log_action(uid,f'Anime yaratildi {aid}');send(uid,f'✅ Anime qo‘shildi!\n🆔 {h(st["code"])}',kb([[('🎞 Video yuklash',f'ad:quickupload:{aid}'),('📢 E’lon',f'ad:announce:{aid}')]]));return
        if k=='admin:uploaded_choose_anime':
            # Admin avval videoni yuborganida: keyin anime kodini so'rash.
            a=db('SELECT * FROM anime WHERE code=? OR CAST(id AS TEXT)=?',(t,t),one=True)
            if not a:send(uid,'❌ Anime topilmadi. To‘g‘ri anime kodini yuboring.');return
            ep,vid=append_episode(a['id'],st['file_id'],st['media_type'])
            clearstate(uid)
            log_action(uid,f'Video biriktirildi {a["id"]}: 1-fasl {ep}-qism')
            send(uid,f'✅ <b>VIDEO SAQLANDI!</b>\n🎌 {h(a["title"])}\n🆔 Kod: {h(a["code"])}\n📺 1-fasl, {ep}-qism\n\n▶️ Endi «Tomosha qilish» ishlaydi.',kb([[('▶️ Videoni tekshirish',f'video:{vid}')], [('🎌 Anime sahifasi',f'anime:{a["id"]}')], [('📤 Yana video yuklash',f'ad:quickupload:{a["id"]}')]]))
            return
        if k=='admin:quick_code':
            a=db('SELECT * FROM anime WHERE code=? OR CAST(id AS TEXT)=?',(t,t),one=True)
            if not a:send(uid,'❌ Anime topilmadi, kodni qayta kiriting.');return
            ask(uid,'admin:quick_video',f'🎞 <b>{h(a["title"])}</b>\n\nEndi video yuboring. Bot 1-faslga avtomatik biriktiradi.\nTugatish uchun «✅ Yakunlash» tugmasini bosing.',aid=a['id'],count=0)
            return
        if k=='admin:quick_video':
            media=uploaded_media(m)
            if not media:
                send(uid,'❌ Video yoki MP4 fayl yuboring. Agar Telegram uni «Document» sifatida yuborsa, foydalanuvchi fayl ko‘rinishida oladi.');return
            ep,vid=append_episode(st['aid'],media[0],media[1])
            state(uid,'admin:quick_video',aid=st['aid'],count=st.get('count',0)+1)
            send(uid,f'✅ {ep}-qism saqlandi! Jami shu sessiyada: {st.get("count",0)+1}\n\n📺 Video tomoshabinlarga ko‘rinadi.',kb([[('▶️ Videoni tekshirish',f'video:{vid}')], [('🎌 Anime sahifasi',f'anime:{st["aid"]}')], [('✅ Yakunlash','ad:quickfinish')]]))
            return
        if k in ('admin:ep_code','admin:bulk_code','admin:delete_ep_code','admin:epbrowse','admin:edit_code','admin:delete_code','admin:announce'):
            a=db('SELECT * FROM anime WHERE code=? OR CAST(id AS TEXT)=?',(t,t),one=True)
            if not a:send(uid,'❌ Anime topilmadi. Kodini qayta yozing.');return
            aid=a['id']
            if k=='admin:ep_code':ask(uid,'admin:ep_season','📺 Fasl raqamini yuboring:',aid=aid)
            elif k=='admin:bulk_code':ask(uid,'admin:bulk_season','📺 Fasl raqamini yuboring:',aid=aid)
            elif k=='admin:delete_ep_code':
                rr=db('SELECT * FROM episodes WHERE anime_id=? ORDER BY season,episode',(aid,),all=True)
                clearstate(uid);send(uid,'🗑 Video variantini tanlang:',kb([[(f"S{r['season']} E{r['episode']} {r['quality']}",f'ad:epdelete:{r["id"]}')] for r in rr[:80]]) if rr else None)
            elif k=='admin:epbrowse':clearstate(uid);seasons(uid,aid)
            elif k=='admin:delete_code':clearstate(uid);delete_anime_confirmation(uid,aid)
            elif k=='admin:announce':clearstate(uid);send(uid,'Tasdiqlang:',kb([[('📢 E’lon yuborish',f'ad:announce:{aid}')]]))
            else:ask(uid,'admin:edit_field',f"✏️ {h(a['title'])}\nO‘zgartirish maydoni: title / alt / description / genres / year / rating / status / code / language / poster",aid=aid)
            return
        if k=='admin:edit_field':
            if t not in ('title','alt','description','genres','year','rating','status','code','language','poster'):send(uid,'❌ Noto‘g‘ri maydon.');return
            ask(uid,'admin:edit_value',f'✏️ {t} yangi qiymatini yuboring'+(' (rasm)' if t=='poster' else ''),aid=st['aid'],field=t);return
        if k=='admin:edit_value':
            field=st['field'];val=m.photo[-1].file_id if field=='poster' and m.content_type=='photo' else t
            if field=='poster' and m.content_type!='photo':send(uid,'🖼 Rasm yuboring.');return
            if field=='code' and not re.fullmatch(r'[A-Za-z0-9_-]{1,25}',val):send(uid,'❌ Kod formati xato.');return
            db(f'UPDATE anime SET {field}=? WHERE id=?',(val,st['aid']));clearstate(uid);log_action(uid,f'Anime tahrir {st["aid"]} {field}');send(uid,'✅ O‘zgartirildi.');return
        if k in ('admin:ep_season','admin:bulk_season'):
            n=int(t)
            if not 1<=n<=999:raise ValueError()
            kind='admin:ep_number' if k=='admin:ep_season' else 'admin:bulk_start'
            ask(uid,kind,'🔢 Qism raqami (ommaviy uchun boshlang‘ich):',aid=st['aid'],season=n);return
        if k in ('admin:ep_number','admin:bulk_start'):
            n=int(t)
            if not 1<=n<=99999:raise ValueError()
            kind='admin:ep_quality' if k=='admin:ep_number' else 'admin:bulk_quality'
            ask(uid,kind,'📹 Sifat (480p/720p/1080p):',aid=st['aid'],season=st['season'],episode=n);return
        if k in ('admin:ep_quality','admin:bulk_quality'):
            kind='admin:ep_video' if k=='admin:ep_quality' else 'admin:bulk_video'
            ask(uid,kind,'📥 Video(lar)ni yuboring. Ommaviy rejimda tugatish uchun /cancel emas, «✅ Yakunlash» tugmasini bosing.',aid=st['aid'],season=st['season'],episode=st['episode'],quality=t[:20],count=0);return
        if k in ('admin:ep_video','admin:bulk_video'):
            media=uploaded_media(m)
            if not media:send(uid,'❌ Video yoki MP4 fayl yuboring.');return
            ep=st['episode']
            vid=save_episode(st['aid'],st['season'],ep,st['quality'],*media)
            if k=='admin:ep_video':
                clearstate(uid)
                send(uid,f'✅ {ep}-qism yuklandi. Endi anime sahifasida «Tomosha qilish» ishlaydi.',kb([[('▶️ Videoni tekshirish',f'video:{vid}')], [('🎌 Anime sahifasi',f'anime:{st["aid"]}')]]))
            else:
                state(uid,'admin:bulk_video',aid=st['aid'],season=st['season'],episode=ep+1,quality=st['quality'],count=st['count']+1)
                send(uid,f"✅ {ep}-qism saqlandi. Jami: {st['count']+1}",kb([[('✅ Yakunlash','ad:bulkfinish')]]))
            return
        if k=='admin:channel_id':
            origin=getattr(m,'forward_origin',None)
            origin_chat=getattr(origin,'chat',None) if origin else None
            channel_hint=(origin_chat.id if origin_chat and getattr(origin_chat,'type',None)=='channel' else None)
            if not channel_hint and getattr(m,'forward_from_chat',None) and m.forward_from_chat.type=='channel':
                channel_hint=m.forward_from_chat.id
            if not channel_hint and not t:
                send(uid,'Kanal ID / @username yozing yoki kanaldan bitta postni forward qiling.');return
            chat=bot.get_chat(channel_hint or (int(t) if t.lstrip('-').isdigit() else t))
            me=bot.get_chat_member(chat.id,bot.get_me().id)
            if me.status not in ('creator','administrator'):send(uid,'❌ Botni kanalga administrator qiling, keyin qayta urinib ko‘ring.');return
            ask(uid,'admin:channel_mode',f"✅ Kanal: {h(chat.title)}\nRejim yozing: oddiy yoki sorov\n\nSo‘rovli rejimga can_invite_users huquqi kerak.",channel_id=chat.id,title=chat.title or t,username=chat.username or '');return
        if k=='admin:channel_mode':
            if norm(t) not in ('oddiy','sorov',"so'rov",'request'):send(uid,'oddiy yoki sorov deb yozing.');return
            mode='request' if norm(t)!='oddiy' else 'normal'
            if mode=='request':
                me=bot.get_chat_member(st['channel_id'],bot.get_me().id)
                if not getattr(me,'can_invite_users',False):send(uid,'❌ So‘rovlarni olish uchun botga «Invite users» admin huquqini bering.');return
                try:
                    link=bot.create_chat_invite_link(st['channel_id'],name='ANIMEUZ_REQUEST',creates_join_request=True).invite_link
                except Exception as e:send(uid,f'❌ So‘rov havolasi yaratilmadi: {h(e)}');return
            else:
                link=f'https://t.me/{st["username"]}' if st['username'] else ''
                if not link:
                    try:link=bot.create_chat_invite_link(st['channel_id'],name='ANIMEUZ_JOIN').invite_link
                    except Exception:send(uid,'❌ Xususiy kanal uchun havola yaratish huquqini bering.');return
            db('INSERT INTO channels(id,title,url,mode) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,url=excluded.url,mode=excluded.mode,enabled=1',(st['channel_id'],st['title'],link,mode))
            clearstate(uid);log_action(uid,f'Kanal qo‘shildi {st["channel_id"]} {mode}');send(uid,'✅ Kanal qo‘shildi!\n'+h(link));return
        if k=='admin:finduser':
            u=db('SELECT * FROM users WHERE id=?',(int(t),),one=True)
            clearstate(uid)
            if not u:send(uid,'Topilmadi.');return
            fav=db('SELECT COUNT(*) n FROM favorites WHERE user_id=?',(u['id'],),one=True)['n']
            send(uid,f"👤 <b>{h(u['name'])}</b>\n🆔 <code>{u['id']}</code>\n@{h(u['username'])}\n📅 {datetime.fromtimestamp(u['joined']).strftime('%d.%m.%Y')}\n❤️ Sevimlilar: {fav}\n🚫 Ban: {u['banned']}");return
        if k=='admin:ban':
            target=int(t)
            if isadmin(target):send(uid,'❌ Adminni bloklash mumkin emas.');return
            r=db('SELECT banned FROM users WHERE id=?',(target,),one=True)
            if not r:send(uid,'User topilmadi.');return
            db('UPDATE users SET banned=? WHERE id=?',(0 if r['banned'] else 1,target));clearstate(uid);send(uid,'✅ Holat o‘zgardi.');return
        if k=='admin:broadcast':
            state(uid,'admin:broadcast_confirm',message=m)
            send(uid,'📢 Xabarni barcha foydalanuvchilarga yuborishni tasdiqlaysizmi?',kb([[('✅ Yuborish','ad:confirmbroadcast')]]));return
        if k=='admin:addadmin':db("INSERT INTO admins(id,role) VALUES(?,'moderator') ON CONFLICT(id) DO UPDATE SET role='moderator'",(int(t),));clearstate(uid);send(uid,'✅ Moderator qo‘shildi.');return
        if k=='admin:removeadmin':
            if int(t)==OWNER_ID:send(uid,'❌ OWNER o‘chirilmaydi.');return
            db('DELETE FROM admins WHERE id=?',(int(t),));clearstate(uid);send(uid,'✅ Admin o‘chirildi.');return
        if k.startswith('admin:setting:'):
            field = k.split(':')[2]
            if field == 'support':
                if role(uid) not in ('owner', 'super'):
                    clearstate(uid);send(uid, '❌ Bu sozlamani o‘zgartirishga ruxsat yo‘q.');return
                username = parse_support_username(t)
                if not username:
                    send(uid, '❌ Username noto‘g‘ri. Faqat 5–32 ta lotin harfi, raqam va _ belgisi ishlatiladi.\nMasalan: <code>@BehruzbekAdmin</code>\n\nQaytadan yuboring yoki /cancel bosing.')
                    return
                set_setting('support', '@' + username)
                clearstate(uid)
                log_action(uid, 'Aloqa username yangilandi: @' + username)
                send(uid, f'✅ <b>Admin kontakti saqlandi!</b>\n\n👤 @{h(username)}\n\nEndi foydalanuvchilar shu akkauntga yozishi mumkin.', contact_markup())
                return
            if field == 'welcome':
                if m.content_type != 'text' or not t:
                    send(uid, '❌ Start xabariga matn yuboring.');return
                set_setting('welcome', t[:3000]);clearstate(uid);send(uid,'✅ Start matni saqlandi.');return
            clearstate(uid);send(uid, '❌ Noma’lum sozlama.');return
    except ValueError:send(uid,'❌ To‘g‘ri raqam kiriting.')
    except sqlite3.IntegrityError:send(uid,'❌ Bu kod yoki ma’lumot oldin mavjud.')
    except Exception as e:log.exception('Admin input %s',k);send(uid,'❌ Xato: '+h(str(e))[:250])

@bot.inline_handler(func=lambda query:True)
def inline_search(q):
    try:
        term=norm(q.query)
        rr=db('SELECT * FROM anime WHERE visible=1 ORDER BY views DESC LIMIT 200',all=True)
        if term:rr=[a for a in rr if term in norm(a['title']+' '+a['alt']+' '+a['code'])]
        results=[]
        for a in rr[:30]:
            desc=f"Kod: {a['code']} | {a['genres']}"
            content=types.InputTextMessageContent(f"🎌 <b>{h(a['title'])}</b>\n🆔 Kod: <code>{h(a['code'])}</code>\n🔗 https://t.me/{BOT_USERNAME}?start=a{a['id']}",parse_mode='HTML')
            if a['poster'] and str(a['poster']).startswith('https://'):
                results.append(types.InlineQueryResultArticle(id=str(a['id']),title=a['title'],description=desc,input_message_content=content,thumbnail_url=a['poster']))
            else:results.append(types.InlineQueryResultArticle(id=str(a['id']),title=a['title'],description=desc,input_message_content=content))
        bot.answer_inline_query(q.id,results,cache_time=15,is_personal=False)
    except Exception:log.exception('Inline search')

ALLOWED_UPDATES = ['message', 'callback_query', 'chat_join_request', 'chat_member', 'inline_query']


def run_webhook():
    """Render Web Service: bitta bot.py ichida Telegram webhook va HTTP server."""
    from flask import Flask, request, jsonify
    from waitress import serve

    base_url = (os.getenv('WEBHOOK_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
    if not base_url.startswith('https://'):
        raise RuntimeError('Webhook uchun HTTPS manzil kerak: RENDER_EXTERNAL_URL yoki WEBHOOK_URL.')
    route = '/telegram-webhook'
    url = base_url if base_url.endswith(route) else base_url + route
    # Telegram secret_token faqat [A-Za-z0-9_-] ishlata oladi.
    secret = os.getenv('WEBHOOK_SECRET') or hashlib.sha256(TOKEN.encode('utf-8')).hexdigest()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
        raise RuntimeError('WEBHOOK_SECRET qiymatida faqat A-Z, a-z, 0-9, _ va - bo‘lishi kerak.')
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024

    @app.get('/')
    @app.get('/health')
    def health():
        return jsonify(status='ok', service='animeuz-bot')

    @app.post(route)
    def telegram_webhook():
        provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(provided, secret):
            return 'Forbidden', 403
        if not request.is_json:
            return 'Invalid Content-Type', 415
        try:
            update = types.Update.de_json(request.get_data(as_text=True))
            bot.process_new_updates([update])
        except Exception:
            log.exception('Webhook yangilanishida xato')
            return 'Error', 500
        return 'OK', 200

    bot.set_webhook(url=url, secret_token=secret, allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
    log.info('Webhook sozlandi: %s', url)
    port = int(os.getenv('PORT', '10000'))
    serve(app, host='0.0.0.0', port=port, threads=8)


if __name__=='__main__':
    init_db()
    BOT_USERNAME = bot.get_me().username
    log.info('Bot @%s ishga tushdi.', BOT_USERNAME)
    mode = os.getenv('BOT_MODE', 'webhook' if os.getenv('RENDER_EXTERNAL_URL') else 'polling').strip().lower()
    if mode == 'webhook':
        run_webhook()
    elif mode == 'polling':
        try:
            bot.remove_webhook()
        except Exception:
            log.warning('Oldingi webhook o‘chirib bo‘lmadi; polling urinishi davom etadi.')
        bot.infinity_polling(timeout=30, long_polling_timeout=30,
                             allowed_updates=ALLOWED_UPDATES, skip_pending=False)
    else:
        raise SystemExit('BOT_MODE faqat webhook yoki polling bo‘lishi mumkin.')
