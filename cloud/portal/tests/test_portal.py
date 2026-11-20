"""
Testes do portal sem banco real (rodam com o .venv do projeto: pandas + streamlit).

    cd cloud/portal && python -m unittest tests.test_portal -v

Cobrem: isolamento por loja e parametrização do SQL, allowlist/escopo de lojas,
geração do secrets.toml do login Google, escape do relatório HTML/CSV e a paridade
dos cálculos de pico/escala com o painel local (src/analytics_db.py).
"""

from collections import defaultdict
from datetime import datetime
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tomllib
import unittest
import zoneinfo

RAIZ_PORTAL = Path(__file__).resolve().parents[1]
RAIZ_REPO = RAIZ_PORTAL.parents[1]
sys.path.insert(0, str(RAIZ_PORTAL))

import pandas as pd  # noqa: E402

from app import acesso, calculos, configurar_auth, consultas  # noqa: E402


# ---------------------------------------------------------------------------
# Conexão falsa: registra SQL/parâmetros e devolve linhas roteiradas
# ---------------------------------------------------------------------------
class _Resultado:
    def __init__(self, linhas):
        self._linhas = linhas

    def fetchall(self):
        return list(self._linhas)

    def fetchone(self):
        return self._linhas[0] if self._linhas else None


class ConexaoFalsa:
    def __init__(self, roteiro=None, erro=None):
        self.chamadas = []
        self.roteiro = roteiro or []  # [(trecho_sql, linhas)]
        self.erro = erro

    def execute(self, sql, params=None):
        self.chamadas.append((sql, params))
        if self.erro:
            raise self.erro
        for trecho, linhas in self.roteiro:
            if trecho in sql:
                return _Resultado(linhas)
        return _Resultado([])


LINHA_RESUMO_V = {"total": 0, "sem_alerta": 0, "perm_med": None, "perm_p90": None, "espera_med": None,
                  "preparo_med": None, "media_pessoas": None}
LINHA_RESUMO_E = {"alertas": 0, "limpezas": 0, "limpezas_avaliaveis": 0, "limpezas_no_prazo": 0, "limpeza_med": None}


def _conexao_resumo(v=None, e=None):
    return ConexaoFalsa([("COUNT(*) AS total,", [v or LINHA_RESUMO_V]),
                         ("AS limpezas_avaliaveis", [e or LINHA_RESUMO_E])])


