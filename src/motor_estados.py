"""
Motor de Estados e Temporizadores de Atendimento em Tempo Real (Fase 5+).
Integra a detecção de pessoas via YOLOv10b com a Máquina de Estados FSM,
calculando cronômetros de atendimento, cozinha e limpeza, emitindo alertas
e sincronizando o estado real e a imagem ao vivo com o Dashboard Web.
"""

import os
import sys
import json
import time
from pathlib import Path
import cv2
import numpy as np
from ultralytics import YOLO

# Garante que a pasta src esteja no path
sys.path.append(str(Path(__file__).parent))
from fsm_mesas import MesaFSM, EstadoMesa, TipoAlerta
from detector_garcom import AnalisadorUniformeHSV, RastreadorAtendimentoMesa

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Paleta de Cores BGR por Estado
CORES_ESTADO = {
    EstadoMesa.LIVRE: (0, 230, 0),                    # Verde
    EstadoMesa.AGUARDANDO_ATENDIMENTO: (0, 0, 240),   # Vermelho
    EstadoMesa.ANOTANDO_PEDIDO: (0, 220, 255),        # Amarelo/Ouro
    EstadoMesa.AGUARDANDO_ENTREGA: (255, 120, 0),     # Azul Claro
    EstadoMesa.REFEICAO_ANDAMENTO: (200, 50, 200),    # Roxo
    EstadoMesa.PENDENTE_LIMPEZA: (0, 140, 255)        # Laranja
}

