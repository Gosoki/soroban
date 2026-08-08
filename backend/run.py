"""soroban 打包入口（PyInstaller 冻结后即 soroban.exe）。

单进程：uvicorn 起 FastAPI（app.main:app），后端**同源**托管打入包内的前端 frontend/dist，
API 与页面同端口。运行前把工作目录切到 exe 同级，使 .env、sqlite:///./soroban.db、scraper/
都相对 exe 目录解析——整包可随目录一起分发。

开发/源码运行不需要它，照旧用 start.bat 或 `uvicorn app.main:app`。

⚠️ 本文件承担 start.sh / start.bat 在源码模式下做的那份**安全初始化**：
生成含随机 SECRET_KEY 的 .env。冻结后没有别的地方会做这件事——曾经就是没做，
导致分发出去的 exe 用公开的默认 SECRET_KEY 签 JWT（任何人可伪造管理员令牌）。
"""

import os
import secrets
import sys
from pathlib import Path

# 首次启动生成的 .env 模板。刻意内联而不是读 .env.example：那要求把示例文件也打进包，
# 多一个会漂的依赖；这里只需要真正影响安全的那几项，其余走 config.py 的默认值。
_ENV_TEMPLATE = """\
# soroban 首次启动自动生成。SECRET_KEY 是随机的，请勿外传、勿提交。
# 它同时用于签发登录令牌与加密数据库连接串——换掉它会让所有人退出登录、
# 且已保存的 MySQL 连接串无法解密（会被视为无配置、回退本地 SQLite）。
SECRET_KEY={secret}

# 本地 SQLite 文件（同时是「控制库」，存当前后端与加密的 MySQL 连接串）。
# 是否使用 MySQL 由应用内「数据库」页决定，不由这里的串决定。
DATABASE_URL=sqlite:///./soroban.db

# 登录有效期（天）
TOKEN_EXPIRE_DAYS=90
"""


def _runtime_dir() -> Path:
    """运行时数据目录：打包后取 exe 同级，源码运行取 backend/。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_env(rt: Path) -> bool:
    """保证运行目录下有 .env（含随机 SECRET_KEY）。返回是否是本次新建的。

    必须在 `import app.*` **之前**调用——app.config 在导入时就实例化 Settings 并读 .env。
    已存在则原样不动（不覆盖用户改过的配置）。
    """
    f = rt / ".env"
    if f.exists():
        return False
    f.write_text(_ENV_TEMPLATE.format(secret=secrets.token_hex(32)), encoding="utf-8")
    try:                                   # 尽量收紧权限；Windows 上 chmod 基本无效，忽略即可
        f.chmod(0o600)
    except OSError:
        pass
    return True


def main() -> None:
    # 本入口只接受一个**精确白名单**：`--use-local-db [--yes]`（MySQL 连不上时的离线自救）。
    # 其余一律拒绝。这道保险是因为：打包成 exe 后 sys.executable 就是 exe 自己，任何
    # 「拿它当 python 跑」的误用（如 `soroban.exe -m venv …`）都会静默地**把 soroban 再启动
    # 一遍**——建 .env、连库跑迁移，最后卡在端口占用。直接报错退出，能把这类误用从
    # 「莫名其妙的影子实例」变成一眼可见的错误。
    argv = sys.argv[1:]
    rescue = False
    if argv:
        if set(argv) <= {"--use-local-db", "--yes"} and "--use-local-db" in argv:
            rescue = True
        else:
            print(f"soroban 不接受命令行参数（收到 {argv}）。"
                  "如果你想用它当 Python 解释器跑模块，那是用错了——请用系统的 python。",
                  file=sys.stderr)
            raise SystemExit(2)
    rt = _runtime_dir()
    os.chdir(rt)  # 让 .env / soroban.db（默认 sqlite:///./soroban.db）落在 exe 同级

    created_env = ensure_env(rt)           # ← 必须在任何 app.* 导入之前

    # 自救必须排在 chdir + ensure_env 之后（app.config 在导入时就读 .env 定 SECRET_KEY，
    # 而控制库的 MySQL 连接串是用它加密的），且排在 `from app.main import app` 之前——
    # 后者会连库跑迁移，而这条路径存在的前提正是「库连不上」。
    if rescue:
        from app.rescue import use_local_db
        raise SystemExit(use_local_db(assume_yes="--yes" in argv))

    # 默认只监听环回：要暴露到局域网得**显式**设 HOST=0.0.0.0。
    # 之前默认 0.0.0.0，配上「无 .env → 默认 SECRET_KEY」就是开箱即用的认证绕过。
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("BACKEND_PORT", "8620"))

    import uvicorn
    from app.main import app  # 触发建表/迁移在 lifespan 内进行

    # 幂等建库/迁移 + 确保有 admin（首次分发即可登录；已存在则跳过）。
    from app.seed import main as seed_admin
    try:
        seed_admin()
    except Exception as e:  # 建号失败不阻断启动（日志提示即可）
        print(f"[warn] 初始化 admin 失败：{e}")

    if created_env:
        print(f"已生成 {rt / '.env'}（含随机 SECRET_KEY）。请勿外传或提交。")
    print(f"soroban 已启动，监听 {host}:{port}  (API 文档 /docs)")
    print(f"  本机访问 -> http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    if host == "0.0.0.0":
        print(f"  局域网访问 -> http://<本机IP>:{port}（需放行防火墙 TCP {port}）")
        print("  ⚠️ 已对外监听：请确认已改掉默认密码，并只在可信网络里这么开。")
    print("按 Ctrl+C 退出。")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
