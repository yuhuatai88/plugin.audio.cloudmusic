# -*- coding: utf-8 -*-
"""QQ 音乐网页 API 客户端（零第三方依赖，Python 3.8 兼容）

2026-09 实测可用，几条关键结论（写死在脑子里，别去改）：
- 播放地址一律走 `u.y.qq.com/cgi-bin/musicu.fcg`，模块 vkey.GetVkeyServer / CgiGetVkey，
  必须带 `platform=yqq.json`（写成 yqq 会 500001），data 用 GET query 传（POST 会失败）
- **一个请求只能传 1 个 filename**，传多个时服务端把所有条目的 vkey 都置空（踩过）
- filename 用 **songmid**，不是 file.media_mid（用 media_mid 反而拿不到 vkey）
- 播放地址 = `sip[0] + midurlinfo[0].purl`（purl 里自带 guid 和 vkey，不要自己拼）
- 匿名 uin=0 时：128k / 192k aac / 192k ogg 有 vkey；320k / FLAC / APE **一律空** → 必须登录
- 部分歌即使 payplay=0、也拿到 vkey，CDN 仍会返回 -46628 file not exist → 必须 HEAD 校验后降级

登录（v1.1）走两条路，都实测接口可达：
- 扫码：ptqrshow → ptqrlogin 轮询 → check_sig 拿 p_skey → authorize 换 code
        → CGI `QQConnectLogin.LoginServer/QQLogin` 换凭证
- 手机验证码：CGI `music.login.LoginServer/SendPhoneAuthCode` + `/Login`
凭证是 musicid + musickey（musickey 以 Q_H_L_ 开头），最终塞进 Cookie 使用。
扫码那套 ptlogin 参数一个都不能少：**u1 必须是腾讯官方中转地址，写成
y.qq.com 直接 403**（踩过一整轮组合测试才找出来）。
"""
import gzip
import json
import random
import re
import ssl
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ptlogin 参数：QQ 音乐的 appid / 第三方标识，缺 pt_3rd_aid 会走错授权流
PT_APPID = '716027609'
PT_DAID = '383'
PT_3RD = '100497308'
PT_HOST = 'https://ssl.ptlogin2.qq.com'
# ⚠️ 必须是腾讯官方中转地址。改成 y.qq.com/... 会被 ptlogin 判定非法，返回 403
PT_U1 = 'https://graph.qq.com/oauth2.0/login_jump'
PT_REFERER = ('https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=%s&daid=%s'
              '&style=40' % (PT_APPID, PT_DAID))

QR_SCAN = 66      # 未扫描
QR_CONF = 67      # 已扫码待确认
QR_DONE = 0       # 成功
QR_DONE_ALT = 405  # 成功（另一分支）
QR_TIMEOUT = 65   # 已失效
QR_REFUSE = 68    # 用户取消
QR_EXPIRE = 402

SMS_SEND = 0
SMS_CAPTCHA = 20276     # 触发风控，返回 securityURL
SMS_FREQUENT = 100001   # 操作过于频繁

# (key, 名称, 文件名前缀, 扩展名, mime)
LEVELS = [
    ('128', '128k MP3', 'M500', '.mp3', 'audio/mpeg'),
    ('192', '192k AAC', 'C200', '.m4a', 'audio/mp4'),
    ('ogg', '192k Ogg', 'O600', '.ogg', 'audio/ogg'),
    ('320', '320k MP3', 'M800', '.mp3', 'audio/mpeg'),
    ('flac', '无损 FLAC', 'F000', '.flac', 'audio/flac'),
    ('ape', '无损 APE', 'A000', '.ape', 'audio/x-ape'),
]

# 高音质优先，逐级降级
AUTO_ORDER = ['flac', 'ape', '320', '192', 'ogg', '128']


def level_name(key):
    for k, name, _p, _e, _m in LEVELS:
        if k == key:
            return name
    return key


