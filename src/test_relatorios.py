"""
Testes de regressão da P4a: correções dos relatórios.

Cobrem: ocupação sem amostras não é inflada pelas visitas, SLA sem dados não vira
100%, SLA de limpeza, CSV/listas sem truncar, contagem por tipo de alerta sobre o
período inteiro, cache da matriz, exportação CSV sem injeção de fórmula e valores
de regras fora da faixa.

Execução: python src/test_relatorios.py
"""

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from analytics_db import RepositorioAnalytics  # noqa: E402
from util_painel import csv_seguro, fmt_pct, na_faixa  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = RepositorioAnalytics(db_path=Path(self._tmp.name) / "h.db")
        self.agora = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def _visita(self, fim_offset_s=600, dur=1800, alerta=False, mesa="M1"):
        self.repo.registrar_visita(
            camera_id="cam_1", camera_nome="Salão", mesa_id=mesa,
            fim_epoch=self.agora - fim_offset_s, duracao_segundos=dur, houve_alerta=alerta,
        )

    def _alerta(self, tipo_alerta, offset_s=60):
        self.repo.registrar_evento(
            epoch=self.agora - offset_s, camera_id="cam_1", camera_nome="Salão", mesa_id="M1",
            tipo_evento="ALERTA_DISPARADO", detalhes={"alerta": tipo_alerta},
        )


class TestOcupacaoESla(_Base):
    def test_hora_sem_amostra_nao_usa_visitas_como_ocupacao(self):
        for i in range(12):
            self._visita(fim_offset_s=600 + i)  # 12 visitas, nenhuma amostra de ocupação
        matriz = self.repo.consultar_matriz_pico_hora_dia(dias=None)
        self.assertTrue(matriz)
        for celula in matriz:
            self.assertEqual(celula["amostras"], 0)
            self.assertEqual(celula["ocupacao_media_mesas"], 0.0)  # antes: 12
            self.assertEqual(celula["garcons_recomendados_pico"], 0)
        self.assertEqual(sum(c["giros_visitas"] for c in matriz), 12)

    def test_resumo_por_hora_pondera_so_amostras_reais(self):
        self.repo.registrar_amostra_ocupacao(self.agora - 120, "cam_1", "Salão", 10, 4, 8, 0)
        self._visita()
        horas = {h["hora"]: h for h in self.repo.consultar_resumo_por_hora(dias=None)}
        hora_amostra = [h for h in horas.values() if h["ocupacao_pico_mesas"] == 4]
        self.assertEqual(len(hora_amostra), 1)
        self.assertEqual(hora_amostra[0]["ocupacao_media_mesas"], 4.0)

    def test_sla_sem_visitas_e_none_e_sla_limpeza(self):
        resumo = self.repo.consultar_resumo_geral(dias=None)
        self.assertIsNone(resumo["sla_sem_alerta_pct"])
        self.assertIsNone(resumo["sla_limpeza_pct"])

        self._visita(alerta=False)
        self._visita(alerta=True, fim_offset_s=700)
        higienizar = lambda det: self.repo.registrar_evento(self.agora - 30, "cam_1", "Salão", "M1", "MESA_HIGIENIZADA", det)
        higienizar({"tempo_limpeza": 90, "com_alerta": False})
        higienizar({"tempo_limpeza": 400, "com_alerta": True})
        higienizar({"tempo_limpeza": 60})  # evento antigo, sem o campo: fora do SLA
        # Alertas de mesa suja de mesas que NÃO foram limpas (cliente sentou na mesa suja)
        # não podem derrubar o SLA das limpezas que aconteceram.
        for _ in range(5):
            self._alerta("MESA_SUJA")
        self._alerta("ESPERA_ATENDIMENTO")

        resumo = self.repo.consultar_resumo_geral(dias=None)
        self.assertEqual(resumo["sla_sem_alerta_pct"], 50.0)
        self.assertEqual(resumo["total_limpezas"], 3)
        self.assertEqual(resumo["sla_limpeza_pct"], 50.0)  # 1 no prazo de 2 avaliáveis

    def test_fsm_marca_limpeza_com_e_sem_alerta(self):
        from fsm_mesas import EstadoMesa, MesaFSM

        def limpar_apos(segundos):
            fsm = MesaFSM("M1", limite_limpeza=180.0)
            fsm.estado = EstadoMesa.PENDENTE_LIMPEZA
            fsm.timestamp_estado = 0.0
            fsm.atualizar(0, False, timestamp_atual=segundos)  # pode disparar MESA_SUJA
            evts = fsm.atualizar(0, True, timestamp_atual=segundos + 1)  # funcionário limpa
            return next(e for e in evts if e["tipo"] == "MESA_HIGIENIZADA")

        self.assertFalse(limpar_apos(60)["com_alerta"])
        self.assertTrue(limpar_apos(300)["com_alerta"])

    def test_alerta_com_json_invalido_nao_derruba_contagem(self):
        self._alerta("ATRASO_COZINHA")
        with self.repo._conectar() as conn:
            conn.execute(
                "INSERT INTO eventos_mesas (epoch, iso_datahora, dia_semana, hora, camera_id, camera_nome, mesa_id, tipo_evento, detalhes_json) "
                "VALUES (?, '', 0, 0, 'cam_1', 'Salão', 'M1', 'ALERTA_DISPARADO', ?)",
                (self.agora - 10, "{json quebrado"),
            )
        rel = self.repo.consultar_relatorio_alertas(dias=None)
        self.assertEqual(rel["por_tipo"], {"ATRASO_COZINHA": 1, "ALERTA": 1})

    def test_desempenho_mesa_sem_visitas_tem_sla_none(self):
        self._alerta("ESPERA_ATENDIMENTO")  # mesa só com alerta, sem visita concluída
        linhas = self.repo.consultar_desempenho_por_mesa(dias=None)
        self.assertEqual(len(linhas), 1)
        self.assertIsNone(linhas[0]["sla_ok_pct"])


