# -*- coding: utf-8 -*-
"""云音乐插件 · QQ音乐子模块（页面与登录逻辑）

由统一入口 main.py 按 src=qq 调度到本模块。API 封装在同目录 qq.py，
二维码放大解码用 qrimg.py，二维码 / 纯色图生成共用 qr.py。
零第三方依赖：只用 Python 标准库 + Kodi 内置模块。
兼容 Kodi 19 / 20 / 21（含 Xbox UWP 版，Python 3.8）。
"""
import json
import os
import sys
import threading
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_PATH = ADDON.getAddonInfo('path')
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE_URL = sys.argv[0] if len(sys.argv) > 0 else ''

import qq  # noqa: E402
import qr  # noqa: E402
import qrimg  # noqa: E402


def translate(p):
    try:
        return xbmcvfs.translatePath(p)
    except AttributeError:
        return xbmc.translatePath(p)


PROFILE = translate(ADDON.getAddonInfo('profile'))
ICON = os.path.join(ADDON_PATH, 'icon.png')
FANART = os.path.join(ADDON_PATH, 'fanart.png')
# 文件名与网易云子模块隔离：同一个插件数据目录下互不干扰
SESSION_FILE = os.path.join(PROFILE, 'qq_session.json')
HISTORY_FILE = os.path.join(PROFILE, 'qq_history.json')
CACHE_DIR = os.path.join(PROFILE, 'qq_cache')
QR_FILE = os.path.join(PROFILE, 'qrcode.png')
BG_FILE = os.path.join(PROFILE, 'skin-bg.png')
CARD_FILE = os.path.join(PROFILE, 'skin-card.png')


def _get(key, default=''):
    try:
        return ADDON.getSetting(key)
    except Exception:
        return default


def _bool(key, default=True):
    v = _get(key, '')
    if v == '':
        return default
    return v in ('true', 'True', '1', 'yes')


def _int(key, default=0):
    try:
        return int(float(_get(key, default)))
    except ValueError:
        return default


LEVEL = _get('qq_level', 'auto') or 'auto'
VERIFY_SSL = _bool('qq_verify_ssl', True)
VERIFY_URL = _bool('qq_verify_url', True)
PAGE_SIZE = _int('qq_page_size', 100) or 100
CACHE_TTL = _int('qq_cache_ttl', 30) * 60

try:
    os.makedirs(PROFILE, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
except Exception:
    pass

API = qq.QQ(timeout=15, verify_ssl=VERIFY_SSL)


# ---------------- 基础工具 ----------------

def build_url(**kw):
    kw.setdefault('src', 'qq')      # 统一入口 main.py 按 src 分流
    return BASE_URL + '?' + urllib.parse.urlencode(kw)


def notify(msg, heading='QQ音乐'):
    try:
        xbmcgui.Dialog().notification(heading, msg,
                                      xbmcgui.NOTIFICATION_INFO, 3000)
    except Exception:
        xbmc.log('[qqmusic] %s' % msg, xbmc.LOGINFO)


def _s(v):
    """Kodi 的 InfoTagMusic 只吃字符串"""
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except Exception:
            return ''
    if isinstance(v, (list, tuple)):
        return ' / '.join(_s(x) for x in v if x)
    return str(v)


def set_music_info(li, info):
    title = _s(info.get('title'))
    artist = _s(info.get('singer'))
    album = _s(info.get('album'))
    dur = int(info.get('duration') or 0)
    if hasattr(li, 'getMusicInfoTag'):
        try:
            tag = li.getMusicInfoTag()
            tag.setMediaType('song')
            tag.setTitle(title)
            # Kodi 21 的 setArtist 只接受字符串，传列表会抛异常
            if artist:
                tag.setArtist(artist.split(' / ')[0])
            if album:
                tag.setAlbum(album)
            if dur > 0:
                tag.setDuration(dur)
            return
        except Exception as e:
            xbmc.log('[qqmusic] setMusicInfoTag failed: %s' % e,
                     xbmc.LOGWARNING)
            return
    try:
        li.setInfo('music', {'title': title, 'artist': artist,
                             'album': album, 'duration': dur})
    except Exception:
        pass


def write_bytes(path, data):
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return True
    except Exception as e:
        xbmc.log('[qqmusic] write %s failed: %s' % (path, e), xbmc.LOGWARNING)
        return False


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def cache_path(key):
    import hashlib
    return os.path.join(CACHE_DIR, hashlib.md5(
        key.encode('utf-8')).hexdigest() + '.json')


def cached(key, func, ttl=None):
    """磁盘缓存；ttl 秒内直接复用"""
    ttl = CACHE_TTL if ttl is None else ttl
    p = cache_path(key)
    try:
        import time
        st = os.stat(p)
        if ttl > 0 and (time.time() - st.st_mtime) < ttl:
            v = read_json(p)
            if v is not None:
                return v
    except Exception:
        pass
    v = func()
    write_bytes(p, json.dumps(v, ensure_ascii=False).encode('utf-8'))
    return v


# ---------------- 列表渲染 ----------------

def add_folder(url, label, art=None, info=None, playable=False,
               menu=None):
    li = xbmcgui.ListItem(label=label)
    if not art:
        art = {}
    art.setdefault('fanart', FANART)
    art.setdefault('icon', ICON)
    li.setArt(art)
    if playable:
        li.setProperty('IsPlayable', 'true')
    if info:
        set_music_info(li, info)
    if menu:
        try:
            li.addContextMenuItems(menu)
        except Exception:
            pass
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=not playable)


