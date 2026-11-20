"""
Funções puras de apoio ao painel (formatação, exportação, validação de valores).
Ficam fora do dashboard.py para poderem ser testadas sem subir o Streamlit e
reaproveitadas pelo portal em nuvem.
"""

from typing import Any, Optional

import pandas as pd

LINHAS_EXIBIDAS_TABELA = 300
_PREFIXOS_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def na_faixa(regras: dict, chave: str, padrao: Any, minimo: Any, maximo: Any, tipo=float):
    """
    Valor de `regras_config.json` ajustado à faixa do controle. Um valor fora da
    faixa (ou inválido) editado à mão derrubava a página inteira do Streamlit.
    """
    try:
        valor = tipo(regras.get(chave, padrao))
    except (TypeError, ValueError):
        valor = tipo(padrao)
    return max(tipo(minimo), min(tipo(maximo), valor))


def fmt_pct(valor: Optional[float]) -> str:
    """Percentual para exibição; '-' quando não há dado (em vez de um falso 100%)."""
    return f"{float(valor):.1f}%" if valor is not None else "-"


def _neutralizar_formula(v: Any) -> Any:
    # "-" sozinho é o marcador de "sem dado", não fórmula
    if isinstance(v, str) and v != "-" and v.startswith(_PREFIXOS_FORMULA):
        return "'" + v
    return v


def csv_seguro(df: pd.DataFrame) -> bytes:
    """
    CSV para Excel (utf-8-sig) sem injeção de fórmula: textos que começam com
    = + - @ (ex.: um nome de mesa '=HYPERLINK(...)') ganham um apóstrofo na frente.
    Cobre colunas `object` e o tipo texto do pandas 3 (`str`).
    """
    seguro = df.copy()
    for col in seguro.columns:
        serie = seguro[col]
        if pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie):
            seguro[col] = serie.map(_neutralizar_formula)
    return seguro.to_csv(index=False).encode("utf-8-sig")
