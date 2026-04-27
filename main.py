
import asyncio
from contextlib import asynccontextmanager

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

from fastapi.staticfiles import StaticFiles

app.include_router(follow_accounts_router)
app.include_router(okx_api_accounts_router)
app.include_router(auth_router)
app.include_router(manual_okx_router)
app.mount("/", StaticFiles(directory="../front/dist", html=True), name="static")

_monitor_tasks_started = False
_position_monitor_task = None
_margin_monitor_task = None
_monitor_lock_db = None
_monitor_lock_key = "okx_follow_monitor_singleton"


@asynccontextmanager
async def lifespan(_app):
    global _monitor_tasks_started, _position_monitor_task, _margin_monitor_task, _monitor_lock_db

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
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, )
