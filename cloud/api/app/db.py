"""Conexão com PostgreSQL (pool psycopg 3) e utilitários de token. Todas as queries são parametrizadas."""

import hashlib
import os
from pathlib import Path
import secrets
from typing import Optional

from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_pool: Optional[ConnectionPool] = None


def obter_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            raise RuntimeError("DATABASE_URL não configurada.")
        _pool = ConnectionPool(dsn, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "10")), open=True)
    return _pool


def fechar_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def aplicar_schema():
    with obter_pool().connection() as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def gerar_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
