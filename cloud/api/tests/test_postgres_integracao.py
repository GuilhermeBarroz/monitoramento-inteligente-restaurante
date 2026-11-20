"""
Integração com Postgres REAL (esquema, ingestão, CLI e permissões do portal).

Só roda com um banco DESCARTÁVEL apontado por TEST_DATABASE_URL (o teste apaga e recria
o schema `public`). Sem a variável, é pulado.

    # exemplo com container temporário:
    docker run --rm -d --name pg-teste -e POSTGRES_PASSWORD=teste -p 55432:5432 postgres:16-alpine
    set TEST_DATABASE_URL=postgresql://postgres:teste@127.0.0.1:55432/postgres
    cd cloud/api && python -m unittest tests.test_postgres_integracao -v
"""

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(DSN, "TEST_DATABASE_URL não definida (banco descartável necessário)")
class TestPostgresReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.psycopg = psycopg
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            for papel in ("portal_teste", "portal_leitura"):
                conn.execute(f"DROP ROLE IF EXISTS {papel}")  # nomes fixos do próprio teste
        os.environ["DATABASE_URL"] = DSN
        from app import db, main, admin

        cls.db, cls.main, cls.admin = db, main, admin
        db.fechar_pool()
        db.aplicar_schema()
        db.aplicar_schema()  # idempotente

        from fastapi.testclient import TestClient

        cls.client = TestClient(main.app)
        cls.client.__enter__()
        token = db.gerar_token()
        with db.obter_pool().connection() as conn:
            cls.est_id = conn.execute(
                "INSERT INTO estabelecimentos (nome, token_hash) VALUES (%s, %s) RETURNING id",
                ("Loja Teste", db.hash_token(token)),
            ).fetchone()[0]
        cls.auth = {"Authorization": f"Bearer {token}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.db.fechar_pool()
        with cls.psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP ROLE IF EXISTS portal_teste")
            conn.execute("DROP ROLE IF EXISTS portal_leitura")

    def _evento(self, i):
        return {
            "id": i, "epoch": 1_790_000_000.0 + i, "iso_datahora": "2026-11-13 12:00:00",
            "dia_semana": 4, "hora": 12, "camera_id": "cam_1", "camera_nome": "Salão",
            "mesa_id": f"Mesa {i}", "tipo_evento": "ALERTA_DISPARADO", "detalhes_json": '{"alerta": "MESA_SUJA"}',
        }

    def test_1_ingestao_idempotente_por_instalacao(self):
        inst_a = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"
        inst_b = "11111111-2222-4333-8444-555555555555"
        corpo = {"instalacao_id": inst_a, "registros": [self._evento(1), self._evento(2)]}
        r = self.client.post("/api/v1/ingest/eventos_mesas", json=corpo, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["inseridos"], 2)
        r = self.client.post("/api/v1/ingest/eventos_mesas", json=corpo, headers=self.auth)
        self.assertEqual(r.json()["inseridos"], 0)  # rowcount real com ON CONFLICT DO NOTHING
        corpo_b = {"instalacao_id": inst_b, "registros": [self._evento(1)]}
        self.assertEqual(self.client.post("/api/v1/ingest/eventos_mesas", json=corpo_b, headers=self.auth).json()["inseridos"], 1)
        with self.db.obter_pool().connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM eventos_mesas WHERE estabelecimento_id = %s", (self.est_id,)).fetchone()[0]
            tipo = conn.execute("SELECT detalhes->>'alerta' FROM eventos_mesas LIMIT 1").fetchone()[0]
        self.assertEqual(total, 3)
        self.assertEqual(tipo, "MESA_SUJA")

    def test_2_cli_usuarios_multiloja(self):
        with self.db.obter_pool().connection() as conn:
            outra = conn.execute(
                "INSERT INTO estabelecimentos (nome, token_hash) VALUES (%s, %s) RETURNING id",
                ("Loja 2", self.db.hash_token(self.db.gerar_token())),
            ).fetchone()[0]
        self.assertEqual(self.admin.main(["adicionar-usuario", "Dono@Exemplo.com", "gerente", "--estabelecimento", str(self.est_id), str(outra)]), 0)
        self.assertEqual(self.admin.main(["definir-fuso", str(outra), "America/Manaus"]), 0)
        self.assertEqual(self.admin.main(["listar"]), 0)
        with self.db.obter_pool().connection() as conn:
            lojas = [r[0] for r in conn.execute(
                "SELECT estabelecimento_id FROM usuario_estabelecimentos WHERE email = %s ORDER BY 1", ("dono@exemplo.com",)
            ).fetchall()]
            fuso = conn.execute("SELECT timezone FROM estabelecimentos WHERE id = %s", (outra,)).fetchone()[0]
        self.assertEqual(lojas, sorted([self.est_id, outra]))
        self.assertEqual(fuso, "America/Manaus")
        with self.assertRaises(self.psycopg.errors.CheckViolation), self.db.obter_pool().connection() as conn:
            conn.execute("INSERT INTO usuarios (email, role) VALUES (%s, %s)", ("Maiuscula@X.com", "gerente"))

    def test_3_papel_do_portal_e_somente_leitura_e_sem_token(self):
        os.environ["PORTAL_DB_PASSWORD"] = "senha-de-teste-portal-123"
        try:
            self.assertEqual(self.admin.main(["criar-login-portal", "--usuario", "portal_teste"]), 0)
        finally:
            os.environ.pop("PORTAL_DB_PASSWORD", None)
        info = self.psycopg.conninfo.conninfo_to_dict(DSN)
        info.update(user="portal_teste", password="senha-de-teste-portal-123")
        with self.psycopg.connect(**info, autocommit=True) as conn:
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM estabelecimentos_portal").fetchone()[0], 1)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM eventos_mesas").fetchone()[0], 1)
            conn.execute("INSERT INTO portal_acessos (email, estabelecimento_id, acao) VALUES (%s, %s, %s)", ("x@y.com", self.est_id, "teste"))
            conn.execute("UPDATE usuarios SET ultimo_login_em = now() WHERE email = %s", ("dono@exemplo.com",))
            for proibido in (
                "SELECT token_hash FROM estabelecimentos",
                "INSERT INTO eventos_mesas (estabelecimento_id) VALUES (1)",
                "UPDATE usuarios SET role = 'admin'",
                "DELETE FROM visitas_concluidas",
            ):
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege, msg=proibido):
                    conn.execute(proibido)


if __name__ == "__main__":
    unittest.main(verbosity=2)