class TestIsolamentoESql(unittest.TestCase):
    MALICIOSO = "x' OR 1=1; DROP TABLE usuarios; --"

    def _rodar_todas(self, conn, est_id=7, camera=None):
        desde = consultas.desde_epoch(30, agora=1_800_000_000)
        consultas.listar_cameras(conn, est_id, agora=1_800_000_000)
        consultas.resumo_geral(conn, est_id, desde, camera)
        consultas.desempenho_por_mesa(conn, est_id, desde, camera)
        consultas.visitas_detalhadas(conn, est_id, "America/Sao_Paulo", desde, camera)
        consultas.relatorio_alertas(conn, est_id, "America/Sao_Paulo", desde, camera)
        consultas.agregados_pico(conn, est_id, "America/Sao_Paulo", desde, camera)

    def test_toda_consulta_filtra_pela_loja(self):
        conn = _conexao_resumo()
        self._rodar_todas(conn, est_id=7)
        self.assertGreaterEqual(len(conn.chamadas), 10)
        for sql, params in conn.chamadas:
            self.assertIn("estabelecimento_id = %(est)s", sql, sql)
            self.assertEqual(params["est"], 7)

    def test_valores_do_usuario_so_entram_como_parametro(self):
        conn = _conexao_resumo()
        self._rodar_todas(conn, camera=self.MALICIOSO)
        com_camera = 0
        for sql, params in conn.chamadas:
            self.assertNotIn("DROP TABLE", sql)
            self.assertNotIn("America/Sao_Paulo", sql)  # fuso também é parâmetro
            if "cam" in (params or {}):
                com_camera += 1
                self.assertEqual(params["cam"], self.MALICIOSO)
                self.assertIn("camera_id = %(cam)s", sql)
        self.assertGreaterEqual(com_camera, 8)

    def test_coluna_de_tempo_fora_da_lista_branca(self):
        with self.assertRaises(ValueError):
            consultas._filtro("epoch; DROP TABLE x", 1, None, None)

    def test_todo_o_historico_nao_filtra_tempo(self):
        sql, params = consultas._filtro("epoch", 3, consultas.desde_epoch(None), None)
        self.assertNotIn("desde", params)
        self.assertNotIn(">=", sql)

    def test_resumo_sem_visitas_nao_inventa_sla(self):
        r = consultas.resumo_geral(_conexao_resumo(), 1, None)
        self.assertIsNone(r["sla_sem_alerta_pct"])
        self.assertIsNone(r["sla_limpeza_pct"])
        self.assertIsNone(r["permanencia_mediana_min"])

    def test_resumo_converte_e_calcula_sla(self):
        v = dict(LINHA_RESUMO_V, total=4, sem_alerta=3, perm_med=1800.0, perm_p90=3000.0, espera_med=120.0, media_pessoas=2.5)
        e = dict(LINHA_RESUMO_E, alertas=2, limpezas=3, limpezas_avaliaveis=2, limpezas_no_prazo=1, limpeza_med=150.0)
        r = consultas.resumo_geral(_conexao_resumo(v, e), 1, None)
        self.assertEqual(r["sla_sem_alerta_pct"], 75.0)
        self.assertEqual(r["sla_limpeza_pct"], 50.0)
        self.assertEqual(r["permanencia_mediana_min"], 30.0)
        self.assertEqual(r["limpeza_mediana_min"], 2.5)
        self.assertEqual(r["total_alertas"], 2)

    def test_extrato_detecta_truncamento(self):
        linha = {"camera_nome": "C", "mesa_id": "M1", "entrada": "a", "saida": "b", "qtd_pessoas": 2,
                 "duracao_segundos": 600.0, "espera_garcom_segundos": None, "tempo_preparo_segundos": None,
                 "houve_alerta": False}
        conn = ConexaoFalsa([("FROM visitas_concluidas", [linha] * 3)])
        linhas, truncado = consultas.visitas_detalhadas(conn, 1, "UTC", None, limite=2)
        self.assertEqual(len(linhas), 2)
        self.assertTrue(truncado)
        self.assertEqual(conn.chamadas[0][1]["lim"], 3)

    def test_desempenho_junta_visitas_e_eventos(self):
        vis = [{"camera_id": "c1", "camera_nome": "Salão", "mesa_id": "M1", "visitas": 4, "sem_alerta": 2,
                "perm_med": 600.0, "perm_p90": 900.0, "espera_med": None}]
        evs = [{"camera_id": "c1", "camera_nome": "Salão", "mesa_id": "M1", "alertas": 3, "limpeza_med": 90.0},
               {"camera_id": "c1", "camera_nome": "Salão", "mesa_id": "M2", "alertas": 1, "limpeza_med": None}]
        conn = ConexaoFalsa([("FROM visitas_concluidas", vis), ("FROM eventos_mesas", evs)])
        linhas = consultas.desempenho_por_mesa(conn, 1, None)
        self.assertEqual([x["mesa_id"] for x in linhas], ["M1", "M2"])
        self.assertEqual(linhas[0]["sla_ok_pct"], 50.0)
        self.assertEqual(linhas[0]["alertas"], 3)
        self.assertEqual(linhas[0]["limpeza_mediana_min"], 1.5)
        self.assertEqual(linhas[1]["visitas"], 0)
        self.assertIsNone(linhas[1]["sla_ok_pct"])


