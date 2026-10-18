# -*- coding: utf-8 -*-
"""云音乐插件 · 网易云音乐子模块（页面与登录逻辑）

由统一入口 main.py 按 src=ne 调度到本模块。API 封装在同目录 netease.py，
二维码 / 纯色图生成用同目录 qr.py。零第三方依赖。
"""
import base64
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

sys.path.insert(0, os.path.join(ADDON_PATH, 'resources', 'lib'))
import netease  # noqa: E402
import qr  # noqa: E402


def translate(p):
    try:
        return xbmcvfs.translatePath(p)
    except AttributeError:
        return xbmc.translatePath(p)


PROFILE = translate(ADDON.getAddonInfo('profile'))
ICON = os.path.join(ADDON_PATH, 'icon.png')
FANART = os.path.join(ADDON_PATH, 'fanart.png')
HISTORY_FILE = os.path.join(PROFILE, 'history.json')
SESSION_FILE = os.path.join(PROFILE, 'session.json')
QR_FILE = os.path.join(PROFILE, 'qr.png')
CACHE_DIR = os.path.join(PROFILE, 'cache')


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


LEVEL = _get('level', 'standard') or 'standard'
VERIFY_SSL = _bool('verify_ssl', True)
USE_HEADERS = _bool('use_headers', True)
AUTO_DOWN = _bool('auto_downgrade', True)
CACHE_TTL = _int('cache_ttl', 30) * 60
PAGE_SIZE = _int('page_size', 100) or 100

API = netease.NetEase(cookie=_get('cookie', ''), verify_ssl=VERIFY_SSL,
                      level=LEVEL)

_UA = netease.UA
LEVEL_NAME = dict(netease.LEVELS)


# ---------------- 基础工具 ----------------

def build_url(**kw):
    kw.setdefault('src', 'ne')      # 统一入口 main.py 按 src 分流
    return BASE_URL + '?' + urllib.parse.urlencode(kw)


def notify(msg, heading='网易云音乐'):
    try:
        xbmcgui.Dialog().notification(heading, msg,
                                      xbmcgui.NOTIFICATION_INFO, 3000)
    except Exception:
        xbmc.log('[netease] %s' % msg, xbmc.LOGINFO)


def add_folder(url, label, art=None, info=None, playable=False):
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
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=not playable)


def _s(v):
    """Kodi 的 InfoTagMusic 只吃字符串，API 回来的可能是 None / 数字 / 列表"""
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ' / '.join(_s(x) for x in v if x)
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8', 'replace')
        except Exception:
            return ''
    return str(v)


def _split_artists(artist):
    s = _s(artist)
    if not s:
        return []
    for sep in (' / ', '/', '、', ',', '，', ';'):
        if sep in s:
            return [x.strip() for x in s.split(sep) if x.strip()]
    return [s]


def set_music_info(li, info):
    """Kodi 20+ 用 MusicInfoTag，旧版回退 setInfo"""
    title = _s(info.get('title', ''))
    artist = _s(info.get('artist', ''))
    album = _s(info.get('album', ''))
    artists = _split_artists(artist)

    # 旧版 Kodi（没有 getMusicInfoTag）走 setInfo
    if not hasattr(li, 'getMusicInfoTag'):
        li.setInfo('music', {
            'title': title, 'artist': artists, 'album': album,
            'tracknumber': info.get('track', 0),
            'duration': info.get('duration', 0),
            'comment': _s(info.get('comment', ''))})
        return

    try:
        tag = li.getMusicInfoTag()
        tag.setMediaType('song')
        tag.setTitle(title)
        if artists:
            # Kodi 21 的 setArtist 只接受单个字符串，传列表会抛
            # "argument \"artist\" for method \"setArtist\" must be unicode or str"
            tag.setArtist(artists[0])
        if album:
            tag.setAlbum(album)
        if info.get('track'):
            tag.setTrack(int(info['track']))
        if info.get('duration'):
            tag.setDuration(int(info['duration']))
        if info.get('comment'):
            tag.setComment(_s(info['comment']))
        return
    except Exception as e:
        try:
            xbmc.log('[netease] set_music_info: %s' % e, xbmc.LOGWARNING)
        except Exception:
            pass
        # 已经走通到这一步就别再调废弃的 setInfo 刷警告了


def end(content='songs', sort=True):
    if content:
        xbmcplugin.setContent(HANDLE, content)
    if sort:
        xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(HANDLE)


def with_headers(url):
    if not USE_HEADERS or not url:
        return url
    return '%s|User-Agent=%s&Referer=%s' % (
        url, urllib.parse.quote(_UA), urllib.parse.quote('https://music.163.com/'))


