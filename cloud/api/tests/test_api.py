"""
Testes da API de ingestão com banco simulado (não exige Postgres).

    cd cloud/api && python -m unittest discover -s tests -v
"""

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from psycopg_pool import PoolTimeout  # noqa: E402

from app import db, main  # noqa: E402

TOKEN_OK = "a" * 43
INSTALACAO = "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b"


class _ResultadoFalso:
    def __init__(self, linha=None, rowcount=0):
        self._linha = linha
        self.rowcount = rowcount

    def fetchone(self):
        return self._linha


class _ConexaoFalsa:
    def __init__(self, pool):
        self.pool = pool

    def execute(self, sql, params=()):
        self.pool.registro.append((sql, params))
        if "FROM estabelecimentos WHERE token_hash" in sql:
            return _ResultadoFalso((7,) if params[0] == db.hash_token(TOKEN_OK) else None)
        return _ResultadoFalso()

    @contextmanager
    def cursor(self):
        pool = self.pool

        class _Cursor:
            rowcount = 0

            def executemany(self, sql, linhas):
                linhas = list(linhas)
                pool.registro.append((sql, linhas))
                # Simula ON CONFLICT DO NOTHING: só conta chaves (tabela, est, instalação, origem) novas
                tabela = sql.split("INSERT INTO", 1)[1].split()[0]
                novas = [(tabela, *l[:3]) for l in linhas if (tabela, *l[:3]) not in pool.chaves]
                pool.chaves.update(novas)
                self.rowcount = len(novas)

        yield _Cursor()


class _PoolFalso:
    def __init__(self):
        self.registro = []
        self.chaves = set()
        self.esgotado = False

    @contextmanager
    def connection(self):
        if self.esgotado:
            raise PoolTimeout("pool esgotado")
        yield _ConexaoFalsa(self)


class _BaseApi(unittest.TestCase):
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


class TestApiIngestao(_BaseApi):
    def _evento(self, i=1, **extra):
        base = {
            "id": i, "epoch": 1_790_000_000.0 + i, "iso_datahora": "2026-11-13 12:00:00",
            "dia_semana": 4, "hora": 12, "camera_id": "cam_1", "camera_nome": "Salão",
            "mesa_id": "Mesa 1", "tipo_evento": "OCUPACAO", "detalhes_json": '{"a": 1}',
        }
        base.update(extra)
        return base

    def _lote(self, *registros, instalacao=INSTALACAO):
        corpo = {"registros": list(registros)}
        if instalacao is not None:
            corpo["instalacao_id"] = instalacao
        return corpo

    def _post(self, corpo, tabela="eventos_mesas", headers=None):
        return self.client.post(f"/api/v1/ingest/{tabela}", json=corpo, headers=self.auth if headers is None else headers)

    def test_sem_token_ou_token_errado_retorna_401(self):
        self.assertEqual(self._post(self._lote(self._evento()), headers={}).status_code, 401)
        r = self._post(self._lote(self._evento()), headers={"Authorization": "Bearer " + "b" * 43})
        self.assertEqual(r.status_code, 401)

    def test_estabelecimento_vem_do_token_nao_do_payload(self):
        r = self._post(self._lote(self._evento()))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"recebidos": 1, "inseridos": 1, "ultimo_id": 1})
        insert = next(p for s, p in self.pool.registro if "INSERT INTO eventos_mesas" in s)
        self.assertEqual(insert[0][0], 7)
        self.assertEqual(insert[0][1], INSTALACAO)
        # Campo extra (ex: tentativa de forjar estabelecimento) é rejeitado.
        self.assertEqual(self._post(self._lote(self._evento(estabelecimento_id=99))).status_code, 422)

    def test_reenvio_nao_conta_como_novo_e_nova_instalacao_sim(self):
        self.assertEqual(self._post(self._lote(self._evento(1), self._evento(2))).json()["inseridos"], 2)
        self.assertEqual(self._post(self._lote(self._evento(1), self._evento(2))).json()["inseridos"], 0)
        # Banco local recriado: mesmos ids de origem, outra instalação → registros novos
        outra = "11111111-2222-4333-8444-555555555555"
        self.assertEqual(self._post(self._lote(self._evento(1), instalacao=outra)).json()["inseridos"], 1)

    def test_payloads_invalidos_retornam_422(self):
        ruins = [
            {"registros": [], "instalacao_id": INSTALACAO},
            self._lote(self._evento(hora=24)),
            self._lote(self._evento(detalhes_json="não-json")),
            self._lote(self._evento(detalhes_json="[1, 2]")),
            self._lote(self._evento(mesa_id="x" * 121)),
            self._lote(self._evento(2), self._evento(1)),
            self._lote(self._evento(1), self._evento(1)),
            self._lote(self._evento(iso_datahora="'; DROP TABLE eventos_mesas; --")),
            self._lote(self._evento(), instalacao=None),          # sem instalacao_id
            self._lote(self._evento(), instalacao="nao-e-uuid"),
            self._lote(self._evento(mesa_id="Mesa\x00 1")),        # NUL: Postgres recusaria (500)
            self._lote(self._evento(detalhes_json='{"a": "x\\u0000y"}')),
        ]
        for corpo in ruins:
            self.assertEqual(self._post(corpo).status_code, 422, corpo)

    def test_detalhes_json_ausente_vira_objeto_vazio(self):
        evento = self._evento()
        del evento["detalhes_json"]
        r = self._post(self._lote(evento))
        self.assertEqual(r.status_code, 200, r.text)
        insert = next(p for s, p in self.pool.registro if "INSERT INTO eventos_mesas" in s)
        self.assertEqual(insert[0][-1], "{}")  # antes: None numa coluna NOT NULL → 500

    def test_sql_e_sempre_parametrizado(self):
        malicioso = "Mesa 1'); DROP TABLE visitas_concluidas; --"
        r = self._post(self._lote(self._evento(mesa_id=malicioso)))
        self.assertEqual(r.status_code, 200)
        sql, linhas = next((s, p) for s, p in self.pool.registro if "INSERT INTO eventos_mesas" in s)
        self.assertNotIn("DROP", sql)
        self.assertIn(malicioso, linhas[0])

    def test_corpo_grande_ou_tamanho_invalido_retorna_413(self):
        # "²" em bytes latin-1: "²".isdigit() é True e int("²") quebrava com 500
        for tamanho in (str(main.TAMANHO_MAX_CORPO + 1), "²".encode("latin-1")):
            r = self.client.post(
                "/api/v1/ingest/eventos_mesas",
                content=b"{}",
                headers={**self.auth, "Content-Length": tamanho, "Content-Type": "application/json"},
            )
            self.assertEqual(r.status_code, 413, tamanho)

    def test_pool_esgotado_retorna_503(self):
        self.pool.esgotado = True
        self.assertEqual(self._post(self._lote(self._evento())).status_code, 503)