class TestAcesso(unittest.TestCase):
    def test_email_da_identidade(self):
        ok = {"is_logged_in": True, "email": " Dono@Gmail.com ", "email_verified": True}
        self.assertEqual(acesso.email_da_identidade(ok), "dono@gmail.com")
        self.assertIsNone(acesso.email_da_identidade(dict(ok, is_logged_in=False)))
        self.assertIsNone(acesso.email_da_identidade(dict(ok, email_verified=False)))
        self.assertIsNone(acesso.email_da_identidade(dict(ok, email_verified="true")))  # só booleano True
        self.assertIsNone(acesso.email_da_identidade({k: v for k, v in ok.items() if k != "email_verified"}))
        self.assertIsNone(acesso.email_da_identidade(dict(ok, email="sem-arroba")))
        self.assertIsNone(acesso.email_da_identidade(dict(ok, email=None)))

    def test_fora_da_allowlist(self):
        conn = ConexaoFalsa()
        self.assertIsNone(acesso.carregar_perfil(conn, "x@y.com"))
        self.assertIn("AND ativo", conn.chamadas[0][0])

    def test_admin_ve_todas_as_lojas(self):
        lojas = [{"id": 1, "nome": "A", "timezone": "America/Sao_Paulo", "ativo": True},
                 {"id": 2, "nome": "B", "timezone": "America/Manaus", "ativo": False}]
        conn = ConexaoFalsa([("FROM usuarios", [{"email": "a@x.com", "nome": None, "role": "admin"}]),
                             ("FROM estabelecimentos_portal", lojas)])
        p = acesso.carregar_perfil(conn, "a@x.com")
        self.assertTrue(p.is_admin)
        self.assertEqual([x.id for x in p.lojas], [1, 2])

    def test_gerente_so_ve_lojas_vinculadas_e_ativas(self):
        conn = ConexaoFalsa([("FROM usuarios", [{"email": "g@x.com", "nome": "G", "role": "gerente"}]),
                             ("FROM estabelecimentos_portal e", [{"id": 5, "nome": "C", "timezone": "UTC", "ativo": True}])])
        p = acesso.carregar_perfil(conn, "g@x.com")
        self.assertFalse(p.is_admin)
        sql, params = conn.chamadas[1]
        self.assertIn("usuario_estabelecimentos", sql)
        self.assertIn("AND e.ativo", sql)
        self.assertEqual(params, {"email": "g@x.com"})
        self.assertEqual(acesso.loja_autorizada(p, 5).nome, "C")
        self.assertEqual(acesso.loja_autorizada(p, "5").id, 5)
        self.assertIsNone(acesso.loja_autorizada(p, 6))
        self.assertIsNone(acesso.loja_autorizada(p, "5 OR 1=1"))
        self.assertIsNone(acesso.loja_autorizada(p, None))

    def test_papel_desconhecido_nao_entra(self):
        conn = ConexaoFalsa([("FROM usuarios", [{"email": "z@x.com", "nome": None, "role": "root"}])])
        self.assertIsNone(acesso.carregar_perfil(conn, "z@x.com"))

    def test_auditoria(self):
        conn = ConexaoFalsa()
        acesso.registrar_acesso(conn, "a@x.com", "ver_relatorios", 3)
        sql, params = conn.chamadas[0]
        self.assertIn("INSERT INTO portal_acessos", sql)
        self.assertEqual(params, {"email": "a@x.com", "est": 3, "acao": "ver_relatorios"})
        with self.assertRaises(ValueError):
            acesso.registrar_acesso(conn, "a@x.com", "acao_inventada")
        # Falha do banco na auditoria não derruba a página
        acesso.registrar_acesso(ConexaoFalsa(erro=RuntimeError("down")), "a@x.com", "login")
        acesso.registrar_login(ConexaoFalsa(erro=RuntimeError("down")), "a@x.com")


