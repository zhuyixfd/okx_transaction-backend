
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


@app.on_event("startup")
async def on_startup() -> None:
    import asyncio

    # 建表；若无 admin 用户则自动创建（与 migrate/init 等价的一次性引导）。
    try:
        if not db_config.MYSQL_DB:
            print("[startup] MYSQL_DB is empty; skipping init_db.")
        else:
            init_db()
            ensure_default_admin_user()
    except Exception as e:
        print(f"[startup] init_db failed: {e!r}")

    from v1.Services.margin_monitor import margin_monitor_loop
    from v1.Services.position_monitor import position_monitor_loop

    asyncio.create_task(position_monitor_loop())
    asyncio.create_task(margin_monitor_loop())


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
