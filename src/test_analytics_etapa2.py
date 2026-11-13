"""
Testes automatizados da Etapa 2:
- Mediana histórica de permanência resistente a outliers e previsão hierárquica de liberação
- Agregações de pico por hora/dia e dimensionamento de equipe (garçons)
- Persistência automática de eventos e visitas no SQLite (queries 100% parametrizadas)
- Proteção contra SQL Injection e integração com MesaFSM + GerenciadorMultiCameras
"""

from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import zoneinfo

sys.path.append(str(Path(__file__).parent))
import analytics_db
from analytics_db import RepositorioAnalytics, calcular_previsao_liberacao
import config_cameras
from fsm_mesas import EstadoMesa, MesaFSM
import multi_cameras
from multi_cameras import GerenciadorMultiCameras

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class _FakeResult:
    boxes = []


class _FakeYOLO:
    def __init__(self, model_path: str):
        self.model_path = model_path

    def __call__(self, frame, classes=None, conf=0.25, verbose=False):
        return [_FakeResult()]


def testar_fsm_permanencia_e_desocupacao():
    fsm = MesaFSM("Mesa Teste")
    # Cliente senta em t=100
    evts = fsm.atualizar(pessoas_detectadas=2, funcionario_presente=False, timestamp_atual=100.0)
    assert evts[0]["tipo"] == "MESA_OCUPADA"
    # Garçom atende em t=145 (45s de espera)
    evts = fsm.atualizar(pessoas_detectadas=3, funcionario_presente=True, timestamp_atual=145.0)
    assert evts[0]["tipo"] == "ATENDIMENTO_INICIADO"
    assert evts[0]["tempo_espera_garcom"] == 45.0
    # Garçom sai em t=160
    evts = fsm.atualizar(pessoas_detectadas=2, funcionario_presente=False, timestamp_atual=160.0)
    assert evts[0]["tipo"] == "PEDIDO_ANOTADO"
    # Prato entregue em t=250 (90s preparo)
    evts = fsm.atualizar(pessoas_detectadas=3, funcionario_presente=True, timestamp_atual=250.0)
    assert evts[0]["tipo"] == "PEDIDO_ENTREGUE"
    # Permanência corrente deve estar atualizada em t=2500
    fsm.atualizar(pessoas_detectadas=2, funcionario_presente=False, timestamp_atual=2500.0)
    snap = fsm.obter_status_dict()
    assert snap["tempo_permanencia_segundos"] == 2400.0

    # Clientes saem em t=2500 (permanência 2400s = 40 min)
    evts = fsm.atualizar(pessoas_detectadas=0, funcionario_presente=False, timestamp_atual=2500.0)
    assert len(evts) == 1
    assert evts[0]["tipo"] == "MESA_DESOCUPADA"
    assert evts[0]["duracao_permanencia"] == 2400.0
    assert evts[0]["espera_garcom"] == 45.0
    assert evts[0]["tempo_preparo"] == 90.0
    assert evts[0]["qtd_pessoas_pico"] == 3
    assert fsm.tempo_permanencia_atual == 0.0

    # Saída rápida (<120s) não emite MESA_DESOCUPADA
    fsm2 = MesaFSM("Mesa Rapida")
    fsm2.atualizar(1, False, timestamp_atual=10.0)
    evts_rapido = fsm2.atualizar(0, False, timestamp_atual=50.0)
    assert evts_rapido == []
    assert fsm2.estado == EstadoMesa.LIVRE

    print("✅ [OK] Teste 1: MesaFSM registra permanência, espera, preparo e pico de pessoas")


def testar_mediana_previsao_e_hierarquia():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = RepositorioAnalytics(db_path=Path(tmpdir) / "hist.db")

        # Fallback sem histórico
        stats_vazio = db.obter_estatisticas_permanencia()
        prev_padrao = calcular_previsao_liberacao(
            "cam_1", "Mesa 1", EstadoMesa.REFEICAO_ANDAMENTO.value, 900.0, stats_vazio, padrao_segundos=2700.0
        )
        assert prev_padrao["fonte_previsao"] == "estimativa_padrao"
        assert prev_padrao["tempo_restante_seg"] == 1800.0

        # Registra 4 visitas em Mesa 1 incluindo outlier extremo de 3h (10800s)
        agora = 1_794_410_000.0
        for idx, dur in enumerate([1800.0, 2400.0, 2400.0, 10800.0]):
            assert db.registrar_visita("cam_1", "Salão", "Mesa 1", agora + idx * 100, dur, espera_garcom_segundos=120.0)

        stats = db.obter_estatisticas_permanencia()
        # Mediana de [1800, 2400, 2400, 10800] é 2400.0 (não 4350s da média!)
        prev_m1 = calcular_previsao_liberacao(
            "cam_1", "Mesa 1", EstadoMesa.REFEICAO_ANDAMENTO.value, 1800.0, stats
        )
        assert prev_m1["fonte_previsao"] == "mesa"
        assert prev_m1["mediana_prevista_seg"] == 2400.0
        assert prev_m1["tempo_restante_seg"] == 600.0
        assert prev_m1["excedeu_mediana"] is False

        # Acima da mediana
        prev_exc = calcular_previsao_liberacao(
            "cam_1", "Mesa 1", EstadoMesa.REFEICAO_ANDAMENTO.value, 3000.0, stats
        )
        assert prev_exc["excedeu_mediana"] is True
        assert prev_exc["tempo_restante_seg"] == 0.0
        assert prev_exc["progresso_pct"] == 100

        # Mesa 2 na mesma câmera sem visitas próprias herda mediana da câmera
        prev_m2 = calcular_previsao_liberacao(
            "cam_1", "Mesa 2", EstadoMesa.REFEICAO_ANDAMENTO.value, 600.0, stats
        )
        assert prev_m2["fonte_previsao"] == "camera"
        assert prev_m2["mediana_prevista_seg"] == 2400.0

        # Câmera nova sem histórico herda mediana global
        prev_c2 = calcular_previsao_liberacao(
            "cam_2", "Varanda 1", EstadoMesa.AGUARDANDO_ENTREGA.value, 600.0, stats
        )
        assert prev_c2["fonte_previsao"] == "global"
        assert prev_c2["mediana_prevista_seg"] == 2400.0

    print("✅ [OK] Teste 2: Mediana resistente a outliers e previsão hierárquica mesa/câmera/global")


