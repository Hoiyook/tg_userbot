#!/usr/bin/env bash
# TG Userbot 一键启停：优雅停机（SIGTERM）+ 启动。
#
#   ./run.sh start      启动（已在跑就跳过，不重复拉）
#   ./run.sh stop       优雅停机：SIGTERM 两个进程，等它们自己收尾
#   ./run.sh restart    先优雅停机，再启动
#   ./run.sh status     看两个进程、CDP 端口与各自最后一行日志
#
# 管的是两个进程：
#   * userbot —— `tg_userbot_final.py`（主客户端 + bot 菜单 + 下载队列）
#   * Agent   —— `python -m tg_userbot.chrome_agent`（CDP 驱动专用 Chrome）
# Chrome 专用实例**不归本脚本管**：停 Agent 时刻意保留它（与 /chrome_stop
# 同款语义，重启 Agent 时直接复用它的 CDP）。
#
# 代理：优先用环境变量 TG_PROXY；没设就读 tg_secrets.json 的 "tg_proxy"。
# 机器相关的值只放 tg_secrets.json（已 gitignore），不写进本脚本——这是
# 公开仓库。两边都没有时会显著警告：直连在国内网络下连不上 Telegram，
# 还会被误判成账号异常（2026-09-10 实测过）。
#
# 停止走 SIGTERM 而不是 kill -9：两个进程都有信号处理器，用户bot 会保存状态、
# 断开 25 条下载 worker、停掉清理任务；Agent 会把 RUNNING 任务原样持久化、
# 下次启动按恢复规则接着跑。超时才升级到 SIGKILL（会明确告警）。
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

USERBOT_SCRIPT="tg_userbot_final.py"
AGENT_MODULE="tg_userbot.chrome_agent"
# 命令行匹配（ps 全量输出里找，不依赖 pgrep——Termux 上不一定有）。
# 一律转小写比较：本机 ps 显示的是 ".../MacOS/Python"（大写 P），
# 大小写敏感地匹配 "python" 会漏判（写好第一版就踩了这个坑）。
USERBOT_PAT="python[0-9.]* .*$(printf '%s' "$USERBOT_SCRIPT" | tr 'A-Z' 'a-z')"
AGENT_PAT="python[0-9.]* .*-m $(printf '%s' "$AGENT_MODULE" | tr 'A-Z' 'a-z')"
# 优雅停机等待上限（秒）；实测两者都在 2 秒内退干净
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "❌ 找不到可用的 python（既无 .venv/bin/python 也无 python3）"
    exit 1
fi

# ------------------------------------------------------------
# 配置（以 config.py 为准，避免脚本里抄一份路径）
# ------------------------------------------------------------
_conf_out="$("$PY" -c 'from tg_userbot import config as c
print(c.RUNTIME_DIR)
print(c.CHROME_AGENT_PID_FILE)' 2>/dev/null)"
RUNTIME_DIR="$(printf '%s\n' "$_conf_out" | sed -n 1p)"
AGENT_PID_FILE="$(printf '%s\n' "$_conf_out" | sed -n 2p)"
# config 读不出来（环境坏了/依赖没装）时回落到默认位置，脚本仍可用
[ -n "$RUNTIME_DIR" ] || RUNTIME_DIR="$HOME/Downloads/Nagram/runtime"
[ -n "$AGENT_PID_FILE" ] || AGENT_PID_FILE="$RUNTIME_DIR/chrome_agent.pid"
mkdir -p "$RUNTIME_DIR" 2>/dev/null || true

# ------------------------------------------------------------
# 进程探测
# ------------------------------------------------------------
ps_line() {  # $1=PID → 该进程的命令行（不存在则空）
    ps -p "$1" -o command= 2>/dev/null
}

# ps 全量输出里按模式找 PID（转小写后匹配，见 USERBOT_PAT 的说明）
pids_matching() {
    ps -eo pid=,command= 2>/dev/null \
        | awk -v pat="$1" '{ if (tolower($0) ~ pat) print $1 }'
}

userbot_pids() {
    pids_matching "$USERBOT_PAT"
}

