"""Pool de conexões do portal (login Postgres SOMENTE LEITURA, membro de `portal_leitura`)."""

import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def criar_pool() -> ConnectionPool:
    dsn = os.environ.get("PORTAL_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("PORTAL_DATABASE_URL não configurada.")
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=int(os.environ.get("PORTAL_DB_POOL_MAX", "5")),
        kwargs={
            "row_factory": dict_row,
            # Leituras e a auditoria não precisam de transação longa
            "autocommit": True,
            # Relatório pesado não pode prender uma conexão indefinidamente
            "options": "-c statement_timeout=20000",
        },
        check=ConnectionPool.check_connection,
        timeout=float(os.environ.get("PORTAL_DB_POOL_TIMEOUT_SEG", "10")),
        max_lifetime=3600.0,
        open=True,
    )