class TestConfigurarAuth(unittest.TestCase):
    ENV = {
        "PORTAL_URL": "https://relatorios.exemplo.com.br/",
        "PORTAL_COOKIE_SECRET": "s" * 40,
        "GOOGLE_CLIENT_ID": "123.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": 'abc"def\\ghi',
        "PORTAL_DATABASE_URL": "postgresql://portal:senha@postgres:5432/monitor_mesas",
    }

    def test_gera_toml_valido_com_valores_exatos(self):
        dados = tomllib.loads(configurar_auth.montar_secrets(self.ENV))
        self.assertEqual(dados["auth"]["redirect_uri"], "https://relatorios.exemplo.com.br/oauth2callback")
        self.assertEqual(dados["auth"]["cookie_secret"], "s" * 40)
        self.assertEqual(dados["auth"]["google"]["client_secret"], 'abc"def\\ghi')  # aspas não quebram o TOML
        self.assertEqual(dados["auth"]["google"]["server_metadata_url"], configurar_auth.GOOGLE_METADATA_URL)

    def test_recusas(self):
        casos = {
            "http fora do localhost": dict(self.ENV, PORTAL_URL="http://relatorios.exemplo.com.br"),
            "domínio não definido": dict(self.ENV, PORTAL_URL="https://portal.invalid"),
            "url com query": dict(self.ENV, PORTAL_URL="https://a.com/?x=1"),
            "cookie curto": dict(self.ENV, PORTAL_COOKIE_SECRET="curto"),
            "client id vazio": dict(self.ENV, GOOGLE_CLIENT_ID="  "),
            "injeção de seção TOML": dict(self.ENV, GOOGLE_CLIENT_SECRET='x"\n[auth]\ncookie_secret="y'),
        }
        for nome, env in casos.items():
            with self.subTest(nome), self.assertRaises(configurar_auth.ConfigInvalida):
                configurar_auth.montar_secrets(env)

    def test_segredo_com_acento_e_emoji_vira_toml_valido(self):
        dados = tomllib.loads(configurar_auth.montar_secrets(dict(self.ENV, GOOGLE_CLIENT_SECRET="çã😀")))
        self.assertEqual(dados["auth"]["google"]["client_secret"], "çã😀")

    def test_localhost_em_http_para_desenvolvimento(self):
        toml = configurar_auth.montar_secrets(dict(self.ENV, PORTAL_URL="http://localhost:8501"))
        self.assertIn("http://localhost:8501/oauth2callback", toml)

    def test_valida_senha_do_banco(self):
        configurar_auth.validar_banco(self.ENV)
        for dsn in ("", "postgresql://portal:@postgres:5432/db"):
            with self.subTest(dsn=dsn), self.assertRaises(configurar_auth.ConfigInvalida):
                configurar_auth.validar_banco(dict(self.ENV, PORTAL_DATABASE_URL=dsn))

    def test_gravacao_atomica_sem_sobras(self):
        with tempfile.TemporaryDirectory() as d:
            destino = Path(d) / ".streamlit" / "secrets.toml"
            configurar_auth.gravar("[auth]\n", destino)
            configurar_auth.gravar("[auth]\nx = 1\n", destino)
            self.assertEqual(destino.read_text(encoding="utf-8"), "[auth]\nx = 1\n")
            self.assertEqual(sorted(p.name for p in destino.parent.iterdir()), ["secrets.toml"])
            if os.name == "posix":
                self.assertEqual(destino.stat().st_mode & 0o777, 0o600)

    def test_main_sem_configuracao_falha_com_codigo(self):
        antigo = dict(os.environ)
        try:
            for k in self.ENV:
                os.environ.pop(k, None)
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(configurar_auth.main([str(Path(d) / "s.toml")]), 1)
                self.assertFalse((Path(d) / "s.toml").exists())
        finally:
            os.environ.clear()
            os.environ.update(antigo)


