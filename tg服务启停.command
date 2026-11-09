#!/bin/bash
# ── tg_userbot 一键启停（macOS Finder 双击执行，2026-11-09）────────
# 逻辑：在跑 → 优雅停；没跑 → 启动。双击一次切换一次状态。
# 全部启停/状态逻辑都在同目录 run.sh，本文件只做「判断 + 切换 + 通知」。
#
# 放置：本文件在项目根即生效；桌面副本也可以（自动回落固定路径）。
# 首次若被 Gatekeeper 拦：右键 → 打开 → 打开（仅首次）。

DIR="$(cd "$(dirname "$0")" && pwd)"
# 桌面副本：本目录没有 run.sh → 回落固定项目路径
[ -f "$DIR/run.sh" ] || DIR="/Users/jiajun-chen/Documents/Claude/tg_userbot"

echo "── tg_userbot 一键启停 ──"
if bash "$DIR/run.sh" status 2>/dev/null | grep -q "🟢 userbot"; then
    echo "当前：🟢 运行中 → 执行 停止…"
    ACTION="stop"
else
    echo "当前：🔴 未运行 → 执行 启动…"
    ACTION="start"
fi

bash "$DIR/run.sh" "$ACTION"

# 结果复核 + macOS 系统通知（不看窗口也能知道切没切成功）
FINAL=$(bash "$DIR/run.sh" status 2>/dev/null)
if echo "$FINAL" | grep -q "🟢 userbot"; then
    echo "✅ 切换完成：服务已【启动】"
    osascript -e 'display notification "tg_userbot 已启动" with title "TG 服务"' \
        >/dev/null 2>&1
else
    echo "✅ 切换完成：服务已【停止】"
    osascript -e 'display notification "tg_userbot 已停止" with title "TG 服务"' \
        >/dev/null 2>&1
fi
echo "── 窗口 3 秒后自动关闭 ──"
sleep 3
