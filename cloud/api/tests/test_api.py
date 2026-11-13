"""
Testes da API de ingestão com banco simulado (não exige Postgres).

    cd cloud/api && python -m unittest discover -s tests -v
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402

TOKEN_OK = "a" * 43
DB_LOCAL = Path(__file__).resolve().parents[3] / "data" / "historico_salao.db"


class _ResultadoFalso:
    def __init__(self, linha=None, rowcount=0):
        self._linha = linha
        self.rowcount = rowcount

    def fetchone(self):
        return self._linha


class _ConexaoFalsa:
    def __init__(self, registro):
        self.registro = registro

    def execute(self, sql, params=()):
        self.registro.append((sql, params))
        if "FROM estabelecimentos WHERE token_hash" in sql:
            return _ResultadoFalso((7,) if params[0] == db.hash_token(TOKEN_OK) else None)
        return _ResultadoFalso()

    @contextmanager
    def cursor(self):
        registro = self.registro

        class _Cursor:
            def executemany(self, sql, linhas):
                registro.append((sql, list(linhas)))

        yield _Cursor()


class _PoolFalso:
    def __init__(self):
        self.registro = []

    @contextmanager
    def connection(self):
        yield _ConexaoFalsa(self.registro)


def _registros_reais(tabela, colunas, limite=50):
    if not DB_LOCAL.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_LOCAL}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = f"SELECT {', '.join(colunas)} FROM {tabela} ORDER BY id LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (limite,)).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


class TestApiIngestao(unittest.TestCase):
    def setUp(self):
        self.pool = _PoolFalso()
        self.patches = [
            mock.patch.object(db, "obter_pool", return_value=self.pool),
            mock.patch.object(db, "aplicar_schema"),
            mock.patch.object(db, "fechar_pool"),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.auth = {"Authorization": f"Bearer {TOKEN_OK}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for p in self.patches:
            p.stop()

    def _evento(self, i=1, **extra):
        base = {
            "id": i, "epoch": 1_790_000_000.0 + i, "iso_datahora": "2026-11-13 12:00:00",
            "dia_semana": 4, "hora": 12, "camera_id": "cam_1", "camera_nome": "Salão",
            "mesa_id": "Mesa 1", "tipo_evento": "OCUPACAO", "detalhes_json": '{"a": 1}',
        }
        base.update(extra)
        return base

    def test_sem_token_ou_token_errado_retorna_401(self):
        r = self.client.post("/api/v1/ingest/eventos_mesas", json={"registros": [self._evento()]})
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            "/api/v1/ingest/eventos_mesas",
            json={"registros": [self._evento()]},
            headers={"Authorization": "Bearer " + "b" * 43},
        )
        self.assertEqual(r.status_code, 401)

    def test_estabelecimento_vem_do_token_nao_do_payload(self):
        r = self.client.post("/api/v1/ingest/eventos_mesas", json={"registros": [self._evento()]}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"recebidos": 1, "ultimo_id": 1})
        insert = next(p for s, p in self.pool.registro if "INSERT INTO eventos_mesas" in s)
        self.assertEqual(insert[0][0], 7)
        # Campo extra (ex: tentativa de forjar estabelecimento) é rejeitado.
        r = self.client.post(
            "/api/v1/ingest/eventos_mesas",
            json={"registros": [self._evento(estabelecimento_id=99)]},
            headers=self.auth,
        )
        self.assertEqual(r.status_code, 422)

    def test_payloads_invalidos_retornam_422(self):
        ruins = [
            {"registros": []},
            {"registros": [self._evento(hora=24)]},
            {"registros": [self._evento(detalhes_json="não-json")]},
            {"registros": [self._evento(mesa_id="x" * 121)]},
            {"registros": [self._evento(2), self._evento(1)]},
            {"registros": [self._evento(1), self._evento(1)]},
            {"registros": [self._evento(iso_datahora="'; DROP TABLE eventos_mesas; --")]},
        ]
        for corpo in ruins:
            r = self.client.post("/api/v1/ingest/eventos_mesas", json=corpo, headers=self.auth)
            self.assertEqual(r.status_code, 422, corpo)

    def test_sql_e_sempre_parametrizado(self):
        malicioso = "Mesa 1'); DROP TABLE visitas_concluidas; --"
        r = self.client.post(
            "/api/v1/ingest/eventos_mesas",
            json={"registros": [self._evento(mesa_id=malicioso)]},
            headers=self.auth,
        )
        self.assertEqual(r.status_code, 200)
        sql, linhas = next((s, p) for s, p in self.pool.registro if "INSERT INTO eventos_mesas" in s)
        self.assertNotIn("DROP", sql)
        self.assertIn(malicioso, linhas[0])

    def test_corpo_grande_demais_retorna_413(self):
        r = self.client.post(
            "/api/v1/ingest/eventos_mesas",
            content=b"{}",
            headers={**self.auth, "Content-Length": str(main.TAMANHO_MAX_CORPO + 1), "Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 413)

    def test_dados_reais_do_sqlite_local_sao_aceitos(self):
        colunas = {
            "eventos_mesas": ["id", "epoch", "iso_datahora", "dia_semana", "hora", "camera_id",
                              "camera_nome", "mesa_id", "tipo_evento", "detalhes_json"],
            "visitas_concluidas": ["id", "camera_id", "camera_nome", "mesa_id", "inicio_epoch", "fim_epoch",
                                   "dia_semana", "hora_inicio", "duracao_segundos", "espera_garcom_segundos",
                                   "tempo_preparo_segundos", "qtd_pessoas", "houve_alerta"],
            "amostras_ocupacao": ["id", "epoch", "dia_semana", "hora", "camera_id", "camera_nome",
                                  "total_mesas", "mesas_ocupadas", "pessoas_sentadas", "alertas_ativos"],
        }
        testou = False
        for tabela, cols in colunas.items():
            registros = _registros_reais(tabela, cols)
            if not registros:
                continue
            testou = True
            r = self.client.post(f"/api/v1/ingest/{tabela}", json={"registros": registros}, headers=self.auth)
            self.assertEqual(r.status_code, 200, f"{tabela}: {r.text[:500]}")
            self.assertEqual(r.json()["ultimo_id"], registros[-1]["id"])
        if not testou:
            self.skipTest("data/historico_salao.db sem registros")


if __name__ == "__main__":
    unittest.main(verbosity=2)