agent_pids() {
    # 两路来源取并集，去重：
    #   ① PID 文件（Agent 自己写的，是权威来源，与 chrome_client.agent_running
    #      同款判据：PID 存活 + 命令行确属 chrome_agent，防 PID 复用误判）；
    #   ② 命令行匹配（兜住「PID 文件被误删 / Agent 是别的方式拉起的」）。
    local pid cmd found=""
    if [ -f "$AGENT_PID_FILE" ]; then
        pid="$(cat "$AGENT_PID_FILE" 2>/dev/null)"
        case "$pid" in
            ''|*[!0-9]*) ;;
            *)
                if kill -0 "$pid" 2>/dev/null; then
                    cmd="$(ps_line "$pid")"
                    case "$cmd" in *chrome_agent*) found="$pid" ;; esac
                fi
                ;;
        esac
    fi
    for pid in $(pids_matching "$AGENT_PAT"); do
        case " $found " in *" $pid "*) ;; *) found="$found $pid" ;; esac
    done
    printf '%s\n' $found
}

agent_pid() {  # 单个 Agent PID（可能为空）；多个时取第一个
    agent_pids | sed -n 1p
}

pids_text() {  # 把 PID 列表拼成一行（去尾随空格），供回执展示
    tr '\n' ' ' | sed 's/ *$//'
}

uptime_of() {  # $1=PID → 已运行时长
    ps -p "$1" -o etime= 2>/dev/null | tr -d ' '
}

# ------------------------------------------------------------
# 代理
# ------------------------------------------------------------
resolve_proxy() {
    if [ -n "${TG_PROXY:-}" ]; then
        echo "env"
        return
    fi
    local value
    value="$("$PY" -c 'import json
try:
    data = json.load(open("tg_secrets.json", encoding="utf-8"))
except Exception:
    data = {}
print((data.get("tg_proxy") or "").strip())' 2>/dev/null)"
    if [ -n "$value" ]; then
        export TG_PROXY="$value"
        echo "secrets"
    else
        echo "none"
    fi
}

warn_no_proxy() {
    echo "⚠️  未配置代理（TG_PROXY 环境变量与 tg_secrets.json 的 tg_proxy 都是空）："
    echo "    将直连 Telegram。国内网络下连不上，还可能被误判成账号异常。"
    echo "    请在 tg_secrets.json 里加一行： \"tg_proxy\": \"socks5://127.0.0.1:<端口>\""
}

# ------------------------------------------------------------
# 动作
# ------------------------------------------------------------
wait_gone() {  # $1=PID $2=名字 → 等它自己退出，超时返回 1
    local pid="$1" name="$2" i=0
    while [ "$i" -lt "$STOP_TIMEOUT" ]; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
        i=$((i + 1))
    done
    echo "⚠️  ${name}（PID ${pid}）${STOP_TIMEOUT}s 内没退干净"
    return 1
}

pgrep_any_userbot() { [ -n "$(userbot_pids)" ]; }

warn_duplicate_agents() {
    # 两个 Agent 同时跑 = 两个写者写 chrome_tasks.json（文件通道的前提是
    # 「Agent 独占写」）。2026-09-10 实测撞到过：一个 22:54 起的 Agent 残留
    # 没退，和后来起的那个并存了半小时，直到本脚本 stop 时才发现（它按
    # PID 文件 + 命令行两路探测，比只认 PID 文件更能兜住这种局面）。
    local n
    n="$(agent_pids | wc -l | tr -d ' ')"
    if [ "$n" -gt 1 ]; then
        echo "⚠️  检测到 ${n} 个 Chrome Agent 同时在跑（$(agent_pids | pids_text)）——"
        echo "    它们会同时写 chrome_tasks.json，任务状态可能互相覆盖。"
        echo "    建议：./run.sh stop 后 ./run.sh start 收拢成一个。"
    fi
}

do_stop() {
    local pid failed=0
    for pid in $(userbot_pids); do
        echo "▶ 优雅停机 userbot（PID ${pid}）…"
        kill -TERM "$pid" 2>/dev/null
        wait_gone "$pid" userbot || failed=1
    done
    if [ "$failed" = "1" ]; then
        for pid in $(userbot_pids); do
            echo "🔨 强制结束 userbot（PID ${pid}）——下次启动会按恢复规则续跑"
            kill -KILL "$pid" 2>/dev/null
        done
    fi

    for pid in $(agent_pids); do
        echo "▶ 优雅停机 Chrome Agent（PID ${pid}）…"
        kill -TERM "$pid" 2>/dev/null
        if ! wait_gone "$pid" "Chrome Agent"; then
            echo "🔨 强制结束 Chrome Agent（PID ${pid}）——RUNNING 任务会按恢复规则重跑"
            kill -KILL "$pid" 2>/dev/null
        fi
    done

    # 收尾：确认真的都没了
    if [ -z "$(userbot_pids)$(agent_pids)" ]; then
        echo "✅ 已全部停止（Chrome 专用实例保持运行）"
    else
        echo "⚠️  仍有进程存活，请用 ./run.sh status 查看"
    fi
}

