"""
Testes da Prioridade 3: controle de acesso do painel local.

- Lógica pura (acesso_local.py): PIN, bloqueio por excesso de tentativas, expiração.
- Página real (dashboard.py) via streamlit.testing.AppTest, num diretório
  temporário: áreas restritas bloqueadas sem PIN e liberadas com PIN.

Execução: python src/test_acesso_local.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from acesso_local import (  # noqa: E402
    SESSAO_OCIOSA_S,
    LimitadorTentativas,
    iniciar_sessao_admin,
    pin_configurado,
    pin_fraco,
    sessao_admin_ativa,
    verificar_pin,
)

DASHBOARD = Path(__file__).parent / "dashboard.py"


class TestPin(unittest.TestCase):
    def test_pin_curto_ou_ausente_desativa_admin(self):
        self.assertIsNone(pin_configurado({}))
        self.assertIsNone(pin_configurado({"PAINEL_ADMIN_PIN": "12345"}))
        self.assertEqual(pin_configurado({"PAINEL_ADMIN_PIN": " 12345678 "}), "12345678")

        ok, msg = verificar_pin("qualquer", "1.1.1.1", LimitadorTentativas(), None)
        self.assertFalse(ok)
        self.assertIn("PAINEL_ADMIN_PIN", msg)

    def test_pin_correto_e_incorreto(self):
        lim = LimitadorTentativas()
        self.assertTrue(verificar_pin("12345678", "ip", lim, "12345678")[0])
        self.assertFalse(verificar_pin("00000000", "ip", lim, "12345678")[0])

    def test_bloqueio_apos_excesso_de_tentativas_e_por_ip(self):
        lim = LimitadorTentativas(max_falhas=3, janela_s=600, bloqueio_s=600)
        for i in range(3):
            verificar_pin("errado", "atacante", lim, "12345678", agora=100.0 + i)

        # Bloqueado mesmo com o PIN certo, enquanto durar o bloqueio
        ok, msg = verificar_pin("12345678", "atacante", lim, "12345678", agora=110.0)
        self.assertFalse(ok)
        self.assertIn("Muitas tentativas", msg)
        # Outro IP não é afetado
        self.assertTrue(verificar_pin("12345678", "gerente", lim, "12345678", agora=110.0)[0])
        # Após o bloqueio, volta a aceitar
        self.assertTrue(verificar_pin("12345678", "atacante", lim, "12345678", agora=110.0 + 601)[0])

    def test_limite_global_contem_rotacao_de_ip(self):
        lim = LimitadorTentativas(max_falhas=5, max_falhas_global=4, janela_global_s=3600, bloqueio_global_s=300)
        for i in range(4):  # cada tentativa de um IP diferente
            verificar_pin("errado", f"10.0.0.{i}", lim, "12345678", agora=10.0 + i)
        ok, msg = verificar_pin("12345678", "10.0.0.99", lim, "12345678", agora=20.0)
        self.assertFalse(ok)
        self.assertIn("Muitas tentativas", msg)
        self.assertTrue(verificar_pin("12345678", "10.0.0.99", lim, "12345678", agora=20.0 + 301)[0])

    def test_tentativa_correta_nao_conta_como_falha(self):
        lim = LimitadorTentativas(max_falhas=2, max_falhas_global=2)
        for t in range(5):
            self.assertTrue(verificar_pin("12345678", "ip", lim, "12345678", agora=float(t))[0])

    def test_pin_fraco(self):
        for fraco in ("123456", "987654", "000000", "aaaaaaaa"):
            self.assertTrue(pin_fraco(fraco), fraco)
        for forte in ("48201937", "135791", "a1b2c3"):
            self.assertFalse(pin_fraco(forte), forte)

    def test_falhas_antigas_fora_da_janela_nao_acumulam(self):
        lim = LimitadorTentativas(max_falhas=3, janela_s=60, bloqueio_s=600)
        verificar_pin("x", "ip", lim, "12345678", agora=0.0)
        verificar_pin("x", "ip", lim, "12345678", agora=1.0)
        verificar_pin("x", "ip", lim, "12345678", agora=500.0)  # as duas primeiras já expiraram
        self.assertEqual(lim.segundos_bloqueado("ip", agora=501.0), 0.0)

    def test_sessao_expira_por_ociosidade_e_fragmento_nao_renova(self):
        estado = {}
        iniciar_sessao_admin(estado, agora=1000.0)
        self.assertTrue(sessao_admin_ativa(estado, agora=1000.0 + 60))
        # Atualização automática (renovar=False) não estende a sessão
        self.assertTrue(sessao_admin_ativa(estado, agora=1060.0 + SESSAO_OCIOSA_S - 1, renovar=False))
        self.assertFalse(sessao_admin_ativa(estado, agora=1060.0 + SESSAO_OCIOSA_S + 1, renovar=False))
        self.assertFalse(estado["_admin_ok"])


class TestDashboardAcesso(unittest.TestCase):
    """Executa o dashboard real (sem navegador) num diretório de dados vazio."""

    def setUp(self):
        from streamlit.testing.v1 import AppTest  # import tardio: só se o Streamlit existir

        self.AppTest = AppTest
        self._orig = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        # Ambiente isolado durante TODO o teste (cada at.run() relê o PIN)
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.chdir(self._orig)
        self._tmp.cleanup()

    def _rodar(self, pin_env):
        if pin_env is None:
            os.environ.pop("PAINEL_ADMIN_PIN", None)
        else:
            os.environ["PAINEL_ADMIN_PIN"] = pin_env
        at = self.AppTest.from_file(str(DASHBOARD), default_timeout=60)
        at.run()
        return at

    @staticmethod
    def _textos(at):
        return " ".join(str(getattr(el, "value", "")) for el in list(at.info) + list(at.error) + list(at.success))

    def test_sem_login_areas_restritas_ficam_bloqueadas(self):
        at = self._rodar(pin_env="12345678")
        self.assertFalse(at.exception, at.exception)
        textos = self._textos(at)
        for area in ("visualização das câmeras", "relatórios gerenciais", "editor de mesas", "cadastro de câmeras"):
            self.assertIn(area, textos)
        # Regras do motor não aparecem para quem não é administrador
        self.assertFalse(any("Tempo Limite de Cozinha" in s.label for s in at.sidebar.slider))

    def test_pin_correto_libera_e_incorreto_nao(self):
        at = self._rodar(pin_env="12345678")
        at.sidebar.text_input[0].input("00000000")
        at.sidebar.button[0].click()
        at.run()
        self.assertIn("PIN incorreto", self._textos(at))
        self.assertFalse(any("Tempo Limite de Cozinha" in s.label for s in at.sidebar.slider))

        at.sidebar.text_input[0].input("12345678")
        at.sidebar.button[0].click()
        at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertIn("Modo administrador", self._textos(at))
        self.assertTrue(any("Tempo Limite de Cozinha" in s.label for s in at.sidebar.slider))
        self.assertNotIn("Área restrita ao administrador: editor de mesas", self._textos(at))

    def test_sem_pin_configurado_admin_fica_desativado(self):
        at = self._rodar(pin_env=None)
        at.sidebar.text_input[0].input("qualquer")
        at.sidebar.button[0].click()
        at.run()
        self.assertIn("PAINEL_ADMIN_PIN", self._textos(at))


if __name__ == "__main__":
    unittest.main(verbosity=2)