class TestCalculosSaida(unittest.TestCase):
    def test_relatorio_html_escapa_textos_do_banco(self):
        x = "<script>alert(1)</script>"
        resumo = {"total_visitas": 1, "sla_sem_alerta_pct": None, "sla_limpeza_pct": None, "permanencia_mediana_min": 1.0,
                  "permanencia_p90_min": 2.0, "espera_mediana_min": None, "limpeza_mediana_min": None, "total_alertas": 0}
        desemp = [{"camera_nome": x, "mesa_id": x, "visitas": 1, "permanencia_mediana_min": 1.0, "espera_mediana_min": None,
                   "limpeza_mediana_min": None, "alertas": 0, "sla_ok_pct": 100.0}]
        turnos = calculos.ranking_turnos(calculos.resumo_por_hora([], 4), 4)
        doc = calculos.gerar_relatorio_html(x, x, x, resumo, desemp, turnos).decode("utf-8")
        self.assertNotIn("<script>", doc)
        self.assertIn("&lt;script&gt;", doc)

    def test_csv_neutraliza_formula(self):
        df = pd.DataFrame([{"Mesa": "=HYPERLINK(\"http://x\")", "Obs": "-", "N": 1}])
        texto = calculos.csv_seguro(df).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", texto)
        self.assertIn(",-,", texto)

    def test_tipo_e_tempo_de_alerta(self):
        self.assertEqual(calculos.tipo_alerta({"alerta": "ESPERA_GARCOM"}), "ESPERA_GARCOM")
        for det in ({}, None, {"alerta": ""}, {"alerta": None}, {"alerta": 5}, [1]):
            self.assertEqual(calculos.tipo_alerta(det), "ALERTA")
        self.assertEqual(calculos.tempo_decorrido_alerta({"tempo_cozinha": 1500}), "25:00")
        self.assertEqual(calculos.tempo_decorrido_alerta({"tempo_espera": "abc"}), "-")
        self.assertEqual(calculos.tempo_decorrido_alerta(None), "-")

    def test_status_sync(self):
        self.assertEqual(calculos.status_sync(60, True), "🟢 Em dia")
        self.assertEqual(calculos.status_sync(3600, True), "🟡 Atrasada")
        self.assertEqual(calculos.status_sync(86400, True), "🔴 Sem contato")
        self.assertEqual(calculos.status_sync(None, True), "⚪ Nunca sincronizou")
        self.assertEqual(calculos.status_sync(60, False), "⛔ Desativada")


# ---------------------------------------------------------------------------
# Paridade com o painel local: mesmos dados → mesma matriz, hora, dia e turnos
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, str(RAIZ_REPO / "src"))
    import analytics_db as local  # noqa: E402
except Exception:  # pragma: no cover - portal testado fora do repositório
    local = None