class TestContratoEdgeNuvem(_BaseApi):
    """
    Contrato entre o Edge Sync e a API: registros GERADOS pelo analytics_db do edge
    (num banco temporário, não o banco real do desenvolvedor) precisam ser aceitos.
    """

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(RAIZ / "src"))
        from analytics_db import RepositorioAnalytics  # noqa: E402
        import edge_sync  # noqa: E402

        self.edge_sync = edge_sync
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "h.db"
        repo = RepositorioAnalytics(db_path=self.db_path)
        agora = time.time()
        repo.registrar_evento(agora, "cam_1", "Salão", "Mesa 1", "ALERTA_DISPARADO", {"alerta": "MESA_SUJA", "t": 200.0})
        repo.registrar_evento(agora + 1, "cam_1", "Salão", "Mesa 2", "MESA_HIGIENIZADA", {"tempo_limpeza": 40.0, "com_alerta": False})
        repo.registrar_evento(agora + 2, "cam_1", "Salão", "Mesa 3", "OCUPACAO")
        repo.registrar_visita("cam_1", "Salão", "Mesa 1", agora, 1800, 120, 900, 3, True)
        repo.registrar_visita("cam_1", "Salão", "Mesa 2", agora, 600, None, None, 1, False)
        repo.registrar_amostra_ocupacao(agora, "cam_1", "Salão", 5, 2, 6, 1)
        self.instalacao = repo.obter_instalacao_id()

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def test_registros_do_edge_sao_aceitos_pela_api(self):
        for tabela in self.edge_sync.TABELAS_SINCRONIZADAS:
            registros = self.edge_sync.ler_lote(self.db_path, tabela, 0, 1000)
            self.assertTrue(registros, tabela)
            r = self.client.post(
                f"/api/v1/ingest/{tabela}",
                json={"instalacao_id": self.instalacao, "registros": registros},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200, f"{tabela}: {r.text[:500]}")
            self.assertEqual(r.json()["ultimo_id"], registros[-1]["id"])
            self.assertEqual(r.json()["inseridos"], len(registros))


if __name__ == "__main__":
    unittest.main(verbosity=2)