# ---------------- 本地存储 ----------------

def _makedirs(p):
    try:
        xbmcvfs.mkdirs(p)
    except Exception:
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            pass


def _read_json(path):
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            return json.loads(f.read() or '{}')
        finally:
            f.close()
    except Exception:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return json.loads(fh.read() or '{}')
        except Exception:
            return {}


def _write_json(path, obj):
    _makedirs(os.path.dirname(path))
    try:
        f = xbmcvfs.File(path, 'w')
        try:
            f.write(json.dumps(obj, ensure_ascii=False))
        finally:
            f.close()
        return
    except Exception:
        pass
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(obj, ensure_ascii=False))
    except OSError:
        pass


def _write_bytes(path, data):
    _makedirs(os.path.dirname(path))
    try:
        f = xbmcvfs.File(path, 'wb')
        try:
            f.write(data)
        finally:
            f.close()
        return True
    except Exception:
        pass
    try:
        with open(path, 'wb') as fh:
            fh.write(data)
        return True
    except OSError:
        return False


def history_load():
    return _read_json(HISTORY_FILE).get('list', [])


def history_add(kw):
    d = _read_json(HISTORY_FILE)
    lst = d.get('list', [])
    if kw in lst:
        lst.remove(kw)
    lst.insert(0, kw)
    d['list'] = lst[:30]
    _write_json(HISTORY_FILE, d)


def history_clear():
    _write_json(HISTORY_FILE, {'list': []})


def cache_get(key):
    if CACHE_TTL <= 0:
        return None
    p = os.path.join(CACHE_DIR, key + '.json')
    data = _read_json(p)
    if not data or time.time() - data.get('t', 0) > CACHE_TTL:
        return None
    return data.get('v')


def cache_put(key, value):
    if CACHE_TTL <= 0:
        return
    _makedirs(CACHE_DIR)
    _write_json(os.path.join(CACHE_DIR, key + '.json'),
                {'t': time.time(), 'v': value})


# ---------------- 登录态 ----------------

def session_load():
    d = _read_json(SESSION_FILE)
    if d.get('cookie'):
        API.set_session(d['cookie'])
    return d


def session_save(profile=None):
    d = _read_json(SESSION_FILE)
    d['cookie'] = API.session_cookie
    d['saved'] = time.time()
    if profile:
        d['profile'] = profile
    _write_json(SESSION_FILE, d)


def session_clear():
    API.set_session('')
    _write_json(SESSION_FILE, {})


def current_profile():
    prof = _read_json(SESSION_FILE).get('profile') or {}
    if not API.logged:
        return {}
    return prof


def require_uid():
    prof = current_profile()
    uid = prof.get('uid')
    if uid:
        return uid
    info = API.account_info()
    if info.get('uid'):
        session_save(info)
        return info['uid']
    return None


def maybe_validate_login():
    """设置里勾了「每次打开时校验登录状态」才跑，最多 10 分钟一次"""
    if not _bool('refresh_on_start', False) or not API.logged:
        return
    d = _read_json(SESSION_FILE)
    now = time.time()
    if now - d.get('checked', 0) < 600:
        return
    info = API.account_info() or {}
    if info.get('uid'):
        session_save(info)
    elif API.refresh_login():
        session_save(API.account_info() or {})
    d = _read_json(SESSION_FILE)
    d['checked'] = now
    _write_json(SESSION_FILE, d)


# ---------------- 扫码登录窗口 ----------------

