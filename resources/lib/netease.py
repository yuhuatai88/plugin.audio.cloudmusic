# -*- coding: utf-8 -*-
"""网易云音乐 Web API 客户端（零第三方依赖）

只依赖 Python 标准库 + 自带的 aes.py，Kodi UWP 环境可直接跑。
"""
import hashlib
import json
import gzip
import zlib
import socket
import ssl
import time
import urllib.parse
import urllib.request

try:  # Kodi 里是以顶层模块方式 import 的
    from .aes import weapi_params, encrypted_id
except ImportError:  # noqa
    from aes import weapi_params, encrypted_id

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
BASE = 'https://music.163.com'
DEFAULT_TIMEOUT = 15
COOKIE_BASE = 'appver=2.0.2; os=pc; '

# 音质：standard=标准 / exhigh=极高 / lossless=无损 / hires=Hi-Res / higher / sky(沉浸声)
LEVELS = [
    ('standard', '标准 (128k)'),
    ('exhigh', '极高 (320k)'),
    ('lossless', '无损 (FLAC)'),
    ('hires', 'Hi-Res'),
]


class ApiError(Exception):
    pass


def _ssl_ctx(verify=True):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class NetEase(object):
    def __init__(self, cookie='', verify_ssl=True, level='standard'):
        self.cookie_user = (cookie or '').strip()
        self.verify_ssl = verify_ssl
        self.level = level
        self.last_headers = None
        self.cookies = {}          # 登录后保存的服务端 cookie
        self.session_cookie = ''   # 序列化后的登录态

    # ---------- 登录态 ----------
    def set_session(self, cookie_str):
        """恢复登录态（字符串形式，'a=1; b=2'）"""
        self.session_cookie = (cookie_str or '').strip()
        self.cookies = {}
        for part in self.session_cookie.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                self.cookies[k.strip()] = v.strip()

    def _merge_cookie(self, real):
        out = {}
        for part in COOKIE_BASE.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                out[k.strip()] = v.strip()
        for part in real.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                out[k.strip()] = v.strip()
        return '; '.join('%s=%s' % (k, v) for k, v in out.items() if v != '')

    def _take_cookie(self):
        """从最近一次响应里抓取 Set-Cookie"""
        raws = []
        try:
            if self.last_headers is not None:
                raws = self.last_headers.get_all('Set-Cookie') or []
        except Exception:
            raws = []
        got = False
        for raw in raws:
            part = (raw or '').split(';')[0].strip()
            if '=' not in part:
                continue
            k, v = part.split('=', 1)
            k, v = k.strip(), v.strip()
            if not k or k.lower() in ('path', 'domain', 'expires', 'max-age',
                                      'httponly', 'secure', 'samesite'):
                continue
            self.cookies[k] = v
            if k in ('MUSIC_U', 'MUSIC_A', '__csrf', 'MUSIC_R_T',
                     '__remember_me'):
                got = True
        if self.cookies:
            self.session_cookie = '; '.join(
                '%s=%s' % (k, v) for k, v in self.cookies.items() if v)
        return got

    @property
    def logged(self):
        return 'MUSIC_U' in self.cookies or 'MUSIC_U' in (self.cookie_user or '')

    @property
    def csrf(self):
        return self.cookies.get('__csrf', '')

    # ---------- HTTP ----------
    @property
    def cookie(self):
        # 登录态优先，其次才是设置里手填的 Cookie
        if self.session_cookie and 'MUSIC_U' in self.session_cookie:
            return self._merge_cookie(self.session_cookie)
        if self.cookie_user and 'MUSIC_U' in self.cookie_user:
            return self._merge_cookie(self.cookie_user)
        return COOKIE_BASE + 'NMTID=' + '%0x' % (int(time.time() * 1000) & 0xFFFFFFFFFFFF)

    def _headers(self, extra=None):
        h = {
            'User-Agent': UA,
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://music.163.com/',
            'Cookie': self.cookie,
        }
        if extra:
            h.update(extra)
        return h

    def http(self, url, data=None, extra=None, timeout=DEFAULT_TIMEOUT):
        """返回 dict / list，失败返回 None"""
        body = None
        if data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data).encode('utf-8')
            else:
                body = data.encode('utf-8') if isinstance(data, str) else data
        req = urllib.request.Request(url, data=body, headers=self._headers(extra))
        ctx = _ssl_ctx(self.verify_ssl)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            self.last_headers = resp.headers
            raw = resp.read(600000)
            enc = (resp.headers.get('Content-Encoding') or '').lower()
            if 'gzip' in enc:
                raw = gzip.decompress(raw)
            elif 'deflate' in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            txt = raw.decode('utf-8', 'ignore').strip()
            if txt.startswith('\ufeff'):
                txt = txt[1:]
            s = txt.find('{')
            if s > 0:
                txt = txt[s:]
            return json.loads(txt)
        except (urllib.error.URLError, socket.timeout, ssl.SSLError,
                ValueError, OSError, json.JSONDecodeError):
            return None

    # ---------- weapi ----------
    def weapi(self, path, obj):
        if isinstance(obj, dict) and 'csrf_token' in obj and not obj['csrf_token']:
            obj['csrf_token'] = self.csrf
        params, seckey = weapi_params(obj)
        return self.http('%s/weapi/%s' % (BASE, path),
                         {'params': params, 'encSecKey': seckey},
                         {'Content-Type': 'application/x-www-form-urlencoded'})

    def call(self, path, obj=None):
        """优先走 /api/ 老接口，失败再试 weapi"""
        if obj is None:
            r = self.http('%s/api/%s' % (BASE, path))
            if r is not None:
                return r
        return self.weapi(path, obj or {})

    # ---------- 业务 ----------
    def toplists(self):
        """榜单列表 -> [{'id','name','cover'}]"""
        r = self.http('%s/api/toplist' % BASE)
        out = []
        for it in (r or {}).get('list', []):
            out.append({'id': it.get('id'), 'name': it.get('name'),
                        'cover': it.get('coverImgUrl', '')})
        return out

    def hot_playlists(self, limit=30, offset=0, cat='全部'):
        """热门歌单"""
        r = self.weapi('playlist/list', {'cat': cat, 'order': 'hot',
                                         'limit': limit, 'offset': offset * limit,
                                         'total': 'true'})
        out = []
        for it in ((r or {}).get('playlists') or []):
            out.append({'id': it.get('id'), 'name': it.get('name'),
                        'cover': it.get('coverImgUrl', ''),
                        'count': it.get('trackCount', 0)})
        return out

    def playlist_detail(self, pid, limit=300, offset=0):
        """歌单 / 榜单歌曲列表 -> [song dict]

        v6 接口一次能返回完整 tracks（weapi v3 现在只给 1 首样例），
        拿不到时再退化成 trackIds + songs_detail。
        """
        r = self.http('%s/api/v6/playlist/detail?id=%s&n=%d'
                      % (BASE, pid, limit))
        pl = (r or {}).get('playlist') or {}
        tracks = pl.get('tracks') or []
        if not tracks:
            wr = self.weapi('v3/playlist/detail',
                            {'id': pid, 'n': 1000, 'csrf_token': ''})
            pl = (wr or {}).get('playlist') or {}
            tracks = pl.get('tracks') or []
            if len(tracks) <= 1:
                ids = [t.get('id') for t in (pl.get('trackIds') or [])
                       if t.get('id')]
                if ids:
                    return self.songs_detail(ids)
        songs = [s for s in (self._song(t) for t in tracks) if s]
        return songs[offset:offset + limit] if offset else songs

    def songs_detail(self, ids):
        """批量歌曲详情"""
        out = []
        for i in range(0, len(ids), 200):
            r = self.call('v3/song/detail',
                          {'c': json.dumps([{'id': x} for x in ids[i:i + 200]]),
                           'csrf_token': ''})
            for it in (r or {}).get('songs', []):
                s = self._song(it)
                if s:
                    out.append(s)
        return out

    @staticmethod
    def _song(it):
        if not isinstance(it, dict) or not it.get('id'):
            return None
        ar = it.get('artists') or it.get('ar') or []
        artist = ' / '.join(a.get('name', '') for a in ar if isinstance(a, dict))
        al = it.get('album') or it.get('al') or {}
        pic = (al.get('picUrl') or '') if isinstance(al, dict) else ''
        dur = it.get('duration') or it.get('dt') or 0
        return {
            'id': it.get('id'),
            'name': it.get('name', ''),
            'artist': artist,
            'album': (al.get('name', '') if isinstance(al, dict) else ''),
            'pic': pic,
            'mv': it.get('mv') or 0,
            'duration': int(dur) // 1000,
        }

    def search(self, kw, stype=1, limit=30, offset=0):
        """搜索：1单曲 10专辑 100歌手 1000歌单"""
        r = self.weapi('cloudsearch/get/web',
                       {'s': kw, 'type': stype, 'limit': limit,
                        'offset': offset, 'total': 'true', 'csrf_token': ''})
        res = (r or {}).get('result') or {}
        songs = []
        for it in (res.get('songs') or []):
            s = self._song(it)
            if s:
                songs.append(s)
        if not songs:
            old = self.http('%s/api/search/get?s=%s&type=%d&limit=%d&offset=%d'
                            % (BASE, urllib.parse.quote(kw), stype, limit, offset))
            for it in ((old or {}).get('result') or {}).get('songs') or []:
                s = self._song(it)
                if s:
                    songs.append(s)
        return songs

    def song_url(self, song_id, level=None):
        """返回 (url, 备注)；拿不到返回 ('', 原因)"""
        lvl = level or self.level
        r = self.weapi('song/enhance/player/url/v1',
                       {'ids': [song_id], 'level': lvl,
                        'encodeType': 'aac', 'csrf_token': ''})
        data = (r or {}).get('data') or []
        if data:
            d = data[0]
            if d.get('url'):
                extra = d.get('level') or lvl
                return d['url'], ('ok:%s' % extra)
            # 会员歌曲 / 音质不匹配
            if lvl != 'standard':
                return '', '当前音质无权限或版权受限'
            return '', '无版权或需要会员'
        return '', '接口未返回地址'

    # ---------- 登录 ----------

    # 网易登录接口的返回码 -> 人话。ctx 区分是哪种登录方式（同一个 400
    # 在验证码登录里是「验证码错」，在密码登录里是「密码错」）。
    RISK_HINT = ('网易风控要求二次验证（滑块/异常网络判定）。\n'
                 '请改用「扫码登录」，或「粘贴 Cookie 登录」。')

    def login_phone(self, phone, password, countrycode='86'):
        """手机号 + 密码登录

        返回 (ok:bool, msg:str, profile:dict)
        """
        r = self.weapi('login/cellphone', {
            'phone': phone,
            'countrycode': countrycode,
            'password': hashlib.md5(password.encode('utf-8')).hexdigest(),
            'rememberLogin': 'true',
            'csrf_token': '',
        }) or {}
        self._take_cookie()
        return self._login_result(r, ctx='pwd')

    def send_captcha(self, phone, countrycode='86'):
        """发送短信验证码 -> (ok, msg, code)"""
        r = self.weapi('sms/captcha/sent', {
            'cellphone': phone, 'ctcode': countrycode,
            'countrycode': countrycode, 'csrf_token': '',
        }) or {}
        self._take_cookie()
        code = r.get('code', -1)
        if code == 200:
            return True, '验证码已发送，5 分钟内有效', code
        if code == 400:
            return False, '手机号格式不对，或参数被服务端拒绝（code=400）', code
        if code == 405:
            return False, '发送太频繁，请等 60 秒后再试（code=405）', code
        if code == 406:
            return False, '该手机号/网络请求太频繁，请过一会儿再试（code=406）', code
        if code == 509:
            return False, '请求过于频繁，请稍后再试（code=509）', code
        if code in (250, 416, 8821):
            return False, '网易要求图形验证码 / 风控拦截，请改用扫码登录', code
        return False, (r.get('message') or '发送失败') + '（code=%s）' % code, code

    def login_captcha(self, phone, captcha, countrycode='86'):
        """手机号 + 验证码登录"""
        r = self.weapi('login/cellphone', {
            'phone': phone, 'countrycode': countrycode,
            'captcha': captcha, 'rememberLogin': 'true', 'csrf_token': '',
        }) or {}
        self._take_cookie()
        return self._login_result(r, ctx='captcha')

    def login_email(self, email, password):
        """邮箱登录"""
        r = self.weapi('login', {
            'username': email,
            'password': hashlib.md5(password.encode('utf-8')).hexdigest(),
            'rememberLogin': 'true', 'csrf_token': '',
        }) or {}
        self._take_cookie()
        return self._login_result(r, ctx='mail')

    def _login_result(self, r, ctx='pwd'):
        code = r.get('code', -1)
        if code == 200:
            prof = r.get('profile') or {}
            uid = prof.get('userId') or (r.get('account') or {}).get('id')
            if not uid:
                # 少数情况下 200 但不带 profile，等下一次请求补
                info = self.account_info() or {}
                uid = info.get('uid')
                if info:
                    return True, '登录成功', info
            return True, '登录成功', {
                'uid': uid,
                'nickname': prof.get('nickname') or '',
                'avatar': prof.get('avatarUrl') or '',
                'vip': prof.get('vipType', 0),
            }
        msg = (r.get('message') or r.get('msg') or '').strip()
        if code == -462:
            return False, self.RISK_HINT, {}
        if code == 400:
            if ctx == 'captcha':
                return False, '验证码错误或已过期，请重新获取', {}
            if ctx == 'mail':
                return False, msg or '邮箱或密码错误', {}
            return False, msg or '手机号或密码错误', {}
        if code == 502:
            return False, msg or '账号或密码错误', {}
        if code == 509:
            return False, msg or '请求过于频繁，请稍后再试', {}
        if code in (801, 860):
            return False, ('这个账号需要先做安全验证。\n' + self.RISK_HINT), {}
        if code == 416:
            return False, '网易要求升级客户端（风控），请改用扫码登录', {}
        if code == 250:
            return False, '需要图形验证码，Kodi 里填不了，请改用扫码登录', {}
        return False, (msg or '登录失败') + '（code=%s）' % code, {}

    # ---------- 诊断用（都不产生副作用） ----------

    def warmup(self):
        """先访问一次官网拿真实 cookie（浏览器化），能降低一点风控概率"""
        try:
            self.http('%s/' % BASE)
        except Exception:
            pass
        self._take_cookie()
        return self.cookies

    def probe_login(self, phone='10000000001'):
        """风控探测：不发送任何短信，只看登录接口肯不肯受理。

        返回 (code, message)。code=-462 就是被风控了。
        """
        r = self.weapi('login/cellphone', {
            'phone': phone, 'countrycode': '86', 'captcha': '000000',
            'rememberLogin': 'true', 'csrf_token': '',
        }) or {}
        return r.get('code'), (r.get('message') or '')

    def qr_key(self):
        """取二维码 key。实测是带横杠的 UUID（36 位），例如
        0784f9d2-b4da-41ce-b2e5-d05e5bf4f3ef"""
        r = self.weapi('login/qrcode/unikey', {'type': 1, 'csrf_token': ''}) or {}
        return r.get('unikey', '') or r.get('codeKey', '')

    def qr_create(self, key):
        """返回 (qrurl, qrimg_base64)

        网易的 /login/qr/create、/login/qrcode/client/create（含明文 /api/
        路由）都已经 404 下线，服务端不再给图。所以这里直接按官方格式
        自己拼二维码内容，图片由 qr.py 本地生成：

            https://music.163.com/login?codekey=<unikey>

        这个格式和 login/qrcode/client/login 轮询用的 key 是配套的。
        """
        url, img = '', ''
        for path in ('login/qrcode/client/create', 'login/qr/create'):
            r = self.weapi(path, {'key': key, 'type': 1, 'qrimg': 'true',
                                  'csrf_token': ''}) or {}
            if r.get('qrurl') or r.get('qrUrl'):
                url = r.get('qrurl') or r.get('qrUrl')
                raw = r.get('qrimg') or ''
                if raw.startswith('data:image'):
                    raw = raw.split(',', 1)[1]
                img = raw
                break
        if not url:
            url = 'https://music.163.com/login?codekey=%s' % key
        return url, img

    def qr_check(self, key):
        """轮询扫码结果 -> (state, msg, profile)

        state: 'wait' 待扫码 / 'scanned' 已扫待确认 /
               'ok' 成功 / 'expired' 过期 / 'error'
        """
        r = self.weapi('login/qrcode/client/login',
                       {'key': key, 'type': 1, 'csrf_token': ''}) or {}
        if r.get('code') is None:      # 老接口兜底
            r = self.weapi('login/qr/check',
                           {'key': key, 'type': 1, 'csrf_token': ''}) or {}
        code = r.get('code', -1)
        if code == 800:
            return 'expired', r.get('message') or '二维码已过期', {}
        if code == 801:
            return 'wait', '等待扫码', {}
        if code == 802:
            nick = ((r.get('nickname') or '') or
                    ((r.get('profile') or {}).get('nickname') or ''))
            return 'scanned', nick or '请在手机上确认', {}
        if code == 803:
            self._take_cookie()
            if not self.logged:
                # 有些情况下 cookie 在下一次请求才下发，补一次
                self.weapi('w/nuser/account/get', {'csrf_token': ''})
                self._take_cookie()
            prof = self.account_info() or {}
            return 'ok', '登录成功', prof
        if code == 502:
            return 'expired', '二维码已失效', {}
        return 'error', r.get('message') or ('未知状态 %s' % code), {}

    def account_info(self):
        """取当前登录账号信息，未登录返回 {}"""
        r = self.weapi('w/nuser/account/get', {'csrf_token': ''}) or {}
        prof = r.get('profile') or {}
        if not prof:
            return {}
        return {
            'uid': prof.get('userId'),
            'nickname': prof.get('nickname') or '',
            'avatar': prof.get('avatarUrl') or '',
            'vip': prof.get('vipType', 0),
        }

    def refresh_login(self):
        r = self.weapi('login/token/refresh', {'csrf_token': ''}) or {}
        self._take_cookie()
        return (r or {}).get('code') == 200

    # ---------- 需要登录的内容 ----------
    def user_playlists(self, uid, limit=100, offset=0):
        """我的歌单"""
        r = self.weapi('user/playlist', {'uid': uid, 'limit': limit,
                                         'offset': offset, 'csrf_token': ''}) or {}
        out = []
        for it in (r.get('playlist') or []):
            out.append({
                'id': it.get('id'),
                'name': it.get('name', ''),
                'cover': it.get('coverImgUrl', ''),
                'count': it.get('trackCount', 0),
                'special': it.get('specialType', 0),
            })
        return out

    def recommend_songs(self):
        """每日推荐（需要登录）"""
        r = self.weapi('v3/discovery/recommend/songs',
                       {'csrf_token': ''}) or {}
        data = (r.get('data') or {})
        songs = []
        for it in (data.get('dailySongs') or []):
            s = self._song(it)
            if s:
                songs.append(s)
        if not songs:
            for it in ((r.get('recommend') or [])):
                s = self._song(it)
                if s:
                    songs.append(s)
        return songs

    def lyric(self, song_id):
        r = self.weapi('song/lyric', {'id': song_id, 'lv': -1, 'cp': 'false',
                                      'csrf_token': ''})
        if not isinstance(r, dict):
            return ''
        return (r.get('lrc') or {}).get('lyric', '')

    def pic_url(self, url, size=300):
        if not url:
            return ''
        return '%s?param=%dy%d' % (url, size, size)
