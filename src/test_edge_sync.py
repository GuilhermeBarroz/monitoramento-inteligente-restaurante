"""
Testes do agente Edge Sync (sem rede externa: usa um servidor HTTP local falso).

    python src/test_edge_sync.py
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analytics_db import RepositorioAnalytics  # noqa: E402
import edge_sync  # noqa: E402

TOKEN = "t" * 43


class _ServidorFalso(BaseHTTPRequestHandler):
    recebidos = []
    falhar_com = None      # código HTTP a devolver para tudo, ou None
    falhar_tabela = {}     # {tabela: código HTTP} para falhar só uma tabela
    recusar_ids = set()    # ids que a "nuvem" recusa com 422 (registro inválido)
    redirecionar = False   # responde 302 para outro host
    pedidos_no_destino = []

    def _responder(self, codigo, headers=None):
        self.send_response(codigo)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/destino"):
            _ServidorFalso.pedidos_no_destino.append(self.headers.get("Authorization"))
            self._responder(200)
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._responder(401)
            return
        if _ServidorFalso.redirecionar:
            self._responder(302, {"Location": "/destino"})
            return
        tabela = self.path.rsplit("/", 1)[-1]
        codigo = _ServidorFalso.falhar_com or _ServidorFalso.falhar_tabela.get(tabela)
        if codigo:
            self._responder(codigo)
            return
        if any(r["id"] in _ServidorFalso.recusar_ids for r in corpo["registros"]):
            self._responder(422)
            return
        assert isinstance(corpo.get("instalacao_id"), str) and len(corpo["instalacao_id"]) == 36
        _ServidorFalso.recebidos.append((tabela, corpo["registros"]))
        resposta = json.dumps({"ultimo_id": corpo["registros"][-1]["id"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resposta)))
        self.end_headers()
        self.wfile.write(resposta)

    def log_message(self, *args):
        pass


class TestEdgeSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = HTTPServer(("127.0.0.1", 0), _ServidorFalso)
        cls.url = f"http://127.0.0.1:{cls.servidor.server_port}"
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()

    def setUp(self):
        _ServidorFalso.recebidos = []
        _ServidorFalso.falhar_com = None
        _ServidorFalso.falhar_tabela = {}
        _ServidorFalso.recusar_ids = set()
        _ServidorFalso.redirecionar = False
        _ServidorFalso.pedidos_no_destino = []
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db = base / "historico.db"
        self.estado = base / "sync_estado.json"
        self.repo = RepositorioAnalytics(db_path=self.db)
        agora = time.time()
        for i in range(7):
            self.repo.registrar_evento(agora + i, "cam_1", "Salão", f"Mesa {i}", "OCUPACAO", {"n": i})
        self.repo.registrar_visita("cam_1", "Salão", "Mesa 1", agora, 1800, 120, 900, 3, True)
        self.repo.registrar_amostra_ocupacao(agora, "cam_1", "Salão", 5, 2, 6, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _ciclo(self, lote=3):
        return edge_sync.executar_ciclo(self.url, TOKEN, self.db, self.estado, lote_max=lote)

    def test_envia_tudo_em_lotes_e_avanca_cursor(self):
        enviados = self._ciclo(lote=3)
        self.assertEqual(enviados, {"eventos_mesas": 7, "visitas_concluidas": 1, "amostras_ocupacao": 1})
        lotes_eventos = [r for t, r in _ServidorFalso.recebidos if t == "eventos_mesas"]
        self.assertEqual([len(l) for l in lotes_eventos], [3, 3, 1])
        self.assertEqual(edge_sync.carregar_estado(self.estado)["eventos_mesas"], 7)

    def test_segundo_ciclo_envia_apenas_novos(self):
        self._ciclo()
        _ServidorFalso.recebidos = []
        self.repo.registrar_evento(time.time(), "cam_1", "Salão", "Mesa 9", "LIVRE")
        enviados = self._ciclo()
        self.assertEqual(enviados["eventos_mesas"], 1)
        self.assertEqual(_ServidorFalso.recebidos[0][1][0]["id"], 8)

    def test_falha_de_rede_nao_avanca_cursor(self):
        _ServidorFalso.falhar_com = 503
        with self.assertRaises(edge_sync.ErroEnvio) as ctx:
            self._ciclo()
        self.assertFalse(ctx.exception.permanente)
        self.assertEqual(edge_sync.carregar_estado(self.estado)["eventos_mesas"], 0)
        _ServidorFalso.falhar_com = None
        self.assertEqual(self._ciclo()["eventos_mesas"], 7)

    def test_token_invalido_e_erro_permanente(self):
        with self.assertRaises(edge_sync.ErroEnvio) as ctx:
            edge_sync.executar_ciclo(self.url, "x" * 43, self.db, self.estado)
        self.assertTrue(ctx.exception.permanente)

    def test_envia_somente_colunas_permitidas(self):
        self._ciclo()
        for tabela, registros in _ServidorFalso.recebidos:
            self.assertEqual(set(registros[0]), set(edge_sync.TABELAS_SINCRONIZADAS[tabela]))

    def test_registro_recusado_vai_para_quarentena_sem_travar_a_fila(self):
        _ServidorFalso.recusar_ids = {4}  # evento #4 inválido para a nuvem
        enviados = self._ciclo(lote=3)
        ids_aceitos = sorted(r["id"] for t, regs in _ServidorFalso.recebidos if t == "eventos_mesas" for r in regs)
        self.assertEqual(ids_aceitos, [1, 2, 3, 5, 6, 7])
        estado = edge_sync.carregar_estado_completo(self.estado)
        self.assertEqual(estado["cursores"]["eventos_mesas"], 7)
        self.assertEqual([(q["tabela"], q["id"], q["status"]) for q in estado["quarentena"]], [("eventos_mesas", 4, 422)])
        self.assertEqual(enviados["visitas_concluidas"], 1)  # as outras tabelas seguiram

    def test_nuvem_recusando_tudo_nao_descarta_historico(self):
        # Ex.: nuvem atualizada incompatível com este edge: todo registro vira 422
        _ServidorFalso.recusar_ids = set(range(1, 100))
        with self.assertRaises(edge_sync.ErroEnvio) as ctx:
            self._ciclo(lote=8)
        # eventos: disjuntor (5 recusas seguidas); visitas/amostras: 1 registro sem comparação
        self.assertIn("eventos_mesas", str(ctx.exception))
        estado = edge_sync.carregar_estado_completo(self.estado)
        self.assertEqual(estado["cursores"], {"eventos_mesas": 0, "visitas_concluidas": 0, "amostras_ocupacao": 0})
        self.assertEqual(estado["cursores"]["eventos_mesas"], 0)  # nada pulado
        self.assertEqual(estado["quarentena"], [])                # nada descartado
        _ServidorFalso.recusar_ids = set()
        self.assertEqual(self._ciclo()["eventos_mesas"], 7)       # corrigida a nuvem, tudo segue

    def test_recusa_no_fim_do_lote_vai_para_quarentena(self):
        _ServidorFalso.recusar_ids = {7}
        self._ciclo(lote=10)
        estado = edge_sync.carregar_estado_completo(self.estado)
        self.assertEqual(estado["cursores"]["eventos_mesas"], 7)
        self.assertEqual([q["id"] for q in estado["quarentena"]], [7])

    def test_falha_numa_tabela_nao_impede_as_outras(self):
        _ServidorFalso.falhar_tabela = {"eventos_mesas": 503}
        with self.assertRaises(edge_sync.ErroEnvio) as ctx:
            self._ciclo()
        self.assertFalse(ctx.exception.permanente)
        cursores = edge_sync.carregar_estado(self.estado)
        self.assertEqual(cursores["eventos_mesas"], 0)
        self.assertEqual(cursores["visitas_concluidas"], 1)
        self.assertEqual(cursores["amostras_ocupacao"], 1)

    def test_banco_recriado_zera_cursores_e_reenvia(self):
        self._ciclo()
        inst_antiga = edge_sync.carregar_estado_completo(self.estado)["instalacao_id"]
        self.db.unlink()
        for sufixo in ("-wal", "-shm"):
            Path(str(self.db) + sufixo).unlink(missing_ok=True)
        repo_novo = RepositorioAnalytics(db_path=self.db)
        repo_novo.registrar_evento(time.time(), "cam_1", "Salão", "Mesa 1", "OCUPACAO")
        _ServidorFalso.recebidos = []

        enviados = self._ciclo()
        self.assertEqual(enviados["eventos_mesas"], 1)  # antes: perdido (id 1 < cursor 7)
        self.assertEqual(_ServidorFalso.recebidos[0][1][0]["id"], 1)
        self.assertNotEqual(edge_sync.carregar_estado_completo(self.estado)["instalacao_id"], inst_antiga)

    def test_estado_antigo_sem_instalacao_detecta_banco_recriado(self):
        self.estado.write_text(json.dumps({"cursores": {"eventos_mesas": 999}}), encoding="utf-8")
        enviados = self._ciclo()
        self.assertEqual(enviados["eventos_mesas"], 7)

    def test_redirecionamento_e_recusado_sem_vazar_token(self):
        _ServidorFalso.redirecionar = True
        with self.assertRaises(edge_sync.ErroEnvio) as ctx:
            self._ciclo()
        self.assertTrue(ctx.exception.permanente)
        self.assertEqual(_ServidorFalso.pedidos_no_destino, [])

    def test_validacao_url(self):
        self.assertEqual(edge_sync.validar_url_api("https://api.exemplo.com/"), "https://api.exemplo.com")
        for ruim in ["http://api.exemplo.com", "file:///etc/passwd", "https://u:s@api.exemplo.com", "", "https://"]:
            with self.assertRaises(edge_sync.ErroConfiguracao, msg=ruim):
                edge_sync.validar_url_api(ruim)
        self.assertTrue(edge_sync.validar_url_api("http://localhost:8000", permitir_http=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