class QRWindow(xbmcgui.WindowXMLDialog):
    """扫码窗口

    三张图（深色底 / 白卡 / 二维码）都在 onInit 里用绝对路径 setImage，
    不用 XML 里的相对纹理名 —— 相对纹理要在 skin 的 media 目录里找，
    脚本窗口经常找不到；而且 image 控件默认 stretch，贴错图就会把二维码
    拉成长方形，手机永远扫不出来（v1.1.1 的毛病）。
    """

    ID_BG, ID_CARD, ID_QR, ID_TIP, ID_STATUS = 1, 2, 100, 101, 102

    def __init__(self, xml, path, skin, png_path, bg_path, card_path, tip=''):
        self._png = png_path
        self._bg = bg_path
        self._card = card_path
        self._tip = tip
        self.failed = False
        self.ready = threading.Event()
        try:
            xbmcgui.WindowXMLDialog.__init__(self, xml, path, skin)
        except Exception:
            try:
                xbmcgui.WindowXMLDialog.__init__(self, xml, path)
            except Exception:
                raise

    def onInit(self):
        # 背景和白卡是纯装饰，失败也不影响扫码
        for cid, p in ((self.ID_BG, self._bg), (self.ID_CARD, self._card)):
            if not p:
                continue
            try:
                self.getControl(cid).setImage(p)
            except Exception:
                pass
        try:
            self.getControl(self.ID_QR).setImage(self._png)
        except Exception as e:
            # 二维码控件都拿不到，只能退回全屏看图
            xbmc.log('[netease] QR control missing: %s' % e, xbmc.LOGWARNING)
            self.failed = True
            try:
                self.close()
            except Exception:
                pass
            return
        try:
            if self._tip:
                self.getControl(self.ID_TIP).setLabel(self._tip)
        except Exception:
            pass
        self.ready.set()

    def set_status(self, text):
        try:
            self.getControl(self.ID_STATUS).setLabel(text)
        except Exception:
            pass

    def onAction(self, action):
        # 任意取消/返回操作都关掉窗口
        aid = action.getId()
        if aid in (9, 10, 92, 61467, 61448):
            self.close()


def _ui_pngs():
    """生成扫码窗口用的纯色图（深色底 + 白色卡片），只在缺的时候写一次"""
    bg = os.path.join(PROFILE, 'bg.png')
    card = os.path.join(PROFILE, 'card.png')
    if not os.path.exists(bg) or os.path.getsize(bg) < 100:
        _write_bytes(bg, qr.solid_png(32, 32, (10, 13, 18, 236)))
    if not os.path.exists(card) or os.path.getsize(card) < 100:
        _write_bytes(card, qr.solid_png(32, 32, (255, 255, 255, 255)))
    return bg, card


# 二维码在屏幕上是 560×560 皮肤像素，1080p 下实际约 840px。
# 直接生成 900px 的图：模块整数倍(45×20=900)，缩放比接近 1:1，最清晰。
QR_PIXELS = 900
QR_TIMEOUT = 150          # 二维码有效期（秒）
QR_MAX_REFRESH = 2        # 过期后自动换几张


def _qr_session():
    """取 key + 生成二维码图片，返回 (key, url, png_path)"""
    key = API.qr_key()
    if not key:
        return '', '', ''
    url, img = API.qr_create(key)
    if not url:
        return '', '', ''
    png = b''
    if img:
        try:
            raw = base64.b64decode(img)
            # 服务端偶尔回的是 JSON 串而不是图，认一下魔数
            if raw[:8] == b'\x89PNG\r\n\x1a\n' or raw[:2] == b'\xff\xd8':
                png = raw
        except Exception:
            png = b''
    if not png:
        png = qr.make_png(url, target=QR_PIXELS, border=4)
    if not png:
        return '', '', ''
    if not _write_bytes(QR_FILE, png):
        return '', '', ''
    return key, url, QR_FILE


def _qr_poll(key, win, out, timeout=QR_TIMEOUT):
    """后台轮询扫码状态"""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if out.get('stop'):
                return
            try:
                state, msg, prof = API.qr_check(key)
            except Exception as e:
                out['state'] = 'error'
                out['msg'] = '轮询出错：%s' % e
                break
            if state == 'scanned':
                if win is not None:
                    win.set_status('已扫码：%s　→ 请在手机上点「确认登录」' % msg)
            elif state == 'ok':
                out['state'] = 'ok'
                out['msg'] = msg
                out['prof'] = prof
                break
            elif state == 'expired':
                out['state'] = 'expired'
                out['msg'] = msg
                break
            elif state == 'error':
                out['state'] = 'error'
                out['msg'] = msg
                break
            for _ in range(4):        # 2 秒，分段便于及时退出
                if out.get('stop'):
                    return
                time.sleep(0.5)
        else:
            out['state'] = 'expired'
            out['msg'] = '二维码已超时'
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:
                pass


