# -*- coding: utf-8 -*-
"""云音乐 Kodi 插件 · 统一入口：网易云音乐 + QQ音乐

Kodi 每次调用插件都会执行本文件。打开插件根目录时显示两个音源的入口，
之后按 URL 里的 src 参数把请求转给对应的子模块：
    src=ne → resources/lib/ne_app.py   （网易云音乐）
    src=qq → resources/lib/qq_app.py   （QQ音乐）

两个子模块各自维护登录态、设置与缓存，互不干扰。零第三方依赖，
兼容 Kodi 19 (Matrix) / 20 (Nexus) / 21 (Omega)，含 Xbox UWP 版。
"""
import json
import os
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_PATH = ADDON.getAddonInfo('path')
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE_URL = sys.argv[0] if len(sys.argv) > 0 else ''

sys.path.insert(0, os.path.join(ADDON_PATH, 'resources', 'lib'))
import ne_app  # noqa: E402
import qq_app  # noqa: E402


def translate(p):
    try:
        return xbmcvfs.translatePath(p)
    except AttributeError:
        return xbmc.translatePath(p)


PROFILE = translate(ADDON.getAddonInfo('profile'))
ICON = os.path.join(ADDON_PATH, 'icon.png')
FANART = os.path.join(ADDON_PATH, 'fanart.png')
NE_SESSION = os.path.join(PROFILE, 'session.json')       # 网易云登录态
QQ_SESSION = os.path.join(PROFILE, 'qq_session.json')    # QQ音乐登录态


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def build_url(**kw):
    return BASE_URL + '?' + urllib.parse.urlencode(kw)


def add_row(label, url):
    li = xbmcgui.ListItem(label=label)
    li.setArt({'icon': ICON, 'fanart': FANART})
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


def end():
    xbmcplugin.endOfDirectory(HANDLE)


def _ne_state():
    d = _read_json(NE_SESSION)
    nick = (d.get('profile') or {}).get('nickname') or ''
    return bool(d.get('cookie')), nick


def _qq_state():
    d = _read_json(QQ_SESSION)
    ok = bool(d.get('musickey')) or bool(d.get('cookie'))
    return ok, d.get('nick') or ''


def home():
    """插件首页：选择音源（只读本地登录态文件，不发网络请求）"""
    ok, nick = _ne_state()
    if ok:
        add_row('[COLOR FF4CAF50]● 网易云音乐[/COLOR]  已登录：%s'
                % (nick or '?'), build_url(src='ne', action='home'))
    else:
        add_row('[COLOR FFDDDDDD]网易云音乐[/COLOR]  未登录 · 点此进入',
                build_url(src='ne', action='home'))

    ok, nick = _qq_state()
    if ok:
        add_row('[COLOR FFFFD54F]● QQ音乐[/COLOR]  已登录：%s'
                % (nick or '?'), build_url(src='qq', action='home'))
    else:
        add_row('[COLOR FFDDDDDD]QQ音乐[/COLOR]  未登录 · 点此进入',
                build_url(src='qq', action='home'))
    end()


def router(argv):
    qs = argv[2].lstrip('?') if len(argv) > 2 else ''
    p = dict(urllib.parse.parse_qsl(qs))
    src = p.get('src', '')

    if not src:
        # 不带 src 的请求：老收藏夹里的网易云链接按网易云处理，
        # 其余（含打开插件根目录）进统一首页
        if p.get('action'):
            ne_app.init()
            ne_app.handle(qs)
        else:
            home()
        return

    app = qq_app if src == 'qq' else ne_app
    app.init()
    app.handle(qs)


if __name__ == '__main__':
    try:
        os.makedirs(PROFILE, exist_ok=True)
    except Exception:
        pass
    router(sys.argv)
