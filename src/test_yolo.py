"""
Script de teste de inferência do modelo YOLO.
Carrega o modelo YOLO (Nano), roda no frame de referência
e salva o resultado com as caixas delimitadoras e classes detectadas.
"""

import sys
from pathlib import Path
import cv2
from ultralytics import YOLO

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def testar_deteccao(imagem_path: str = "data/videos/frame_referencia.jpg", output_path: str = "data/videos/deteccao_teste.jpg"):
    img_path = Path(imagem_path)
    if not img_path.exists():
        print(f"Erro: Imagem '{imagem_path}' não encontrada.")
        return

    print("Carregando modelo YOLOv10n...")
    model = YOLO("yolov10n.pt")

    print(f"Executando detecção em '{img_path.name}'...")
    results = model.predict(source=str(img_path), conf=0.3, save=False)

    result = results[0]
    total_deteccoes = len(result.boxes)
    print(f"Total de objetos detectados: {total_deteccoes}")

    pessoas = 0
    mesas_detectadas = 0

    for box in result.boxes:
        cls_id = int(box.cls[0].item())
        conf = float(box.conf[0].item())
        nome_classe = model.names[cls_id]

        if nome_classe == "person":
            pessoas += 1
        elif nome_classe in ["dining table", "chair"]:
            mesas_detectadas += 1

        print(f"  * Detectado: {nome_classe:<15} (Confiança: {conf:.1%})")

    print("=" * 50)
    print(f"Resumo: {pessoas} pessoas detectadas, {mesas_detectadas} cadeiras/mesas identificadas.")
    print("=" * 50)

    # Salva a imagem anotada
    anotada = result.plot()
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_file), anotada)
    print(f"Resultado anotado salvo em: {out_file.resolve()}")

if __name__ == "__main__":
    testar_deteccao()