def login_by_qr():
    """扫码登录：过期自动换一张，最多 QR_MAX_REFRESH 次"""
    bg, card = _ui_pngs()
    for attempt in range(QR_MAX_REFRESH + 1):
        key, url, png_path = _qr_session()
        if not key or not png_path:
            notify('取不到二维码，请改用「粘贴 Cookie 登录」')
            return False
        xbmc.log('[netease] QR url=%s' % url, xbmc.LOGINFO)

        out = {'stop': False}
        win = None
        try:
            win = QRWindow('qr.xml', ADDON_PATH, 'Default', png_path, bg, card,
                           '手机打开「网易云音乐」App → 右上角 → 扫一扫')
        except Exception as e:
            xbmc.log('[netease] QR window failed: %s' % e, xbmc.LOGWARNING)
            win = None

        t = threading.Thread(target=_qr_poll, args=(key, win, out))
        t.daemon = True
        t.start()

        used_window = win is not None
        if used_window:
            try:
                win.doModal()
            except Exception as e:
                xbmc.log('[netease] doModal failed: %s' % e, xbmc.LOGWARNING)
                used_window = False
            if used_window and getattr(win, 'failed', False):
                used_window = False

        if not used_window:
            # 自绘窗口用不了，退回全屏看图（图是 900px 的，放大也不糊）
            xbmc.log('[netease] QR skin unavailable, fallback ShowPicture',
                     xbmc.LOGWARNING)
            xbmc.executebuiltin('ShowPicture(%s)' % png_path)
            while t.is_alive() and not out.get('state'):
                time.sleep(0.5)
            xbmc.executebuiltin('Action(Back)')

        out['stop'] = True
        try:
            t.join(timeout=3)
        except Exception:
            pass

        state = out.get('state')
        if state == 'ok':
            prof = out.get('prof') or {}
            if not prof.get('uid'):
                prof = API.account_info() or prof
            session_save(prof)
            notify('登录成功：%s' % (prof.get('nickname') or ''))
            return True
        if state == 'expired':
            if attempt < QR_MAX_REFRESH:
                notify('二维码已过期，正在换新的…')
                continue
            notify('二维码已过期，请再点一次')
            return False
        if state == 'error':
            notify(out.get('msg') or '扫码登录失败')
            return False
        return False       # 用户自己按了返回
    return False


def _dialog_input(heading, kind=None, hide=False, default=''):
    """带异常兜底的输入框（某些平台不支持 hide option）"""
    dlg = xbmcgui.Dialog()
    kind = xbmcgui.INPUT_ALPHANUM if kind is None else kind
    if hide:
        try:
            return dlg.input(heading, default, type=kind,
                             option=xbmcgui.ALPHANUM_HIDE_INPUT)
        except TypeError:
            pass
    return dlg.input(heading, default, type=kind)


def _phone_ok(phone):
    return phone.isdigit() and len(phone) == 11


def _mask(phone):
    return phone[:3] + '****' + phone[-4:] if len(phone) == 11 else phone


def _finish_login(ok, msg, prof):
    if ok:
        session_save(prof)
        notify('登录成功：%s' % (prof.get('nickname') or ''))
        return True
    xbmcgui.Dialog().ok('登录未成功', msg)
    return False


def login_by_password():
    phone = _dialog_input('手机号（11 位）', xbmcgui.INPUT_NUMERIC)
    if not phone:
        return False
    phone = phone.strip()
    if not _phone_ok(phone):
        xbmcgui.Dialog().ok('手机号不对', '请输入 11 位手机号，例如 13800000000')
        return False
    pwd = _dialog_input('登录密码', hide=True)
    if not pwd:
        return False
    API.warmup()
    ok, msg, prof = API.login_phone(phone, pwd)
    return _finish_login(ok, msg, prof)


def login_by_captcha():
    """手机号 + 短信验证码

    流程：发码 → 最多输错 3 次（不重发）→ 仍失败就给出人话原因。
    网易对非浏览器客户端有风控(-462)，一旦命中会明确提示改用扫码/Cookie。
    """
    phone = _dialog_input('手机号（11 位）', xbmcgui.INPUT_NUMERIC)
    if not phone:
        return False
    phone = phone.strip()
    if not _phone_ok(phone):
        xbmcgui.Dialog().ok('手机号不对', '请输入 11 位手机号，例如 13800000000')
        return False

    API.warmup()
    notify('正在发送验证码…')
    ok, msg, code = API.send_captcha(phone)
    if not ok:
        xbmcgui.Dialog().ok('发送验证码失败', '%s\n\n返回码：%s' % (msg, code))
        return False
    notify('验证码已发到 %s' % _mask(phone))

    for attempt in range(3):
        tip = '验证码已发到 %s' % _mask(phone)
        if attempt:
            tip += '（第 %d 次尝试）' % (attempt + 1)
        code_in = _dialog_input(tip, xbmcgui.INPUT_NUMERIC)
        if not code_in:
            return False
        code_in = code_in.strip()
        ok, msg, prof = API.login_captcha(phone, code_in)
        if ok:
            return _finish_login(True, msg, prof)
        # 只对「验证码错」重试，其他原因（风控/频繁）直接报出来
        if '验证码错误' in msg or '已过期' in msg:
            if attempt < 2:
                continue
        xbmcgui.Dialog().ok('登录未成功', msg)
        return False
    return False