def level_spec(key):
    for k, _n, p, e, m in LEVELS:
        if k == key:
            return p, e, m
    return 'M500', '.mp3', 'audio/mpeg'


def hash33(s, seed=0):
    """腾讯的 hash33。ptqrtoken 用 seed=0，g_tk 用 seed=5381"""
    e = seed
    for c in s or '':
        e = (e + (e << 5) + ord(c)) & 0x7FFFFFFF
    return e


def parse_ptui(text):
    """解析 `ptuiCB('66','0','','0','二维码未失效。','')`

    -> dict(state, uin, sigx, url, msg)
    state: scan 未扫 / conf 待确认 / done 成功 / timeout 失效
           refuse 取消 / error 出错
    """
    out = {'state': 'error', 'uin': '', 'sigx': '', 'url': '', 'msg': ''}
    m = re.search(r'ptuiCB\s*\((.*?)\)', text or '', re.S)
    if not m:
        out['msg'] = '无法解析登录返回'
        return out
    raw_parts = [p.strip().strip("'\"") for p in m.group(1).split(',')]
    parts = raw_parts + [''] * (6 - len(raw_parts))
    try:
        code = int(parts[0])
    except ValueError:
        out['msg'] = parts[4] or '未知状态'
        return out
    out['msg'] = parts[4] or ''
    out['url'] = parts[2] or ''
    if code in (QR_DONE, QR_DONE_ALT):
        out['state'] = 'done'
        mu = re.search(r'uin=([^&]+)', out['url'])
        ms = re.search(r'ptsigx=([^&]+)', out['url'])
        out['uin'] = mu.group(1) if mu else ''
        out['sigx'] = ms.group(1) if ms else ''
    elif code == QR_SCAN:
        out['state'] = 'scan'
    elif code == QR_CONF:
        out['state'] = 'conf'
    elif code in (QR_TIMEOUT, QR_EXPIRE):
        out['state'] = 'timeout'
    elif code == QR_REFUSE:
        out['state'] = 'refuse'
    return out


class QQError(Exception):
    pass


