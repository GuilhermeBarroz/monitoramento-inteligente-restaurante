"""
Script de teste e inspeção do vídeo de exemplo.
Lê o arquivo de vídeo, exibe metadados e salva o primeiro frame
para servir como imagem de calibração das mesas.
"""

import sys
from pathlib import Path
import cv2

# Força UTF-8 no stdout para compatibilidade com terminais Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def inspecionar_video(video_path: str, output_frame_path: str = "data/videos/frame_referencia.jpg"):
    path = Path(video_path)
    if not path.exists():
        print(f"Erro: Arquivo de vídeo não encontrado em '{video_path}'")
        return

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print(f"Erro: Não foi possível abrir o vídeo '{video_path}'")
        return

    # Extrai metadados do vídeo
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duracao_segundos = total_frames / fps if fps > 0 else 0

    print("=" * 50)
    print("Metadados do Vídeo de Teste:")
    print(f"  * Arquivo:       {path.name}")
    print(f"  * Resolução:     {width} x {height} px")
    print(f"  * Taxa de FPS:   {fps:.2f} quadros/s")
    print(f"  * Total Frames:  {total_frames}")
    print(f"  * Duração Total: {duracao_segundos:.1f} segundos")
    print("=" * 50)

    # Captura o primeiro frame de referência
    sucesso, frame = cap.read()
    if sucesso:
        out_path = Path(output_frame_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), frame)
        print(f"Frame de referência salvo em: {out_path.resolve()}")
    else:
        print("Aviso: Não foi possível ler o primeiro frame.")

    cap.release()

if __name__ == "__main__":
    inspecionar_video("data/videos/sample_restaurante.mp4")
