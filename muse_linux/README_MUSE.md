# Vidpick Muse Linux 上手说明

版本：1.0.0-muse；打包日期：2026-11-01。

这个包只下载单个抖音视频，适合没有桌面的 Ubuntu 服务器。下载完成后会打印 mp4 的绝对路径，方便 Muse 继续读取文件。视频内容的理解、语音转写和笔记由 Muse 自己完成，Vidpick 不提供这些能力。

## 安装

下面每行是一条命令，依次执行。压缩包需先传到你的家目录：

```bash
mkdir -p ~/vidpick
unzip -o ~/vidpick-muse-linux.zip -d ~/vidpick
cd ~/vidpick
bash install.sh
```

脚本只在 `~/vidpick/` 内创建 `.venv`、缓存、日志和下载目录，不需要 sudo；重复执行可以更新依赖。优先使用 `/opt/meta-chromium/chrome`。如果那里没有浏览器，脚本会把 Playwright Chromium 安装到 `~/vidpick/.cache/ms-playwright/`。不会调用 snap。

## 下载

在 `~/vidpick/` 下执行：

```bash
.venv/bin/python vidpick_cli.py 'https://www.douyin.com/video/1234567890123456789'
```

整段分享文案也可以直接放在引号里：

```bash
.venv/bin/python vidpick_cli.py '看看这个视频！https://v.douyin.com/xxxx/ 复制链接打开抖音'
```

如果浏览器不在默认位置：

```bash
.venv/bin/python vidpick_cli.py --browser /你的/chromium/路径 '抖音链接'
```

也可以设置 `VIDPICK_CHROMIUM` 环境变量。单次运行默认最多 180 秒；可用 `--timeout 300` 调整。只支持单个视频，主页链接和图文链接会报错。失败时退出码非 0，错误写到 stderr；成功时退出码 0，stdout 仅是一行 mp4 的绝对路径。

## 文件和日志

视频保存在 `~/vidpick/output/<作者>/<标题>__<视频ID>/video.mp4`；同目录有 `video.json`，包含标题、作者、视频ID、时长（秒）、原始链接和文件大小（字节）。失败日志在 `~/vidpick/logs/vidpick.log`。作品标题含中文可直接保存。

## cookies

普通下载若提示登录态、播放地址不可用或 cookies 过期，可从你**自己的已登录抖音浏览器**导出 Playwright `storageState` JSON（至少包含 `cookies` 数组），保存为 `~/vidpick/cookies.json`。可对照随包的 `cookies.example.json`，它是空示例，不包含账号。文件权限建议设为仅本人可读：

```bash
chmod 600 ~/vidpick/cookies.json
```

使用时加上：

```bash
.venv/bin/python vidpick_cli.py --cookies ~/vidpick/cookies.json '抖音链接'
```

也可以设置 `VIDPICK_COOKIES` 环境变量。cookies 过期后，重新从已登录浏览器导出并覆盖这个文件。不要把 `cookies.json` 分享给别人或放进 zip。服务器上不能自动替你登录；即使有 cookies，抖音仍可能因网络、地区或页面变化拒绝访问。

## 卸载

先确认 `~/vidpick/output/` 里没有需要保留的视频，再删除整个 `~/vidpick/` 文件夹；所有安装和下载文件都在其中。

## 测试范围

打包和自动化测试在 Windows 开发机完成；**尚未在 Muse 的 Ubuntu 24.04.5、`/opt/meta-chromium/chrome` 155、真实 Linux cookies 或真实 Linux 视频下载上测试**。Muse 首次安装和下载后的报错原文请反馈给开发者。此包没有 Linux 桌面 GUI，也没有验证批量采集、分类收藏和视频内容理解。
