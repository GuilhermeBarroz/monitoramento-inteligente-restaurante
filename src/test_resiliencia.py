"""
Testes de regressão da Prioridade 2 (resiliência do motor 24/7).

Cobrem: config de câmeras nunca sobrescrita em erro de leitura, URL com porta
inválida, escrita atômica, entrega única de frame / detecção de stream congelado,
cursor de eventos que só avança após gravar, isolamento de falha por câmera,
zona malformada e regras inválidas sem aplicação parcial.

Execução: python src/test_resiliencia.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import config_cameras  # noqa: E402
import multi_cameras  # noqa: E402
from camera_stream import StreamCamera  # noqa: E402
from io_atomico import (  # noqa: E402
    assinatura_arquivo,
    escrever_imagem_atomica,
    escrever_json_atomico,
    limpar_temporarios_orfaos,
)
from motor_estados import MotorEstados  # noqa: E402


class _DirTemporario(unittest.TestCase):
    """Executa cada teste dentro de um diretório temporário (data/ relativo)."""

    def setUp(self):
        self._orig = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        config_cameras._ultima_config_valida.clear()

    def tearDown(self):
        os.chdir(self._orig)
        self._tmp.cleanup()


class TestConfigCameras(_DirTemporario):
    def _salvar_uma_camera(self, cfg: Path):
        return config_cameras.salvar_cameras_config(
            [{"id": "cam_1", "nome": "Salão", "fonte": "rtsp://192.168.1.10/stream", "ativa": True}],
            config_path=cfg,
        )

    def test_json_corrompido_nao_sobrescreve_e_usa_ultima_valida(self):
        cfg = Path("data") / "cameras_config.json"
        self._salvar_uma_camera(cfg)
        self.assertEqual(config_cameras.carregar_cameras_config(cfg)[0]["nome"], "Salão")

        cfg.write_text("{ isto não é json", encoding="utf-8")
        cams = config_cameras.carregar_cameras_config(cfg)

        self.assertEqual(cams[0]["nome"], "Salão")  # última válida em memória
        self.assertEqual(cfg.read_text(encoding="utf-8"), "{ isto não é json")  # arquivo preservado

    def test_json_corrompido_sem_cache_levanta_e_preserva_arquivo(self):
        cfg = Path("data") / "cameras_config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"cameras": []}', encoding="utf-8")

        with self.assertRaises(ValueError):
            config_cameras.carregar_cameras_config(cfg)
        self.assertEqual(cfg.read_text(encoding="utf-8"), '{"cameras": []}')

    def test_porta_invalida_rejeitada_e_mascara_nao_levanta(self):
        with self.assertRaisesRegex(ValueError, "porta"):
            config_cameras.sanitizar_fonte_camera("rtsp://u:senha@192.168.1.10:abc/s")
        mascarada = config_cameras.mascarar_fonte_segura("rtsp://u:senha@192.168.1.10:99999/s")
        self.assertNotIn("senha", mascarada)


class TestIoAtomico(_DirTemporario):
    def test_json_e_sem_temporarios_sobrando(self):
        destino = Path("data") / "estado.json"
        escrever_json_atomico(destino, {"a": 1})
        escrever_json_atomico(destino, {"a": 2})
        self.assertEqual(json.loads(destino.read_text(encoding="utf-8")), {"a": 2})
        self.assertEqual([p.name for p in destino.parent.iterdir()], ["estado.json"])

    def test_imwrite_falho_levanta_e_preserva_original(self):
        destino = Path("data") / "frame.jpg"
        escrever_imagem_atomica(destino, np.zeros((10, 10, 3), dtype=np.uint8))
        original = destino.read_bytes()

        with self.assertRaises(Exception):
            escrever_imagem_atomica(destino, np.zeros((0, 0, 3), dtype=np.uint8))
        self.assertEqual(destino.read_bytes(), original)
        self.assertEqual([p.name for p in destino.parent.iterdir()], ["frame.jpg"])

    @unittest.skipIf(os.name == "nt", "permissões POSIX")
    def test_arquivo_final_legivel_por_outros_processos(self):
        destino = Path("data") / "estado.json"
        escrever_json_atomico(destino, {"a": 1})
        self.assertEqual(destino.stat().st_mode & 0o777, 0o644)

    def test_limpa_temporarios_orfaos_antigos_e_preserva_recentes(self):
        pasta = Path("data")
        pasta.mkdir()
        velho = pasta / ".estado_atual_abc123.json.tmp"
        recente = pasta / ".estado_atual_def456.json.tmp"
        legado = pasta / "estado_atual.tmp.json"
        normal = pasta / "estado_atual.json"
        for p in (velho, recente, legado, normal):
            p.write_text("{}", encoding="utf-8")
        antigo = time.time() - 7200
        for p in (velho, legado, normal):
            os.utime(p, (antigo, antigo))

        self.assertEqual(limpar_temporarios_orfaos(pasta), 2)
        self.assertEqual(sorted(p.name for p in pasta.iterdir()), sorted([recente.name, normal.name]))

    def test_assinatura_muda_a_cada_escrita_atomica(self):
        destino = Path("data") / "regras.json"
        escrever_json_atomico(destino, {"v": 1})
        a1 = assinatura_arquivo(destino)
        mtime = destino.stat().st_mtime
        escrever_json_atomico(destino, {"v": 2})
        os.utime(destino, (mtime, mtime))  # simula mesmo tique de relógio
        self.assertNotEqual(assinatura_arquivo(destino), a1)
        self.assertIsNone(assinatura_arquivo(Path("data") / "nao_existe.json"))


class TestStreamFrescor(unittest.TestCase):
    def _stream_sem_thread(self) -> StreamCamera:
        s = StreamCamera(fonte="rtsp://192.168.1.10/stream", target_fps=1000.0)
        s.ultimo_frame = np.zeros((4, 4, 3), dtype=np.uint8)
        s.ultimo_status = True
        s._seq_frame = 1
        s._ts_ultimo_frame = time.monotonic()
        return s

    def test_cada_frame_entregue_uma_unica_vez(self):
        s = self._stream_sem_thread()
        ok1, _ = s.ler_se_pronto()
        s.ultimo_tempo_entrega = 0.0  # isola do controle de FPS: testa só a regra de frame repetido
        ok2, _ = s.ler_se_pronto()
        self.assertTrue(ok1)
        self.assertFalse(ok2)  # mesmo frame não é reprocessado

        s._seq_frame = 2
        s._ts_ultimo_frame = time.monotonic()
        s.ultimo_tempo_entrega = 0.0
        ok3, _ = s.ler_se_pronto()
        self.assertTrue(ok3)

    def test_stream_congelado_vira_desconectado(self):
        s = self._stream_sem_thread()
        self.assertTrue(s.conectada)
        s._ts_ultimo_frame = time.monotonic() - (StreamCamera.TIMEOUT_FRAME_NOVO_S + 1)
        self.assertFalse(s.conectada)
        ok, _ = s.ler_se_pronto()
        self.assertFalse(ok)

    def test_iniciar_nao_bloqueia_em_fonte_inacessivel(self):
        # TEST-NET-1 (RFC 5737): nunca responde. A conexão roda na thread.
        s = StreamCamera(fonte="rtsp://192.0.2.1:554/stream", target_fps=2.0)
        t0 = time.monotonic()
        s.iniciar()
        decorrido = time.monotonic() - t0
        s.parar(aguardar_s=0.1)
        self.assertLess(decorrido, 2.0)
        self.assertFalse(s.conectada)

    def test_reiniciar_apos_parar_nao_deixa_thread_antiga_interferir(self):
        s = StreamCamera(fonte="rtsp://192.0.2.1:554/stream", target_fps=2.0)
        s.iniciar()
        evento_antigo, thread_antiga = s._parar_evento, s.thread
        s.parar(aguardar_s=0.0)  # thread antiga ainda presa no open
        s.iniciar()
        try:
            self.assertIsNot(s._parar_evento, evento_antigo)
            self.assertTrue(evento_antigo.is_set())       # a antiga continua sinalizada
            self.assertFalse(s._parar_evento.is_set())    # a nova roda
            self.assertIsNot(s.thread, thread_antiga)
        finally:
            s.parar(aguardar_s=0.0)


class _BancoFalso:
    """Simula o RepositorioAnalytics com falhas programáveis."""

    def __init__(self):
        self.eventos = []
        self.visitas = []
        self.falhar_evento = []   # exceções a levantar nas próximas gravações de evento
        self.falhar_visita = []

    def registrar_evento(self, **kw):
        if self.falhar_evento:
            raise self.falhar_evento.pop(0)
        self.eventos.append(kw)

    def registrar_visita(self, **kw):
        if self.falhar_visita:
            raise self.falhar_visita.pop(0)
        self.visitas.append(kw)
        return True

    def obter_estatisticas_permanencia(self):
        return {}


def _gerenciador_sem_init(banco: _BancoFalso) -> multi_cameras.GerenciadorMultiCameras:
    ger = object.__new__(multi_cameras.GerenciadorMultiCameras)
    ger.analytics_db = banco
    ger.historico_global = multi_cameras.deque(maxlen=multi_cameras.MAX_EVENTOS_MEMORIA)
    ger.total_eventos = 0
    ger._ultimo_idx_eventos = {}
    ger._evento_parcial = {}
    ger._falha_evento = {}
    ger._ultimo_log_erro = {}
    ger._stats_permanencia = {}
    ger.erros_por_id = {}
    ger.runners = {}
    return ger


class TestCursorEventos(unittest.TestCase):
    def _motor(self, eventos):
        return SimpleNamespace(historico_alertas=list(eventos), camera_nome="Salão")

    def test_banco_ocupado_nao_perde_evento(self):
        banco = _BancoFalso()
        banco.falhar_evento = [sqlite3.OperationalError("database is locked")]
        ger = _gerenciador_sem_init(banco)
        motor = self._motor([{"tipo": "MESA_OCUPADA", "mesa_id": "M1", "epoch": 1.7e9}])

        ger.registrar_eventos_motor(motor, "cam_1")
        self.assertEqual(banco.eventos, [])
        self.assertEqual(ger._ultimo_idx_eventos["cam_1"], 0)  # cursor não avançou

        ger.registrar_eventos_motor(motor, "cam_1")
        self.assertEqual(len(banco.eventos), 1)
        self.assertEqual(ger.total_eventos, 1)

    def test_erro_permanente_do_banco_descarta_evento_apos_limite(self):
        banco = _BancoFalso()
        banco.falhar_evento = [sqlite3.OperationalError("disk I/O error")]  # só "A" falha
        ger = _gerenciador_sem_init(banco)
        motor = self._motor([
            {"tipo": "A", "mesa_id": "M1", "epoch": 1.7e9},
            {"tipo": "B", "mesa_id": "M2", "epoch": 1.7e9},
        ])
        with patch.object(multi_cameras, "LIMITE_RETENTATIVA_EVENTO_S", 0.0), patch("sys.stderr"):
            ger.registrar_eventos_motor(motor, "cam_1")
        # "A" descartado após o limite; a fila andou e "B" foi gravado
        self.assertEqual([e["tipo_evento"] for e in banco.eventos], ["B"])

    def test_evento_com_epoch_invalido_nao_trava_camera(self):
        banco = _BancoFalso()
        ger = _gerenciador_sem_init(banco)
        motor = self._motor([
            {"tipo": "A", "mesa_id": "M1", "epoch": "não-é-número"},
            {"tipo": "B", "mesa_id": "M2", "epoch": 1.7e9},
        ])
        with patch("sys.stderr"):
            ger.registrar_eventos_motor(motor, "cam_1")
        self.assertEqual([e["tipo_evento"] for e in banco.eventos], ["B"])

    def test_visita_falha_nao_duplica_evento(self):
        banco = _BancoFalso()
        banco.falhar_visita = [sqlite3.OperationalError("database is locked")]
        ger = _gerenciador_sem_init(banco)
        motor = self._motor([{"tipo": "MESA_DESOCUPADA", "mesa_id": "M1", "epoch": 1.7e9, "duracao_permanencia": 600}])

        ger.registrar_eventos_motor(motor, "cam_1")
        ger.registrar_eventos_motor(motor, "cam_1")
        self.assertEqual(len(banco.eventos), 1)
        self.assertEqual(len(banco.visitas), 1)

    def test_evento_invalido_e_pulado_sem_travar_fila(self):
        banco = _BancoFalso()
        banco.falhar_evento = [TypeError("dado inválido")]
        ger = _gerenciador_sem_init(banco)
        motor = self._motor([
            {"tipo": "X", "mesa_id": "M1", "epoch": 1.7e9},
            {"tipo": "Y", "mesa_id": "M2", "epoch": 1.7e9},
        ])
        with patch("sys.stderr"):
            ger.registrar_eventos_motor(motor, "cam_1")
        self.assertEqual([e["tipo_evento"] for e in banco.eventos], ["Y"])

    def test_historico_do_motor_e_compactado(self):
        banco = _BancoFalso()
        ger = _gerenciador_sem_init(banco)
        n = multi_cameras.COMPACTAR_HISTORICO_MOTOR_ACIMA + 5
        motor = self._motor([{"tipo": "E", "mesa_id": "M", "epoch": 1.7e9} for _ in range(n)])

        ger.registrar_eventos_motor(motor, "cam_1")
        self.assertEqual(len(banco.eventos), n)
        self.assertEqual(motor.historico_alertas, [])
        self.assertEqual(ger._ultimo_idx_eventos["cam_1"], 0)


class _FimDoLaco(Exception):
    pass


class TestIsolamentoCameras(unittest.TestCase):
    def test_falha_numa_camera_nao_para_as_outras(self):
        ger = _gerenciador_sem_init(_BancoFalso())
        ger.runners = {"cam_ruim": {}, "cam_boa": {}}
        ger._ultimo_refresh_stats = time.time()
        ger._ultima_amostra_db = time.time()
        ger.intervalo_amostra_seg = 9999
        processadas = []

        def processar(cid, runner, agora, t0):
            if cid == "cam_ruim":
                raise RuntimeError("frame corrompido")
            processadas.append(cid)
            return False

        def sleep_fim(_):
            raise _FimDoLaco()

        with patch.object(ger, "_recarregar_cameras", return_value=False), \
             patch.object(ger, "_processar_camera", side_effect=processar), \
             patch.object(ger, "exportar_estado_consolidado"), \
             patch.object(ger, "_salvar_json_atomico"), \
             patch.object(ger, "_parar_runner"), \
             patch.object(multi_cameras.time, "sleep", side_effect=sleep_fim), \
             patch("sys.stderr"):
            with self.assertRaises(_FimDoLaco):
                ger.executar_continuo()

        self.assertEqual(processadas, ["cam_boa"])
        self.assertIn("frame corrompido", ger.erros_por_id["cam_ruim"])


class TestMotorConfiguracao(_DirTemporario):
    def _criar_motor(self):
        cfg = Path("data") / "mesas_config.json"
        regras = Path("data") / "regras_config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"mesas": [{"id": "M1", "pontos": [[0, 0], [10, 0], [10, 10]]}]}), encoding="utf-8")
        regras.write_text(json.dumps({"limite_cozinha_minutos": 20}), encoding="utf-8")
        motor = MotorEstados(config_path=str(cfg), regras_path=str(regras), model_compartilhado=object())
        return motor, cfg, regras

    def test_zona_malformada_mantem_mesas_atuais(self):
        motor, cfg, _ = self._criar_motor()
        cfg.write_text(json.dumps({"mesas": [{"pontos": [[0, 0], [1, 1], [2, 2]]}]}), encoding="utf-8")  # sem id
        os.utime(cfg, (time.time() + 5, time.time() + 5))

        self.assertFalse(motor._carregar_mesas_do_disco())
        self.assertEqual([m["id"] for m in motor.mesas], ["M1"])
        self.assertFalse(motor._carregar_mesas_do_disco())  # mesma versão: não reprocessa

    def test_regras_invalidas_nao_aplicam_pela_metade(self):
        motor, _, regras = self._criar_motor()
        self.assertEqual(motor.fsms["M1"].limite_cozinha, 1200.0)

        regras.write_text(json.dumps({"limite_cozinha_minutos": 30, "uniforme_margem_px": "abc"}), encoding="utf-8")
        os.utime(regras, (time.time() + 5, time.time() + 5))
        motor._atualizar_regras_do_disco()

        self.assertEqual(motor.fsms["M1"].limite_cozinha, 1200.0)  # nada aplicado


if __name__ == "__main__":
    unittest.main(verbosity=2)
