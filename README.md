# plugin.audio.netease —— 网易云音乐 Kodi 插件

零第三方依赖的网易云音乐 Kodi 音频插件。只用 Python 标准库 + Kodi 内置模块 + 自带实现（AES 加密、二维码生成），无需安装 xbmcswift2 / requests / pycryptodome / qrcode 等任何第三方模块。

## 功能特性

- 登录方式：扫码 / 手机号+密码 / 短信验证码 / 邮箱 / 粘贴 Cookie
- 登录后：我的歌单、每日推荐，可播放会员歌曲
- 浏览：官方榜单（63 个）、热门歌单、搜索
- 音质：标准 / 极高 / 无损 / Hi-Res 切换
- 登录态本地保存，重开 Kodi 无需重新登录
- 登录诊断：一键查看各接口真实返回码
- 兼容 Kodi 19 (Matrix) / 20 (Nexus) / 21 (Omega)，含 Xbox UWP 版

## 安装方法

1. 下载本仓库代码并打包为 zip（或直接下载 Releases 包）
2. Kodi 中进入「插件」→ 右上角「安装来自 zip 文件」
3. 选择 zip 包安装
4. 安装后在「插件 → 音频插件」中找到「网易云音乐」

> 首次使用建议先登录（扫码或 Cookie 最稳定）。如遇网易风控（错误码 -462），请使用「粘贴 Cookie」方式登录。

## 目录结构

```
plugin.audio.netease/
├── addon.xml                 # 插件清单（id、版本、许可证）
├── main.py                   # 插件主程序入口
├── icon.png / fanart.png     # 图标与背景图
├── LICENSE                   # GPL-2.0 许可证
└── resources/
    ├── settings.xml          # 插件设置
    ├── lib/
    │   ├── netease.py        # 网易云音乐 API 封装
    │   ├── aes.py            # AES 加密实现（零依赖）
    │   ├── qr.py             # 二维码生成（零依赖）
    │   └── __init__.py
    └── skins/Default/720p/qr.xml   # 扫码登录界面
```

## 开源协议

本项目基于 [GPL-2.0](LICENSE) 协议开源。

## 声明

本插件只是网易云音乐 Web 接口的解析客户端，**不包含任何音乐内容**。所有音乐版权归网易及相关权利人所有，请支持正版。