class QQ(object):
    def __init__(self, cookie='', timeout=15, verify_ssl=True):
        self.timeout = timeout
        self.cookie = (cookie or '').strip()
        self.verify_ssl = verify_ssl
        self.guid = ''.join(random.choice(string.digits) for _ in range(10))
        self._ctx = None
        # 登录凭证（v1.1）
        self.musicid = ''
        self.musickey = ''
        self.refresh_key = ''
        self.cred_extra = {}
        self._uin_from_cookie = ''

    # ---------- 基础 HTTP ----------
    def _ssl_ctx(self):
        if self._ctx is None:
            if self.verify_ssl:
                self._ctx = ssl.create_default_context()
            else:
                c = ssl.create_default_context()
                c.check_hostname = False
                c.verify_mode = ssl.CERT_NONE
                self._ctx = c
        return self._ctx

    def http(self, url, data=None, referer='https://y.qq.com/',
             headers=None, timeout=None, raw=False):
        """返回 (status, body[bytes])。失败抛 QQError"""
        h = {
            'User-Agent': UA,
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if referer:
            h['Referer'] = referer
        if self.cookie:
            h['Cookie'] = self.cookie
        if headers:
            h.update(headers)
        body = None
        if data is not None:
            if isinstance(data, str):
                data = data.encode('utf-8')
            body = data
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=body, headers=h)
        try:
            resp = urllib.request.urlopen(
                req, timeout=timeout or self.timeout,
                context=self._ssl_ctx())
            status = resp.getcode()
            rawbytes = resp.read()
            resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            # 404 也要读正文，上层可能要看 errorcode
            return e.code, (e.read() or b'')
        except Exception as e:
            raise QQError('%s' % e)
        if not raw:
            enc = (str(resp_headers.get('Content-Encoding') or '')).lower()
            if 'gzip' in enc:
                try:
                    rawbytes = gzip.decompress(rawbytes)
                except Exception:
                    pass
            elif 'deflate' in enc:
                try:
                    rawbytes = zlib.decompress(rawbytes)
                except Exception:
                    try:
                        rawbytes = zlib.decompress(rawbytes, -zlib.MAX_WBITS)
                    except Exception:
                        pass
        return status, rawbytes

    def http_ex(self, url, referer=None, headers=None, post=None,
                no_redirect=False, timeout=None):
        """返回 (status, body, headers)。

        登录链路需要读 Set-Cookie / Location，所以要拿到响应头。
        no_redirect=True 时不跟随 302（authorize 的 Location 里藏着 code）。
        """
        h = {
            'User-Agent': UA,
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if referer:
            h['Referer'] = referer
        if self.cookie:
            h['Cookie'] = self.cookie
        if headers:
            h.update(headers)
        data = None
        if post is not None:
            if isinstance(post, dict):
                data = urllib.parse.urlencode(post).encode('utf-8')
                h['Content-Type'] = 'application/x-www-form-urlencoded'
            else:
                data = post.encode('utf-8') if isinstance(post, str) else post
                h['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=data, headers=h)
        opener = None
        if no_redirect:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **kw):
                    return None
            # 必须显式带 HTTPSHandler 传 context，否则 build_opener 会丢 ssl 设置
            opener = urllib.request.build_opener(
                _NoRedirect,
                urllib.request.HTTPSHandler(context=self._ssl_ctx()))
        try:
            if opener is None:
                resp = urllib.request.urlopen(
                    req, timeout=timeout or self.timeout,
                    context=self._ssl_ctx())
            else:
                resp = opener.open(req, timeout=timeout or self.timeout)
            return resp.getcode(), resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b''), e.headers
        except Exception as e:
            raise QQError('%s' % e)

    def get_json(self, url, referer='https://y.qq.com/'):
        st, raw = self.http(url, referer=referer)
        text = self._decode(raw)
        j = None
        try:
            j = json.loads(text.strip())
        except Exception:
            t = text.strip()
            i, jj = t.find('('), t.rfind(')')
            if i > 0 and jj > i:
                try:
                    j = json.loads(t[i + 1:jj])
                except Exception:
                    j = None
        if j is None:
            raise QQError('响应不是 JSON (http=%s): %s' % (st, text[:120]))
        return j

    @staticmethod
    def _decode(raw):
        for enc in ('utf-8', 'gb18030', 'latin-1'):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return ''

    # ---------- 登录态 ----------
    def uin(self):
        """账号标识。登录以 musicid 为准，没登录时从 Cookie 里抠 QQ 号"""
        if self.musicid:
            return str(self.musicid)
        m = re.search(r'\buin=o?(\d+)', self.cookie or '')
        if m:
            return m.group(1)
        m = re.search(r'\bp_luin=o?(\d+)', self.cookie or '')
        return m.group(1) if m else '0'

    def set_cookie(self, cookie):
        self.cookie = (cookie or '').strip()
        # 手工粘贴的 Cookie 里如果有 uin，也认（保持向后兼容）
        if not self.musicid:
            m = re.search(r'\buin=o?(\d+)', self.cookie)
            if m:
                self._uin_from_cookie = m.group(1)

    def set_credential(self, musicid, musickey, refresh_key='',
                       extra=None):
        self._uin_from_cookie = ''
        """登录凭证 -> 可用的 Cookie。

        QQ 音乐的登录态是 musicid + musickey。把它塞成 Cookie 后，
        vkey 接口才会放行 320k / FLAC / VIP。
        几种 cookie 名一起给：腾讯不同接口认的名字不一样，多给无害。
        """
        self.musicid = str(musicid or '')
        self.musickey = (musickey or '').strip()
        self.refresh_key = refresh_key or ''
        parts = []
        if self.musicid:
            parts.append('uin=%s' % self.musicid)
            parts.append('qm_u=%s' % self.musicid)
        if self.musickey:
            parts.append('qqmusic_key=%s' % self.musickey)
            parts.append('qm_keyst=%s' % self.musickey)
        parts.append('tmeLoginType=2')
        self.cookie = '; '.join(parts)
        self.cred_extra = dict(extra or {})

    def clear_credential(self):
        self.musicid = ''
        self.musickey = ''
        self.refresh_key = ''
        self.cookie = ''
        self.cred_extra = {}
        self._uin_from_cookie = ''

    def logged_in(self):
        if self.musickey or self.musicid:
            return True
        # 只粘贴了浏览器 Cookie（没有 musickey）也算登录态：
        # 请求能带上身份，只是不一定能解 320k / 无损
        return bool(getattr(self, '_uin_from_cookie', ''))

    # ---------- 通用 CGI ----------
    CGI_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg'

    def cgi(self, module, method, param, comm=None, referer='https://y.qq.com/',
            wrap='req_0'):
        """调 musicu.fcg。返回 (code, data)

        code 是业务层的 req.code（0 = 成功），data 是 req.data。
        """
        base = {'ct': 24, 'cv': 0, 'uin': self.uin()}
        if comm:
            base.update(comm)
        payload = {'comm': base,
                   wrap: {'module': module, 'method': method,
                          'param': param}}
        url = (self.CGI_URL + '?format=json&inCharset=utf8'
               '&outCharset=utf-8&data=%s'
               % urllib.parse.quote(json.dumps(payload), safe=''))
        j = self.get_json(url, referer=referer)
        node = j.get(wrap) or j.get('req_0') or j.get('req') or {}
        return node.get('code', -1), (node.get('data') or {})

    # ---------- 扫码登录 ----------
    def qr_new(self):
        """取一张新二维码。返回 (png_bytes, qrsig)"""
        url = ('%s/ptqrshow?appid=%s&e=2&l=M&s=3&d=72&v=4&t=%s'
               '&daid=%s&pt_3rd_aid=%s'
               % (PT_HOST, PT_APPID, random.random(), PT_DAID, PT_3RD))
        st, body, hdrs = self.http_ex(url, referer=PT_REFERER)
        if st != 200 or body[:4] != b'\x89PNG':
            raise QQError('取二维码失败 http=%s' % st)
        qrsig = ''
        for line in (hdrs.get_all('Set-Cookie') or []):
            m = re.search(r'qrsig=([^;,\s]+)', line)
            if m:
                qrsig = m.group(1)
        if not qrsig:
            raise QQError('没拿到 qrsig')
        return body, qrsig

    def qr_poll(self, qrsig):
        """轮询一次二维码状态。

        返回 dict(state, uin, sigx, url, msg)
        state: scan / conf / done / timeout / refuse
        """
        token = hash33(qrsig)
        url = ('%s/ptqrlogin?u1=%s&ptqrtoken=%s&ptredirect=0&h=1&t=1&g=1'
               '&from_ui=1&ptlang=2052&action=0-0-%d&js_ver=20102616'
               '&js_type=1&login_sig=&pt_uistyle=40&aid=%s&daid=%s'
               '&pt_3rd_aid=%s&has_onekey=1'
               % (PT_HOST, urllib.parse.quote(PT_U1, safe=''), token,
                  int(time.time() * 1000), PT_APPID, PT_DAID, PT_3RD))
        st, body, _h = self.http_ex(url, referer=PT_REFERER,
                                    headers={'Cookie': 'qrsig=%s' % qrsig})
        if st != 200:
            return {'state': 'error', 'msg': '轮询失败 http=%s' % st}
        return parse_ptui(self._decode(body))

    def qr_finish(self, uin, sigx):
        """扫码确认后换凭证。返回 credential dict"""
        if not uin or not sigx:
            raise QQError('缺少 uin/ptsigx')
        ck = self._check_sig(uin, sigx)
        p_skey = ck.get('p_skey', '')
        if not p_skey:
            raise QQError('拿不到 p_skey（%.80s）'
                          % (ck.get('_raw') or '可能已在别处登录/风控'))
        code = self._authorize_code(p_skey, ck.get('_cookie', ''))
        if not code:
            raise QQError('拿不到授权 code')
        return self.qq_login(code)

    def _check_sig(self, uin, sigx):
        """check_sig -> dict(p_skey=..., _cookie=整条 cookie, _raw=...)"""
        url = ('https://ssl.ptlogin2.graph.qq.com/check_sig?uin=%s'
               '&pttype=1&service=ptqrlogin&nodirect=0&ptsigx=%s'
               '&s_url=%s&ptlang=2052&ptredirect=100&aid=%s&daid=%s'
               '&j_later=0&low_login_hour=0&regmaster=0&pt_login_type=3'
               '&pt_aid=0&pt_aaid=16&pt_light=0&pt_3rd_aid=%s'
               % (urllib.parse.quote(str(uin)), urllib.parse.quote(str(sigx)),
                  urllib.parse.quote(PT_U1, safe=''), PT_APPID, PT_DAID,
                  PT_3RD))
        st, body, hdrs = self.http_ex(url, referer='https://xui.ptlogin2.qq.com/',
                                      no_redirect=False)
        jar = {}
        raw_txt = self._decode(body)
        for line in (hdrs.get_all('Set-Cookie') or []):
            m = re.match(r'\s*([^=]+)=([^;]*)', line)
            if m:
                jar[m.group(1).strip()] = m.group(2).strip()
        if 'p_skey' not in jar:
            # 有时 cookie 藏在页面里的 <script>，或直接体现在 _raw
            m = re.search(r"p_skey=([^;'\"]+)", raw_txt)
            if m:
                jar['p_skey'] = m.group(1)
        out = dict(jar)
        out['_cookie'] = '; '.join('%s=%s' % (k, v) for k, v in jar.items())
        out['_body'] = raw_txt[:200]
        out['_raw'] = ('http=%s %s' % (st, raw_txt[:120])) if st != 200 else ''
        return out

    def _authorize_code(self, p_skey, cookie):
        """用 p_skey 换 OAuth code"""
        data = {
            'response_type': 'code',
            'client_id': PT_3RD,
            'redirect_uri': ('https://y.qq.com/portal/wx_redirect.html'
                             '?login_type=1&surl=https://y.qq.com/'),
            'scope': 'get_user_info,get_app_friends',
            'state': 'state',
            'switch': '',
            'from_ptlogin': '1',
            'src': '1',
            'update_auth': '1',
            'openapi': '1010_1030',
            'g_tk': str(hash33(p_skey, 5381)),
            'auth_time': str(int(time.time() * 1000)),
            'ui': str(random.randint(10 ** 15, 10 ** 16 - 1)),
        }
        hdrs = {
            'Referer': ('https://graph.qq.com/oauth2.0/show?which=Login'
                        '&display=pc&response_type=code&client_id=%s'
                        '&redirect_uri=https://y.qq.com/portal/'
                        'wx_redirect.html' % PT_3RD),
            'Cookie': cookie,
        }
        _st, _body, hdr = self.http_ex('https://graph.qq.com/oauth2.0/authorize',
                                       post=data, headers=hdrs,
                                       referer=None, no_redirect=True)
        loc = ''
        try:
            loc = hdr.get('Location', '') or ''
        except Exception:
            loc = ''
        m = re.search(r'code=([^&]+)', loc)
        return urllib.parse.unquote(m.group(1)) if m else ''

    def qq_login(self, code):
        """OAuth code -> 凭证"""
        c, d = self.cgi('QQConnectLogin.LoginServer', 'QQLogin',
                        {'code': code}, comm={'tmeLoginType': 2})
        return self._cred_or_err(c, d)

    # ---------- 手机验证码登录 ----------
    def sms_send(self, phone, country='86'):
        """发送验证码。返回 (event, info)

        event: 'send' / 'captcha' / 'frequent'
        """
        c, d = self.cgi('music.login.LoginServer', 'SendPhoneAuthCode',
                        {'tmeAppid': 'qqmusic', 'areaCode': str(country),
                         'phoneNo': str(phone)},
                        comm={'tmeLoginMethod': 3})
        if c == SMS_CAPTCHA:
            return 'captcha', d.get('securityURL') or ''
        if c == SMS_FREQUENT:
            return 'frequent', ''
        if c == SMS_SEND:
            return 'send', ''
        return '', (d.get('errMsg') or d.get('errTip')
                    or ('发送失败 code=%s' % c))

    def sms_login(self, phone, auth_code, country='86'):
        """验证码 -> 凭证"""
        c, d = self.cgi('music.login.LoginServer', 'Login',
                        {'code': str(auth_code), 'loginMode': 1,
                         'phoneNo': str(phone)},
                        comm={'tmeLoginMethod': 3, 'tmeLoginType': 0})
        return self._cred_or_err(c, d)

    def _cred_or_err(self, code, data):
        """把 CGI 错误码翻译成人话；成功则返回凭证 dict"""
        if code == 0:
            return {'ok': True, 'musicid': data.get('musicid'),
                    'musickey': data.get('musickey'),
                    'refresh_key': data.get('refresh_key', ''),
                    'nick': data.get('nick') or '',
                    'extra': data}
        msg = (data.get('errMsg') or data.get('errtip') or '')
        table = {
            1000: '授权参数无效或已过期，请重新发起登录',
            104400: '授权已失效，请重新发起登录',
            104401: '授权已过期，请重新发起登录',
            20271: '验证码不正确，请重新输入',
            20277: '账号受限或被冻结，请先在 QQ 音乐 App 里确认',
            20278: '账号受限，请联系 QQ 音乐客服',
            20279: '登录设备数量超限，请先在别处退出',
            20450: '账号存在风险已被限制登录',
            104604: '操作过于频繁，请稍后再试',
        }
        if code in table:
            return {'ok': False, 'code': code, 'msg': table[code]}
        return {'ok': False, 'code': code,
                'msg': msg or ('登录失败 code=%s' % code)}

    def check_credential(self):
        """凭证是否还有效。返回 (ok, msg)"""
        if not self.musickey:
            return False, '未登录'
        c, d = self.cgi('music.login.LoginServer', 'GetUserInfo',
                        {'uin': self.uin()}, comm={'tmeLoginMethod': 3})
        if c == 0:
            return True, (d.get('nick') or '有效')
        return False, (d.get('errMsg') or ('校验失败 code=%s' % c))

    # ---------- 列表类接口 ----------
    def toplist_groups(self):
        """返回 [{name, toplists:[{id, title, sub, period, total, pic}]}]"""
        payload = {
            'comm': {'ct': 24, 'cv': 0},
            'toplist': {'module': 'musicToplist.ToplistInfoServer',
                        'method': 'GetAll', 'param': {}},
        }
        url = ('https://u.y.qq.com/cgi-bin/musicu.fcg?_webcgikey=getToplist'
               '&format=json&inCharset=utf8&outCharset=utf-8&data=%s'
               % urllib.parse.quote(json.dumps(payload), safe=''))
        j = self.get_json(url)
        data = ((j.get('toplist') or {}).get('data') or {})
        groups = []
        for g in (data.get('group') or []):
            tops = []
            for t in (g.get('toplist') or []):
                tops.append({
                    'id': t.get('topId'),
                    'title': t.get('title') or '',
                    'sub': t.get('titleSub') or '',
                    'period': t.get('period') or '',
                    'update': t.get('updateTime') or '',
                    'total': t.get('totalNum') or 0,
                    'pic': t.get('frontPicUrl') or t.get('headPicUrl') or '',
                })
            if tops:
                groups.append({'name': g.get('groupName') or '未分组',
                               'toplists': tops})
        return groups

    def toplist_songs(self, topid):
        """返回 (歌曲列表, 榜单名)"""
        url = ('https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg'
               '?topid=%s&page=detail&type=top&format=json&tpl=3'
               '&g_tk=5381&inCharset=utf8&outCharset=utf-8&notice=0'
               '&platform=yqq&needNewCode=0' % topid)
        j = self.get_json(url)
        name = ((j.get('topinfo') or {}).get('ListName') or '榜单')
        songs = []
        for it in (j.get('songlist') or []):
            d = it.get('data') or {}
            if not d.get('songmid'):
                continue
            s = norm_song(d)
            s['rank'] = it.get('cur_count') or ''
            songs.append(s)
        return songs, name

    def search(self, kw, page=1, size=30):
        url = ('https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp'
               '?g_tk=5381&uin=%s&format=json&inCharset=utf-8&outCharset=utf-8'
               '&notice=0&platform=h5&needNewCode=1&w=%s&zhidaqu=1&catZhida=1'
               '&t=0&flag=1&ie=utf-8&sem=1&aggr=0&perpage=%d&n=%d&p=%d'
               '&remoteplace=txt.mqq.all&_=%d'
               % (self.uin(), urllib.parse.quote(kw), size, size, page,
                  int(time.time() * 1000)))
        j = self.get_json(url)
        data = j.get('data') or {}
        lst = ((data.get('song') or {}).get('list') or [])
        songs = [norm_song(x) for x in lst if x.get('songmid')]
        total = (data.get('song') or {}).get('totalnum') or 0
        return songs, total

    def album_songs(self, albummid):
        url = ('https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg'
               '?ct=24&albummid=%s&g_tk=5381&format=json&inCharset=utf8'
               '&outCharset=utf-8&notice=0&platform=yqq&needNewCode=0'
               % albummid)
        j = self.get_json(url)
        data = j.get('data') or {}
        songs = [norm_song(x) for x in (data.get('list') or [])
                 if x.get('songmid')]
        return songs, data.get('name') or '专辑'

    def playlist_songs(self, dissid):
        url = ('https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg'
               '?type=1&json=1&utf8=1&onlysong=0&disstid=%s&format=json'
               '&g_tk=5381&loginUin=%s&hostUin=0&inCharset=utf8'
               '&outCharset=utf-8&notice=0&platform=yqq&needNewCode=0'
               % (dissid, self.uin()))
        j = self.get_json(url)
        cds = j.get('cdlist') or []
        if not cds:
            raise QQError('取不到歌单内容（code=%s）' % j.get('code'))
        cd = cds[0]
        songs = [norm_song(x) for x in (cd.get('songlist') or [])
                 if x.get('songmid')]
        return songs, cd.get('dissname') or '歌单', (cd.get('desc') or '')

    # ---------- 播放地址 ----------
    def _purl(self, mid, key):
        """单个音质取播放地址，返回 (url, note)"""
        pre, ext, mime = level_spec(key)
        fn = '%s%s%s' % (pre, mid, ext)
        uin = self.uin()
        payload = {
            'req_0': {'module': 'vkey.GetVkeyServer', 'method': 'CgiGetVkey',
                      'param': {'guid': self.guid, 'songmid': [mid],
                                'songtype': [0], 'uin': uin,
                                'loginflag': 1 if uin != '0' else 0,
                                'platform': '20', 'filename': [fn]}},
            'comm': {'uin': uin, 'format': 'json', 'ct': 24, 'cv': 0},
        }
        url = ('https://u.y.qq.com/cgi-bin/musicu.fcg?callback=&g_tk=5381'
               '&jsonpCallback=&loginUin=%s&hostUin=0&format=json'
               '&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json'
               '&needNewCode=0&data=%s'
               % (uin, urllib.parse.quote(json.dumps(payload), safe='')))
        j = self.get_json(url)
        d0 = ((j.get('req_0') or {}).get('data') or {})
        mids = d0.get('midurlinfo') or []
        sip = d0.get('sip') or []
        if not mids:
            return '', '无返回'
        mu = mids[0]
        purl = mu.get('purl') or ''
        if not purl:
            return '', '无版权或未登录'
        host = ''
        for s in sip:
            s = (s or '').strip()
            if s:
                host = s if s.startswith('http') else ('https://' + s)
                break
        if not host:
            host = 'https://dl.stream.qqmusic.qq.com'
        return '%s/%s' % (host.rstrip('/'), purl), mime

    def head_ok(self, url, timeout=8):
        """CDN 上真的有这个文件吗（有些歌拿到 vkey 仍是 -46628）"""
        if not url:
            return False
        h = {'User-Agent': UA, 'Accept': '*/*', 'Range': 'bytes=0-16',
             'Referer': 'https://y.qq.com/'}
        if self.cookie:
            h['Cookie'] = self.cookie
        try:
            req = urllib.request.Request(url, headers=h)
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx())
            return 200 <= resp.getcode() < 300
        except urllib.error.HTTPError as e:
            return 200 <= e.code < 300
        except Exception:
            return False

    def song_url(self, mid, level='auto', verify=True):
        """返回 (url, mime, note)。level='auto' 或具体档位"""
        order = [level] if level != 'auto' else list(AUTO_ORDER)
        if level != 'auto' and level != 'flac':
            # 指定的档位失败后，也向下降级
            order = [level] + [k for k in AUTO_ORDER if k != level]
        tried = []
        for key in order:
            try:
                u, mime = self._purl(mid, key)
            except Exception as e:
                tried.append('%s:%s' % (level_name(key), e))
                continue
            if not u:
                tried.append('%s:无址' % level_name(key))
                continue
            if verify and not self.head_ok(u):
                tried.append('%s:CDN404' % level_name(key))
                continue
            return u, mime, '' if not tried else ('降级：' + '; '.join(tried[:2]))
        return '', '', ('; '.join(tried[:3]) or '取不到播放地址')


