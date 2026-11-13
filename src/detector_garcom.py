"""
Módulo de Detecção de Atendimento (Garçom) e Análise de Uniformes (HSV).
Combina duas estratégias complementares:
1. Análise de cor de uniforme/avental no tórax (HSV) dentro ou próximo à mesa.
2. Heurística por excedente de pessoas acima da linha de base estável da mesa.
"""

from collections import deque
import statistics
from typing import Dict, List, Optional, Tuple
import cv2
import numpy as np

# Faixas HSV no espaço OpenCV (H: 0..179, S: 0..255, V: 0..255)
PRESETS_UNIFORME_HSV: Dict[str, List[Tuple[Tuple[int, int, int], Tuple[int, int, int]]]] = {
    "Preto / Escuro": [((0, 0, 0), (179, 255, 65))],
    "Branco": [((0, 0, 195), (179, 45, 255))],
    "Azul": [((95, 70, 50), (132, 255, 255))],
    "Verde": [((38, 60, 50), (85, 255, 255))],
    "Amarelo": [((18, 80, 80), (35, 255, 255))],
    "Vermelho": [
        ((0, 80, 50), (10, 255, 255)),
        ((170, 80, 50), (179, 255, 255)),
    ],
}


class AnalisadorUniformeHSV:
    """Detecta se uma pessoa detectada veste o uniforme configurado analisando o tórax em HSV."""

    def __init__(self, cor_preset: str = "Preto / Escuro", ratio_min: float = 0.25):
        self.cor_preset = cor_preset if cor_preset in PRESETS_UNIFORME_HSV else "Preto / Escuro"
        self.ratio_min = float(max(0.05, min(0.95, ratio_min)))

    def configurar(self, cor_preset: str, ratio_min: float):
        if cor_preset in PRESETS_UNIFORME_HSV:
            self.cor_preset = cor_preset
        self.ratio_min = float(max(0.05, min(0.95, ratio_min)))

    def verificar_pessoa(self, frame_bgr: np.ndarray, xyxy: Tuple[float, float, float, float]) -> Tuple[bool, float]:
        """
        Analisa a região superior do tronco da detecção e retorna (eh_garcom, proporcao_cor).
        Recorta apenas a área central do peito/avental para reduzir ruído de fundo.
        """
        h_img, w_img = frame_bgr.shape[:2]
        x1, y1, x2, y2 = xyxy
        bw = x2 - x1
        bh = y2 - y1
        if bw < 10 or bh < 14:
            return False, 0.0

        tx1 = max(0, int(x1 + bw * 0.20))
        tx2 = min(w_img, int(x2 - bw * 0.20))
        ty1 = max(0, int(y1 + bh * 0.18))
        ty2 = min(h_img, int(y1 + bh * 0.58))

        if tx2 - tx1 < 4 or ty2 - ty1 < 4:
            return False, 0.0

        roi_bgr = frame_bgr[ty1:ty2, tx1:tx2]
        roi_hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)

        faixas = PRESETS_UNIFORME_HSV[self.cor_preset]
        mask_total = None
        for low, high in faixas:
            m = cv2.inRange(roi_hsv, np.array(low, dtype=np.uint8), np.array(high, dtype=np.uint8))
            mask_total = m if mask_total is None else cv2.bitwise_or(mask_total, m)

        if mask_total is None or mask_total.size == 0:
            return False, 0.0

        proporcao = float(cv2.countNonZero(mask_total)) / float(mask_total.size)
        return proporcao >= self.ratio_min, proporcao


class RastreadorAtendimentoMesa:
    """
    Avalia por mesa se há funcionário presente combinando uniforme (HSV) e
    excedente temporário de pessoas acima da linha de base estável dos clientes.
    """

    def __init__(
        self,
        janela_base_s: float = 300.0,
        visita_min_s: float = 4.0,
        visita_max_s: float = 180.0,
        uniforme_debounce_s: float = 1.0,
        min_amostras_base: int = 4,
    ):
        self.janela_base_s = janela_base_s
        self.visita_min_s = visita_min_s
        self.visita_max_s = visita_max_s
        self.uniforme_debounce_s = uniforme_debounce_s
        self.min_amostras_base = min_amostras_base

        self.hist_base: deque = deque()
        self.base_atual: int = 0
        self.inicio_excedente: Optional[float] = None
        self.visita_excedente_ativa: bool = False
        self.inicio_uniforme: Optional[float] = None

    def resetar(self):
        self.hist_base.clear()
        self.base_atual = 0
        self.inicio_excedente = None
        self.visita_excedente_ativa = False
        self.inicio_uniforme = None

    def avaliar(
        self,
        estado_valor: str,
        qtd_clientes: int,
        garcom_uniforme: bool,
        timestamp: float,
        usar_excedente: bool = True,
    ) -> Tuple[bool, bool]:
        """
        Retorna `(funcionario_presente, cancelar_visita_falsa)`.
        `cancelar_visita_falsa` sinaliza que o excedente ultrapassou `visita_max_s`
        (indicando novo cliente sentado, e não um garçom em atendimento).
        """
        if estado_valor == "LIVRE":
            self.resetar()
            return False, False

        # 1. Confirmação por uniforme (debounce curto evita ruído de 1 único frame)
        uniforme_confirmado = False
        if garcom_uniforme:
            if self.inicio_uniforme is None:
                self.inicio_uniforme = timestamp
            if (timestamp - self.inicio_uniforme) >= self.uniforme_debounce_s:
                uniforme_confirmado = True
        else:
            self.inicio_uniforme = None

        # Na etapa PENDENTE_LIMPEZA, apenas uniforme atua como sinal direto de funcionário;
        # visitas sem uniforme são tratadas pela própria janela de limpeza da FSM.
        if estado_valor == "PENDENTE_LIMPEZA":
            self.hist_base.clear()
            self.base_atual = 0
            self.inicio_excedente = None
            self.visita_excedente_ativa = False
            return uniforme_confirmado, False

        # 2. Heurística por excedente de pessoas enquanto a mesa está ocupada
        cancelar_visita_falsa = False
        if usar_excedente and qtd_clientes > 0:
            while self.hist_base and (timestamp - self.hist_base[0][0]) > self.janela_base_s:
                self.hist_base.popleft()

            base_estavel = (
                int(statistics.mode([n for _, n in self.hist_base]))
                if len(self.hist_base) >= self.min_amostras_base
                else None
            )

            if base_estavel is not None and qtd_clientes > base_estavel:
                if self.inicio_excedente is None:
                    self.inicio_excedente = timestamp
                duracao = timestamp - self.inicio_excedente

                if duracao >= self.visita_max_s:
                    # Excedente permanente -> novo cliente entrou na mesa
                    self.hist_base.clear()
                    self.hist_base.append((timestamp, qtd_clientes))
                    self.base_atual = qtd_clientes
                    self.inicio_excedente = None
                    self.visita_excedente_ativa = False
                    if not uniforme_confirmado:
                        cancelar_visita_falsa = True
                elif duracao >= self.visita_min_s:
                    self.visita_excedente_ativa = True
            else:
                self.inicio_excedente = None
                self.visita_excedente_ativa = False
                self.hist_base.append((timestamp, qtd_clientes))
                if len(self.hist_base) >= self.min_amostras_base:
                    self.base_atual = int(statistics.mode([n for _, n in self.hist_base]))
        else:
            self.inicio_excedente = None
            self.visita_excedente_ativa = False

        funcionario_presente = uniforme_confirmado or self.visita_excedente_ativa
        return funcionario_presente, cancelar_visita_falsa