def login_by_email():
    email = _dialog_input('邮箱账号')
    if not email:
        return False
    pwd = _dialog_input('邮箱密码', hide=True)
    if not pwd:
        return False
    API.warmup()
    ok, msg, prof = API.login_email(email.strip(), pwd)
    return _finish_login(ok, msg, prof)


def login_by_cookie():
    """粘贴浏览器 Cookie —— 风控拦死时的兜底方案，成功率最高"""
    if not xbmcgui.Dialog().yesno(
            '粘贴 Cookie 登录',
            '这个方法不受网易风控影响，最稳。步骤：',
            '1. 电脑浏览器打开 music.163.com 并登录',
            '2. 按 F12 → Network（网络）→ 随便点一个请求',
            '   → 找到 Request Headers 里的 Cookie，整条复制',
            '3. 一路「确定」后粘进来（必须含 MUSIC_U=）',
            nolabel='取消', yeslabel='开始粘贴'):
        return False
    ck = _dialog_input('粘贴整条 Cookie', default=_get('cookie', ''))
    if not ck:
        return False
    ck = ck.strip()
    if 'MUSIC_U' not in ck:
        xbmcgui.Dialog().ok('内容不对', '这段里没有 MUSIC_U，'
                            '请复制浏览器请求头里的整条 Cookie。')
        return False
    old = API.session_cookie
    API.set_session(ck)
    info = API.account_info() or {}
    if not info.get('uid'):
        API.set_session(old)
        xbmcgui.Dialog().ok('Cookie 无效',
                            '服务端没认出这个登录态，可能已过期。\n'
                            '请重新登录网页版再复制一次。')
        return False
    # 登录态优先于设置里手填的 cookie，这里顺手把设置项清掉避免打架
    try:
        ADDON.setSetting('cookie', '')
    except Exception:
        pass
    session_save(info)
    notify('登录成功：%s' % (info.get('nickname') or ''))
    return True


def run_login_diag():
    """登录诊断：把所有关键接口的真实返回码摊开，方便定位问题"""
    L = ['网易云音乐插件 · 登录诊断',
         '=' * 34,
         '时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'),
         'Kodi：%s' % (xbmc.getInfoLabel('System.BuildVersion') or '?'),
         '脚本：Python %s' % sys.version.split()[0],
         '']
    L.append('【1】登录态')
    prof = current_profile()
    L.append('    API 识别到登录态：%s' % ('是' if API.logged else '否'))
    L.append('    本地保存账号：%s' % (prof.get('nickname') or '（无）'))
    L.append('')
    L.append('【2】二维码接口')
    key = API.qr_key()
    L.append('    unikey：%s' % (key or '取不到 ✗'))
    if key:
        L.append('    二维码内容：')
        L.append('    https://music.163.com/login?codekey=%s' % key)
        st, msg, _ = API.qr_check(key)
        L.append('    状态轮询：%s（%s）' % (st, msg))
        L.append('    → 手机扫码后这里会变成 scanned / ok')
    L.append('')
    L.append('【3】登录风控探测（不发短信，只看返回码）')
    code, msg = API.probe_login()
    if code == -462:
        L.append('    返回 code=-462 → 这个网络被网易风控拦住了')
        L.append('    ✗ 手机验证码 / 密码登录在当前网络下走不通')
        L.append('    ✓ 请用「扫码登录」或「粘贴 Cookie 登录」')
    elif code in (400, 501, 502, 509):
        L.append('    返回 code=%s → 未被风控（正常，因为验证码是假的）' % code)
        L.append('    ✓ 手机验证码 / 密码登录可用，放心用')
    else:
        L.append('    返回 code=%s %s' % (code, msg))
    L.append('')
    L.append('【4】搜索 + 播放地址接口（游客身份，试到能出地址为止）')
    try:
        songs = API.search('海阔天空', limit=8)
    except Exception as e:
        songs = []
        L.append('    搜索出错：%s' % e)
    L.append('    搜索返回曲目：%d 首' % len(songs))
    hit = ''
    for s in songs:
        try:
            url, note = API.song_url(s['id'], 'standard')
        except Exception as e:
            url, note = '', '出错 %s' % e
        if url:
            hit = '%s - %s' % (s['name'], s.get('artist') or '')
            break
    if hit:
        L.append('    能取到播放地址 ✓（%s）' % hit)
        L.append('    → 播放链路正常。没登录时只有部分曲目可播，属版权限制')
    else:
        L.append('    试了 %d 首都没取到地址 ✗' % len(songs))
        L.append('    → 可能是网络被拦，或这些曲子都要会员')
    L.append('')
    L.append('【5】其他')
    L.append('    短信发送接口：weapi/sms/captcha/sent（此处不真实触发）')
    L.append('    二维码图片尺寸：%d px（屏幕控件 560 皮肤像素）' % QR_PIXELS)

    text = '\n'.join(L)
    path = os.path.join(PROFILE, 'login-diag.txt')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    except Exception:
        path = '(写不进文件)'
    try:
        xbmcgui.Dialog().textviewer('登录诊断', text + '\n\n已保存到：\n' + path)
    except Exception:
        xbmcgui.Dialog().ok('登录诊断', text[:900])
    return text


