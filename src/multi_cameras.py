"""
Orquestrador Multi-Câmeras em Tempo Real com Hot-Reload.
Compartilha uma única instância do modelo YOLO na memória RAM entre todas as câmeras
ativas, escalonando leituras não-bloqueantes e exportando o estado consolidado para o Dashboard.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from ultralytics import YOLO

sys.path.append(str(Path(__file__).parent))
from analytics_db import (
    DB_PATH_PADRAO,
    RepositorioAnalytics,
    calcular_previsao_liberacao,
)
from camera_stream import StreamCamera
from config_cameras import (
    CAMERAS_CONFIG_PATH,
    carregar_cameras_config,
    mascarar_fonte_segura,
)
from motor_estados import MotorEstados

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class GerenciadorMultiCameras:
    """Gerencia múltiplas câmeras simultâneas reutilizando o mesmo modelo YOLO."""

    def __init__(
        self,
        cameras_config_path: Path = CAMERAS_CONFIG_PATH,
        regras_path: Path = Path("data/regras_config.json"),
        model_path: str = "yolov10b.pt",
        conf_thresh: float = 0.18,
        db_path: Path = DB_PATH_PADRAO,
        intervalo_amostra_seg: float = 60.0,
    ):
        self.cameras_config_path = Path(cameras_config_path)
        self.regras_path = Path(regras_path)
        self.model_path = model_path
        self.conf_thresh = conf_thresh

        print(f"Carregando YOLOv10 compartilhado: {model_path}...")
        self.model = YOLO(model_path)

        self.analytics_db = RepositorioAnalytics(db_path=db_path)
        self.intervalo_amostra_seg = float(intervalo_amostra_seg)
        self._stats_permanencia = self.analytics_db.obter_estatisticas_permanencia()
        self._ultimo_refresh_stats = time.time()
        self._ultima_amostra_db = 0.0

        self._cameras_mtime: Optional[float] = None
        self.runners: Dict[str, Dict[str, Any]] = {}
        self.configs_por_id: Dict[str, dict] = {}
        self.erros_por_id: Dict[str, str] = {}
        self.historico_global: List[dict] = []
        self._ultimo_idx_eventos: Dict[str, int] = {}

        self._recarregar_cameras(inicial=True)

    def _salvar_json_atomico(self, caminho: Path, dados: dict):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = caminho.with_suffix(".tmp.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)
        os.replace(tmp, caminho)

    def _iniciar_runner(self, cfg: dict):
        cid = cfg["id"]
        try:
            motor = MotorEstados(
                config_path=cfg["config_mesas_path"],
                regras_path=str(self.regras_path),
                model_path=self.model_path,
                conf_thresh=self.conf_thresh,
                camera_id=cid,
                camera_nome=cfg["nome"],
                model_compartilhado=self.model,
                exigir_config=False,
            )
            stream = StreamCamera(fonte=cfg["fonte"], target_fps=float(cfg.get("fps_alvo", 2.5)))
            stream.iniciar()
            self.runners[cid] = {
                "cfg": dict(cfg),
                "motor": motor,
                "stream": stream,
                "ultimo_limpo": 0.0,
                "ultimo_vivo": 0.0,
            }
            self._ultimo_idx_eventos[cid] = 0
            self.erros_por_id.pop(cid, None)
            print(f"📡 Câmera ativa [{cid} - {cfg['nome']}]: {mascarar_fonte_segura(cfg['fonte'])}")
        except Exception as e:
            self.erros_por_id[cid] = str(e)
            print(f"⚠️ Erro ao iniciar câmera [{cid}]: {e}")

    def _parar_runner(self, cid: str):
        runner = self.runners.pop(cid, None)
        self.erros_por_id.pop(cid, None)
        if runner:
            try:
                runner["stream"].parar()
            except Exception:
                pass
            self._ultimo_idx_eventos.pop(cid, None)

    def _recarregar_cameras(self, inicial: bool = False) -> bool:
        """Detecta mudanças em `data/cameras_config.json` e atualiza streams sem reiniciar."""
        if not inicial and self.cameras_config_path.exists():
            try:
                mtime = self.cameras_config_path.stat().st_mtime
                if self._cameras_mtime is not None and mtime == self._cameras_mtime:
                    return False
            except Exception:
                return False

        configs = carregar_cameras_config(self.cameras_config_path)
        try:
            self._cameras_mtime = self.cameras_config_path.stat().st_mtime
        except Exception:
            self._cameras_mtime = None

        novos_map = {c["id"]: c for c in configs}
        self.configs_por_id = novos_map

        # Remove câmeras excluídas ou pausadas
        for cid in list(self.runners.keys()):
            cfg_nova = novos_map.get(cid)
            if not cfg_nova or not cfg_nova.get("ativa", True):
                print(f"⏸️ Encerrando stream da câmera [{cid}]...")
                self._parar_runner(cid)

        # Cria ou atualiza câmeras ativas
        for cfg in configs:
            cid = cfg["id"]
            if not cfg.get("ativa", True):
                continue

            atual = self.runners.get(cid)
            if atual is None:
                self._iniciar_runner(cfg)
                continue

            cfg_antiga = atual["cfg"]
            trocou_fonte = (
                cfg["fonte"] != cfg_antiga["fonte"]
                or float(cfg.get("fps_alvo", 2.5)) != float(cfg_antiga.get("fps_alvo", 2.5))
                or cfg["config_mesas_path"] != cfg_antiga["config_mesas_path"]
            )
            if trocou_fonte:
                print(f"🔄 Fonte da câmera [{cid}] alterada. Reconectando stream...")
                self._parar_runner(cid)
                self._iniciar_runner(cfg)
            else:
                atual["cfg"] = dict(cfg)
                atual["motor"].camera_nome = cfg["nome"]

        return True

    def registrar_eventos_motor(self, motor: MotorEstados, cid: str, agora: Optional[float] = None):
        """Persiste novos eventos no SQLite e atualiza medianas ao concluir visitas."""
        t_ref = agora if agora is not None else time.time()
        idx_ant = self._ultimo_idx_eventos.get(cid, 0)
        if len(motor.historico_alertas) <= idx_ant:
            return

        novos = motor.historico_alertas[idx_ant:]
        self.historico_global.extend(novos)
        self._ultimo_idx_eventos[cid] = len(motor.historico_alertas)

        atualizar_stats = False
        for evt in novos:
            epoch_evt = float(evt.get("epoch", t_ref))
            cam_id = evt.get("camera_id", cid)
            cam_nome = evt.get("camera_nome", motor.camera_nome)
            mesa_id = evt.get("mesa_id", "")
            tipo = evt.get("tipo", "EVENTO")
            try:
                self.analytics_db.registrar_evento(
                    epoch=epoch_evt,
                    camera_id=cam_id,
                    camera_nome=cam_nome,
                    mesa_id=mesa_id,
                    tipo_evento=tipo,
                    detalhes=evt,
                )
                if tipo == "MESA_DESOCUPADA" and evt.get("duracao_permanencia") is not None:
                    if self.analytics_db.registrar_visita(
                        camera_id=cam_id,
                        camera_nome=cam_nome,
                        mesa_id=mesa_id,
                        fim_epoch=epoch_evt,
                        duracao_segundos=float(evt["duracao_permanencia"]),
                        espera_garcom_segundos=evt.get("espera_garcom"),
                        tempo_preparo_segundos=evt.get("tempo_preparo"),
                        qtd_pessoas=int(evt.get("qtd_pessoas_pico", 1)),
                        houve_alerta=bool(evt.get("houve_alerta", False)),
                    ):
                        atualizar_stats = True
            except Exception as e:
                print(f"⚠️ Erro ao persistir evento analítico [{cid}/{mesa_id}]: {e}")

        if atualizar_stats:
            self._stats_permanencia = self.analytics_db.obter_estatisticas_permanencia()

    def registrar_amostras_ocupacao(self, agora: Optional[float] = None):
        """Grava retrato periódico da ocupação de cada câmera ativa para relatórios de pico."""
        t_ref = agora if agora is not None else time.time()
        for cid, runner in list(self.runners.items()):
            motor: MotorEstados = runner["motor"]
            snap = motor.obter_mesas_snapshot()
            total_m = len(snap)
            if total_m == 0:
                continue
            ocupadas = sum(1 for m in snap.values() if m.get("estado") != "LIVRE")
            pessoas = sum(int(m.get("qtd_pessoas", 0)) for m in snap.values())
            alertas = sum(len(m.get("alertas", [])) for m in snap.values())
            try:
                self.analytics_db.registrar_amostra_ocupacao(
                    epoch=t_ref,
                    camera_id=cid,
                    camera_nome=runner["cfg"]["nome"],
                    total_mesas=total_m,
                    mesas_ocupadas=ocupadas,
                    pessoas_sentadas=pessoas,
                    alertas_ativos=alertas,
                )
            except Exception as e:
                print(f"⚠️ Erro ao registrar amostra horária [{cid}]: {e}")

    def exportar_estado_consolidado(self, estado_path: Path = Path("data/estado_atual.json")):
        """Exporta estado consolidado de todas as câmeras configuradas e métricas somadas."""
        cameras_dict = {}
        mesas_plano = {}
        total_mesas = 0

        for cid, cfg in self.configs_por_id.items():
            runner = self.runners.get(cid)
            if runner is not None:
                motor: MotorEstados = runner["motor"]
                stream: StreamCamera = runner["stream"]
                snap_mesas = motor.obter_mesas_snapshot()
                conectada = stream.conectada
                padrao_seg = motor.permanencia_padrao_segundos
            else:
                snap_mesas = {}
                conectada = False
                padrao_seg = 2700.0

            for m_id, m_dados in snap_mesas.items():
                m_dados["previsao_liberacao"] = calcular_previsao_liberacao(
                    camera_id=cid,
                    mesa_id=m_id,
                    estado_valor=m_dados.get("estado", ""),
                    tempo_permanencia_seg=float(m_dados.get("tempo_permanencia_segundos", 0.0)),
                    stats=self._stats_permanencia,
                    padrao_segundos=padrao_seg,
                )

            total_mesas += len(snap_mesas)
            cameras_dict[cid] = {
                "id": cid,
                "nome": cfg["nome"],
                "ativa": bool(cfg.get("ativa", True)),
                "conectada": conectada,
                "erro": self.erros_por_id.get(cid, ""),
                "fonte_mascarada": mascarar_fonte_segura(cfg["fonte"]),
                "fps_alvo": float(cfg.get("fps_alvo", 2.5)),
                "config_mesas_path": cfg["config_mesas_path"],
                "frame_limpo_path": cfg["frame_limpo_path"],
                "frame_vivo_path": cfg["frame_vivo_path"],
                "total_mesas": len(snap_mesas),
                "mesas": snap_mesas,
            }

            for m_id, m_dados in snap_mesas.items():
                chave = m_id if m_id not in mesas_plano else f"{cfg['nome']} - {m_id}"
                mesas_plano[chave] = m_dados

        dados = {
            "atualizado_em": time.time(),
            "atualizado_em_texto": time.strftime("%H:%M:%S"),
            "total_cameras": len(self.configs_por_id),
            "cameras": cameras_dict,
            "total_mesas": total_mesas,
            "mesas": mesas_plano,
        }
        self._salvar_json_atomico(estado_path, dados)

    def executar_continuo(
        self,
        estado_path: Path = Path("data/estado_atual.json"),
        json_log_path: Path = Path("data/alertas_log.json"),
    ):
        """Loop contínuo 24/7 processando todas as câmeras ativas com hot-reload."""
        print(f"🚀 Gerenciador Multi-Câmeras em execução ({len(self.runners)} câmera(s) ativa(s))...")
        t0 = time.time()
        ultimo_export = 0.0
        ultimo_log = 0.0

        try:
            while True:
                agora = time.time()
                if self._recarregar_cameras():
                    ultimo_export = 0.0

                for cid, runner in list(self.runners.items()):
                    cfg = runner["cfg"]
                    motor: MotorEstados = runner["motor"]
                    stream: StreamCamera = runner["stream"]

                    if motor._carregar_mesas_do_disco():
                        ultimo_export = 0.0
                    motor._atualizar_regras_do_disco()

                    ret, frame = stream.ler_se_pronto()
                    if not ret or frame is None:
                        continue

                    if agora - runner["ultimo_limpo"] >= 5.0:
                        runner["ultimo_limpo"] = agora
                        motor._salvar_imagem_atomica(Path(cfg["frame_limpo_path"]), frame)

                    anotado = motor.processar_frame(frame, timestamp_simulado=agora - t0)
                    self.registrar_eventos_motor(motor, cid, agora=agora)

                    if agora - runner["ultimo_vivo"] >= 1.0:
                        runner["ultimo_vivo"] = agora
                        motor._salvar_imagem_atomica(Path(cfg["frame_vivo_path"]), anotado)

                if agora - self._ultimo_refresh_stats >= 30.0:
                    self._ultimo_refresh_stats = agora
                    self._stats_permanencia = self.analytics_db.obter_estatisticas_permanencia()

                if agora - t0 >= 10.0 and agora - self._ultima_amostra_db >= self.intervalo_amostra_seg:
                    self._ultima_amostra_db = agora
                    self.registrar_amostras_ocupacao(agora)

                if agora - ultimo_export >= 1.0:
                    ultimo_export = agora
                    self.exportar_estado_consolidado(estado_path)

                if agora - ultimo_log >= 5.0:
                    ultimo_log = agora
                    self._salvar_json_atomico(json_log_path, {
                        "total_eventos": len(self.historico_global),
                        "eventos": self.historico_global[-100:],
                    })

                time.sleep(0.02)
        finally:
            for cid in list(self.runners.keys()):
                self._parar_runner(cid)
