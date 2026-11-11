"""Gumroad 购买内容下载（gum.sh 桥接，2026-11-07）。

下载管线本体在 ~/gum.sh（解析 gumroad.com/d/ 产品页 → 逐文件下载 →
尺寸校验 → .ok 标记），这里只把指令/看板调用转给 gum.sh 的进程封装。
与 pd_daemon 同模式：本机 shell 调用，超时/异常全收口。

命令：
  /gum <链接> [目录]    —— 启动下载（重复调用幂等）
  /gumst [目录]         —— 查看进度
"""
import os
import re
import subprocess
import asyncio

GUM_SH = os.path.expanduser('~/gum.sh')
REG = os.path.expanduser('~/.gum_jobs.json')
PATH_ENV = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'

def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False

def _load_reg():
    try:
        import json
        return json.load(open(REG))
    except Exception:
        return {}

def _save_reg(reg):
    import json
    json.dump(reg, open(REG, 'w'), ensure_ascii=False, indent=1)

def is_gum_command(cmd_text):
    cmd = str(cmd_text or '').strip().split(' ', 1)[0]
    return cmd in ('/gum', '/gumst', '/gumjobs')

def _pid_of(link):
    m = re.search(r'gumroad\.com/d/([a-f0-9]+)', str(link or ''))
    return m.group(1) if m else ''

def _dest_of(link):
    pid = _pid_of(link)
    return f'/Volumes/V1/gumroad/{pid}' if pid else ''

def _run_sh(link, dest):
    env = dict(os.environ, PATH=PATH_ENV)
    cmd = ['bash', GUM_SH, link] + ([dest] if dest else [])
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)

def _count_dir(dest):
    ok = part = 0
    try:
        for f in os.listdir(dest):
            if f.endswith('.ok'): ok += 1
            if f.endswith('.part'): part += 1
    except Exception:
        pass
    return ok, part

def status_text(dest=None):
    if not dest:
        return '用法: /gumst <目录>'
    ok, part = _count_dir(dest)
    lines = [f'📦 {dest}',
             f'已完成: {ok} · 半截: {part}']
    try:
        total = os.popen(f'du -sh {dest} 2>/dev/null').read().split('\t')[0]
        lines.append(f'大小: {total}')
    except Exception:
        pass
    return '\n'.join(lines)

async def command_reply(cmd_text):
    parts = str(cmd_text or '').strip().split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ''
    loop = asyncio.get_event_loop()
    if cmd == '/gum':
        if not arg:
            return ('用法: /gum <gumroad.com/d/ 链接> [目录]\n'
                    '下载到 /Volumes/V1/gumroad/<产品id>/，重跑即续传。')
        sub = arg.split(maxsplit=1)
        link = sub[0]
        dest = sub[1].strip() if len(sub) > 1 else ''
        pid = _pid_of(link)
        if not pid:
            return '链接不是 gumroad.com/d/ 形式'
        default_dest = f'/Volumes/V1/gumroad/{pid}'
        dest = dest or default_dest
        # 幂等: 同 dest 已有 .gum_running 标记则拒绝重复启动
        marker = os.path.join(dest, '.gum_running')
        if os.path.exists(marker):
            return (f'⏳ 该任务已在下载中。\n目录: {dest}\n查进度: /gumst {dest}')
        os.makedirs(dest, exist_ok=True)
        open(marker, 'w').close()
        await loop.run_in_executor(None, _run_sh, link, dest)
        reg = _load_reg()
        reg[f'gum-{pid}'] = {'link': link, 'dest': dest,
                             'started': __import__('time').strftime('%m-%d %H:%M:%S')}
        _save_reg(reg)
        return (f'🚀 Gumroad 下载已开始\n目录: {dest}\n'
                f'查进度: /gumst {dest}\n(完成后 .ok 标记齐全, marker 自动清除)')
    if cmd == '/gumst':
        if not arg:
            reg = _load_reg()
            if not reg:
                return '📭 没有 Gumroad 任务记录。发 /gum <链接> 开始。'
            lines = ['📦 Gumroad 任务:']
            for tag, j in reg.items():
                alive = '🟢' if os.path.exists(os.path.join(j.get('dest', ''), '.gum_running')) else '⚪/✅'
                ok, part = _count_dir(j.get('dest', ''))
                lines.append(f"{alive} {tag} · 完成 {ok} · 半截 {part}\n   {j.get('dest', '?')}")
            return '\n'.join(lines)
        return status_text(arg.strip())
    if cmd == '/gumjobs':
        return _fmt_jobs_sync()
    return '未知子命令'

def _fmt_jobs_sync():
    reg = _load_reg()
    if not reg:
        return '📭 没有 Gumroad 任务。'
    lines = ['📦 Gumroad 任务:']
    for tag, j in reg.items():
        alive = '🟢 运行中' if os.path.exists(os.path.join(j.get('dest', ''), '.gum_running')) else '⚪ 已结束'
        lines.append(f"{alive} {tag}\n   {j.get('dest', '?')}")
    return '\n'.join(lines)

# ============================================================
# /api 端点薄封装
# ============================================================
def api_start(link, dest=None):
    try:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(command_reply(f'/gum {link} {dest or ""}'))
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def api_status(dest):
    return {'ok': True, 'status': status_text(dest)}
