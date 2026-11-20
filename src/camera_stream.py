"""
Módulo de Ingestão Resiliente de Câmeras CFTV (RTSP / ONVIF / Webcam / Vídeo).
Implementa leitura assíncrona em thread separada com:
- Zero Latência: Descarta frames acumulados em buffer de rede (apenas o frame mais recente é consumido).
- Reconexão Automática: Recuperação contínua caso a câmera reinicie ou a rede Wi-Fi/cabo oscile.
- Amostragem Inteligente de FPS: Limita o processamento a 3~5 FPS para economizar 80%+ de CPU/GPU.
- Sanitização de URL: Validação estrita de protocolos (rtsp, http, https, webcam int, arquivo local).
"""

import os
import sys
import time
import threading
import ipaddress
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from typing import Union, Tuple, Optional
import cv2
import numpy as np

# Força transporte TCP em RTSP para evitar perda de pacotes/frames corrompidos
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

class StreamCamera:
    """
    Gerenciador resiliente de captura de vídeo com buffer zero-lag e auto-reconexão.
    Compatível com qualquer fabricante (Intelbras, Hikvision, Dahua, TP-Link, ONVIF e Webcams).
    """
    # Timeouts do backend FFmpeg (abertura e leitura). Sem eles, um RTSP que para
    # de responder pode bloquear `cap.read()` indefinidamente.
    TIMEOUT_ABERTURA_MS = 8000
    TIMEOUT_LEITURA_MS = 5000
    # Sem frame NOVO por este tempo, a câmera é considerada desconectada
    # (cobre stream congelado que continua "aberto").
    TIMEOUT_FRAME_NOVO_S = 10.0
    RECONEXAO_MAX_S = 60.0

    def __init__(self, fonte: Union[str, int] = "data/videos/sample_restaurante.mp4",
                 target_fps: float = 4.0,
                 reconnect_timeout: float = 5.0):

        self.fonte_original = fonte
        self.fonte_sanitizada = self._sanitizar_fonte(fonte)
        self.target_fps = target_fps
        self.reconnect_timeout = reconnect_timeout

        # Estado da thread. O cv2.VideoCapture é local da thread de captura.
        self.running = False
        self._parar_evento = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        # Buffer mais recente (apenas 1 frame) + controle de frescor
        self.ultimo_frame: Optional[np.ndarray] = None
        self.ultimo_status = False
        self._seq_frame = 0            # incrementa a cada frame novo lido da fonte
        self._seq_entregue = 0         # último frame entregue por ler_se_pronto()
        self._ts_ultimo_frame = 0.0    # time.monotonic() do último frame novo
        self.total_frames_lidos = 0
        self.total_reconexoes = 0
        self.is_arquivo = isinstance(self.fonte_sanitizada, str) and Path(self.fonte_sanitizada).is_file()

        # Controle de taxa de entrega (FPS)
        self.intervalo_alvo = 1.0 / self.target_fps if self.target_fps > 0 else 0
        self.ultimo_tempo_entrega = 0.0

    def _sanitizar_fonte(self, fonte: Union[str, int]) -> Union[str, int]:
        """Valida e sanitiza a fonte de entrada contra injeções e protocolos perigosos."""
        if isinstance(fonte, int) or (isinstance(fonte, str) and fonte.strip().isdigit()):
            idx = int(str(fonte).strip())
            if idx < 0 or idx > 10:
                raise ValueError("Índice de webcam deve estar entre 0 e 10.")
            return idx

        fonte_str = str(fonte).strip()

        # Checa se é arquivo local existente
        caminho_local = Path(fonte_str)
        if caminho_local.exists():
            return str(caminho_local.resolve())

        # Validação estrita de URLs de rede (RTSP/HTTP)
        parsed = urlparse(fonte_str)
        protocolos_permitidos = ["rtsp", "rtsps", "http", "https"]
        if parsed.scheme.lower() not in protocolos_permitidos:
            raise ValueError(f"Protocolo '{parsed.scheme}' não suportado. Utilize RTSP, HTTP, HTTPS ou arquivo local.")

        if not parsed.hostname:
            raise ValueError("URL de câmera inválida: hostname/IP não informado.")

        try:
            ip = ipaddress.ip_address(parsed.hostname)
            ip_bloqueado = ip.is_link_local or ip.is_unspecified or ip.is_multicast
        except ValueError:
            ip_bloqueado = False
        if ip_bloqueado:
            raise ValueError("Endereço IP reservado/link-local bloqueado.")

        return fonte_str

    def _fonte_mascarada(self) -> str:
        """Fonte para logs, com a senha oculta. Nunca levanta exceção (porta inválida etc.)."""
        s = str(self.fonte_original)
        try:
            parsed = urlparse(s)
            if parsed.scheme in {"rtsp", "rtsps", "http", "https"} and parsed.password:
                port = f":{parsed.port}" if parsed.port else ""
                return urlunparse(parsed._replace(netloc=f"{parsed.username or ''}:****@{parsed.hostname or ''}{port}"))
            return s
        except ValueError:
            return "<fonte com URL inválida>"

    def iniciar(self):
        """
        Inicia a thread de captura em segundo plano. A conexão acontece dentro da
        thread: uma câmera inacessível não trava quem chamou (o laço multi-câmera).
        """
        if self.running:
            return

        # Cada início ganha seu próprio evento de parada ("geração"). Uma thread de
        # um início anterior que ainda esteja saindo (presa num open/read lento) só
        # enxerga o próprio evento, que já está setado, e não interfere nesta.
        self.running = True
        self._parar_evento = threading.Event()
        self.thread = threading.Thread(target=self._loop_captura, args=(self._parar_evento,), daemon=True)
        self.thread.start()
        print(f"📡 Stream iniciado na fonte: {self._fonte_mascarada()} (Meta: {self.target_fps} FPS)")

    @staticmethod
    def _liberar(cap: Optional[cv2.VideoCapture]) -> None:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _abrir_captura(self) -> cv2.VideoCapture:
        """Cria o cv2.VideoCapture com timeouts de abertura/leitura e buffer zerado."""
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.TIMEOUT_ABERTURA_MS,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.TIMEOUT_LEITURA_MS,
        ]
        if isinstance(self.fonte_sanitizada, str) and not self.is_arquivo:
            # RTSP/HTTP: backend FFmpeg (TCP via OPENCV_FFMPEG_CAPTURE_OPTIONS) com timeouts
            cap = cv2.VideoCapture(self.fonte_sanitizada, cv2.CAP_FFMPEG, params)
        else:
            cap = cv2.VideoCapture(self.fonte_sanitizada)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _marcar_sem_sinal(self, parar: threading.Event):
        with self.lock:
            if parar is self._parar_evento:  # thread de geração antiga não mexe no estado
                self.ultimo_status = False

    def _loop_captura(self, parar: threading.Event):
        """
        Thread interna: conecta, drena o buffer retendo só o frame atual e reconecta
        com backoff. O `cap` é LOCAL desta thread: só ela abre, lê e libera.
        """
        cap: Optional[cv2.VideoCapture] = None
        falhas_seguidas = 0
        espera_reconexao = self.reconnect_timeout
        primeira_conexao = True
        try:
            while not parar.is_set():
                if cap is None or not cap.isOpened():
                    if not primeira_conexao:
                        self._marcar_sem_sinal(parar)
                        print(f"⚠️ Conexão perdida em {self._fonte_mascarada()}. Tentando reconectar em {espera_reconexao:.0f}s...")
                        if parar.wait(espera_reconexao):
                            break
                        self.total_reconexoes += 1
                    self._liberar(cap)
                    cap = self._abrir_captura()
                    if parar.is_set():
                        break
                    if not cap.isOpened():
                        if primeira_conexao:
                            print(f"⚠️ Não foi possível abrir {self._fonte_mascarada()}. Nova tentativa em {espera_reconexao:.0f}s...")
                        primeira_conexao = False
                        espera_reconexao = min(espera_reconexao * 2, self.RECONEXAO_MAX_S)
                        continue
                    if not primeira_conexao:
                        print(f"✅ Conexão restabelecida: {self._fonte_mascarada()}")
                    primeira_conexao = False
                    falhas_seguidas = 0
                    espera_reconexao = self.reconnect_timeout

                ret, frame = cap.read()

                if ret and frame is not None:
                    falhas_seguidas = 0
                    with self.lock:
                        if parar is not self._parar_evento:
                            break  # geração antiga: não sobrescreve o frame da atual
                        self.ultimo_frame = frame
                        self.ultimo_status = True
                        self._seq_frame += 1
                        self._ts_ultimo_frame = time.monotonic()
                        self.total_frames_lidos += 1
                elif self.is_arquivo:
                    # Arquivo de teste: reinicia o vídeo em loop contínuo
                    falhas_seguidas += 1
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    if falhas_seguidas >= 30:  # arquivo ilegível: não gira a CPU sem parar
                        self._marcar_sem_sinal(parar)
                        if parar.wait(1.0):
                            break
                else:
                    falhas_seguidas += 1
                    self._marcar_sem_sinal(parar)
                    if falhas_seguidas >= 30:
                        self._liberar(cap)
                        cap = None
                        falhas_seguidas = 0
                    if parar.wait(0.1):
                        break

                # Previne consumo excessivo da CPU na thread de leitura
                if parar.wait(0.005):
                    break
        finally:
            self._liberar(cap)
            self._marcar_sem_sinal(parar)

    def _frame_fresco(self) -> bool:
        """True se chegou frame novo da fonte nos últimos TIMEOUT_FRAME_NOVO_S segundos (chamar com lock)."""
        return self._seq_frame > 0 and (time.monotonic() - self._ts_ultimo_frame) <= self.TIMEOUT_FRAME_NOVO_S

    def ler_se_pronto(self) -> Tuple[bool, Optional[np.ndarray]]:
        """
        Leitura não-bloqueante para escalonamento multi-câmera. Entrega cada frame
        da fonte no máximo uma vez: um stream congelado não é processado como se
        estivesse ao vivo (os cronômetros não avançam sobre imagem parada).
        """
        agora = time.time()
        if (agora - self.ultimo_tempo_entrega) < self.intervalo_alvo:
            return False, None

        with self.lock:
            if (not self.ultimo_status or self.ultimo_frame is None
                    or self._seq_frame == self._seq_entregue or not self._frame_fresco()):
                return False, None
            frame_copia = self.ultimo_frame.copy()
            self._seq_entregue = self._seq_frame

        self.ultimo_tempo_entrega = agora
        return True, frame_copia

    @property
    def conectada(self) -> bool:
        """Conectada = último read com sucesso E frame novo recente (detecta stream congelado)."""
        with self.lock:
            return bool(self.ultimo_status) and self._frame_fresco()

    def ler(self) -> Tuple[bool, Optional[np.ndarray]]:
        """
        Retorna o frame mais recente conforme a taxa de FPS configurada.
        Bloqueia levemente se necessário para sincronizar com target_fps.
        """
        agora = time.time()
        tempo_passado = agora - self.ultimo_tempo_entrega

        if tempo_passado < self.intervalo_alvo:
            time.sleep(self.intervalo_alvo - tempo_passado)

        with self.lock:
            frame_copia = self.ultimo_frame.copy() if self.ultimo_frame is not None else None
            status = self.ultimo_status

        self.ultimo_tempo_entrega = time.time()
        return status, frame_copia

    def parar(self, aguardar_s: float = 2.0):
        """
        Sinaliza a thread para parar e aguarda até `aguardar_s`. O `cap` é local da
        thread e liberado por ela ao sair; se estiver presa num open/read lento, sai
        sozinha quando o timeout vencer (daemon), sem afetar um novo iniciar().
        """
        self.running = False
        self._parar_evento.set()
        with self.lock:
            self.ultimo_status = False
        if self.thread and self.thread.is_alive() and aguardar_s > 0:
            self.thread.join(timeout=aguardar_s)
            if self.thread.is_alive():
                print(f"⏳ Stream {self._fonte_mascarada()} ainda encerrando (leitura em andamento); será liberado em segundo plano.")
        print(f"🔌 Stream da câmera finalizado. Total de frames lidos: {self.total_frames_lidos}")

    def __enter__(self):
        self.iniciar()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.parar()

# Teste unitário e de carga do stream
if __name__ == "__main__":
    print("=" * 60)
    print("🎥 Testando Ingestão Resiliente de Câmera")
    print("=" * 60)

    # Teste com amostragem a 4 FPS
    with StreamCamera(fonte="data/videos/sample_restaurante.mp4", target_fps=4.0) as cam:
        time.sleep(0.5)  # Aguarda primeiro frame

        for i in range(20):
            sucesso, frame = cam.ler()
            if sucesso and frame is not None:
                h, w, _ = frame.shape
                print(f"  • Frame {i+1}/20 capturado com sucesso: {w}x{h} px | Total lido na thread: {cam.total_frames_lidos}")
            else:
                print(f"  • Aguardando sinal da câmera...")

    print("✅ Teste de ingestão de câmera finalizado com sucesso!")

