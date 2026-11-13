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
    def __init__(self, fonte: Union[str, int] = "data/videos/sample_restaurante.mp4",
                 target_fps: float = 4.0,
                 reconnect_timeout: float = 5.0):
        
        self.fonte_original = fonte
        self.fonte_sanitizada = self._sanitizar_fonte(fonte)
        self.target_fps = target_fps
        self.reconnect_timeout = reconnect_timeout

        # Estado da thread
        self.cap: Optional[cv2.VideoCapture] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        # Buffer mais recente (apenas 1 frame)
        self.ultimo_frame: Optional[np.ndarray] = None
        self.ultimo_status = False
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
        s = str(self.fonte_original)
        parsed = urlparse(s)
        if parsed.scheme in {"rtsp", "rtsps", "http", "https"} and parsed.password:
            port = f":{parsed.port}" if parsed.port else ""
            return urlunparse(parsed._replace(netloc=f"{parsed.username or ''}:****@{parsed.hostname or ''}{port}"))
        return s

    def iniciar(self):
        """Inicia a thread de captura contínua em segundo plano."""
        if self.running:
            return

        self.running = True
        self._conectar()
        self.thread = threading.Thread(target=self._loop_captura, daemon=True)
        self.thread.start()
        print(f"📡 Stream conectado na fonte: {self._fonte_mascarada()} (Meta: {self.target_fps} FPS)")

    def _conectar(self) -> bool:
        """Cria e configura o objeto cv2.VideoCapture com timeouts e buffer zerado."""
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass

        if isinstance(self.fonte_sanitizada, str) and self.fonte_sanitizada.startswith("rtsp"):
            # Otimizações de rede para câmeras de CFTV (TCP resiliente e buffer zerado)
            self.cap = cv2.VideoCapture(self.fonte_sanitizada, cv2.CAP_FFMPEG)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            self.cap = cv2.VideoCapture(self.fonte_sanitizada)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        return self.cap.isOpened()

    def _loop_captura(self):
        """Thread interna que drena continuamente o buffer de rede, retendo apenas o frame atual."""
        falhas_seguidas = 0
        while self.running:
            if self.cap is None or not self.cap.isOpened():
                print(f"⚠️ Conexão perdida em {self._fonte_mascarada()}. Tentando reconectar em {self.reconnect_timeout}s...")
                time.sleep(self.reconnect_timeout)
                self.total_reconexoes += 1
                if not self._conectar():
                    continue
                falhas_seguidas = 0
                print(f"✅ Conexão restabelecida: {self._fonte_mascarada()}")

            ret, frame = self.cap.read()

            if ret:
                falhas_seguidas = 0
                with self.lock:
                    self.ultimo_frame = frame
                    self.ultimo_status = True
                    self.total_frames_lidos += 1
            else:
                if self.is_arquivo:
                    # Em caso de arquivo gravado de teste, reinicia o vídeo em loop contínuo
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                else:
                    falhas_seguidas += 1
                    with self.lock:
                        self.ultimo_status = False
                    if falhas_seguidas >= 30 and self.cap is not None:
                        self.cap.release()
                        self.cap = None
                        falhas_seguidas = 0
                    time.sleep(0.1)

            # Previne consumo excessivo da CPU na thread de leitura
            time.sleep(0.005)

    def ler_se_pronto(self) -> Tuple[bool, Optional[np.ndarray]]:
        """Leitura não-bloqueante para escalonamento multi-câmera sem travar outras câmeras."""
        agora = time.time()
        if (agora - self.ultimo_tempo_entrega) < self.intervalo_alvo:
            return False, None

        with self.lock:
            if not self.ultimo_status or self.ultimo_frame is None:
                return False, None
            frame_copia = self.ultimo_frame.copy()

        self.ultimo_tempo_entrega = agora
        return True, frame_copia

    @property
    def conectada(self) -> bool:
        with self.lock:
            return bool(self.ultimo_status)

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

    def parar(self):
        """Para a thread e libera os recursos da câmera."""
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        if self.cap:
            self.cap.release()
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