def testar_pico_hora_dia_e_dimensionamento_equipe():
    tz = zoneinfo.ZoneInfo("America/Sao_Paulo")
    with tempfile.TemporaryDirectory() as tmpdir:
        db = RepositorioAnalytics(db_path=Path(tmpdir) / "hist.db")

        # Sexta-feira 06/11/2026: almoço 12:30 (6 mesas), jantar 20:15 (11 mesas)
        sex_almoco = datetime(2026, 11, 6, 12, 30, tzinfo=tz).timestamp()
        sex_jantar = datetime(2026, 11, 6, 20, 15, tzinfo=tz).timestamp()
        # Terça-feira 10/11/2026: tarde 15:10 (2 mesas)
        ter_tarde = datetime(2026, 11, 10, 15, 10, tzinfo=tz).timestamp()

        db.registrar_amostra_ocupacao(sex_almoco, "cam_1", "Salão", 12, 6, 14, 0)
        db.registrar_amostra_ocupacao(sex_jantar, "cam_1", "Salão", 12, 11, 28, 1)
        db.registrar_amostra_ocupacao(ter_tarde, "cam_1", "Salão", 12, 2, 4, 0)

        resumo_hora = {item["hora"]: item for item in db.consultar_resumo_por_hora(dias=None, mesas_por_garcom=4)}
        assert resumo_hora[12]["ocupacao_pico_mesas"] == 6
        assert resumo_hora[12]["garcons_recomendados_pico"] == 2
        assert resumo_hora[20]["ocupacao_pico_mesas"] == 11
        assert resumo_hora[20]["garcons_recomendados_pico"] == 3
        assert resumo_hora[15]["garcons_recomendados_pico"] == 1

        turnos = {t["turno"]: t for t in db.consultar_ranking_turnos(dias=None, mesas_por_garcom=4)}
        assert turnos["Jantar (18h–23h)"]["garcons_pico"] == 3
        assert turnos["Almoço (11h–14h)"]["garcons_pico"] == 2

        dias = {d["dia_nome"]: d for d in db.consultar_resumo_por_dia_semana(dias=None, mesas_por_garcom=4)}
        assert dias["Sex"]["ocupacao_pico_mesas"] == 11
        assert dias["Sex"]["garcons_recomendados_pico"] == 3
        assert dias["Ter"]["ocupacao_pico_mesas"] == 2

    print("✅ [OK] Teste 3: Pico por hora/dia e escala recomendada de garçons exatos")


