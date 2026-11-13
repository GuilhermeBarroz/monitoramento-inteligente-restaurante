"""
Módulo de Visão Computacional: Motor de Detecção de Ocupação de Mesas.
Cruza as detecções do YOLOv10 (classe 'person') com os polígonos das mesas
utilizando Point-in-Polygon (cv2.pointPolygonTest) e filtro temporal (debounce).

Exibe/Salva o vídeo com:
- Polígono Verde: Mesa Livre
- Polígono Vermelho: Mesa Ocupada (+ contagem de clientes e tempo)
"""

import sys
import json
import time
from pathlib import Path
import cv2
import numpy as np
from ultralytics import YOLO

# Força UTF-8 no stdout para compatibilidade no Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

class MotorOcupacao:
    def __init__(self, config_path: str = "data/mesas_config.json", model_path: str = "yolov10b.pt", conf_thresh: float = 0.18):
        self.config_path = Path(config_path)
        self.conf_thresh = conf_thresh
        
        if not self.config_path.exists():
            raise FileNotFoundError(f"Arquivo de configuração '{config_path}' não encontrado.")

        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        # Prepara polígonos das mesas para o cv2.pointPolygonTest
        self.mesas = []
        for m in self.config.get("mesas", []):
            pts_np = np.array(m["pontos"], np.int32).reshape((-1, 1, 2))
            self.mesas.append({
                "id": m["id"],
                "descricao": m.get("descricao", ""),
                "pontos_np": pts_np,
                "pontos_raw": m["pontos"],
                # Estado de debounce
                "ocupada": False,
                "frames_com_presenca": 0,
                "frames_sem_presenca": 0,
                "qtd_pessoas": 0,
                "timestamp_inicio_ocupacao": None
            })

        print(f"Carregadas {len(self.mesas)} mesas de '{self.config_path.name}'.")
        print(f"Carregando modelo de IA: {model_path}...")
        self.model = YOLO(model_path)
        print("Modelo carregado com sucesso.")

    def verificar_ponto_na_mesa(self, ponto: tuple, mesa_pts_np) -> bool:
        """Verifica se uma coordenada (x, y) está dentro do polígono da mesa."""
        return cv2.pointPolygonTest(mesa_pts_np, ponto, False) >= 0

    def processar_frame(self, frame: np.ndarray, fps: float = 30.0) -> np.ndarray:
        """
        Executa inferência no frame, cruza as pessoas com as mesas
        e desenha a interface gráfica com status e métricas.
        """
        display = frame.copy()
        h, w, _ = frame.shape

        # 1. Inferência YOLO (detecta apenas pessoas)
        results = self.model.predict(source=frame, conf=self.conf_thresh, classes=[0], verbose=False)
        boxes = results[0].boxes

        # Extrai pontos-chave de cada pessoa e atribui a no máximo uma mesa
        contagem_bruta = {m["id"]: 0 for m in self.mesas}
        for box in boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx = int((x1 + x2) / 2)
            centro = (cx, int((y1 + y2) / 2))
            peito = (cx, int(y1 + (y2 - y1) * 0.40))

            # Desenha bounding box suave da pessoa
            cv2.rectangle(display, (int(x1), int(y1)), (int(x2), int(y2)), (200, 200, 200), 1)
            cv2.circle(display, centro, 4, (0, 255, 255), -1)

            for mesa in self.mesas:
                if self.verificar_ponto_na_mesa(centro, mesa["pontos_np"]) or self.verificar_ponto_na_mesa(peito, mesa["pontos_np"]):
                    contagem_bruta[mesa["id"]] += 1
                    break

        # 2. Atualiza estado de cada mesa
        DEBOUNCE_FRAMES_OCUPAR = 2   # Requer 2 frames de presença para considerar ocupada
        DEBOUNCE_FRAMES_LIBERAR = 6  # Requer 6 frames vazios consecutivos para considerar liberada

        for mesa in self.mesas:
            pessoas_na_mesa = contagem_bruta[mesa["id"]]

            if pessoas_na_mesa > 0:
                mesa["frames_com_presenca"] += 1
                mesa["frames_sem_presenca"] = 0
                mesa["qtd_pessoas"] = pessoas_na_mesa

                if mesa["frames_com_presenca"] >= DEBOUNCE_FRAMES_OCUPAR and not mesa["ocupada"]:
                    mesa["ocupada"] = True
                    mesa["timestamp_inicio_ocupacao"] = time.time()
            else:
                mesa["frames_sem_presenca"] += 1
                if mesa["frames_sem_presenca"] >= DEBOUNCE_FRAMES_LIBERAR:
                    mesa["ocupada"] = False
                    mesa["frames_com_presenca"] = 0
                    mesa["qtd_pessoas"] = 0
                    mesa["timestamp_inicio_ocupacao"] = None

            # 3. Desenho do polígono da mesa conforme estado
            if mesa["ocupada"]:
                cor_poligono = (0, 0, 255)      # Vermelho (Ocupada)
                texto_status = f"{mesa['id']}: OCUPADA ({mesa['qtd_pessoas']}p)"
            else:
                cor_poligono = (0, 255, 0)      # Verde (Livre)
                texto_status = f"{mesa['id']}: LIVRE"

            # Preenchimento translúcido da mesa
            overlay = display.copy()
            cv2.fillPoly(overlay, [mesa["pontos_np"]], color=cor_poligono)
            cv2.addWeighted(overlay, 0.25, display, 0.75, 0, display)
            cv2.polylines(display, [mesa["pontos_np"]], isClosed=True, color=cor_poligono, thickness=2)

            # Placa de identificação no primeiro vértice
            p1 = mesa["pontos_raw"][0]
            largura_tarja = len(texto_status) * 9 + 12
            cv2.rectangle(display, (p1[0], p1[1] - 22), (p1[0] + largura_tarja, p1[1]), (25, 25, 25), -1)
            cv2.putText(display, texto_status, (p1[0] + 5, p1[1] - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

        # 4. Barra Superior de Monitoramento
        mesas_ocupadas = sum(1 for m in self.mesas if m["ocupada"])
        total = len(self.mesas)
        taxa_ocupacao = (mesas_ocupadas / total) * 100 if total > 0 else 0

        cv2.rectangle(display, (0, 0), (w, 38), (15, 15, 15), -1)
        resumo_texto = f"Monitoramento Salão | Mesas Ocupadas: {mesas_ocupadas}/{total} ({taxa_ocupacao:.0f}%) | Livres: {total - mesas_ocupadas}"
        cv2.putText(display, resumo_texto, (15, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 200), 2, cv2.LINE_AA)

        return display

    def processar_video(self, video_path: str = "data/videos/sample_restaurante.mp4",
                        output_path: str = "data/videos/output_ocupacao.mp4",
                        max_frames: int = 300,
                        mostrar: bool = False):
        """
        Processa o arquivo de vídeo e opcionalmente salva o vídeo anotado.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"Erro: Não foi possível abrir o vídeo '{video_path}'.")
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        limite = min(max_frames, total_frames) if max_frames else total_frames
        print(f"Processando {limite} frames a partir de '{video_path}'...")

        writer = None
        if output_path:
            out_file = Path(output_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            writer = cv2.VideoWriter(str(out_file), fourcc, fps, (width, height))

        frames_processados = 0
        t0 = time.time()

        while frames_processados < limite:
            ret, frame = cap.read()
            if not ret:
                break

            anotado = self.processar_frame(frame, fps=fps)

            if writer:
                writer.write(anotado)

            if mostrar:
                cv2.imshow("Monitor de Mesas com IA", anotado)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            frames_processados += 1
            if frames_processados % 50 == 0 or frames_processados == limite:
                tempo_decorrido = time.time() - t0
                fps_real = frames_processados / tempo_decorrido if tempo_decorrido > 0 else 0
                ocupadas = [m["id"] for m in self.mesas if m["ocupada"]]
                print(f"Frame {frames_processados}/{limite} ({fps_real:.1f} FPS) -> Ocupadas: {ocupadas or 'Nenhuma'}")

        cap.release()
        if writer:
            writer.release()
            print(f"✅ Vídeo resultante salvo com sucesso em: {Path(output_path).resolve()}")

        if mostrar:
            cv2.destroyAllWindows()

if __name__ == "__main__":
    motor = MotorOcupacao()
    # Processa os primeiros 200 frames para teste
    motor.processar_video(max_frames=200, output_path="data/videos/output_ocupacao.mp4")

