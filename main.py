
import uvicorn

from config.settings import *
from config.constant import config as db_config
from config.db import get_db
from fastapi import Depends
from sqlalchemy import text
from config.db import init_db, SessionLocal

from v1.Models import FollowAccount, User  # noqa: F401 (register model metadata)
from v1.Routes.follow_accounts import router as follow_accounts_router, seed_follow_accounts
from v1.Routes.auth import router as auth_router, seed_admin_user

app = app

app.include_router(follow_accounts_router)
app.include_router(auth_router)


@app.on_event("startup")
def on_startup() -> None:
    # Create tables + seed initial follow accounts.
    # Failures should not block the service start; endpoints will report DB errors.
    try:
        if not db_config.MYSQL_DB:
            print("[startup] MYSQL_DB is empty; skipping init_db/seed.")
            return

        init_db()
        db = SessionLocal()
        try:
            seed_follow_accounts(db)
            seed_admin_user(db)
        finally:
            db.close()
    except Exception as e:
        print(f"[startup] init_db/seed failed: {e!r}")


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


@app.get("/login")
def login_page() -> "HTMLResponse":
    """
    Backend-served login page.

    Note: this page talks to `POST /auth/login` and stores the token in browser localStorage.
    """
    from starlette.responses import HTMLResponse

    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>登录 - okx跟单系统</title>
    <style>
      body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,'Apple Color Emoji','Segoe UI Emoji'; padding: 28px; }
      .card { max-width: 420px; margin: 0 auto; border: 1px solid rgba(0,0,0,.12); border-radius: 12px; padding: 20px; }
      h1 { font-size: 18px; margin: 0 0 12px 0; }
      label { display:block; margin-top: 12px; font-size: 13px; opacity: .9; }
      input { width:100%; box-sizing:border-box; padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(0,0,0,.16); outline: none; }
      button { width:100%; margin-top: 16px; padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(0,0,0,.16); background: #111; color:#fff; font-weight: 600; cursor:pointer; }
      .err { margin-top: 12px; color: #b00020; font-size: 13px; min-height: 18px; }
      .hint { margin-top: 10px; font-size: 12px; opacity: .7; line-height: 1.4; }
      code { font-family: ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono','Courier New',monospace; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>登录</h1>

      <label>用户名</label>
      <input id="username" type="text" autocomplete="username" />

      <label>密码</label>
      <input id="password" type="password" autocomplete="current-password" />

      <button id="btn" type="button">登录</button>
      <div id="err" class="err"></div>

      <div class="hint">
        默认账号：<code>admin</code> / <code>admin123456</code>（可通过环境变量 <code>DEFAULT_ADMIN_USERNAME</code> / <code>DEFAULT_ADMIN_PASSWORD</code> 修改）
      </div>
    </div>

    <script>
      const errEl = document.getElementById('err');
      const btn = document.getElementById('btn');
      btn.addEventListener('click', async () => {
        errEl.textContent = '';
        btn.disabled = true;
        btn.textContent = '登录中...';
        try {
          const username = document.getElementById('username').value.trim();
          const password = document.getElementById('password').value;
          const res = await fetch('/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            errEl.textContent = data.detail || '登录失败';
            return;
          }
          localStorage.setItem('okx_token', data.token);
          window.location.href = '/';
        } catch (e) {
          errEl.textContent = '请求失败：' + (e && e.message ? e.message : e);
        } finally {
          btn.disabled = false;
          btn.textContent = '登录';
        }
      });
    </script>
  </body>
</html>
""".strip()
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, )
