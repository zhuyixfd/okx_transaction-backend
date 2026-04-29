
import asyncio
import hashlib
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn

from config.settings import *
from config.constant import config as db_config
from config.db import get_db
from fastapi import Depends
from sqlalchemy import text
from config.db import init_db, SessionLocal

from v1.Models import (  # noqa: F401 — register model metadata for init_db()
    FollowAccount,
    FollowPositionEvent,
    FollowPositionSnapshot,
    FollowSimRecord,
    OkxApiAccount,
    User,
)
from v1.Routes.follow_accounts import router as follow_accounts_router
from v1.Routes.okx_api_accounts import router as okx_api_accounts_router
from v1.Routes.auth import ensure_default_admin_user, router as auth_router
from v1.Routes.manual_okx import router as manual_okx_router

app = app

app.include_router(follow_accounts_router)
app.include_router(okx_api_accounts_router)
app.include_router(auth_router)
app.include_router(manual_okx_router)

_monitor_tasks_started = False
_position_monitor_task = None
_margin_monitor_task = None
_monitor_lock_db = None
_monitor_lock_key = "okx_follow_monitor_singleton"
_monitor_lock_file_path: Path | None = None
_FILE_LOCK_STALE_SEC = 120.0


def _monitor_singleton_lock_path() -> Path:
    h = hashlib.sha256(_monitor_lock_key.encode("utf-8")).hexdigest()[:16]
    return Path(__file__).resolve().parent / "data" / "locks" / f"monitor_{h}.lock"


def _try_acquire_file_lock(lock_path: Path) -> bool:
    try:
        os.makedirs(lock_path.parent, exist_ok=True)
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age > _FILE_LOCK_STALE_SEC:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
            else:
                return False

        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def _release_file_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app):
    global _monitor_tasks_started, _position_monitor_task, _margin_monitor_task, _monitor_lock_db, _monitor_lock_file_path

    print(f"[startup] db_backend={db_config.database_backend} db_url={db_config.database_url}")

    # OKX REST 连接域名/是否模拟盘头：只在启动阶段读取一次，然后注入给 follow_order。
    try:
        from pydantic_settings import BaseSettings, SettingsConfigDict
        from module.follow_order import init_okx_connection_settings
        from pathlib import Path
        import sys

        def _pick_env_path() -> Path:
            candidates: list[Path] = []
            try:
                candidates.append(Path.cwd() / ".env")
            except Exception:
                pass
            try:
                candidates.append(Path(__file__).resolve().parent / ".env")
            except Exception:
                pass
            if getattr(sys, "frozen", False):
                try:
                    candidates.append(Path(sys.executable).resolve().parent / ".env")
                except Exception:
                    pass
                meipass = getattr(sys, "_MEIPASS", None)
                if meipass:
                    try:
                        candidates.append(Path(meipass) / ".env")
                    except Exception:
                        pass
            return next((p for p in candidates if p.exists() and p.is_file()), candidates[0] if candidates else Path(__file__).resolve().parent / ".env")

        env_path = _pick_env_path()

        class _OkxEnv(BaseSettings):
            model_config = SettingsConfigDict(env_file=str(env_path), env_file_encoding="utf-8", extra="ignore")
            OKX_FOLLOW_USE_PAPER: bool = False
            OKX_FOLLOW_REST_BASE: str

        okx_env = _OkxEnv()
        init_okx_connection_settings(use_paper=okx_env.OKX_FOLLOW_USE_PAPER, rest_base=okx_env.OKX_FOLLOW_REST_BASE)
        print(f"[startup] okx_rest_base={okx_env.OKX_FOLLOW_REST_BASE} okx_use_paper={okx_env.OKX_FOLLOW_USE_PAPER}")
    except Exception as e:
        print(f"[startup] okx config load/init failed: {e!r}")
        raise

    # 建表；若无 admin 用户则自动创建（与 migrate/init 等价的一次性引导）。
    try:
        init_db()
        ensure_default_admin_user()
    except Exception as e:
        print(f"[startup] init_db failed: {e!r}")

    from v1.Services.margin_monitor import margin_monitor_loop
    from v1.Services.position_monitor import position_monitor_loop

    if db_config.database_backend == "mysql":
        try:
            lock_db = SessionLocal()
            got = lock_db.execute(
                text("SELECT GET_LOCK(:k, 0)"),
                {"k": _monitor_lock_key},
            ).scalar_one_or_none()
            if int(got or 0) != 1:
                lock_db.close()
                print("[startup] monitor singleton lock busy; skip monitor tasks in this process.")
                return
            _monitor_lock_db = lock_db
            print("[startup] monitor singleton lock acquired.")
        except Exception as e:
            print(f"[startup] acquire monitor singleton lock failed: {e!r}")
            yield
            return
    else:
        try:
            _monitor_lock_file_path = _monitor_singleton_lock_path()
            got = _try_acquire_file_lock(_monitor_lock_file_path)
            if not got:
                print("[startup] monitor singleton file lock busy; skip monitor tasks in this process.")
                return
            print("[startup] monitor singleton file lock acquired.")
        except Exception as e:
            print(f"[startup] acquire monitor singleton file lock failed: {e!r}")
            yield
            return

    if not _monitor_tasks_started:
        _position_monitor_task = asyncio.create_task(position_monitor_loop())
        _margin_monitor_task = asyncio.create_task(margin_monitor_loop())
        _monitor_tasks_started = True

    try:
        yield
    finally:
        tasks = [t for t in (_position_monitor_task, _margin_monitor_task) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _position_monitor_task = None
        _margin_monitor_task = None
        _monitor_tasks_started = False
        if _monitor_lock_db is not None:
            try:
                _monitor_lock_db.execute(
                    text("SELECT RELEASE_LOCK(:k)"),
                    {"k": _monitor_lock_key},
                )
                print("[shutdown] monitor singleton lock released.")
            except Exception as e:
                print(f"[shutdown] release monitor singleton lock failed: {e!r}")
            finally:
                try:
                    _monitor_lock_db.close()
                except Exception:
                    pass
                _monitor_lock_db = None
        if _monitor_lock_file_path is not None:
            try:
                _release_file_lock(_monitor_lock_file_path)
                print("[shutdown] monitor singleton file lock released.")
            except Exception:
                pass
            _monitor_lock_file_path = None


app.router.lifespan_context = lifespan


@app.get("/health/db")
def health_db(db=Depends(get_db)):
    """
    DB health check (lazy, on request).
    """
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
