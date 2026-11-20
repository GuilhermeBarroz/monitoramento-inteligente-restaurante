"""
Orquestrador Multi-Câmeras em Tempo Real com Hot-Reload.
Compartilha uma única instância do modelo YOLO na memória RAM entre todas as câmeras
ativas, escalonando leituras não-bloqueantes e exportando o estado consolidado para o Dashboard.
"""

import sqlite3
import sys
import time
import traceback
from collections import deque
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
from io_atomico import assinatura_arquivo, escrever_json_atomico, limpar_temporarios_orfaos
from motor_estados import MODELO_PADRAO, MotorEstados

# Limites de memória para operação 24/7
MAX_EVENTOS_MEMORIA = 1000          # eventos recentes mantidos para o log JSON
COMPACTAR_HISTORICO_MOTOR_ACIMA = 200  # eventos já persistidos são descartados do motor
INTERVALO_REPETICAO_LOG_S = 60.0    # mesmo erro repetido só é impresso 1x por minuto
LIMITE_RETENTATIVA_EVENTO_S = 120.0  # evento que falha no banco por mais que isso é descartado

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
        model_path: str = MODELO_PADRAO,
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

        self._cameras_mtime: Optional[tuple] = None  # assinatura (mtime_ns, tamanho, inode)
        self.runners: Dict[str, Dict[str, Any]] = {}
        self.configs_por_id: Dict[str, dict] = {}
        self.erros_por_id: Dict[str, str] = {}
        self.historico_global: deque = deque(maxlen=MAX_EVENTOS_MEMORIA)
        self.total_eventos = 0
        self._ultimo_idx_eventos: Dict[str, int] = {}
        self._evento_parcial: Dict[str, dict] = {}  # evento já gravado cuja visita falhou
        self._falha_evento: Dict[str, tuple] = {}   # (evento, monotonic da 1ª falha) por câmera
        self._ultimo_log_erro: Dict[str, float] = {}

        # Temporários deixados por um processo morto no meio de uma escrita
        for pasta in (Path("data"), Path("data") / "videos", Path("data") / "cameras"):
            limpar_temporarios_orfaos(pasta)

        self._recarregar_cameras(inicial=True)

    def _salvar_json_atomico(self, caminho: Path, dados: dict):
        escrever_json_atomico(Path(caminho), dados)

    def _logar_erro(self, chave: str, mensagem: str, exc: Optional[BaseException] = None):
        """Imprime o erro (com traceback na 1ª vez) sem inundar o log se ele se repetir a cada laço."""
        try:
            agora = time.monotonic()
            ultimo = self._ultimo_log_erro.get(chave)
            if ultimo is not None and agora - ultimo < INTERVALO_REPETICAO_LOG_S:
                return
            primeira_vez = ultimo is None
            self._ultimo_log_erro[chave] = agora
            print(f"⚠️ {mensagem}")
            if primeira_vez and exc is not None:
                traceback.print_exception(type(exc), exc, exc.__traceback__)
        except Exception:
            pass  # o log nunca pode derrubar o laço 24/7 (stdout fechado, encoding...)

    def _limpar_erro(self, chave: str):
        self._ultimo_log_erro.pop(chave, None)

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
                # Espera curta: o cap é local da thread de captura, que termina sozinha;
                # não vale travar as outras câmeras esperando um open/read lento.
                runner["stream"].parar(aguardar_s=0.5)
            except Exception:
                pass
            # Última chance de gravar eventos pendentes antes de descartar o motor
            try:
                self.registrar_eventos_motor(runner["motor"], cid)
            except Exception as e:
                self._logar_erro(f"eventos_finais_{cid}", f"Eventos pendentes da câmera [{cid}] não gravados: {e}", e)
            self._ultimo_idx_eventos.pop(cid, None)
            self._evento_parcial.pop(cid, None)
            self._falha_evento.pop(cid, None)

    def _recarregar_cameras(self, inicial: bool = False) -> bool:
        """Detecta mudanças em `data/cameras_config.json` e atualiza streams sem reiniciar."""
        # mtime ANTES da leitura: se o arquivo mudar durante a leitura, a próxima
        # iteração vê um mtime diferente e relê (antes a edição podia ser perdida).
        mtime = assinatura_arquivo(self.cameras_config_path)
        if not inicial and mtime is not None and self._cameras_mtime is not None and mtime == self._cameras_mtime:
            return False

        try:
            configs = carregar_cameras_config(self.cameras_config_path)
        except Exception as e:
            if inicial:
                raise
            # Arquivo ilegível: mantém as câmeras rodando como estão
            self._cameras_mtime = mtime
            self._logar_erro("cameras_config", f"Falha ao recarregar câmeras (mantendo as atuais): {e}", e)
            return False
        self._limpar_erro("cameras_config")
        # Arquivo criado agora pela câmera padrão: registra a assinatura dele
        self._cameras_mtime = mtime if mtime is not None else assinatura_arquivo(self.cameras_config_path)

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
        """
        Persiste novos eventos no SQLite e atualiza medianas ao concluir visitas.

        O cursor por câmera só avança DEPOIS que o evento foi gravado. Se o banco
        estiver bloqueado/ocupado (sqlite3.OperationalError, transitório), para e
        tenta de novo na próxima iteração, sem perder o evento. Erros de outro tipo
        (dado inválido) são registrados e o evento é pulado, para não travar a fila.
        Eventos já persistidos são descartados do motor para a memória não crescer.
        """
        t_ref = agora if agora is not None else time.time()
        idx = self._ultimo_idx_eventos.get(cid, 0)
        historico = motor.historico_alertas
        if len(historico) <= idx:
            return

        atualizar_stats = False
        while idx < len(historico):
            evt = historico[idx]
            mesa_id, tipo = "", "EVENTO"
            try:
                epoch_evt = float(evt.get("epoch", t_ref))
                cam_id = evt.get("camera_id", cid)
                cam_nome = evt.get("camera_nome", motor.camera_nome)
                mesa_id = evt.get("mesa_id", "")
                tipo = evt.get("tipo", "EVENTO")
                # Se numa tentativa anterior o evento foi gravado e só a visita
                # falhou (banco ocupado), não grava o evento de novo (sem duplicar).
                if self._evento_parcial.get(cid) is not evt:
                    self.analytics_db.registrar_evento(
                        epoch=epoch_evt,
                        camera_id=cam_id,
                        camera_nome=cam_nome,
                        mesa_id=mesa_id,
                        tipo_evento=tipo,
                        detalhes=evt,
                    )
                    self._evento_parcial[cid] = evt
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
            except sqlite3.OperationalError as e:
                # Normalmente transitório (database is locked/busy): mantém o cursor e
                # tenta depois. Se o MESMO evento falhar por mais de LIMITE_RETENTATIVA_EVENTO_S
                # (disco cheio, banco somente leitura...), descarta para a fila não travar.
                evt_falho, desde = self._falha_evento.get(cid, (None, None))
                if evt_falho is not evt:
                    self._falha_evento[cid] = (evt, time.monotonic())
                    desde = time.monotonic()
                if time.monotonic() - desde < LIMITE_RETENTATIVA_EVENTO_S:
                    self._logar_erro(f"db_evento_{cid}", f"Banco indisponível ao gravar evento [{cid}/{mesa_id}]; nova tentativa em seguida: {e}")
                    break
                self._logar_erro(f"db_evento_descartado_{cid}", f"Evento descartado após {LIMITE_RETENTATIVA_EVENTO_S:.0f}s de falhas no banco [{cid}/{mesa_id}/{tipo}]: {e}", e)
            except Exception as e:
                self._logar_erro(f"evento_invalido_{cid}", f"Evento descartado por erro ao persistir [{cid}/{mesa_id}/{tipo}]: {e}", e)

            self._falha_evento.pop(cid, None)
            self._evento_parcial.pop(cid, None)
            self.historico_global.append(evt)
            self.total_eventos += 1
            idx += 1

        # Compacta o histórico do motor: o que já foi persistido sai da memória
        if idx >= COMPACTAR_HISTORICO_MOTOR_ACIMA:
            del historico[:idx]
            idx = 0
        self._ultimo_idx_eventos[cid] = idx

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
                try:
                    if self._recarregar_cameras():
                        ultimo_export = 0.0
                except Exception as e:
                    self._logar_erro("recarregar_cameras", f"Erro ao recarregar câmeras: {e}", e)

                for cid, runner in list(self.runners.items()):
                    # Isolamento por câmera: um frame ruim, zona malformada ou falha de
                    # escrita numa câmera não derruba as demais nem o processo.
                    try:
                        if self._processar_camera(cid, runner, agora, t0):
                            ultimo_export = 0.0
                        if self.erros_por_id.pop(cid, None) is not None:
                            print(f"✅ Câmera [{cid}] voltou a processar normalmente.")
                        self._limpar_erro(f"camera_{cid}")
                    except Exception as e:
                        self.erros_por_id[cid] = f"Falha no processamento: {e}"
                        self._logar_erro(f"camera_{cid}", f"Erro ao processar câmera [{cid}]: {e}", e)

                if agora - self._ultimo_refresh_stats >= 30.0:
                    self._ultimo_refresh_stats = agora
                    self._tarefa_protegida("refresh_stats", self._atualizar_stats)

                if agora - t0 >= 10.0 and agora - self._ultima_amostra_db >= self.intervalo_amostra_seg:
                    self._ultima_amostra_db = agora
                    self._tarefa_protegida("amostras", self.registrar_amostras_ocupacao, agora)

                if agora - ultimo_export >= 1.0:
                    ultimo_export = agora
                    self._tarefa_protegida("export_estado", self.exportar_estado_consolidado, estado_path)

                if agora - ultimo_log >= 5.0:
                    ultimo_log = agora
                    self._tarefa_protegida("log_alertas", self._salvar_json_atomico, json_log_path, {
                        "total_eventos": self.total_eventos,
                        "eventos": list(self.historico_global)[-100:],
                    })

                time.sleep(0.02)
        finally:
            for cid in list(self.runners.keys()):
                self._parar_runner(cid)

    def _processar_camera(self, cid: str, runner: Dict[str, Any], agora: float, t0: float) -> bool:
        """Um passo de processamento de uma câmera. Retorna True se as mesas foram recarregadas."""
        cfg = runner["cfg"]
        motor: MotorEstados = runner["motor"]
        stream: StreamCamera = runner["stream"]

        mesas_recarregadas = motor._carregar_mesas_do_disco()
        motor._atualizar_regras_do_disco()

        ret, frame = stream.ler_se_pronto()
        if not ret or frame is None:
            # Persiste eventos pendentes mesmo sem frame novo (ex.: banco estava ocupado)
            self.registrar_eventos_motor(motor, cid, agora=agora)
            return mesas_recarregadas

        if agora - runner["ultimo_limpo"] >= 5.0:
            runner["ultimo_limpo"] = agora
            self._tarefa_protegida(f"frame_limpo_{cid}", motor._salvar_imagem_atomica, Path(cfg["frame_limpo_path"]), frame)

        anotado = motor.processar_frame(frame, timestamp_simulado=agora - t0)
        self.registrar_eventos_motor(motor, cid, agora=agora)

        if agora - runner["ultimo_vivo"] >= 1.0:
            runner["ultimo_vivo"] = agora
            self._tarefa_protegida(f"frame_vivo_{cid}", motor._salvar_imagem_atomica, Path(cfg["frame_vivo_path"]), anotado)
        return mesas_recarregadas

    def _atualizar_stats(self):
        self._stats_permanencia = self.analytics_db.obter_estatisticas_permanencia()

    def _tarefa_protegida(self, chave: str, fn, *args) -> bool:
        """Executa uma tarefa periódica sem deixar a exceção derrubar o laço principal."""
        try:
            fn(*args)
            self._limpar_erro(chave)
            return True
        except Exception as e:
            self._logar_erro(chave, f"Falha na tarefa '{chave}': {e}", e)
            return False