def album_pic(albummid, size=300):
    if not albummid:
        return ''
    return ('https://y.gtimg.cn/music/photo_new/T002R%sx%sM000%s.jpg'
            % (size, size, albummid))


def singer_pic(singermid, size=300):
    if not singermid:
        return ''
    return ('https://y.gtimg.cn/music/photo_new/T001R%sx%sM000%s.jpg'
            % (size, size, singermid))


def norm_song(d):
    """把各接口返回的歌曲对象统一成本插件内部格式"""
    singers = []
    for s in (d.get('singer') or []):
        n = s.get('name')
        if n:
            singers.append(n)
    singer = ' / '.join(singers)
    if not singer and d.get('singerName'):
        singer = d.get('singerName')
    pay = d.get('pay') or {}
    payplay = pay.get('payplay', 0)
    albummid = d.get('albummid') or ''
    singermid = ''
    slist = d.get('singer') or []
    if slist:
        singermid = slist[0].get('mid') or ''
    if not singermid:
        singermid = d.get('singerMid') or ''
    albummid = d.get('albummid') or ''
    return {
        'mid': d.get('songmid') or '',
        'id': d.get('songid') or 0,
        'title': d.get('songname') or d.get('title') or '',
        'singer': singer,
        'singermid': singermid,
        'album': d.get('albumname') or '',
        'albummid': albummid,
        'duration': int(d.get('interval') or 0),
        'pic': album_pic(albummid),
        'pay': 1 if payplay else 0,
        'size128': int(d.get('size128') or 0),
        'size320': int(d.get('size320') or 0),
        'sizeflac': int(d.get('sizeflac') or 0),
        'sizeape': int(d.get('sizeape') or 0),
        'sizeogg': int(d.get('sizeogg') or 0),
        'rank': d.get('rank') or '',
    }