class TestSemTruncar(_Base):
    def test_visitas_e_alertas_sem_limite_e_contagem_por_tipo_do_periodo(self):
        for i in range(350):
            self._visita(fim_offset_s=600 + i)
        for i in range(320):
            self._alerta("ATRASO_COZINHA" if i % 2 else "ESPERA_ATENDIMENTO", offset_s=60 + i)

        self.assertEqual(len(self.repo.consultar_visitas_detalhadas(dias=None)), 300)
        self.assertEqual(len(self.repo.consultar_visitas_detalhadas(dias=None, limite=None)), 350)

        rel = self.repo.consultar_relatorio_alertas(dias=None, limite=10)
        self.assertEqual(len(rel["alertas"]), 10)
        self.assertEqual(sum(rel["por_tipo"].values()), 320)  # antes: só os 300 listados
        self.assertEqual(rel["por_tipo"], {"ATRASO_COZINHA": 160, "ESPERA_ATENDIMENTO": 160})
        self.assertEqual(self.repo.consultar_resumo_geral(dias=None)["total_alertas"], 320)


class TestCacheMatriz(_Base):
    def test_matriz_reaproveitada_na_mesma_instancia_e_isolada(self):
        self.repo.registrar_amostra_ocupacao(self.agora - 120, "cam_1", "Salão", 10, 4, 8, 0)
        m1 = self.repo.consultar_matriz_pico_hora_dia(dias=None)
        m1[0]["ocupacao_media_mesas"] = 999  # alterar o retorno não pode contaminar o cache
        self.repo.registrar_amostra_ocupacao(self.agora - 60, "cam_1", "Salão", 10, 10, 20, 0)
        m2 = self.repo.consultar_matriz_pico_hora_dia(dias=None)
        self.assertEqual(m2[0]["ocupacao_media_mesas"], 4.0)  # mesma instância: cache
        novo = RepositorioAnalytics(db_path=self.repo.db_path)
        self.assertEqual(novo.consultar_matriz_pico_hora_dia(dias=None)[0]["ocupacao_pico_mesas"], 10)


class TestUtilPainel(unittest.TestCase):
    def test_csv_neutraliza_formula_inclusive_tipo_texto_do_pandas(self):
        df = pd.DataFrame({
            "Mesa": ['=HYPERLINK("http://x","y")', "+SOMA(A1)", "@cmd", "Mesa 1", "-"],
            "Visitas": [1, 2, 3, 4, 5],
        })
        texto = csv_seguro(df).decode("utf-8-sig")
        lido = pd.read_csv(io.StringIO(texto), keep_default_na=False)
        self.assertEqual(list(lido["Mesa"]), ["'=HYPERLINK(\"http://x\",\"y\")", "'+SOMA(A1)", "'@cmd", "Mesa 1", "-"])
        self.assertEqual(list(lido["Visitas"]), [1, 2, 3, 4, 5])

    def test_na_faixa_e_fmt_pct(self):
        self.assertEqual(na_faixa({"limite_cozinha_minutos": 3}, "limite_cozinha_minutos", 20.0, 5.0, 45.0), 5.0)
        self.assertEqual(na_faixa({"x": 999}, "x", 4, 1, 12, int), 12)
        self.assertEqual(na_faixa({"x": "abc"}, "x", 20.0, 5.0, 45.0), 20.0)
        self.assertEqual(na_faixa({}, "x", 20.0, 5.0, 45.0), 20.0)
        self.assertEqual(fmt_pct(None), "-")
        self.assertEqual(fmt_pct(87.456), "87.5%")


class TestDashboardRegrasForaDaFaixa(unittest.TestCase):
    """O painel não pode cair com regras_config.json editado à mão fora da faixa."""

    def test_painel_admin_abre_com_regra_fora_da_faixa(self):
        import json
        import os
        from unittest.mock import patch

        from streamlit.testing.v1 import AppTest

        orig = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PAINEL_ADMIN_PIN": "48201937"}):
            os.chdir(tmp)
            try:
                Path("data").mkdir()
                Path("data/regras_config.json").write_text(
                    json.dumps({"limite_cozinha_minutos": 3, "mesas_por_garcom": 99, "uniforme_margem_px": "x"}),
                    encoding="utf-8",
                )
                at = AppTest.from_file(str(Path(__file__).parent / "dashboard.py"), default_timeout=60)
                at.run()
                at.sidebar.text_input[0].input("48201937")
                at.sidebar.button[0].click()
                at.run()
                self.assertFalse(at.exception, at.exception)
                slider = next(s for s in at.sidebar.slider if "Cozinha" in s.label)
                self.assertEqual(slider.value, 5.0)
            finally:
                os.chdir(orig)


if __name__ == "__main__":
    unittest.main(verbosity=2)
