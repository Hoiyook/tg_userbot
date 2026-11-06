"""pixeldrain 下载（pd_daemon 桥接，2026-11-04）。

下载管线本体在 pd.sh + GitHub runner（pixeldrain → runner 中转 → 本地
拉回 → SHA256 校验），这里的职责只是把指令/看板的调用转给本机常驻的
pd_daemon（127.0.0.1:8791，见工作区 pd_daemon.py），daemon 再驱动 pd.sh。
daemon 不可达时一切指令都回启动提示，绝不抛异常。

命令：
  /pd <链接> [目录]   —— 启动下载（重复调用幂等，按 tag 去重）
  /pdst [tag]         —— 全部/单任务进度
  /pdclean <链接>     —— 清理云端中转 release
tag 规则：/l/<id> → pd-<id>；/file/<id> → pd-f-<id>。
"""
import json
import os
import time
import asyncio
import urllib.request

_PD_DAEMON = 'http://127.0.0.1:8791'
_START_HINT = ('⚠️ pd_daemon 不可达。启动：cd '
               '~/Documents/Claude/tg_userbot && python3 '
               'pd_daemon.py 所在工作区 && 见工作区 pd_daemon.py')

def _daemon_api(method, path, body=None, timeout=8):
    """调 daemon。连不上/非 2xx 都抛 OSError——调用方自行收口。"""
    url = _PD_DAEMON + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def tag_of(link):
    link = str(link or '')
    m = link.rsplit('/l/', 1)
    if len(m) == 2:
        return 'pd-' + m[1].split('?')[0].strip()
    m = link.rsplit('/file/', 1)
    if len(m) == 2:
        return 'pd-f-' + m[1].split('?')[0].split('/')[0].strip()
    return ''

def is_pd_command(cmd_text):
    cmd = str(cmd_text or '').strip().split(' ', 1)[0]
    return cmd in ('/pd', '/pdst', '/pdjobs', '/pdclean')

def _fmt_jobs(data):
    jobs = data.get('jobs') or {}
    if not jobs:
        return '📭 没有 pixeldrain 下载任务。发 /pd <链接> 开始。'
    lines = ['📥 pixeldrain 任务：']
    for tag, j in jobs.items():
        alive = '🟢 运行中' if j.get('alive') else '⚪ 已结束'
        lines.append(f"{alive} {tag}\n   目录: {j.get('dest') or '?'}\n   启动: {j.get('started', '?')}")
    return '\n'.join(lines)

def status_text(tag=None):
    """全部/单任务进度文本。daemon 不可达回启动提示。"""
    try:
        if tag:
            d = _daemon_api('GET', f'/status?tag={tag}')
            lines = [f"📥 {tag}（{'运行中' if d.get('alive') else '已结束'}）",
                     f"目录: {d.get('dest') or '?'} · 已校验: {d.get('ok_files', 0)} 个"]
            tail = (d.get('log_tail') or '').strip()
            if tail:
                lines.append('日志尾部:\n' + '\n'.join(tail.splitlines()[-6:]))
            return '\n'.join(lines)
        return _fmt_jobs(_daemon_api('GET', '/jobs'))
    except OSError:
        return _START_HINT

async def command_reply(cmd_text):
    """/pd /pdst /pdclean 统一入口。"""
    parts = str(cmd_text or '').strip().split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ''
    try:
        if cmd == '/pd':
            if not arg:
                return _fmt_jobs(_daemon_api('GET', '/jobs'))
            sub = arg.split(maxsplit=1)
            link = sub[0]
            dest = sub[1].strip() if len(sub) > 1 else ''
            body = {'link': link}
            if dest:
                body['dest'] = dest
            d = _daemon_api('POST', '/start', body)
            if d.get('status') == 'already_running':
                return (f"⏳ 该任务已在下载中（{d.get('tag')}）。\n"
                        f"目录: {d.get('dest', '?')}\n查进度: /pdst {d.get('tag')}")
            return (f"🚀 已开始下载（{d.get('tag')}）\n"
                    f"完成标记: 目录内 .ok 文件\n查进度: /pdst {d.get('tag')}")
        if cmd == '/pdst':
            return status_text(arg.strip() or None)
        if cmd == '/pdjobs':
            return _fmt_jobs(_daemon_api('GET', '/jobs'))
        if cmd == '/pdclean':
            if not arg:
                return '用法: /pdclean <链接>（确认本地文件完好后再清）'
            d = _daemon_api('POST', '/clean', {'link': arg})
            return ('🧹 云端中转已清理' if d.get('ok')
                    else f'清理失败: {d.get("error", "未知")}')
        return f'未知子命令: {cmd}'
    except OSError:
        return _START_HINT

_WATCH_STATE = {}   # tag → 已通知的文件名集合（进程内幂等）


async def completion_watch_loop(interval=120.0):
    """pixeldrain 完成观察器（2026-11-06 补盲区）。

    pd.sh 的 .ok 落地此前无观察者——完成无通知、台账无记录。本循环对
    注册表内 dest 目录数 .ok，有新完成文件即通知 + 写 history（幂等：
    记录过的文件名不重复通知）。daemon 不可达时静默跳过。"""
    while True:
        try:
            d = _daemon_api("GET", "/jobs")
            for tag, j in (d.get("jobs") or {}).items():
                dest = j.get("dest")
                if not dest or not os.path.isdir(dest):
                    continue
                oks = {f for f in os.listdir(dest) if f.endswith(".ok")}
                seen = _WATCH_STATE.setdefault(tag, set())
                fresh = {f for f in oks if f not in seen}
                if not fresh:
                    continue
                seen |= fresh
                n = len(oks)
                size = sum(
                    os.path.getsize(os.path.join(dest, f[:-3]))
                    for f in oks
                    if os.path.exists(os.path.join(dest, f[:-3])))
                from .history import append_history
                from .naming import format_size
                append_history(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} | pixeldrain | "
                    f"{os.path.basename(dest.rstrip('/'))} | "
                    f"{n} 个文件 | {format_size(size)} | 来源：pixeldrain下载")
                from . import notify
                try:
                    await notify.notify_user(
                        f"📥 pixeldrain 新增完成 {n} 个文件（{tag}）\n"
                        f"落盘 {dest}\n（云端中转可用 /pdclean <链接> 清理）")
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(interval)


# ============================================================
# /api 端点薄封装（api_server 调用；异常收口成 ok=False，绝不裸抛）
# ============================================================
def api_start(link, dest=None):
    try:
        d = _daemon_api('POST', '/start', {'link': link, 'dest': dest},
                        timeout=15)
        d['ok'] = True
        return d
    except OSError as e:
        return {'ok': False, 'error': f'pd_daemon 不可达: {e}'}

def api_status(tag=None):
    try:
        path = '/jobs' if not tag else f'/status?tag={tag}'
        d = _daemon_api('GET', path)
        d['ok'] = True
        return d
    except OSError as e:
        return {'ok': False, 'error': f'pd_daemon 不可达: {e}'}

def api_jobs():
    try:
        d = _daemon_api('GET', '/jobs')
        d['ok'] = True
        return d
    except OSError as e:
        return {'ok': False, 'error': f'pd_daemon 不可达: {e}'}

def api_clean(link):
    try:
        d = _daemon_api('POST', '/clean', {'link': link})
        d['ok'] = d.get('ok', True)
        return d
    except OSError as e:
        return {'ok': False, 'error': f'pd_daemon 不可达: {e}'}