# ---------------- 页面 ----------------

def home():
    maybe_validate_login()
    prof = current_profile()
    if prof.get('nickname'):
        vip = prof.get('vip') or 0
        tag = '  [COLOR FFFF6B6B]VIP[/COLOR]' if vip else ''
        add_folder(build_url(action='account'),
                   '[COLOR FF4CAF50]● %s[/COLOR]%s   (点击管理)' % (prof['nickname'], tag))
        add_folder(build_url(action='myplaylists'), '我的歌单')
        add_folder(build_url(action='recommend'), '每日推荐')
    else:
        add_folder(build_url(action='account'),
                   '[COLOR FFFF9800]未登录 — 点这里登录[/COLOR]')
    add_folder(build_url(action='search'), '[COLOR FF4CAF50]搜索歌曲[/COLOR]')
    hist = history_load()
    for kw in hist[:10]:
        add_folder(build_url(action='searchresults', kw=kw), '  历史：%s' % kw)
    if hist:
        add_folder(build_url(action='clearhistory'), '[COLOR FF888888]清空搜索历史[/COLOR]')
    add_folder(build_url(action='toplists'), '热门榜单')
    add_folder(build_url(action='hotplaylists', pg=1), '热门歌单')
    end('albums', sort=False)


def page_account():
    prof = current_profile()
    if prof.get('nickname'):
        vip = prof.get('vip') or 0
        add_folder(build_url(action='noop'),
                   '当前账号：%s%s' % (prof['nickname'], '  (VIP)' if vip else ''))
        add_folder(build_url(action='noop'), 'UID：%s' % (prof.get('uid') or '-'))
        add_folder(build_url(action='refresh'), '[COLOR FF4CAF50]刷新登录状态[/COLOR]')
        add_folder(build_url(action='myplaylists'), '我的歌单')
        add_folder(build_url(action='recommend'), '每日推荐')
        add_folder(build_url(action='diag'), '登录诊断（看接口真实返回）')
        add_folder(build_url(action='logout'), '[COLOR FFFF5252]退出登录[/COLOR]')
    else:
        add_folder(build_url(action='loginqr'),
                   '[COLOR FF4CAF50]1. 扫码登录（推荐，最不容易被风控）[/COLOR]')
        add_folder(build_url(action='logincookie'),
                   '[COLOR FF4CAF50]2. 粘贴 Cookie 登录（最稳，扫码不行就用这个）[/COLOR]')
        add_folder(build_url(action='loginsms'), '3. 手机号 + 短信验证码')
        add_folder(build_url(action='loginpwd'), '4. 手机号 + 密码')
        add_folder(build_url(action='loginmail'), '5. 邮箱 + 密码')
        add_folder(build_url(action='diag'),
                   '[COLOR FFFF9800]登录不上？点这里做登录诊断[/COLOR]')
        add_folder(build_url(action='noop'),
                   '— 登录后可播放会员歌曲、查看我的歌单与每日推荐 —')
        add_folder(build_url(action='noop'),
                   '— 提示：网易对密码/短信登录有风控，报 -462 时请改用 1 或 2 —')
    end('', sort=False)


def page_my_playlists():
    uid = require_uid()
    if not uid:
        notify('登录已失效，请重新登录')
        page_account()
        return
    key = 'mypl_%s' % uid
    pls = cache_get(key)
    if pls is None:
        pls = API.user_playlists(uid)
        cache_put(key, pls)
    if not pls:
        notify('没有取到歌单，可能是登录已失效')
        end('albums', sort=False)
        return
    for p in pls:
        name = p['name']
        if p.get('special') == 5:
            name = '[COLOR FFFF6B6B]%s[/COLOR]' % name
        add_folder(build_url(action='playlist', pid=p['id'], name=p['name']),
                   '%s   [%d 首]' % (name, p.get('count', 0)),
                   art={'thumb': API.pic_url(p.get('cover', ''), 300)})
    end('albums', sort=False)