def testar_seguranca_sqlite_e_integracao_multicam():
    from test_multi_cameras import _criar_video_sintetico

    orig_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            os.chdir(tmpdir)
            tmp_data = Path("data")
            vid = tmp_data / "videos" / "cam1.mp4"
            _criar_video_sintetico(vid)

            mesas1 = tmp_data / "mesas_config.json"
            mesas1.parent.mkdir(parents=True, exist_ok=True)
            mesas1.write_text(
                json.dumps({
                    "resolucao": {"largura": 320, "altura": 240},
                    "mesas": [{"id": "Mesa 1", "descricao": "Salão", "pontos": [[10, 10], [100, 10], [100, 100], [10, 100]]}],
                }),
                encoding="utf-8",
            )

            cfg_cams = tmp_data / "cameras_config.json"
            config_cameras.salvar_cameras_config(
                [{"id": "cam_1", "nome": "Salão Principal", "fonte": "data/videos/cam1.mp4", "ativa": True, "fps_alvo": 5.0}],
                config_path=cfg_cams,
            )

            with patch.object(multi_cameras, "YOLO", _FakeYOLO):
                ger = GerenciadorMultiCameras(
                    cameras_config_path=cfg_cams,
                    regras_path=tmp_data / "regras_config.json",
                    model_path="fake.pt",
                    db_path=tmp_data / "historico_salao.db",
                )
                try:
                    motor = ger.runners["cam_1"]["motor"]
                    fsm = motor.fsms["Mesa 1"]
                    fsm.atualizar(2, False, timestamp_atual=10.0)
                    fsm.atualizar(0, False, timestamp_atual=1810.0)
                    motor.historico_alertas.append({
                        "epoch": 1_794_410_000.0,
                        "camera_id": "cam_1",
                        "camera_nome": "Salão Principal",
                        "tipo": "MESA_DESOCUPADA",
                        "mesa_id": "Mesa 1",
                        "duracao_permanencia": 1800.0,
                        "espera_garcom": 60.0,
                        "qtd_pessoas_pico": 2,
                    })
                    ger.registrar_eventos_motor(motor, "cam_1", agora=1_794_410_000.0)

                    # Verifica payload anti-SQLi tratado estritamente como literal
                    payload_sqli = "cam_1' OR '1'='1'; DROP TABLE visitas_concluidas;--"
                    res_sqli = ger.analytics_db.consultar_resumo_geral(dias=None, camera_id=payload_sqli)
                    assert res_sqli["total_visitas"] == 0

                    # Banco segue íntegro após tentativa de SQLi
                    res_ok = ger.analytics_db.consultar_resumo_geral(dias=None, camera_id="cam_1")
                    assert res_ok["total_visitas"] == 1
                    assert res_ok["permanencia_mediana_min"] == 30.0

                    # Mesa ocupada novamente exporta previsão de liberação no JSON consolidado
                    fsm.atualizar(2, False, timestamp_atual=1850.0)
                    fsm.atualizar(2, False, timestamp_atual=2150.0)
                    estado_path = tmp_data / "estado_atual.json"
                    ger.exportar_estado_consolidado(estado_path)
                    snap = json.loads(estado_path.read_text(encoding="utf-8"))
                    prev = snap["cameras"]["cam_1"]["mesas"]["Mesa 1"]["previsao_liberacao"]
                    assert isinstance(prev, dict)
                    assert prev["tempo_restante_seg"] > 0
                finally:
                    for cid in list(ger.runners.keys()):
                        ger._parar_runner(cid)
        finally:
            os.chdir(orig_cwd)

    print("✅ [OK] Teste 4: Consultas SQLite parametrizadas (anti-SQLi) e exportação com previsão de giro")


def testar_relatorios_desempenho_sla_visitas_alertas():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = RepositorioAnalytics(db_path=Path(tmpdir) / "rel.db")
        t0 = 1_794_410_000.0

        db.registrar_visita("cam_1", "Salão", "Mesa 1", t0, 1800.0, espera_garcom_segundos=60.0, tempo_preparo_segundos=600.0, qtd_pessoas=2, houve_alerta=False)
        db.registrar_visita("cam_1", "Salão", "Mesa 1", t0 + 3600, 2400.0, espera_garcom_segundos=360.0, tempo_preparo_segundos=900.0, qtd_pessoas=4, houve_alerta=True)
        db.registrar_evento(t0 + 1850, "cam_1", "Salão", "Mesa 1", "MESA_HIGIENIZADA", {"tempo_limpeza": 45.0})
        db.registrar_evento(t0 + 3600, "cam_1", "Salão", "Mesa 1", "ALERTA_DISPARADO", {"alerta": "ESPERA_ATENDIMENTO", "tempo_espera": 360.0})

        resumo = db.consultar_resumo_geral(dias=None)
        assert resumo["total_visitas"] == 2
        assert resumo["sla_sem_alerta_pct"] == 50.0
        assert resumo["limpeza_mediana_min"] == 0.8

        des = db.consultar_desempenho_por_mesa(dias=None)
        assert len(des) == 1
        assert des[0]["mesa_id"] == "Mesa 1"
        assert des[0]["alertas"] == 1
        assert des[0]["sla_ok_pct"] == 50.0

        visitas = db.consultar_visitas_detalhadas(dias=None)
        assert len(visitas) == 2
        assert visitas[0]["sla"] == "⚠️ Com alerta"

        rel_al = db.consultar_relatorio_alertas(dias=None)
        assert rel_al["por_tipo"].get("ESPERA_ATENDIMENTO") == 1
        assert rel_al["alertas"][0]["tempo_decorrido"] == "06:00"

    print("✅ [OK] Teste 5: Relatórios de desempenho por mesa, SLA, visitas e alertas")


if __name__ == "__main__":
    testar_fsm_permanencia_e_desocupacao()
    testar_mediana_previsao_e_hierarquia()
    testar_pico_hora_dia_e_dimensionamento_equipe()
    testar_seguranca_sqlite_e_integracao_multicam()
    testar_relatorios_desempenho_sla_visitas_alertas()
    print("\n🎉 Todos os testes da Etapa 2 passaram com sucesso!")