do_start() {
    local proxy older
    proxy="$(resolve_proxy)"
    if [ "$proxy" = "none" ]; then
        warn_no_proxy
    else
        echo "▶ 代理来源：${proxy}"
    fi

    if pgrep_any_userbot; then
        echo "ℹ️  userbot 已在运行（PID $(userbot_pids | pids_text))，跳过"
    else
        nohup "$PY" "$USERBOT_SCRIPT" >>"$RUNTIME_DIR/userbot.out" 2>&1 &
        disown 2>/dev/null || true
        sleep 2
        if pgrep_any_userbot; then
            echo "✅ userbot 已启动（PID $(userbot_pids | pids_text)）"
            echo "   日志：${RUNTIME_DIR}/download.log"
            echo "   启动期 stdout/stderr：${RUNTIME_DIR}/userbot.out"
        else
            echo "❌ userbot 启动失败，看 ${RUNTIME_DIR}/userbot.out"
            return 1
        fi
    fi

    if [ -n "$(agent_pids)" ]; then
        echo "ℹ️  Chrome Agent 已在运行（PID $(agent_pids | pids_text)），跳过"
        warn_duplicate_agents
    else
        nohup "$PY" -m "$AGENT_MODULE" >>"$RUNTIME_DIR/chrome_agent.out" 2>&1 &
        disown 2>/dev/null || true
        sleep 2
        if [ -n "$(agent_pids)" ]; then
            echo "✅ Chrome Agent 已启动（PID $(agent_pids | pids_text)）"
            echo "   日志：${RUNTIME_DIR}/chrome_agent.log"
        else
            echo "⚠️  Chrome Agent 没起来，看 ${RUNTIME_DIR}/chrome_agent.out"
            echo "    （Chrome 不可用/未安装时 Agent 会主动退出，主流程不受影响）"
        fi
    fi
}

last_line() {  # $1=日志文件 → 最后一行
    [ -f "$1" ] && tail -n 1 "$1" 2>/dev/null
}

do_status() {
    local pid
    for pid in $(userbot_pids); do
        echo "🟢 userbot        PID ${pid}  已运行 $(uptime_of "$pid")"
        break
    done
    pgrep_any_userbot || echo "🔴 userbot        未运行"

    pid="$(agent_pid)"
    if [ -n "$pid" ]; then
        echo "🟢 Chrome Agent   PID ${pid}  已运行 $(uptime_of "$pid")"
    else
        echo "🔴 Chrome Agent   未运行"
    fi

    warn_duplicate_agents

    if command -v curl >/dev/null 2>&1; then
        if curl -s -m 2 "http://127.0.0.1:${CHROME_CDP_PORT:-9222}/json/version" \
                >/dev/null 2>&1; then
            echo "🟢 CDP            127.0.0.1:${CHROME_CDP_PORT:-9222} 可达"
        else
            echo "🔴 CDP            127.0.0.1:${CHROME_CDP_PORT:-9222} 不可达"
        fi
    fi

    case "$(resolve_proxy)" in
        none) echo "⚠️  代理           未配置（将直连）" ;;
        *)    echo "🟢 代理           已配置" ;;
    esac

    echo "── 最近日志 ──"
    echo "  userbot: $(last_line "$RUNTIME_DIR/download.log")"
    echo "  Agent  : $(last_line "$RUNTIME_DIR/chrome_agent.log")"
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop && do_start ;;
    status)  do_status ;;
    *)
        echo "用法：$(basename "$0") {start|stop|restart|status}"
        echo "  start    启动（已在跑就跳过）"
        echo "  stop     优雅停机（SIGTERM，超时才 SIGKILL）"
        echo "  restart  先优雅停机再启动"
        echo "  status   查看进程/CDP/代理状态与最近日志"
        exit 2
        ;;
esac