def page_recommend():
    uid = require_uid()
    if not uid:
        notify('登录已失效，请重新登录')
        page_account()
        return
    src = 'rec_%s' % uid
    if cache_get(src) is None:
        songs = API.recommend_songs()
        if not songs:
            notify('每日推荐取不到，可能需要会员或登录已失效')
            end('songs', sort=False)
            return
        cache_put(src, songs)
    page_list(src, 0, '每日推荐')


def page_toplists():
    tops = cache_get('toplists')
    if tops is None:
        tops = API.toplists()
        cache_put('toplists', tops)
    if not tops:
        notify('榜单接口没有返回数据')
    for t in tops:
        add_folder(build_url(action='playlist', pid=t['id'], name=t['name']),
                   t['name'], art={'thumb': API.pic_url(t.get('cover', ''), 300)})
    end('albums', sort=False)


def page_hot_playlists(pg=1):
    key = 'hotpl%d' % pg
    pls = cache_get(key)
    if pls is None:
        pls = API.hot_playlists(limit=30, offset=pg - 1)
        cache_put(key, pls)
    for p in pls:
        add_folder(build_url(action='playlist', pid=p['id'], name=p['name']),
                   '%s   [%d 首]' % (p['name'], p.get('count', 0)),
                   art={'thumb': API.pic_url(p.get('cover', ''), 300)})
    if pls:
        add_folder(build_url(action='hotplaylists', pg=pg + 1),
                   '[COLOR FF4CAF50]下一页 →[/COLOR]')
    end('albums', sort=False)


def page_list(src, offset=0, title='歌曲'):
    """通用歌曲列表：src 是缓存 key"""
    songs = cache_get(src)
    if songs is None:
        # 缓存过期就重新拉
        if src.startswith('pl'):
            songs = API.playlist_detail(src[2:])
        elif src.startswith('search:'):
            songs = API.search(src[7:], limit=PAGE_SIZE)
        elif src.startswith('rec'):
            songs = API.recommend_songs()
        else:
            songs = []
        if songs:
            cache_put(src, songs)
    if not songs:
        notify('没有取到歌曲，可能已过期，请返回重试')
        end('songs', sort=False)
        return
    total = len(songs)
    page = songs[offset:offset + PAGE_SIZE]
    for i, s in enumerate(page, 1):
        label = '[COLOR FFDDDDDD]%02d[/COLOR]  %s' % (offset + i, s['name'])
        if s.get('artist'):
            label += '  [COLOR FF888888]- %s[/COLOR]' % s['artist']
        if s.get('album'):
            label += '  [COLOR FF666666][%s][/COLOR]' % s['album']
        info = {'title': s['name'], 'artist': s.get('artist', ''),
                'album': s.get('album', ''), 'track': offset + i,
                'duration': s.get('duration', 0), 'comment': '网易云音乐'}
        art = {'thumb': API.pic_url(s.get('pic', ''), 300), 'fanart': FANART}
        li = xbmcgui.ListItem(label=label)
        li.setArt(art)
        li.setProperty('IsPlayable', 'true')
        set_music_info(li, info)
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action='play', sid=s['id'], name=s['name'],
                      artist=s.get('artist', ''), album=s.get('album', ''),
                      pic=s.get('pic', '')),
            li, isFolder=False)
    if offset + PAGE_SIZE < total:
        add_folder(build_url(action='list', src=src, offset=offset + PAGE_SIZE,
                             title=title),
                   '[COLOR FF4CAF50]下一页 → (%d/%d)[/COLOR]' % (offset + PAGE_SIZE, total))
    end('songs', sort=False)


def page_playlist(pid, name='歌单', offset=0):
    key = 'pl%s' % pid
    if cache_get(key) is None:
        songs = API.playlist_detail(pid)
        if not songs:
            notify('这个歌单取不到歌曲，可能是版权限制')
            end('songs', sort=False)
            return
        cache_put(key, songs)
    page_list(key, offset, name)


def page_search():
    kb = xbmcgui.Dialog().input('搜索歌曲', type=xbmcgui.INPUT_ALPHANUM)
    if not kb:
        end('songs', sort=False)
        return
    kw = kb.strip()
    history_add(kw)
    songs = API.search(kw, limit=PAGE_SIZE)
    src = 'search:%s' % kw
    if songs:
        cache_put(src, songs)
    else:
        notify('没有搜到：%s' % kw)
    page_list(src, 0, '搜索：%s' % kw)