class MotorEstados:
    def __init__(self, config_path: str = "data/mesas_config.json",
                 regras_path: str = "data/regras_config.json",
                 model_path: str = "yolov10b.pt",
                 conf_thresh: float = 0.18,
                 limite_espera_garcom: float = 300.0,
                 limite_cozinha: float = 1200.0,
                 limite_limpeza: float = 180.0,
                 camera_id: str = "cam_1",
                 camera_nome: str = "Salao Principal",
                 model_compartilhado=None,
                 exigir_config: bool = True):

        self.config_path = Path(config_path)
        self.regras_path = Path(regras_path)
        if exigir_config and not self.config_path.exists():
            raise FileNotFoundError(f"Arquivo '{config_path}' não encontrado.")

        self.camera_id = camera_id
        self.camera_nome = camera_nome
        self.conf_thresh = conf_thresh
        self.limite_espera_garcom = limite_espera_garcom
        self.limite_cozinha = limite_cozinha
        self.limite_limpeza = limite_limpeza
        self.deteccao_garcom_excedente = True
        self.visita_min_segundos = 4.0
        self.visita_max_segundos = 180.0
        self.limpeza_min_segundos = 5.0
        self.ocupar_apos_suja_segundos = 45.0
        self.uniforme_ativo = False
        self.uniforme_cor_preset = "Preto / Escuro"
        self.uniforme_ratio_min = 0.25
        self.uniforme_margem_px = 35
        self.permanencia_padrao_segundos = 2700.0
        self.mesas_por_garcom = 4
        self.analisador_uniforme = AnalisadorUniformeHSV(self.uniforme_cor_preset, self.uniforme_ratio_min)
        self._mesas_mtime = None

        self.historico_alertas = []
        self.mesas = []
        self.fsms = {}
        self.debounce_cache = {}  # Suavização anti-flicker de detecções
        self.rastreadores_atendimento = {}

        self._carregar_mesas_do_disco(inicial=True)

        if model_compartilhado is not None:
            self.model = model_compartilhado
        else:
            print(f"Carregando YOLOv10: {model_path}...")
            self.model = YOLO(model_path)
        print(f"Motor FSM [{self.camera_id}] inicializado para {len(self.mesas)} mesas.")

    def _carregar_mesas_do_disco(self, inicial: bool = False) -> bool:
        """Recarrega data/mesas_config.json quando alterado no Editor Web, preservando cronômetros compatíveis."""
        if not self.config_path.exists():
            return False

        try:
            mtime = self.config_path.stat().st_mtime
            if not inicial and self._mesas_mtime is not None and mtime == self._mesas_mtime:
                return False

            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            if inicial:
                raise
            print(f"⚠️ Erro ao recarregar configuração de mesas: {e}")
            return False

        novas_mesas = []
        novos_ids = set()
        fsms_atuais = {}
        caches_atuais = {}
        rastreadores_atuais = {}

        for m in config.get("mesas", []):
            pontos = m.get("pontos", [])
            if len(pontos) < 3:
                continue
            pts_np = np.array(pontos, np.int32).reshape((-1, 1, 2))
            mesa_id = m["id"]
            novos_ids.add(mesa_id)

            novas_mesas.append({
                "id": mesa_id,
                "descricao": m.get("descricao", ""),
                "pontos_np": pts_np,
                "pontos_raw": pontos
            })

            if mesa_id in self.fsms:
                fsms_atuais[mesa_id] = self.fsms[mesa_id]
                caches_atuais[mesa_id] = self.debounce_cache[mesa_id]
                rastreadores_atuais[mesa_id] = self.rastreadores_atendimento.get(
                    mesa_id,
                    RastreadorAtendimentoMesa(
                        visita_min_s=self.visita_min_segundos,
                        visita_max_s=self.visita_max_segundos,
                    ),
                )
            else:
                fsms_atuais[mesa_id] = MesaFSM(
                    mesa_id=mesa_id,
                    limite_espera_garcom=self.limite_espera_garcom,
                    limite_cozinha=self.limite_cozinha,
                    limite_limpeza=self.limite_limpeza,
                    limpeza_min_segundos=self.limpeza_min_segundos,
                    ocupar_apos_suja_segundos=self.ocupar_apos_suja_segundos,
                )
                caches_atuais[mesa_id] = {
                    "frames_presente": 0,
                    "frames_ausente": 0,
                    "qtd_suavizada": 0
                }
                rastreadores_atuais[mesa_id] = RastreadorAtendimentoMesa(
                    visita_min_s=self.visita_min_segundos,
                    visita_max_s=self.visita_max_segundos,
                )

        self.mesas = novas_mesas
        self.fsms = fsms_atuais
        self.debounce_cache = caches_atuais
        self.rastreadores_atendimento = rastreadores_atuais
        self._mesas_mtime = mtime
        self._atualizar_regras_do_disco()

        if not inicial:
            print(f"🔄 Configuração de mesas recarregada ao vivo ({len(self.mesas)} mesas).")
        return True

    def _atualizar_regras_do_disco(self):
        """Lê data/regras_config.json para aplicar ajustes dinâmicos feitos no Dashboard."""
        if self.regras_path.exists():
            try:
                with open(self.regras_path, "r", encoding="utf-8") as f:
                    regras = json.load(f)
                espera_seg = float(regras.get("limite_espera_garcom_minutos", 5.0)) * 60.0
                cozinha_seg = float(regras.get("limite_cozinha_minutos", 20.0)) * 60.0
                limpeza_seg = float(regras.get("limite_limpeza_minutos", 3.0)) * 60.0

                self.deteccao_garcom_excedente = bool(regras.get("deteccao_garcom_excedente", True))
                self.visita_min_segundos = float(regras.get("visita_min_segundos", 4.0))
                self.visita_max_segundos = float(regras.get("visita_max_segundos", 180.0))
                self.limpeza_min_segundos = float(regras.get("limpeza_min_segundos", 5.0))
                self.ocupar_apos_suja_segundos = float(regras.get("ocupar_apos_suja_segundos", 45.0))

                self.uniforme_ativo = bool(regras.get("uniforme_ativo", False))
                self.uniforme_cor_preset = str(regras.get("uniforme_cor_preset", "Preto / Escuro"))
                self.uniforme_ratio_min = float(regras.get("uniforme_ratio_min", 0.25))
                self.uniforme_margem_px = int(regras.get("uniforme_margem_px", 35))
                self.permanencia_padrao_segundos = max(60.0, float(regras.get("permanencia_padrao_minutos", 45.0)) * 60.0)
                self.mesas_por_garcom = max(1, int(regras.get("mesas_por_garcom", 4)))
                self.analisador_uniforme.configurar(self.uniforme_cor_preset, self.uniforme_ratio_min)

                for fsm in self.fsms.values():
                    fsm.limite_espera_garcom = espera_seg
                    fsm.limite_cozinha = cozinha_seg
                    fsm.limite_limpeza = limpeza_seg
                    fsm.limpeza_min_segundos = self.limpeza_min_segundos
                    fsm.ocupar_apos_suja_segundos = self.ocupar_apos_suja_segundos

                for rastreador in self.rastreadores_atendimento.values():
                    rastreador.visita_min_s = self.visita_min_segundos
                    rastreador.visita_max_s = self.visita_max_segundos
            except Exception:
                pass

    def formatar_tempo(self, segundos: float) -> str:
        """Converte segundos em formato MM:SS."""
        m = int(segundos // 60)
        s = int(segundos % 60)
        return f"{m:02d}:{s:02d}"

    def _salvar_json_atomico(self, caminho: Path, dados: dict):
        """Escreve JSON em arquivo temporário e renomeia atomicamente para evitar leitura corrompida."""
        caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = caminho.with_suffix(".tmp.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, caminho)

    def _salvar_imagem_atomica(self, caminho: Path, img: np.ndarray):
        """Salva JPEG atomicamente para leitura concorrente segura pelo Dashboard."""
        caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = caminho.with_name(f"{caminho.stem}_tmp{caminho.suffix}")
        cv2.imwrite(str(tmp_path), img)
        os.replace(tmp_path, caminho)

    def processar_frame(self, frame: np.ndarray, timestamp_simulado: float) -> np.ndarray:
        display = frame.copy()
        h, w, _ = frame.shape

        # 1. Inferência YOLO (apenas pessoas)
        results = self.model.predict(source=frame, imgsz=640, conf=self.conf_thresh, classes=[0], verbose=False)
        boxes = results[0].boxes

        # 2. Atribuição única de cada pessoa a no máximo uma mesa (via centro/tórax)
        # Se modo uniforme HSV estiver ativo, garçons não entram na contagem de clientes
        contagem_bruta = {m["id"]: 0 for m in self.mesas}
        garcom_uniforme_mesas = {m["id"]: False for m in self.mesas}

        for box in boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            centro = (int((x1 + x2) / 2), int((y1 + y2) / 2))
            peito = (int((x1 + x2) / 2), int(y1 + (y2 - y1) * 0.40))

            eh_garcom_uniforme = False
            if self.uniforme_ativo:
                eh_garcom_uniforme, _ = self.analisador_uniforme.verificar_pessoa(frame, (x1, y1, x2, y2))

            if eh_garcom_uniforme:
                # Funcionário uniformizado: associa à mesa mais próxima dentro da margem sem contar como cliente
                cv2.rectangle(display, (int(x1), int(y1)), (int(x2), int(y2)), (0, 220, 255), 2)
                cv2.putText(display, "GARCOM", (int(x1), max(int(y1) - 6, 15)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 220, 255), 2, cv2.LINE_AA)

                melhor_mesa = None
                melhor_dist = -float(self.uniforme_margem_px) - 0.001
                for mesa in self.mesas:
                    d_centro = cv2.pointPolygonTest(mesa["pontos_np"], centro, True)
                    d_peito = cv2.pointPolygonTest(mesa["pontos_np"], peito, True)
                    dist = max(d_centro, d_peito)
                    if dist >= -float(self.uniforme_margem_px) and dist > melhor_dist:
                        melhor_dist = dist
                        melhor_mesa = mesa["id"]
                if melhor_mesa is not None:
                    garcom_uniforme_mesas[melhor_mesa] = True
                continue

            # Desenha detecção de cliente no frame ao vivo
            cv2.rectangle(display, (int(x1), int(y1)), (int(x2), int(y2)), (220, 220, 220), 1)
            cv2.circle(display, centro, 4, (0, 255, 255), -1)

            for mesa in self.mesas:
                if (cv2.pointPolygonTest(mesa["pontos_np"], centro, False) >= 0 or
                    cv2.pointPolygonTest(mesa["pontos_np"], peito, False) >= 0):
                    contagem_bruta[mesa["id"]] += 1
                    break  # Evita dupla contagem em mesas vizinhas

        # 3. Atualiza FSM com suavização anti-flicker e detecção automática de garçom/limpeza
        alertas_atuais = []
        DEBOUNCE_OCUPAR = 2
        DEBOUNCE_SAIR = 6

        for mesa in self.mesas:
            mesa_id = mesa["id"]
            fsm = self.fsms[mesa_id]
            cache = self.debounce_cache[mesa_id]
            rastreador = self.rastreadores_atendimento[mesa_id]
            qtd_agora = contagem_bruta[mesa_id]

            if qtd_agora > 0:
                cache["frames_presente"] += 1
                cache["frames_ausente"] = 0
                if cache["frames_presente"] >= DEBOUNCE_OCUPAR:
                    cache["qtd_suavizada"] = qtd_agora
            else:
                cache["frames_ausente"] += 1
                if cache["frames_ausente"] >= DEBOUNCE_SAIR:
                    cache["frames_presente"] = 0
                    cache["qtd_suavizada"] = 0

            func_presente, cancelar_visita = rastreador.avaliar(
                estado_valor=fsm.estado.value,
                qtd_clientes=cache["qtd_suavizada"],
                garcom_uniforme=garcom_uniforme_mesas[mesa_id],
                timestamp=timestamp_simulado,
                usar_excedente=self.deteccao_garcom_excedente,
            )

            # Atualiza FSM com a contagem estabilizada e presença automática de garçom
            novos_eventos = fsm.atualizar(
                pessoas_detectadas=cache["qtd_suavizada"],
                funcionario_presente=func_presente,
                timestamp_atual=timestamp_simulado,
                cancelar_visita_falsa=cancelar_visita,
            )

            for evt in novos_eventos:
                evt_enriquecido = {
                    "data_hora": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "epoch": time.time(),
                    "camera_id": self.camera_id,
                    "camera_nome": self.camera_nome,
                    **evt,
                }
                self.historico_alertas.append(evt_enriquecido)
                if evt_enriquecido.get("tipo") == "ALERTA_DISPARADO":
                    print(f"⚠️ [{self.camera_nome} | {self.formatar_tempo(timestamp_simulado)}] ALERTA: {evt['alerta']} na {evt['mesa_id']}!")

            if fsm.alertas_ativos:
                alertas_atuais.extend([f"{mesa_id}: {a}" for a in fsm.alertas_ativos])

            # 4. Desenho e Anotação Visual sincronizada
            cor = CORES_ESTADO.get(fsm.estado, (255, 255, 255))
            pts = mesa["pontos_np"]

            overlay = display.copy()
            cv2.fillPoly(overlay, [pts], color=cor)
            cv2.addWeighted(overlay, 0.25, display, 0.75, 0, display)
            cv2.polylines(display, [pts], isClosed=True, color=cor, thickness=2)

            p1 = mesa["pontos_raw"][0]
            cronometro = self.formatar_tempo(fsm.tempo_decorrido_estado)
            alerta_sinal = " [!]" if fsm.alertas_ativos else ""
            linha1 = f"{mesa_id} [{fsm.estado.value[:12]}]"
            linha2 = f"Tempo: {cronometro}{alerta_sinal} ({fsm.qtd_pessoas}p)"

            largura = max(len(linha1), len(linha2)) * 8 + 16
            x_box = max(0, min(p1[0], w - largura))
            y_box = max(p1[1], 40)
            cv2.rectangle(display, (x_box, y_box - 38), (min(w, x_box + largura), y_box), (20, 20, 20), -1)
            cv2.putText(display, linha1, (x_box + 5, y_box - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.putText(display, linha2, (x_box + 5, y_box - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, cor, 1, cv2.LINE_AA)

        # 5. Barra Superior de Monitoramento
        mesas_ocupadas = sum(1 for f in self.fsms.values() if f.estado != EstadoMesa.LIVRE)
        if alertas_atuais:
            cv2.rectangle(display, (0, 0), (w, 40), (0, 0, 180), -1)
            alerta_msg = " | ".join(alertas_atuais[:2])
            cv2.putText(display, f"ALERTA ATIVO: {alerta_msg}", (15, 27),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        else:
            cv2.rectangle(display, (0, 0), (w, 38), (15, 15, 15), -1)
            cv2.putText(display, f"{self.camera_nome} | Ocupadas: {mesas_ocupadas}/{len(self.mesas)} | Livres: {len(self.mesas) - mesas_ocupadas}",
                        (15, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 255, 180), 2, cv2.LINE_AA)

        return display

    def obter_mesas_snapshot(self) -> dict:
        """Retorna dicionário serializável do estado atual das mesas desta câmera."""
        mesas_dict = {}
        for m in self.mesas:
            m_id = m["id"]
            fsm = self.fsms[m_id]
            rastreador = self.rastreadores_atendimento[m_id]
            snap = fsm.obter_status_dict()
            snap["tempo_formatado"] = self.formatar_tempo(fsm.tempo_decorrido_estado)
            snap["tempo_permanencia_formatado"] = self.formatar_tempo(fsm.tempo_permanencia_atual)
            snap["descricao"] = m.get("descricao", "")
            snap["base_clientes"] = rastreador.base_atual
            snap["garcom_presente"] = bool(
                rastreador.visita_excedente_ativa or rastreador.inicio_uniforme is not None
            )
            mesas_dict[m_id] = snap
        return mesas_dict

    def exportar_estado_atual(self, estado_path: Path = Path("data/estado_atual.json")):
        """Exporta snapshot fiel de todas as mesas para consumo em tempo real pelo Dashboard."""
        mesas_dict = self.obter_mesas_snapshot()

        dados = {
            "atualizado_em": time.time(),
            "atualizado_em_texto": time.strftime("%H:%M:%S"),
            "total_mesas": len(self.mesas),
            "mesas": mesas_dict
        }
        self._salvar_json_atomico(estado_path, dados)

    def executar(self, video_path: str = "data/videos/sample_restaurante.mp4",
                 output_path: str = "data/videos/output_fsm_cronometros.mp4",
                 max_frames: int = 300,
                 json_log_path: str = "data/alertas_log.json"):

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Erro ao abrir vídeo: {video_path}")
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        limite = min(max_frames, total_frames) if max_frames else total_frames
        print(f"Processando {limite} frames com Máquina de Estados e Cronômetros...")

        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(str(out_file), fourcc, fps, (width, height))

        frames_processados = 0
        while frames_processados < limite:
            ret, frame = cap.read()
            if not ret:
                break

            timestamp_simulado = frames_processados / fps
            anotado = self.processar_frame(frame, timestamp_simulado=timestamp_simulado)
            writer.write(anotado)

            frames_processados += 1
            if frames_processados % 50 == 0 or frames_processados == limite:
                self.exportar_estado_atual()
                self._salvar_imagem_atomica(Path("data/videos/frame_ao_vivo.jpg"), anotado)
                print(f"  • Processado frame {frames_processados}/{limite} ({timestamp_simulado:.1f}s)...")

        cap.release()
        writer.release()
        print(f"Vídeo com cronômetros salvo em: {out_file.resolve()}")

        self._salvar_json_atomico(Path(json_log_path), {
            "total_eventos": len(self.historico_alertas),
            "eventos": self.historico_alertas[-100:]
        })
        print(f"Log de eventos salvo em: {Path(json_log_path).resolve()}")

    def executar_continuo(self, video_path: str = "data/videos/sample_restaurante.mp4",
                           json_log_path: str = "data/alertas_log.json"):
        """Monitoramento contínuo 24/7 usando StreamCamera com sincronização em tempo real."""
        from camera_stream import StreamCamera
        print(f"📡 Iniciando monitoramento contínuo sincronizado: {video_path}")
        log_path = Path(json_log_path)
        estado_path = Path("data/estado_atual.json")
        frame_vivo_path = Path("data/videos/frame_ao_vivo.jpg")
        frame_limpo_path = Path("data/videos/frame_limpo.jpg")

        with StreamCamera(fonte=video_path, target_fps=2.5) as cam:
            t0 = time.time()
            ultimo_export = 0.0
            ultimo_log = 0.0
            ultimo_limpo = 0.0

            while True:
                ret, frame = cam.ler()
                if ret and frame is not None:
                    agora = time.time()
                    timestamp = agora - t0

                    # Recarrega zonas/nomes das mesas e regras caso editados no Dashboard
                    if self._carregar_mesas_do_disco():
                        ultimo_export = 0.0
                    self._atualizar_regras_do_disco()

                    if agora - ultimo_limpo >= 5.0:
                        ultimo_limpo = agora
                        self._salvar_imagem_atomica(frame_limpo_path, frame)

                    anotado = self.processar_frame(frame, timestamp_simulado=timestamp)

                    # Exporta estado e frame ao vivo a cada 1 segundo para o Dashboard
                    if agora - ultimo_export >= 1.0:
                        ultimo_export = agora
                        self.exportar_estado_atual(estado_path)
                        self._salvar_imagem_atomica(frame_vivo_path, anotado)

                    # Salva histórico de eventos a cada 5 segundos
                    if agora - ultimo_log >= 5.0:
                        ultimo_log = agora
                        self._salvar_json_atomico(log_path, {
                            "total_eventos": len(self.historico_alertas),
                            "eventos": self.historico_alertas[-50:]
                        })
                time.sleep(0.01)

if __name__ == "__main__":
    if "--loop" in sys.argv:
        from multi_cameras import GerenciadorMultiCameras
        GerenciadorMultiCameras().executar_continuo()
    else:
        motor = MotorEstados()
        motor.executar(max_frames=300)