def end(content='songs', sort=False):
    try:
        xbmcplugin.setContent(HANDLE, content)
    except Exception:
        pass
    if not sort:
        xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(HANDLE)


def dur_str(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return ''
    return '%d:%02d' % (sec // 60, sec % 60)


def size_str(n):
    n = int(n or 0)
    if n <= 0:
        return ''
    if n >= 1024 * 1024 * 1024:
        return '%.1fGB' % (n / 1073741824.0)
    return '%.1fMB' % (n / 1048576.0)


def song_label(s, idx=None):
    """列表标题：[VIP] 序号. 歌名 - 歌手  (时长)"""
    tags = []
    if s.get('pay'):
        tags.append('[COLOR FFFFB74D][VIP][/COLOR]')
    if s.get('rank'):
        tags.append('[COLOR FF4CAF50]#%s[/COLOR]' % s['rank'])
    head = '%s. ' % idx if idx else ''
    extra = ''
    mismatched = [size_str(s.get('size%s' % k))
                  for k in ('flac', '320', '128')]
    mismatched = [x for x in mismatched if x]
    if mismatched:
        extra = '  [COLOR FF90A4AE]%s[/COLOR]' % '/'.join(mismatched[:2])
    d = dur_str(s.get('duration'))
    if d:
        extra += '  [COLOR FF90A4AE]%s[/COLOR]' % d
    return '%s%s%s - %s%s' % (' '.join(tags) + ' ' if tags else '', head,
                              s.get('title') or '(无题)',
                              s.get('singer') or '未知', extra)


def render_songs(songs, page=1, more_url=None):
    start = (page - 1) * PAGE_SIZE
    chunk = songs[start:start + PAGE_SIZE]
    for i, s in enumerate(chunk, start=1):
        url = build_url(action='play', mid=s['mid'],
                        title=s.get('title', ''), album=s.get('album', ''),
                        singer=s.get('singer', ''),
                        dur=int(s.get('duration') or 0),
                        pic=s.get('pic') or '')
        info = {'title': s.get('title'), 'singer': s.get('singer'),
                'album': s.get('album'), 'duration': s.get('duration')}
        art = {'thumb': s.get('pic') or ICON, 'poster': s.get('pic') or ICON}
        menu = [('查看专辑', 'Container.Update(%s)' % build_url(
            action='album', albummid=s.get('albummid', '')))] if s.get(
            'albummid') else None
        add_folder(url, song_label(s, start + i), art=art, info=info,
                   playable=True, menu=menu)
    if more_url and start + PAGE_SIZE < len(songs):
        add_folder(more_url, '[COLOR FF64B5F6]下一页 >>[/COLOR]')
    end('songs', sort=True)


# ---------------- 登录态 ----------------

def cred_load():
    """读本地保存的登录态。返回 dict(musicid, musickey, refresh_key, nick)"""
    d = read_json(SESSION_FILE) or {}
    if d.get('musickey'):
        return d
    # 老版本只存了 cookie原样，兼容一下
    ck = d.get('cookie') or _get('qq_cookie', '')
    if ck:
        return {'musicid': '', 'musickey': '', 'cookie': ck}
    return {}


def cred_save(cred):
    old = read_json(SESSION_FILE) or {}
    if isinstance(old, dict) and old.get('cookie') and not cred.get('cookie'):
        cred['cookie'] = old.get('cookie')
    write_bytes(SESSION_FILE, json.dumps(
        cred, ensure_ascii=False).encode('utf-8'))


def cred_clear():
    write_bytes(SESSION_FILE, b'{}')


def init_session():
    cred = cred_load()
    if cred.get('musickey'):
        API.set_credential(cred.get('musicid') or '', cred.get('musickey'),
                           cred.get('refresh_key') or '')
    elif cred.get('cookie'):
        API.set_cookie(cred['cookie'])


def _migrate_old_session():
    """装过独立版 plugin.audio.qqmusic 的话，把它的登录态迁过来（只做一次）"""
    try:
        if os.path.exists(SESSION_FILE):
            return
        old = translate('special://profile/addon_data/'
                        'plugin.audio.qqmusic/session.json')
        if not old or not os.path.exists(old):
            return
        with open(old, 'r', encoding='utf-8') as f:
            data = f.read()
        if data:
            write_bytes(SESSION_FILE, data.encode('utf-8'))
            xbmc.log('[qqmusic] migrated session from plugin.audio.qqmusic',
                     xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('[qqmusic] migrate old session failed: %s' % e,
                 xbmc.LOGWARNING)


# ---------------- 页面路由 ----------------

def home():
    nick = _get('nick', '') or (cred_load() or {}).get('nick') or ''
    if API.logged_in():
        add_folder(build_url(action='noop'),
                   '[COLOR FF4CAF50]已登录%s[/COLOR]'
                   % (('：%s' % nick) if nick else ''))
    else:
        add_folder(build_url(action='account'),
                   '[COLOR FF90A4AE]未登录 · 点这里扫码/短信登录[/COLOR]')
    add_folder(build_url(action='toplist', topid=26, page=1),
               '[COLOR FF4CAF50]1. 热歌榜（300 首，不用登录就能听）[/COLOR]')
    add_folder(build_url(action='groups'), '2. 全部排行榜')
    add_folder(build_url(action='search'), '3. 搜索歌曲 / 歌手')
    add_folder(build_url(action='inputplaylist'), '4. 输入歌单 ID / 链接打开')
    if API.logged_in():
        add_folder(build_url(action='account'), '账号管理')
    liked = read_json(HISTORY_FILE) or []
    for kw in liked[:8]:
        add_folder(build_url(action='result', kw=kw), '历史搜索：%s' % kw)
    end('', sort=False)


def page_groups():
    groups = cached('groups', API.toplist_groups)
    for gi, g in enumerate(groups):
        add_folder(build_url(action='toplists', group=gi),
                   '%s (%d 个)' % (g['name'], len(g['toplists'])))
    end('')


def page_toplists(group):
    groups = cached('groups', API.toplist_groups)
    gi = int(group)
    if gi >= len(groups):
        notify('榜单组不存在')
        return
    for t in groups[gi]['toplists']:
        art = {'thumb': t.get('pic') or ICON} if t.get('pic') else None
        add_folder(build_url(action='toplist', topid=t['id']),
                   '%s  [COLOR FF90A4AE]%s %s[/COLOR]'
                   % (t['title'], t.get('period') or '',
                      ('(%d 首)' % t['total']) if t.get('total') else ''),
                   art=art)
    end('')


def page_toplist(topid, page=1):
    key = 'toplist-%s' % topid
    data = cached(key, lambda: list(API.toplist_songs(topid)))
    songs, name = data[0], data[1]
    xbmcplugin.setPluginCategory(HANDLE, name)
    render_songs(songs, page, build_url(action='toplist', topid=topid,
                                        page=page + 1))


def page_album(albummid):
    data = cached('album-%s' % albummid,
                  lambda: list(API.album_songs(albummid)))
    songs, name = data[0], data[1]
    xbmcplugin.setPluginCategory(HANDLE, name)
    render_songs(songs, 1)


def page_playlist(dissid, page=1):
    data = cached('playlist-%s' % dissid,
                  lambda: list(API.playlist_songs(dissid)))
    songs, name = data[0], data[1]
    xbmcplugin.setPluginCategory(HANDLE, name)
    render_songs(songs, page, build_url(action='playlist', id=dissid,
                                        page=page + 1))


def page_input_playlist():
    dlg = xbmcgui.Dialog()
    text = dlg.input('歌单 ID 或 QQ音乐歌单链接', type=xbmcgui.INPUT_ALPHANUM)
    if not text:
        return
    import re
    m = re.search(r'(\d{5,})', text)
    if not m:
        notify('没找到歌单 ID')
        return
    try:
        page_playlist(m.group(1))
    except Exception as e:
        notify('打不开这个歌单：%s' % e)


def page_search():
    dlg = xbmcgui.Dialog()
    kw = dlg.input('搜索歌曲 / 歌手', type=xbmcgui.INPUT_ALPHANUM)
    if not kw:
        return
    hist = read_json(HISTORY_FILE) or []
    if kw not in hist:
        hist.insert(0, kw)
        write_bytes(HISTORY_FILE, json.dumps(
            hist[:30], ensure_ascii=False).encode('utf-8'))
    page_result(kw, 1)


def page_result(kw, page=1):
    size = 30
    try:
        songs, total = API.search(kw, page, size)
    except Exception as e:
        notify('搜索失败：%s' % e)
        end('')
        return
    xbmcplugin.setPluginCategory(HANDLE, '搜索：%s' % kw)
    start = (page - 1) * size
    for i, s in enumerate(songs, start=1):
        url = build_url(action='play', mid=s['mid'], title=s.get('title', ''),
                        album=s.get('album', ''), singer=s.get('singer', ''),
                        dur=int(s.get('duration') or 0), pic=s.get('pic') or '')
        add_folder(url, song_label(s, start + i),
                   art={'thumb': s.get('pic') or ICON},
                   info={'title': s.get('title'), 'singer': s.get('singer'),
                         'album': s.get('album'),
                         'duration': s.get('duration')}, playable=True)
    if start + size < total:
        add_folder(build_url(action='result', kw=kw, page=page + 1),
                   '[COLOR FF64B5F6]下一页 >>[/COLOR]')
    end('songs', sort=True)


# ---------------- 扫码登录窗口 ----------------

class QRWindow(xbmcgui.WindowXMLDialog):
    """扫码窗口。三张图都在 onInit 里用绝对路径设置（见 qr.xml 注释）"""

    def __init__(self, xml, path, skin, png_path, tip=''):
        self._png = png_path
        self._tip = tip
        self.failed = False
        try:
            xbmcgui.WindowXMLDialog.__init__(self, xml, path, skin)
        except Exception:
            xbmcgui.WindowXMLDialog.__init__(self, xml, path)

    def onInit(self):
        try:
            self.getControl(1).setImage(BG_FILE)
            self.getControl(2).setImage(CARD_FILE)
            self.getControl(100).setImage(self._png)
            if self._tip:
                self.getControl(101).setLabel(self._tip)
        except Exception as e:
            xbmc.log('[qqmusic] QR skin control failed: %s' % e,
                     xbmc.LOGWARNING)
            self.failed = True
            self.close()

    def set_status(self, text):
        try:
            self.getControl(102).setLabel(text)
        except Exception:
            pass

    def onAction(self, action):
        aid = action.getId()
        if aid in (9, 10, 92, 61467, 61448):
            self.close()


def _qr_files():
    """准备好背景 / 白卡两张纯色图（必须先于窗口创建）"""
    write_bytes(BG_FILE, qr.solid_png(4, 4, (12, 16, 22, 255)))
    write_bytes(CARD_FILE, qr.solid_png(4, 4, (255, 255, 255, 255)))


def qr_build_file(png_bytes, target=666):
    """官方二维码 PNG -> 本地放大的锐利 PNG 路径。

    官方返回的是 1-bit 灰度 111x111，直接拉伸会糊。这里解码成模块矩阵
    再整数倍放大，保证每格边缘锐利、手机能秒扫。
    """
    try:
        _w, _h, matrix = qrimg.decode(png_bytes)
        big, _side = qrimg.fit(matrix, target)
        if big:
            out = qr.to_png(big, scale=1, border=0)
            if out and write_bytes(QR_FILE, out):
                return QR_FILE
    except Exception as e:
        xbmc.log('[qqmusic] qr upscale failed: %s' % e, xbmc.LOGWARNING)
    # 兜底：放大失败就直接用官方原图（宁可小一点也不要打不开）
    if write_bytes(QR_FILE, png_bytes):
        return QR_FILE
    return ''


def _qr_poll(qrsig, win, out, timeout=180):
    """后台轮询扫码状态"""
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        if out.get('stop'):
            return
        try:
            r = API.qr_poll(qrsig)
        except Exception as e:
            out['state'] = 'error'
            out['msg'] = '轮询出错：%s' % e
            break
        st = r.get('state')
        if win is not None and r.get('msg') and r['msg'] != last:
            last = r['msg']
            win.set_status(r['msg'])
        if st == 'done':
            out['state'] = 'done'
            out['uin'] = r.get('uin') or ''
            out['sigx'] = r.get('sigx') or ''
            break
        if st in ('timeout', 'refuse', 'error'):
            out['state'] = st
            out['msg'] = r.get('msg') or ''
            break
        for _ in range(4):
            if out.get('stop'):
                return
            time.sleep(0.5)
    else:
        out['state'] = 'timeout'
        out['msg'] = '二维码已超时'
    if win is not None:
        try:
            win.close()
        except Exception:
            pass


def login_by_qr(timeout=180):
    try:
        png, qrsig = API.qr_new()
    except Exception as e:
        notify('取二维码失败：%s' % e)
        return False
    path = qr_build_file(png)
    if not path:
        notify('二维码生成失败')
        return False
    _qr_files()

    out = {'stop': False}
    win = None
    try:
        win = QRWindow('qr.xml', ADDON_PATH, 'Default', path,
                       'QQ / QQ音乐 App → 右上角 ＋ → 扫一扫')
    except Exception as e:
        xbmc.log('[qqmusic] QR window failed: %s' % e, xbmc.LOGWARNING)
        win = None

    t = threading.Thread(target=_qr_poll, args=(qrsig, win, out, timeout))
    t.daemon = True
    t.start()

    if win is not None:
        try:
            win.doModal()
        except Exception as e:
            xbmc.log('[qqmusic] doModal failed: %s' % e, xbmc.LOGWARNING)
            win = None
        if win is not None and getattr(win, 'failed', False):
            win = None

    if win is None:
        # 皮肤没加载起来 -> 退化成全屏看图，轮询照旧
        xbmc.executebuiltin('ShowPicture(%s)' % path)
        while t.is_alive():
            time.sleep(0.5)
        xbmc.executebuiltin('Action(Back)')

    out['stop'] = True
    try:
        t.join(timeout=3)
    except Exception:
        pass

    if out.get('state') != 'done':
        st = out.get('state')
        if st == 'timeout':
            notify('二维码已超时，请重新点一次')
        elif st == 'refuse':
            notify('扫码已取消')
        elif st == 'error':
            notify(out.get('msg') or '扫码登录失败')
        return False

    notify('扫码成功，正在完成登录…')
    try:
        cred = API.qr_finish(out.get('uin', ''), out.get('sigx', ''))
    except Exception as e:
        notify('完成登录失败：%s' % e)
        xbmc.log('[qqmusic] qr_finish: %s' % e, xbmc.LOGERROR)
        return False
    return apply_credential(cred)


def apply_credential(cred):
    """把登录结果落盘并应用。cred 是 qq._cred_or_err 的返回值"""
    if not isinstance(cred, dict) or not cred.get('ok'):
        notify((cred or {}).get('msg') or '登录失败')
        return False
    musicid = str(cred.get('musicid') or '').strip()
    musickey = (cred.get('musickey') or '').strip()
    if not musicid or not musickey:
        notify('登录返回不完整，请重试')
        return False
    API.set_credential(musicid, musickey, cred.get('refresh_key') or '')
    cred_save({'musicid': musicid, 'musickey': musickey,
               'refresh_key': cred.get('refresh_key') or '',
               'nick': cred.get('nick') or '',
               'ts': int(time.time())})
    notify('登录成功：QQ %s' % musicid)
    return True


def login_by_sms():
    dlg = xbmcgui.Dialog()
    phone = dlg.input('手机号', type=xbmcgui.INPUT_NUMERIC)
    if not phone:
        return False
    phone = phone.strip()
    cc = '86'
    try:
        ev, info = API.sms_send(phone, cc)
    except Exception as e:
        notify('发送验证码失败：%s' % e)
        xbmc.log('[qqmusic] sms_send: %s' % e, xbmc.LOGERROR)
        return False
    if ev == 'captcha':
        _show_captcha(info)
        return False
    if ev == 'frequent':
        notify('操作过于频繁，请稍后再试')
        return False
    if ev != 'send':
        notify(info or '验证码发送失败')
        return False
    notify('验证码已发送')
    for _try in range(3):
        code = dlg.input('短信验证码（6 位）', type=xbmcgui.INPUT_NUMERIC)
        if not code:
            return False
        code = code.strip()
        if len(code) != 6 or not code.isdigit():
            notify('请输入 6 位数字')
            continue
        try:
            cred = API.sms_login(phone, code, cc)
        except Exception as e:
            notify('登录失败：%s' % e)
            xbmc.log('[qqmusic] sms_login: %s' % e, xbmc.LOGERROR)
            return False
        if cred.get('ok'):
            return apply_credential(cred)
        notify(cred.get('msg') or '登录失败')
        if cred.get('code') in (1000, 104400, 104401):
            break
    return False


def _show_captcha(url):
    """腾讯要求滑块验证。Kodi 里没法做滑块，把链接给用户"""
    lines = [
        '腾讯检测到需要人机验证（凑不住的接口层风控）',
        '',
        '请在这台电脑或手机的浏览器里打开下面链接，',
        '完成滑块验证后，回到这里再试一次短信登录：',
        '',
        url,
        '',
        '嫌麻烦的话，建议改用「扫码登录」，一般不会被拦。',
    ]
    try:
        xbmcgui.Dialog().textviewer('需要人机验证', '\n'.join(lines))
    except Exception:
        notify('需要人机验证，建议改用扫码登录')
    write_bytes(os.path.join(PROFILE, 'captcha-url.txt'),
                '\n'.join(lines).encode('utf-8'))


def login_by_cookie():
    dlg = xbmcgui.Dialog()
    ck = dlg.input('粘贴 y.qq.com 的 Cookie（需含 uin=）',
                   type=xbmcgui.INPUT_ALPHANUM)
    if not ck:
        return False
    ck = ck.strip()
    API.set_cookie(ck)
    if API.uin() == '0':
        notify('Cookie 里没找到 uin，可能没登录或复制不全')
        API.clear_credential()
        return False
    cred_save({'musicid': '', 'musickey': '', 'cookie': ck})
    notify('已保存 Cookie：QQ %s' % API.uin())
    return True


def do_logout():
    API.clear_credential()
    cred_clear()
    notify('已退出登录')


def page_account():
    if API.logged_in():
        cred = cred_load() or {}
        nick = cred.get('nick') or ''
        add_folder(build_url(action='noop'),
                   '[COLOR FF4CAF50]已登录：QQ %s%s[/COLOR]'
                   % (API.uin(), ('（%s）' % nick) if nick else ''))
        add_folder(build_url(action='checkcred'), '检查登录是否还有效')
        add_folder(build_url(action='testhq'), '测试高音质 / VIP 是否可播')
        add_folder(build_url(action='logout'),
                   '[COLOR FFFF5252]退出登录[/COLOR]')
    else:
        add_folder(build_url(action='loginqr'),
                   '[COLOR FF4CAF50]1. 扫码登录（推荐）[/COLOR]')
        add_folder(build_url(action='loginsms'),
                   '2. 手机号 + 短信验证码登录')
        add_folder(build_url(action='login'),
                   '3. 粘贴浏览器 Cookie 登录（兜底）')
        add_folder(build_url(action='diag'), '登录不上？点这里看诊断')
        add_folder(build_url(action='noop'),
                   '— 不登录也能听：热歌榜 300 首、搜索、歌单（128k/192k）—')
        add_folder(build_url(action='noop'),
                   '— 登录后可听：320k、无损、VIP 歌曲 —')
    end('')


def page_check_cred():
    try:
        ok, msg = API.check_credential()
    except Exception as e:
        notify('校验失败：%s' % e)
        xbmc.log('[qqmusic] check_credential: %s' % e, xbmc.LOGERROR)
        return
    notify('登录状态：%s' % ('有效' if ok else msg))


def page_diag():
    """把登录链路每一步的真实返回摆出来，一眼看出卡在哪"""
    lines = ['QQ音乐 登录诊断', '生成时间：%s'
             % time.strftime('%Y-%m-%d %H:%M:%S'), '']
    try:
        import socket
        lines.append('【1】域名连通性')
        for host in ('u.y.qq.com', 'ssl.ptlogin2.qq.com'):
            try:
                ip = socket.gethostbyname(host)
                lines.append('    %-24s -> %s  ✓' % (host, ip))
            except Exception as e:
                lines.append('    %-24s -> 解析失败 %s  ✗' % (host, e))
    except Exception:
        pass

    lines.append('')
    lines.append('【2】扫码链路')
    try:
        png, qrsig = API.qr_new()
        lines.append('    取二维码：✓ %d 字节  qrsig=%s…'
                     % (len(png), qrsig[:12]))
        r = API.qr_poll(qrsig)
        lines.append('    轮询：%s（%s）' % (r.get('state'), r.get('msg')))
    except Exception as e:
        lines.append('    取码/轮询失败：%s' % e)

    lines.append('')
    lines.append('【3】短信登录链路（用假号码试，不会真发）')
    try:
        ev, info = API.sms_send('13800138000', '86')
        lines.append('    发送接口：%s' % (ev or '未知'))
        if ev == 'captcha':
            lines.append('    ⚠ 本机出口 IP 被腾讯风控，要求滑块验证')
            lines.append('      （你自己的宽带通常不会触发）')
        elif info:
            lines.append('    %s' % info[:120])
    except Exception as e:
        lines.append('    发送失败：%s' % e)

    lines.append('')
    lines.append('【4】凭证交换接口')
    try:
        res = API.qq_login('FAKECODE00')
        lines.append('    QQLogin：code=%s %s'
                     % (res.get('code'), res.get('msg')))
        lines.append('    （假 code 返回 1000 = 接口可达，属预期）')
    except Exception as e:
        lines.append('    QQLogin 失败：%s' % e)

    lines.append('')
    lines.append('【5】当前登录态')
    try:
        ok, msg = API.check_credential()
        lines.append('    %s：%s' % ('已登录' if ok else '未登录', msg))
    except Exception as e:
        lines.append('    校验出错：%s' % e)

    text = '\n'.join(lines)
    write_bytes(os.path.join(PROFILE, 'qq-login-diag.txt'),
                text.encode('utf-8'))
    try:
        xbmcgui.Dialog().textviewer('QQ音乐 登录诊断', text)
    except Exception:
        notify(text[:200])


def page_test_hq():
    """诊断高音质：把各档位能不能播放摊给用户看"""
    lines = ['QQ音乐 高音质诊断', '当前登录：%s' % (
        ('QQ %s' % API.uin()) if API.logged_in() else '未登录'), '']
    try:
        songs, _total = API.search('周杰伦', 1, 5)
    except Exception as e:
        songs = []
        lines.append('搜索失败：%s' % e)
    target = None
    for s in songs:
        if s.get('pay'):
            target = s
            break
    if not target and songs:
        target = songs[0]
    if not target:
        lines.append('取不到测试歌曲')
    else:
        lines.append('测试曲目：%s - %s (pay=%s)'
                     % (target['title'], target['singer'], target['pay']))
        lines.append('')
        for key, name, _p, _e, _m in qq.LEVELS:
            try:
                u, mime = API._purl(target['mid'], key)
            except Exception as e:
                u, mime = '', ''
                lines.append('  %-9s 请求出错 %s' % (name, e))
                continue
            if not u:
                lines.append('  %-9s 拿不到地址（未登录或无版权）' % name)
                continue
            lines.append('  %-9s %s' % (name, '可用' if API.head_ok(u)
                                        else 'CDN 无此文件'))
    text = '\n'.join(lines)
    try:
        xbmcgui.Dialog().textviewer('QQ音乐 高音质诊断', text)
    except Exception:
        notify(text[:200])
    write_bytes(os.path.join(PROFILE, 'hq-diag.txt'),
                text.encode('utf-8'))


def do_play(params):
    mid = params.get('mid', [''])[0]
    title = params.get('title', [''])[0]
    if not mid:
        notify('缺少歌曲 ID')
        return
    url, mime, note = API.song_url(mid, LEVEL, VERIFY_URL)
    if not url:
        notify('取不到播放地址：%s' % (note or '未知'))
        return
    li = xbmcgui.ListItem(path=url)
    set_music_info(li, {
        'title': urllib.parse.unquote_plus(title),
        'singer': urllib.parse.unquote_plus(params.get('singer', [''])[0]),
        'album': urllib.parse.unquote_plus(params.get('album', [''])[0]),
        'duration': params.get('dur', ['0'])[0],
    })
    pic = params.get('pic', [''])[0]
    if pic:
        li.setArt({'thumb': pic, 'poster': pic, 'fanart': FANART})
    li.setMimeType(mime or 'audio/mpeg')
    li.setContentLookup(False)
    if note:
        xbmc.log('[qqmusic] play %s note=%s' % (mid, note), xbmc.LOGINFO)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def init():
    """每次插件调用开始时由 main.py 调一次"""
    _migrate_old_session()
    init_session()


def handle(paramstring=''):
    qs = paramstring.lstrip('?')
    xbmc.log('[qqmusic] qs=%s' % qs, xbmc.LOGDEBUG)
    params = urllib.parse.parse_qs(qs)
    action = params.get('action', [''])[0] or ''

    try:
        if action == '':
            home()
        elif action == 'groups':
            page_groups()
        elif action == 'toplists':
            page_toplists(int(params.get('group', ['0'])[0]))
        elif action == 'toplist':
            page_toplist(params.get('topid', ['26'])[0],
                         int(params.get('page', ['1'])[0]))
        elif action == 'album':
            page_album(params.get('albummid', [''])[0])
        elif action == 'playlist':
            page_playlist(params.get('id', [''])[0],
                          int(params.get('page', ['1'])[0]))
        elif action == 'inputplaylist':
            page_input_playlist()
        elif action == 'search':
            page_search()
        elif action == 'result':
            page_result(params.get('kw', [''])[0],
                        int(params.get('page', ['1'])[0]))
        elif action == 'account':
            page_account()
        elif action == 'loginqr':
            login_by_qr()
            page_account()
        elif action == 'loginsms':
            login_by_sms()
            page_account()
        elif action == 'checkcred':
            page_check_cred()
        elif action == 'diag':
            page_diag()
            page_account()
        elif action == 'login':
            login_by_cookie()
            page_account()
        elif action == 'logout':
            do_logout()
            page_account()
        elif action == 'testhq':
            page_test_hq()
            page_account()
        elif action == 'play':
            do_play(params)
        else:
            home()
    except qq.QQError as e:
        notify('QQ音乐接口出错：%s' % e)
        xbmc.log('[qqmusic] QQError: %s' % e, xbmc.LOGERROR)
        try:
            end('')
        except Exception:
            pass
    except Exception as e:
        notify('出错了：%s' % e)
        xbmc.log('[qqmusic] ERROR: %s' % e, xbmc.LOGERROR)
        try:
            end('')
        except Exception:
            pass