def page_search_results(kw):
    src = 'search:%s' % kw
    if cache_get(src) is None:
        songs = API.search(kw, limit=PAGE_SIZE)
        if songs:
            cache_put(src, songs)
        else:
            notify('没有搜到：%s' % kw)
    page_list(src, 0, '搜索：%s' % kw)


def do_play(sid, name, artist, album, pic=''):
    order = [LEVEL]
    if AUTO_DOWN:
        order = [LEVEL] + [x for x in ['hires', 'lossless', 'exhigh', 'standard']
                           if x != LEVEL]
    if LEVEL == 'lossless':
        order = ['lossless', 'hires', 'exhigh', 'standard'] if AUTO_DOWN else ['lossless']
    url, note = '', ''
    for lv in order:
        url, note = API.song_url(sid, lv)
        if url:
            break
    if not url:
        hint = note or '版权限制'
        if not API.logged:
            hint = '需要登录或会员：%s' % hint
        notify('取不到播放地址：%s' % hint)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    li = xbmcgui.ListItem(path=with_headers(url))
    set_music_info(li, {'title': name, 'artist': artist, 'album': album,
                        'comment': note})
    li.setArt({'thumb': API.pic_url(pic, 300), 'fanart': FANART})
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# ---------------- 路由 ----------------

def router(paramstring):
    p = dict(urllib.parse.parse_qsl(paramstring.lstrip('?')))
    action = p.get('action', '')

    if action == 'search':
        page_search()
    elif action == 'searchresults':
        page_search_results(p.get('kw', ''))
    elif action == 'clearhistory':
        history_clear()
        notify('已清空')
        home()
    elif action == 'toplists':
        page_toplists()
    elif action == 'hotplaylists':
        page_hot_playlists(int(p.get('pg', '1') or 1))
    elif action == 'playlist':
        page_playlist(p.get('pid', ''), p.get('name', '歌单'),
                      int(p.get('offset', '0') or 0))
    elif action == 'list':
        page_list(p.get('src', ''), int(p.get('offset', '0') or 0),
                  p.get('title', '歌曲'))
    elif action == 'play':
        do_play(p.get('sid', ''), p.get('name', ''),
                p.get('artist', ''), p.get('album', ''), p.get('pic', ''))

    # ---- 账号 ----
    elif action == 'account':
        page_account()
    elif action == 'loginqr':
        login_by_qr()
        page_account()
    elif action == 'loginpwd':
        login_by_password()
        page_account()
    elif action == 'loginsms':
        login_by_captcha()
        page_account()
    elif action == 'loginmail':
        login_by_email()
        page_account()
    elif action == 'logincookie':
        login_by_cookie()
        page_account()
    elif action == 'diag':
        run_login_diag()
        page_account()
    elif action == 'logout':
        session_clear()
        notify('已退出登录')
        home()
    elif action == 'refresh':
        info = API.account_info()
        if info.get('uid'):
            session_save(info)
            notify('登录状态有效：%s' % info.get('nickname', ''))
        else:
            if API.refresh_login():
                info = API.account_info()
                session_save(info)
                notify('已续期')
            else:
                notify('登录已失效，请重新登录')
        page_account()
    elif action == 'myplaylists':
        page_my_playlists()
    elif action == 'recommend':
        page_recommend()
    elif action == 'noop':
        page_account()
    else:
        home()


def _migrate_old_session():
    """插件 ID 改为 plugin.audio.cloudmusic 后，把旧数据目录
    （plugin.audio.netease）里的网易云登录态迁过来（只做一次）"""
    try:
        if os.path.exists(SESSION_FILE):
            return
        old = os.path.join(translate('special://profile/addon_data/'
                                     'plugin.audio.netease/'),
                           'session.json')
        if not os.path.exists(old):
            return
        with open(old, 'rb') as f:
            data = f.read()
        if data:
            _makedirs(PROFILE)
            _write_bytes(SESSION_FILE, data)
            xbmc.log('[netease] migrated session from old addon id',
                     xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('[netease] migrate old session failed: %s' % e,
                 xbmc.LOGWARNING)


def init():
    """每次插件调用开始时由 main.py 调一次"""
    _migrate_old_session()
    _makedirs(PROFILE)
    _makedirs(CACHE_DIR)
    session_load()


def handle(paramstring=''):
    """处理带 src=ne 的所有请求（兼容老收藏夹里不带 src 的网易云链接）"""
    router(paramstring.lstrip('?'))
