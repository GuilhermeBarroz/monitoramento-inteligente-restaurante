"""
Utilitários de validação, sanitização e persistência atômica da configuração de mesas.
Compartilhado entre o Editor Web no Dashboard e scripts de calibração.
"""

import json
import math
import os
import re
from pathlib import Path
from typing import Any, List, Optional
import cv2
import numpy as np

CORES_PREVIEW_BGR = [
    (0, 255, 0),    # Verde
    (255, 200, 0),  # Ciano/Amarelo
    (255, 100, 0),  # Azul
    (0, 165, 255),  # Laranja
    (180, 0, 255),  # Roxo
    (255, 0, 180),  # Magenta
    (0, 220, 220),  # Amarelo
]

_CONTROLE_REGEX = re.compile(r"[\x00-\x1f\x7f]")


def _sanitizar_texto(valor: Any, tamanho_max: int) -> str:
    """Remove caracteres de controle e espaços extras, limitando comprimento."""
    texto = _CONTROLE_REGEX.sub("", str(valor or "")).strip()
    return texto[:tamanho_max]


def validar_e_normalizar_mesas(mesas_raw: Any, largura: int, altura: int) -> List[dict]:
    """
    Valida estritamente a estrutura das mesas recebida do navegador/editor.
    Previne nomes vazios, duplicados e coordenadas inválidas fora dos limites da imagem.
    """
    if not isinstance(largura, (int, float)) or not isinstance(altura, (int, float)):
        raise ValueError("Resolução de imagem inválida.")

    larg_int = int(largura)
    alt_int = int(altura)
    if larg_int <= 0 or alt_int <= 0:
        raise ValueError("Dimensões da imagem devem ser positivas.")

    if not isinstance(mesas_raw, list):
        raise ValueError("Lista de mesas inválida.")
    if len(mesas_raw) > 200:
        raise ValueError("Limite máximo de 200 mesas excedido.")

    ids_vistos = set()
    mesas_limpas: List[dict] = []

    for idx, item in enumerate(mesas_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item #{idx} possui formato inválido.")

        mesa_id = _sanitizar_texto(item.get("id", ""), tamanho_max=40)
        if not mesa_id:
            raise ValueError(f"A mesa #{idx} está sem nome/ID.")

        chave_normalizada = mesa_id.casefold()
        if chave_normalizada in ids_vistos:
            raise ValueError(f"Nome duplicado detectado: '{mesa_id}'. Cada mesa deve ter um nome único.")
        ids_vistos.add(chave_normalizada)

        descricao = _sanitizar_texto(item.get("descricao", ""), tamanho_max=80)
        pontos_raw = item.get("pontos", [])
        if not isinstance(pontos_raw, list) or len(pontos_raw) < 3:
            raise ValueError(f"'{mesa_id}' precisa ter pelo menos 3 vértices para formar uma zona.")
        if len(pontos_raw) > 50:
            raise ValueError(f"'{mesa_id}' excede o limite de 50 vértices.")

        pontos_validos: List[List[int]] = []
        for pt in pontos_raw:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ValueError(f"Coordenada inválida em '{mesa_id}'.")
            x_val, y_val = float(pt[0]), float(pt[1])
            if not (math.isfinite(x_val) and math.isfinite(y_val)):
                raise ValueError(f"Coordenada não finita em '{mesa_id}'.")

            x_clamp = max(0, min(larg_int, int(round(x_val))))
            y_clamp = max(0, min(alt_int, int(round(y_val))))
            pontos_validos.append([x_clamp, y_clamp])

        mesas_limpas.append({
            "id": mesa_id,
            "descricao": descricao,
            "pontos": pontos_validos,
        })

    return mesas_limpas


def gerar_preview_calibracao(mesas: List[dict], img_base_path: Path, preview_out_path: Path) -> None:
    """Gera imagem anotada de preview das zonas de forma atômica."""
    if not img_base_path.exists():
        return

    img = cv2.imread(str(img_base_path))
    if img is None:
        return

    h, w, _ = img.shape
    preview = img.copy()

    for i, mesa in enumerate(mesas):
        cor = CORES_PREVIEW_BGR[i % len(CORES_PREVIEW_BGR)]
        pts = np.array(mesa["pontos"], np.int32).reshape((-1, 1, 2))

        overlay = preview.copy()
        cv2.fillPoly(overlay, [pts], color=cor)
        cv2.addWeighted(overlay, 0.25, preview, 0.75, 0, preview)
        cv2.polylines(preview, [pts], isClosed=True, color=cor, thickness=2)

        p_label = mesa["pontos"][0]
        y_top = max(p_label[1] - 25, 0)
        largura_box = max(110, len(mesa["id"]) * 11 + 16)
        cv2.rectangle(preview, (p_label[0], y_top), (min(w, p_label[0] + largura_box), y_top + 25), (20, 20, 20), -1)
        cv2.putText(preview, mesa["id"], (p_label[0] + 5, y_top + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

    cv2.rectangle(preview, (0, 0), (w, 38), (15, 15, 15), -1)
    cv2.putText(preview, f"Calibracao Atualizada: {len(mesas)} Mesas Mapeadas", (15, 26),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 200), 2)

    preview_out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_preview = preview_out_path.with_name(f"{preview_out_path.stem}_tmp{preview_out_path.suffix}")
    cv2.imwrite(str(tmp_preview), preview)
    os.replace(tmp_preview, preview_out_path)


def salvar_configuracao_mesas(
    mesas_raw: Any,
    largura: int,
    altura: int,
    imagem_referencia: str,
    config_path: Path,
    img_base_path: Optional[Path] = None,
    preview_out_path: Optional[Path] = None,
) -> List[dict]:
    """Valida e grava `mesas_config.json` atomicamente, atualizando o preview visual."""
    mesas_validadas = validar_e_normalizar_mesas(mesas_raw, largura, altura)
    ref_nome = Path(_sanitizar_texto(imagem_referencia, 120) or "frame_referencia.jpg").name

    dados = {
        "imagem_referencia": ref_nome,
        "resolucao": {"largura": int(largura), "altura": int(altura)},
        "total_mesas": len(mesas_validadas),
        "mesas": mesas_validadas,
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp.json")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, config_path)

    if img_base_path and preview_out_path:
        gerar_preview_calibracao(mesas_validadas, img_base_path, preview_out_path)

    return mesas_validadas

