"""
Integração do portal com Postgres REAL: SQL dos relatórios, fuso da loja, isolamento
entre lojas e permissões do login somente leitura (`portal_leitura`).

Só roda com um banco DESCARTÁVEL em TEST_DATABASE_URL (o teste apaga e recria o
schema `public`). Precisa de psycopg + pandas no ambiente.

    docker run --rm -d --name pg-teste -e POSTGRES_PASSWORD=teste -p 55432:5432 postgres:16-alpine
    set TEST_DATABASE_URL=postgresql://postgres:teste@127.0.0.1:55432/postgres
    cd cloud/portal && python -m unittest tests.test_portal_postgres -v
"""

from datetime import datetime
import math
import os
from pathlib import Path
import statistics
import sys
import time
import unittest
from urllib.parse import urlparse, urlunparse
import zoneinfo

RAIZ_PORTAL = Path(__file__).resolve().parents[1]
SCHEMA = RAIZ_PORTAL.parent / "api" / "app" / "schema.sql"
sys.path.insert(0, str(RAIZ_PORTAL))

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()
SENHA_PORTAL = "senha-portal-teste"
TZ = "America/Sao_Paulo"
INST = "00000000-0000-0000-0000-000000000001"


def _p90(valores):
    v = sorted(valores)
    pos = 0.9 * (len(v) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return v[lo] if lo == hi else v[lo] * (hi - pos) + v[hi] * (pos - lo)


@unittest.skipUnless(DSN, "TEST_DATABASE_URL não definida (banco descartável necessário)")
class TestPortalPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        from app import acesso, consultas

        cls.psycopg, cls.acesso, cls.consultas = psycopg, acesso, consultas
        cls.agora = time.time()
        cls.base = cls.agora - 2 * 86400

        with psycopg.connect(DSN, autocommit=True) as su:
            su.execute("DROP SCHEMA public CASCADE")
            su.execute("CREATE SCHEMA public")
            for papel in ("portal_teste", "portal_leitura"):
                su.execute(f"DROP ROLE IF EXISTS {papel}")  # nomes fixos do próprio teste
            su.execute(SCHEMA.read_text(encoding="utf-8"))
            su.execute(f"CREATE ROLE portal_teste LOGIN PASSWORD '{SENHA_PORTAL}'")  # constante do teste
            su.execute("GRANT portal_leitura TO portal_teste")

            ids = []
            for nome, tok in (("Loja A", "a" * 64), ("Loja B", "b" * 64)):
                ids.append(su.execute(
                    "INSERT INTO estabelecimentos (nome, token_hash, timezone) VALUES (%s, %s, %s) RETURNING id",
                    (nome, tok, TZ),
                ).fetchone()[0])
            cls.loja_a, cls.loja_b = ids
            su.execute("UPDATE estabelecimentos SET ultimo_sync_em = now() WHERE id = %s", (cls.loja_a,))
            su.execute("INSERT INTO usuarios (email, role) VALUES ('g@x.com', 'gerente'), ('adm@x.com', 'admin')")
            su.execute("INSERT INTO usuario_estabelecimentos VALUES ('g@x.com', %s)", (cls.loja_a,))

            # Loja A: 5 visitas com durações conhecidas; 1 com alerta
            cls.durs = [600.0, 900.0, 1200.0, 1800.0, 3600.0]
            for i, dur in enumerate(cls.durs):
                ini = cls.base + i * 3600
                su.execute(
                    "INSERT INTO visitas_concluidas VALUES (%s, %s, %s, 'c1', 'Salão', %s, %s, %s, 0, 0, %s, %s, NULL, 2, %s)",
                    (cls.loja_a, INST, i + 1, f"M{i % 2}", ini, ini + dur, dur, 60.0 * (i + 1), i == 4),
                )
            # Loja B: dados que NÃO podem aparecer nos relatórios da loja A
            su.execute(
                "INSERT INTO visitas_concluidas VALUES (%s, %s, 1, 'c1', 'Salão', 'M0', %s, %s, 0, 0, 99999, NULL, NULL, 9, TRUE)",
                (cls.loja_b, INST, cls.base, cls.base + 99999),
            )
            eventos = [
                ("ALERTA_DISPARADO", {"alerta": "ESPERA_GARCOM", "tempo_espera": 400}),
                ("ALERTA_DISPARADO", {"alerta": 5}),  # tipo não-texto -> 'ALERTA'
                ("MESA_HIGIENIZADA", {"tempo_limpeza": 120, "com_alerta": False}),
                ("MESA_HIGIENIZADA", {"tempo_limpeza": 240, "com_alerta": True}),
                ("MESA_HIGIENIZADA", {"tempo_limpeza": "lixo"}),  # não numérico: ignorado, sem derrubar a consulta
            ]
            cls.epoch_alerta = cls.base + 1800
            for i, (tipo, det) in enumerate(eventos):
                su.execute(
                    "INSERT INTO eventos_mesas VALUES (%s, %s, %s, %s, 'x', 0, 0, 'c1', 'Salão', 'M0', %s, %s)",
                    (cls.loja_a, INST, i + 1, cls.epoch_alerta + i, tipo, Jsonb(det)),
                )
            # Amostras: 2 câmeras no mesmo minuto (somam), em 3 minutos seguidos
            cls.epoch_amostra = float(int(cls.base) // 60 * 60)
            n = 0
            for m in range(3):
                for cam, oc in (("c1", 2 + m), ("c2", 1)):
                    n += 1
                    su.execute(
                        "INSERT INTO amostras_ocupacao VALUES (%s, %s, %s, %s, 0, 0, %s, %s, 8, %s, %s, 0)",
                        (cls.loja_a, INST, n, cls.epoch_amostra + m * 60 + (5 if cam == "c2" else 0), cam,
                         "Salão" if cam == "c1" else "Salão 2", oc, oc * 2),
                    )

        u = urlparse(DSN)
        dsn_portal = urlunparse(u._replace(netloc=f"portal_teste:{SENHA_PORTAL}@{u.hostname}:{u.port or 5432}"))
        cls.conn = psycopg.connect(dsn_portal, autocommit=True, row_factory=dict_row)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        with cls.psycopg.connect(DSN, autocommit=True) as su:
            su.execute("DROP SCHEMA public CASCADE")
            su.execute("CREATE SCHEMA public")
            for papel in ("portal_teste", "portal_leitura"):
                su.execute(f"DROP ROLE IF EXISTS {papel}")  # nomes fixos do próprio teste

    def _desde(self, dias=30):
        return self.consultas.desde_epoch(dias, self.agora)

    def test_resumo_geral_e_isolamento(self):
        r = self.consultas.resumo_geral(self.conn, self.loja_a, self._desde())
        self.assertEqual(r["total_visitas"], 5)  # a visita da loja B não entra
        self.assertEqual(r["sla_sem_alerta_pct"], 80.0)
        self.assertEqual(r["permanencia_mediana_min"], round(statistics.median(self.durs) / 60, 1))
        self.assertEqual(r["permanencia_p90_min"], round(_p90(self.durs) / 60, 1))
        self.assertEqual(r["espera_mediana_min"], 3.0)
        self.assertIsNone(r["preparo_mediano_min"])
        self.assertEqual(r["total_alertas"], 2)
        self.assertEqual(r["total_limpezas"], 3)
        self.assertEqual(r["sla_limpeza_pct"], 50.0)
        self.assertEqual(r["limpeza_mediana_min"], 3.0)
        self.assertEqual(r["media_pessoas_por_mesa"], 2.0)

    def test_filtro_de_camera_e_periodo(self):
        self.assertEqual(self.consultas.resumo_geral(self.conn, self.loja_a, self._desde(), "c9")["total_visitas"], 0)
        self.assertEqual(self.consultas.resumo_geral(self.conn, self.loja_a, self.agora - 60)["total_visitas"], 0)
        self.assertEqual(self.consultas.resumo_geral(self.conn, self.loja_a, None)["total_visitas"], 5)

    def test_desempenho_por_mesa(self):
        linhas = self.consultas.desempenho_por_mesa(self.conn, self.loja_a, self._desde())
        por_mesa = {x["mesa_id"]: x for x in linhas}
        self.assertEqual(por_mesa["M0"]["visitas"], 3)
        self.assertEqual(por_mesa["M0"]["alertas"], 2)
        self.assertEqual(por_mesa["M0"]["limpeza_mediana_min"], 3.0)
        self.assertEqual(por_mesa["M1"]["visitas"], 2)

    def test_extrato_no_fuso_da_loja(self):
        visitas, truncado = self.consultas.visitas_detalhadas(self.conn, self.loja_a, TZ, self._desde())
        self.assertFalse(truncado)
        self.assertEqual(len(visitas), 5)
        ini = self.base + 4 * 3600
        esperado = datetime.fromtimestamp(ini, zoneinfo.ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(visitas[0]["entrada"], esperado)  # mais recente primeiro
        _, truncado = self.consultas.visitas_detalhadas(self.conn, self.loja_a, TZ, self._desde(), limite=2)
        self.assertTrue(truncado)

    def test_alertas_por_tipo(self):
        rel = self.consultas.relatorio_alertas(self.conn, self.loja_a, TZ, self._desde())
        self.assertEqual(rel["por_tipo"], {"ESPERA_GARCOM": 1, "ALERTA": 1})
        self.assertEqual({a["tipo_alerta"] for a in rel["alertas"]}, {"ESPERA_GARCOM", "ALERTA"})
        self.assertIn("06:40", [a["tempo_decorrido"] for a in rel["alertas"]])

    def test_pico_por_celula_no_fuso(self):
        am, vi, al = self.consultas.agregados_pico(self.conn, self.loja_a, TZ, self._desde())
        dt = datetime.fromtimestamp(self.epoch_amostra, zoneinfo.ZoneInfo(TZ))
        cel = (dt.weekday(), dt.hour)
        if datetime.fromtimestamp(self.epoch_amostra + 180, zoneinfo.ZoneInfo(TZ)).hour == dt.hour:
            self.assertEqual(am[cel]["amostras"], 3)
            self.assertEqual(am[cel]["oc_pico"], 5)  # 4 (c1) + 1 (c2) no 3º minuto
            self.assertAlmostEqual(am[cel]["oc_media"], 4.0)
        self.assertEqual(sum(v["giros"] for v in vi.values()), 5)
        dt_al = datetime.fromtimestamp(self.epoch_alerta, zoneinfo.ZoneInfo(TZ))
        self.assertEqual(al[(dt_al.weekday(), dt_al.hour)], 2)

    def test_listar_cameras(self):
        cams = self.consultas.listar_cameras(self.conn, self.loja_a, self.agora)
        self.assertEqual([c["camera_id"] for c in cams], ["c1", "c2"])

    def test_saude_e_acesso_com_login_somente_leitura(self):
        saude = {x["id"]: x for x in self.consultas.saude_lojas(self.conn)}
        self.assertEqual(saude[self.loja_a]["status"], "🟢 Em dia")
        self.assertEqual(saude[self.loja_b]["status"], "⚪ Nunca sincronizou")

        g = self.acesso.carregar_perfil(self.conn, "g@x.com")
        self.assertEqual([x.id for x in g.lojas], [self.loja_a])
        adm = self.acesso.carregar_perfil(self.conn, "adm@x.com")
        self.assertEqual({x.id for x in adm.lojas}, {self.loja_a, self.loja_b})
        self.assertIsNone(self.acesso.carregar_perfil(self.conn, "ninguem@x.com"))

        self.acesso.registrar_login(self.conn, "g@x.com")
        n = self.conn.execute("SELECT count(*) AS n FROM usuarios WHERE ultimo_login_em IS NOT NULL").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_login_do_portal_nao_le_tokens_nem_escreve_dados(self):
        erro = self.psycopg.errors.InsufficientPrivilege
        for sql in (
            "SELECT token_hash FROM estabelecimentos",
            "DELETE FROM visitas_concluidas",
            "UPDATE usuarios SET role = 'admin'",
            "INSERT INTO usuarios (email, role) VALUES ('x@x.com', 'admin')",
            "SELECT * FROM portal_acessos",
        ):
            with self.subTest(sql=sql), self.assertRaises(erro):
                self.conn.execute(sql)


if __name__ == "__main__":
    unittest.main()
