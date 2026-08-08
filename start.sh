#!/usr/bin/env bash
# soroban 一键启动：首次运行自动建 venv、装依赖、建 admin；之后直接起后端+前端。
# 用法：./start.sh   （Ctrl+C 同时停掉两个服务）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
PY_BIN="$BACKEND/.venv/bin/python"
UVICORN_BIN="$BACKEND/.venv/bin/uvicorn"

# 端口：避开常见默认(8000/5173)，防与其它项目冲突。可用环境变量覆盖，如 BACKEND_PORT=9620 ./start.sh
BACKEND_PORT="${BACKEND_PORT:-8620}"
FRONTEND_PORT="${FRONTEND_PORT:-8621}"

# 监听地址：前后端**同一个**旋钮，默认只绑环回。要暴露到局域网：HOST=0.0.0.0 ./start.sh
# 分成两个旋钮会出事——前端 dev server 反代 /api 到后端，前端单边对外就等于后端也对外。
HOST="${HOST:-127.0.0.1}"

export BACKEND_PORT FRONTEND_PORT HOST   # vite.config 读这仨配监听/端口/代理；后端读 BACKEND_PORT 拼插件回灌地址

green() { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red() { printf "\033[31m%s\033[0m\n" "$1"; }

command -v python3 >/dev/null || { red "缺少 python3"; exit 1; }
command -v node >/dev/null || { red "缺少 node（前端需要）"; exit 1; }

# 建 venv 用的解释器：需 Python 3.11 或 3.12。
#  · 下限 3.11——插件发现用了标准库 tomllib（3.11 才有）。
#  · 上限 <3.13——OCR 依赖 rapidocr_onnxruntime→onnxruntime/numpy 在全新 Python 上常无预编译 wheel，3.13 装依赖会失败。
# 优先显式的 python3.12 / python3.11，其次默认 python3（若其恰在区间内）。仅新建 venv 时才需要它。
pick_py() {
  for c in python3.12 python3.11 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info < (3,13) else 1)' 2>/dev/null \
      && { echo "$c"; return 0; }
  done
  return 1
}

# requirements/package.json 指纹：用系统 python3 算 sha256（跨平台，免依赖 shasum/sha256sum 差异）
sha256() { python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

# ---- 后端依赖：venv 不存在则建；requirements 变了(如新增 alembic)则自动同步，避免启动缺库 ----
if [ ! -d "$BACKEND/.venv" ]; then
  VENV_PY="$(pick_py)" || {
    red "需要 Python 3.11 或 3.12 建环境（3.13+ 暂不支持：OCR 依赖 onnxruntime 尚无 3.13 预编译包）。"
    red "当前默认 $(python3 -V)。装个 3.12 再跑，例如：brew install python@3.12  或  pyenv install 3.12"
    exit 1
  }
  yellow "首次运行：用 $("$VENV_PY" -V) 创建 Python 虚拟环境…"
  "$VENV_PY" -m venv "$BACKEND/.venv"
  "$PY_BIN" -m pip install --quiet --upgrade pip
fi
REQ_STAMP="$BACKEND/.venv/.requirements.sha256"
REQ_HASH="$(sha256 "$BACKEND/requirements.txt")"
if [ "$(cat "$REQ_STAMP" 2>/dev/null || true)" != "$REQ_HASH" ]; then
  yellow "同步后端依赖（首次安装或 requirements 有更新）…"
  "$PY_BIN" -m pip install --quiet -r "$BACKEND/requirements.txt"
  echo "$REQ_HASH" > "$REQ_STAMP"   # 装成功才落指纹（失败时 set -e 会先退出，不会误记）
  green "后端依赖已就绪。"
fi

# .env：不存在则从模板生成，并写入随机 SECRET_KEY
if [ ! -f "$BACKEND/.env" ]; then
  SECRET="$("$PY_BIN" -c 'import secrets; print(secrets.token_hex(32))')"
  sed "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" "$BACKEND/.env.example" > "$BACKEND/.env"
  green "已生成 backend/.env（含随机 SECRET_KEY）。"
fi

# 建/确认 admin 账号（幂等）
( cd "$BACKEND" && "$PY_BIN" -m app.seed )

# ---- 前端依赖：node_modules 不存在或 package.json 变了则 npm install，避免启动缺库 ----
PKG_STAMP="$FRONTEND/node_modules/.package.sha256"
PKG_HASH="$(sha256 "$FRONTEND/package.json")"
if [ ! -d "$FRONTEND/node_modules" ] || [ "$(cat "$PKG_STAMP" 2>/dev/null || true)" != "$PKG_HASH" ]; then
  yellow "同步前端依赖（首次安装或 package.json 有更新）…"
  ( cd "$FRONTEND" && npm install )
  echo "$PKG_HASH" > "$PKG_STAMP"
  green "前端依赖已就绪。"
fi

# ---- 启动 ----
BACK_PID=""
FRONT_PID=""
cleanup() {
  echo
  yellow "正在停止…"
  [ -n "$BACK_PID" ] && kill "$BACK_PID" 2>/dev/null || true
  [ -n "$FRONT_PID" ] && kill "$FRONT_PID" 2>/dev/null || true
  # 顺手清掉可能残留的子进程
  pkill -P "$BACK_PID" 2>/dev/null || true
  pkill -P "$FRONT_PID" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

green "启动后端  → http://127.0.0.1:$BACKEND_PORT  (API 文档 /docs)"
( cd "$BACKEND" && exec "$UVICORN_BIN" app.main:app --host "$HOST" --port "$BACKEND_PORT" --reload ) &
BACK_PID=$!

green "启动前端  → http://localhost:$FRONTEND_PORT"
( cd "$FRONTEND" && exec npm run dev ) &
FRONT_PID=$!

echo
green "soroban 已启动。浏览器打开 http://localhost:$FRONTEND_PORT ，Ctrl+C 停止全部。"
if [ "$HOST" = "0.0.0.0" ]; then
  red "⚠️  已对外监听（HOST=0.0.0.0），前后端都能被局域网访问：请确认已改掉默认密码。"
fi
echo

wait