@unittest.skipIf(local is None, "src/analytics_db.py indisponível")
class TestParidadeComPainelLocal(unittest.TestCase):
    TZ = "America/Sao_Paulo"

    def _celula(self, epoch):
        dt = datetime.fromtimestamp(epoch, tz=zoneinfo.ZoneInfo(self.TZ))
        return dt.weekday(), dt.hour

    def _agregar_como_sql(self, amostras, visitas, alertas):
        """Reproduz em Python a semântica das consultas de `consultas.agregados_pico`."""
        por_minuto = defaultdict(lambda: {"oc": 0, "tot": 0, "pes": 0, "epoch": None})
        for a in amostras:
            b = por_minuto[int(a["epoch"] // 60)]
            b["oc"] += a["ocupadas"]
            b["tot"] += a["total"]
            b["pes"] += a["pessoas"]
            b["epoch"] = a["epoch"] if b["epoch"] is None else min(b["epoch"], a["epoch"])
        por_cel = defaultdict(list)
        for b in por_minuto.values():
            por_cel[self._celula(b["epoch"])].append(b)
        cel_am = {
            c: {"amostras": len(bs), "oc_media": sum(b["oc"] for b in bs) / len(bs), "oc_pico": max(b["oc"] for b in bs),
                "tot_media": sum(b["tot"] for b in bs) / len(bs), "pes_media": sum(b["pes"] for b in bs) / len(bs)}
            for c, bs in por_cel.items()
        }
        durs = defaultdict(list)
        for v in visitas:
            durs[self._celula(v["fim"] - v["dur"])].append(v["dur"])
        cel_vi = {c: {"giros": len(d), "perm_mediana_seg": statistics.median(d)} for c, d in durs.items()}
        cel_al = defaultdict(int)
        for e in alertas:
            cel_al[self._celula(e)] += 1
        return cel_am, cel_vi, dict(cel_al)

    def test_mesmos_resultados(self):
        base = float(int(time.time() - 3 * 86400) // 60 * 60)  # início de minuto: as 2 câmeras caem no mesmo instante
        amostras, visitas, alertas = [], [], []
        for i in range(300):  # 5h de amostras de 2 câmeras, a cada minuto
            ep = base + i * 60
            amostras.append({"epoch": ep, "cam": "c1", "ocupadas": i % 7, "total": 8, "pessoas": (i % 7) * 2})
            amostras.append({"epoch": ep + 5, "cam": "c2", "ocupadas": i % 3, "total": 4, "pessoas": i % 3})
        for i in range(25):
            dur = 1200.0 + i * 30
            visitas.append({"fim": base + i * 700 + dur, "dur": dur, "mesa": f"M{i % 5}"})
        for i in range(9):
            alertas.append(base + 400 + i * 1500)

        with tempfile.TemporaryDirectory() as d:
            repo = local.RepositorioAnalytics(db_path=Path(d) / "h.db", timezone_nome=self.TZ)
            for a in amostras:
                repo.registrar_amostra_ocupacao(a["epoch"], a["cam"], a["cam"], a["total"], a["ocupadas"], a["pessoas"], 0)
            for v in visitas:
                self.assertTrue(repo.registrar_visita("c1", "c1", v["mesa"], v["fim"], v["dur"], qtd_pessoas=2))
            for ep in alertas:
                repo.registrar_evento(ep, "c1", "c1", "M1", "ALERTA_DISPARADO", {"alerta": "ESPERA_GARCOM"})

            cel = self._agregar_como_sql(amostras, visitas, alertas)
            for ratio in (3, 4):
                with self.subTest(ratio=ratio):
                    m_portal = calculos.montar_matriz(*cel, ratio)
                    self.assertEqual(m_portal, repo.consultar_matriz_pico_hora_dia(dias=None, mesas_por_garcom=ratio))
                    h_portal = calculos.resumo_por_hora(m_portal, ratio)
                    self.assertEqual(h_portal, repo.consultar_resumo_por_hora(dias=None, mesas_por_garcom=ratio))
                    self.assertEqual(calculos.resumo_por_dia_semana(m_portal, ratio),
                                     repo.consultar_resumo_por_dia_semana(dias=None, mesas_por_garcom=ratio))
                    self.assertEqual(calculos.ranking_turnos(h_portal, ratio),
                                     repo.consultar_ranking_turnos(dias=None, mesas_por_garcom=ratio))


# ---------------------------------------------------------------------------
# Fluxo da página (Streamlit AppTest) com usuário Google simulado e banco falso
# ---------------------------------------------------------------------------
class _UsuarioFalso:
    def __init__(self, dados):
        self._dados = dados

    @property
    def is_logged_in(self):
        return bool(self._dados.get("is_logged_in"))

    def to_dict(self):
        return dict(self._dados)


class _PoolFalso:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        conn = self.conn

        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        return _Ctx()


class TestPaginaPortal(unittest.TestCase):
    SECRETS = {"redirect_uri": "http://localhost:8501/oauth2callback", "cookie_secret": "x" * 40,
               "google": {"client_id": "a", "client_secret": "b",
                          "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration"}}

    def _rodar(self, usuario: dict, conn: ConexaoFalsa):
        import types
        from unittest import mock

        import streamlit
        from streamlit.testing.v1 import AppTest

        import app as pacote_app

        streamlit.cache_data.clear()
        streamlit.cache_resource.clear()
        db_falso = types.SimpleNamespace(criar_pool=lambda: _PoolFalso(conn))
        with mock.patch.object(streamlit, "user", _UsuarioFalso(usuario)), \
                mock.patch.dict(sys.modules, {"app.db": db_falso}), \
                mock.patch.object(pacote_app, "db", db_falso, create=True):
            at = AppTest.from_file(str(RAIZ_PORTAL / "app" / "portal.py"), default_timeout=30)
            at.secrets["auth"] = self.SECRETS
            at.run()
        return at

    def test_sem_login_mostra_botao_e_nao_consulta_o_banco(self):
        conn = ConexaoFalsa()
        at = self._rodar({"is_logged_in": False}, conn)
        self.assertFalse(at.exception)
        self.assertEqual([b.label for b in at.button], ["Entrar com Google"])
        self.assertEqual(conn.chamadas, [])

    def test_email_nao_verificado_e_barrado(self):
        conn = ConexaoFalsa()
        at = self._rodar({"is_logged_in": True, "email": "a@x.com", "email_verified": False}, conn)
        self.assertIn("e-mail verificado", at.error[0].value)
        self.assertEqual(conn.chamadas, [])

    def test_fora_da_allowlist_e_barrado_e_auditado(self):
        conn = ConexaoFalsa()
        at = self._rodar({"is_logged_in": True, "email": "intruso@x.com", "email_verified": True}, conn)
        self.assertFalse(at.exception)
        self.assertIn("não está autorizado", at.error[0].value)
        acoes = [p.get("acao") for s, p in conn.chamadas if "portal_acessos" in s]
        self.assertEqual(acoes, ["acesso_negado"])
        self.assertFalse(any("visitas_concluidas" in s for s, _ in conn.chamadas))

    def test_gerente_ve_somente_a_propria_loja(self):
        conn = ConexaoFalsa([
            ("FROM usuarios", [{"email": "g@x.com", "nome": "Gerente", "role": "gerente"}]),
            ("FROM estabelecimentos_portal e", [{"id": 5, "nome": "Loja Cinco", "timezone": "America/Sao_Paulo", "ativo": True}]),
            ("COUNT(*) AS total,", [LINHA_RESUMO_V]),
            ("AS limpezas_avaliaveis", [LINHA_RESUMO_E]),
        ])
        at = self._rodar({"is_logged_in": True, "email": "G@x.com", "email_verified": True}, conn)
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(at.selectbox(key="loja").options, ["Loja Cinco"])
        self.assertEqual(len(at.tabs), 3)  # gerente não tem a aba de saúde das lojas
        consultas_loja = [p for s, p in conn.chamadas if p and "est" in p and "portal_acessos" not in s]
        self.assertGreaterEqual(len(consultas_loja), 8)
        self.assertTrue(all(p["est"] == 5 for p in consultas_loja))
        acoes = [p.get("acao") for s, p in conn.chamadas if "portal_acessos" in s]
        self.assertEqual(acoes, ["login", "ver_relatorios"])
        self.assertFalse(any("LATERAL" in s for s, _ in conn.chamadas))  # saúde das lojas é só do admin
        self.assertFalse(any("LIMIT %(lim)s" in s for s, _ in conn.chamadas))  # extrato pesado só sob demanda

    def test_admin_ve_aba_de_saude(self):
        conn = ConexaoFalsa([
            ("FROM usuarios", [{"email": "adm@x.com", "nome": None, "role": "admin"}]),
            ("LATERAL", [{"id": 1, "nome": "A", "ativo": True, "timezone": "UTC", "seg_desde_sync": 30.0,
                          "ultimo_sync_local": "2026-11-20 10:00", "ultima_amostra": None}]),
            ("FROM estabelecimentos_portal", [{"id": 1, "nome": "A", "timezone": "UTC", "ativo": True},
                                              {"id": 2, "nome": "B", "timezone": "UTC", "ativo": False}]),
            ("COUNT(*) AS total,", [LINHA_RESUMO_V]),
            ("AS limpezas_avaliaveis", [LINHA_RESUMO_E]),
        ])
        at = self._rodar({"is_logged_in": True, "email": "adm@x.com", "email_verified": True}, conn)
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(at.selectbox(key="loja").options, ["A", "B (desativada)"])
        self.assertEqual(len(at.tabs), 5)  # Relatórios + Saúde, e as 3 sub-abas
        acoes = [p.get("acao") for s, p in conn.chamadas if "portal_acessos" in s]
        self.assertEqual(acoes, ["login", "ver_relatorios", "ver_saude"])


if __name__ == "__main__":
    unittest.main()
