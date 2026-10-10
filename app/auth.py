"""账号与登录:PBKDF2 密码哈希 + 数据库会话令牌(Cookie)。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings

COOKIE_NAME = "yx_session"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def create_user(username: str, display_name: str, password: str, role: str = "teacher",
                must_change: bool = True) -> int:
    if role not in ("admin", "teacher"):
        raise ValueError("角色只能是 admin 或 teacher")
    return db.execute(
        "INSERT INTO users (username, display_name, password_hash, role, must_change_password, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (username, display_name or username, hash_password(password), role, int(must_change), db.now()),
    )


def ensure_admin() -> str | None:
    """首次启动时创建管理员。返回值:None=已有管理员;"env"=用 .env 中的密码;其他=随机生成的初始密码。"""
    if db.query_one("SELECT id FROM users WHERE role='admin' LIMIT 1"):
        return None
    if settings.admin_password:
        create_user(settings.admin_username, "教研负责人", settings.admin_password, role="admin", must_change=False)
        return "env"
    password = secrets.token_urlsafe(9)
    create_user(settings.admin_username, "教研负责人", password, role="admin", must_change=True)
    return password


def login(username: str, password: str) -> str | None:
    user = db.query_one("SELECT * FROM users WHERE username=? AND active=1", (username,))
    if not user or not verify_password(password, user["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=settings.session_days)).isoformat(timespec="seconds")
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
        conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                     (token, user["id"], expires))
    return token


def logout(token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token=?", (token,))


def user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    return db.query_one(
        "SELECT u.id, u.username, u.display_name, u.role, u.must_change_password"
        " FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token=? AND s.expires_at > ? AND u.active=1",
        (token, db.now()),
    )


def current_user(request: Request) -> dict:
    user = user_from_token(request.cookies.get(COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅教研负责人(管理员)可操作")
    return user
