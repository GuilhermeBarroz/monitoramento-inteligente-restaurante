"""
Script de Calibração das Mesas.
Gera o arquivo 'data/mesas_config.json' com as zonas de interesse (ROIs) das mesas
e produz uma imagem anotada de preview 'data/videos/calibracao_mesas_preview.jpg'.
"""

import json
from pathlib import Path
import cv2
import numpy as np

def criar_calibracao(frame_referencia_path: str = "data/videos/frame_referencia.jpg",
                     json_out_path: str = "data/mesas_config.json",
                     preview_out_path: str = "data/videos/calibracao_mesas_preview.jpg"):
    
    img_path = Path(frame_referencia_path)
    if not img_path.exists():
        print(f"Erro: Frame de referência '{frame_referencia_path}' não encontrado.")
        return

    img = cv2.imread(str(img_path))
    h, w, _ = img.shape

    # Zonas das Mesas calibradas sem sobreposições entre mesas adjacentes
    mesas_definidas = [
        {
            "id": "Mesa 1",
            "descricao": "Mesa Lateral Esquerda Superior",
            "pontos": [
                [90, 100],
                [400, 100],
                [400, 325],
                [90, 325]
            ]
        },
        {
            "id": "Mesa 2",
            "descricao": "Mesa Central Fundo",
            "pontos": [
                [550, 10],
                [785, 10],
                [785, 265],
                [550, 265]
            ]
        },
        {
            "id": "Mesa 3",
            "descricao": "Mesa Lateral Direita Fundo",
            "pontos": [
                [790, 130],
                [1210, 130],
                [1210, 365],
                [790, 365]
            ]
        },
        {
            "id": "Mesa 4",
            "descricao": "Mesa Lateral Esquerda Inferior",
            "pontos": [
                [75, 330],
                [420, 330],
                [420, 705],
                [75, 705]
            ]
        },
        {
            "id": "Mesa 5",
            "descricao": "Mesa Central / Direita Inferior",
            "pontos": [
                [470, 380],
                [1120, 380],
                [1120, 710],
                [470, 710]
            ]
        }
    ]

    # Salva arquivo JSON
    json_path = Path(json_out_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    dados = {
        "imagem_referencia": img_path.name,
        "resolucao": {"largura": w, "altura": h},
        "total_mesas": len(mesas_definidas),
        "mesas": mesas_definidas
    }
    
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    print(f"Configuração salva em: {json_path.resolve()}")

    # Gera preview visual com polígonos coloridos
    preview = img.copy()
    cores = [
        (0, 255, 0),    # Verde
        (255, 200, 0),  # Ciano/Amarelo
        (255, 100, 0),  # Azul
        (0, 165, 255),  # Laranja
        (180, 0, 255)   # Roxo
    ]

    for i, mesa in enumerate(mesas_definidas):
        cor = cores[i % len(cores)]
        pts = np.array(mesa["pontos"], np.int32).reshape((-1, 1, 2))
        
        overlay = preview.copy()
        cv2.fillPoly(overlay, [pts], color=cor)
        cv2.addWeighted(overlay, 0.25, preview, 0.75, 0, preview)
        cv2.polylines(preview, [pts], isClosed=True, color=cor, thickness=2)

        p_label = mesa["pontos"][0]
        y_top = max(p_label[1] - 25, 0)
        cv2.rectangle(preview, (p_label[0], y_top), (p_label[0] + 120, y_top + 25), (20, 20, 20), -1)
        cv2.putText(preview, mesa["id"], (p_label[0] + 5, y_top + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    cv2.rectangle(preview, (0, 0), (w, 38), (15, 15, 15), -1)
    cv2.putText(preview, f"Calibracao Concluida: {len(mesas_definidas)} Mesas Mapeadas", (15, 26),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 200), 2)

    preview_path = Path(preview_out_path)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(preview_path), preview)
    print(f"Preview da calibração salvo em: {preview_path.resolve()}")

if __name__ == "__main__":
    criar_calibracao()
