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
    falhar_com = None  # código HTTP a devolver, ou None

    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return
        if _ServidorFalso.falhar_com:
            self.send_response(_ServidorFalso.falhar_com)
            self.end_headers()
            return
        tabela = self.path.rsplit("/", 1)[-1]
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

    def test_validacao_url(self):
        self.assertEqual(edge_sync.validar_url_api("https://api.exemplo.com/"), "https://api.exemplo.com")
        for ruim in ["http://api.exemplo.com", "file:///etc/passwd", "https://u:s@api.exemplo.com", "", "https://"]:
            with self.assertRaises(edge_sync.ErroConfiguracao, msg=ruim):
                edge_sync.validar_url_api(ruim)
        self.assertTrue(edge_sync.validar_url_api("http://localhost:8000", permitir_http=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
