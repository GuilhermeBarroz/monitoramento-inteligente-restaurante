"""
Cálculos puros do portal (sem banco e sem Streamlit): montagem da matriz de pico,
resumos por hora / dia / turno, sugestão de garçons, formatação, CSV seguro e o
relatório HTML imprimível.

Espelham as regras do painel local (`src/analytics_db.py` e `src/util_painel.py`).
O portal roda em outro container (a imagem só recebe `cloud/portal`), por isso as
funções são replicadas aqui; `tests/test_portal.py` compara os dois lados para
detectar divergência.
"""

import html
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

DIAS_SEMANA_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
TURNOS_PADRAO = [
    ("Madrugada (00h–05h)", 0, 5),
    ("Manhã (06h–10h)", 6, 10),
    ("Almoço (11h–14h)", 11, 14),
    ("Tarde (15h–17h)", 15, 17),
    ("Jantar (18h–23h)", 18, 23),
]
_PREFIXOS_FORMULA = ("=", "+", "-", "@", "\t", "\r")


# ---------------------------------------------------------------------------
# Formatação e exportação
# ---------------------------------------------------------------------------
def fmt_pct(valor: Optional[float]) -> str:
    """Percentual para exibição; '-' quando não há dado (em vez de um falso 100%)."""
    return f"{float(valor):.1f}%" if valor is not None else "-"


def fmt_min(valor: Optional[float]) -> str:
    return f"{float(valor):.1f} min" if valor is not None else "-"


def formatar_duracao_curta(segundos: Optional[float]) -> str:
    """Segundos em `MM:SS` ou `Xh YYm`; '-' sem dado."""
    if segundos is None:
        return "-"
    s = max(0, int(round(float(segundos))))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 60:02d}:{s % 60:02d}"


def seg_para_min(valor: Optional[float]) -> Optional[float]:
    return round(float(valor) / 60.0, 1) if valor is not None else None


def _neutralizar_formula(v: Any) -> Any:
    # "-" sozinho é o marcador de "sem dado", não fórmula
    if isinstance(v, str) and v != "-" and v.startswith(_PREFIXOS_FORMULA):
        return "'" + v
    return v


def csv_seguro(df: pd.DataFrame) -> bytes:
    """CSV para Excel (utf-8-sig) sem injeção de fórmula (= + - @ ganham apóstrofo)."""
    seguro = df.copy()
    for col in seguro.columns:
        serie = seguro[col]
        if pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie):
            seguro[col] = serie.map(_neutralizar_formula)
    return seguro.to_csv(index=False).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------
def tipo_alerta(detalhes: Any) -> str:
    """
    Tipo do alerta a partir do JSON do evento; 'ALERTA' se ausente, vazio ou não-texto.
    Mesma regra da contagem por tipo em SQL (`consultas._TIPO_ALERTA`).
    """
    valor = detalhes.get("alerta") if isinstance(detalhes, dict) else None
    return valor if isinstance(valor, str) and valor else "ALERTA"


def tempo_decorrido_alerta(detalhes: Any) -> str:
    det = detalhes if isinstance(detalhes, dict) else {}
    tempo = det.get("tempo_espera") or det.get("tempo_cozinha") or det.get("tempo_espera_limpeza")
    try:
        return formatar_duracao_curta(float(tempo)) if tempo is not None else "-"
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# Pico, escala de garçons e turnos
# ---------------------------------------------------------------------------
def sugerir_garcons(mesas_ocupadas: float, mesas_por_garcom: int, minimo_se_ativo: bool = True) -> int:
    divisor = max(1, int(mesas_por_garcom))
    if mesas_ocupadas <= 0:
        return 0
    estimativa = int(math.ceil(float(mesas_ocupadas) / divisor))
    return max(1, estimativa) if minimo_se_ativo else estimativa


def _media_ponderada_por_amostras(itens: List[dict]) -> float:
    """Média de ocupação ponderada pelo nº de amostras; células sem amostra não entram."""
    peso = sum(int(i.get("amostras", 0)) for i in itens)
    if peso <= 0:
        return 0.0
    return sum(i["ocupacao_media_mesas"] * int(i.get("amostras", 0)) for i in itens) / peso


Celula = Tuple[int, int]


def montar_matriz(
    amostras: Dict[Celula, dict],
    visitas: Dict[Celula, dict],
    alertas: Dict[Celula, int],
    mesas_por_garcom: int,
) -> List[dict]:
    """
    Matriz `(dia_semana, hora)` a partir dos agregados do banco:
    - amostras[cel] = {amostras, oc_media, oc_pico, tot_media, pes_media}
      (média de instantes de 1 minuto, somando as câmeras em cada instante)
    - visitas[cel]  = {giros, perm_mediana_seg}
    - alertas[cel]  = quantidade de ALERTA_DISPARADO
    """
    ativas = set(amostras) | set(visitas) | set(alertas)
    matriz: List[dict] = []
    for dia in range(7):
        for hora in range(24):
            cel = (dia, hora)
            if cel not in ativas:
                continue
            am = amostras.get(cel)
            vi = visitas.get(cel) or {}
            if am and int(am.get("amostras", 0)) > 0:
                oc_media = float(am["oc_media"])
                oc_pico = int(am["oc_pico"])
                tot_media = float(am["tot_media"])
                pes_media = float(am["pes_media"])
                taxa = (oc_media / tot_media * 100.0) if tot_media > 0 else 0.0
                n_am = int(am["amostras"])
            else:
                # Sem amostras de ocupação nesta hora não há como saber quantas mesas
                # estavam ocupadas ao mesmo tempo (mesma regra do painel local).
                oc_media, oc_pico, pes_media, taxa, n_am = 0.0, 0, 0.0, 0.0, 0
            perm = vi.get("perm_mediana_seg")
            matriz.append({
                "dia_semana": dia,
                "dia_nome": DIAS_SEMANA_PT[dia],
                "hora": hora,
                "hora_label": f"{hora:02d}:00",
                "amostras": n_am,
                "ocupacao_media_mesas": round(oc_media, 2),
                "ocupacao_pico_mesas": int(oc_pico),
                "taxa_ocupacao_media_pct": round(taxa, 1),
                "pessoas_media": round(pes_media, 1),
                "giros_visitas": int(vi.get("giros", 0)),
                "permanencia_mediana_min": seg_para_min(perm),
                "alertas_disparados": int(alertas.get(cel, 0)),
                "garcons_recomendados_medio": sugerir_garcons(oc_media, mesas_por_garcom, minimo_se_ativo=oc_media > 0),
                "garcons_recomendados_pico": sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0),
            })
    return matriz


def resumo_por_hora(matriz: Iterable[dict], mesas_por_garcom: int) -> List[dict]:
    por_hora: Dict[int, List[dict]] = {h: [] for h in range(24)}
    for item in matriz:
        por_hora[item["hora"]].append(item)
    linhas: List[dict] = []
    for h in range(24):
        itens = por_hora[h]
        oc_media = _media_ponderada_por_amostras(itens) if itens else 0.0
        oc_pico = max((i["ocupacao_pico_mesas"] for i in itens), default=0)
        linhas.append({
            "hora": h,
            "hora_label": f"{h:02d}:00",
            "ocupacao_media_mesas": round(oc_media, 2),
            "ocupacao_pico_mesas": int(oc_pico),
            "giros_visitas": int(sum(i["giros_visitas"] for i in itens)),
            "alertas_disparados": int(sum(i["alertas_disparados"] for i in itens)),
            "garcons_recomendados_medio": sugerir_garcons(oc_media, mesas_por_garcom, minimo_se_ativo=oc_media > 0),
            "garcons_recomendados_pico": sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0),
        })
    return linhas


def resumo_por_dia_semana(matriz: Iterable[dict], mesas_por_garcom: int) -> List[dict]:
    por_dia: Dict[int, List[dict]] = {d: [] for d in range(7)}
    for item in matriz:
        por_dia[item["dia_semana"]].append(item)
    linhas: List[dict] = []
    for d in range(7):
        itens = por_dia[d]
        oc_media = _media_ponderada_por_amostras(itens) if itens else 0.0
        oc_pico = max((i["ocupacao_pico_mesas"] for i in itens), default=0)
        linhas.append({
            "dia_semana": d,
            "dia_nome": DIAS_SEMANA_PT[d],
            "ocupacao_media_mesas": round(oc_media, 2),
            "ocupacao_pico_mesas": int(oc_pico),
            "giros_visitas": int(sum(i["giros_visitas"] for i in itens)),
            "alertas_disparados": int(sum(i["alertas_disparados"] for i in itens)),
            "garcons_recomendados_pico": sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0),
        })
    return linhas


def ranking_turnos(por_hora: List[dict], mesas_por_garcom: int) -> List[dict]:
    idx = {item["hora"]: item for item in por_hora}
    turnos: List[dict] = []
    for nome, h_ini, h_fim in TURNOS_PADRAO:
        fatias = [idx[h] for h in range(h_ini, h_fim + 1)]
        ativas = [f for f in fatias if f["ocupacao_media_mesas"] > 0 or f["giros_visitas"] > 0 or f["alertas_disparados"] > 0]
        oc_media = sum(f["ocupacao_media_mesas"] for f in ativas) / len(ativas) if ativas else 0.0
        oc_pico = max((f["ocupacao_pico_mesas"] for f in fatias), default=0)
        g_med = sugerir_garcons(oc_media, mesas_por_garcom, minimo_se_ativo=oc_media > 0)
        g_pico = sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0)
        turnos.append({
            "turno": nome,
            "ocupacao_media_mesas": round(oc_media, 2),
            "ocupacao_pico_mesas": int(oc_pico),
            "giros_visitas": sum(f["giros_visitas"] for f in fatias),
            "alertas_disparados": sum(f["alertas_disparados"] for f in fatias),
            "garcons_medios": g_med,
            "garcons_pico": g_pico,
            "escala_sugerida": (
                f"{g_med} garçom(ns) ({g_pico} no pico)"
                if g_pico > g_med
                else (f"{g_med} garçom(ns)" if g_med > 0 else "Sem movimento registrado")
            ),
        })
    return turnos


# ---------------------------------------------------------------------------
# Saúde das lojas (admin)
# ---------------------------------------------------------------------------
def status_sync(segundos_desde_sync: Optional[float], ativo: bool, limite_atraso_seg: float = 900.0) -> str:
    if not ativo:
        return "⛔ Desativada"
    if segundos_desde_sync is None:
        return "⚪ Nunca sincronizou"
    if segundos_desde_sync <= limite_atraso_seg:
        return "🟢 Em dia"
    if segundos_desde_sync <= 6 * 3600:
        return "🟡 Atrasada"
    return "🔴 Sem contato"


# ---------------------------------------------------------------------------
# Relatório executivo imprimível
# ---------------------------------------------------------------------------
def _num(v: Any) -> str:
    return "-" if v is None else html.escape(str(v))


def gerar_relatorio_html(
    loja_nome: str,
    periodo_label: str,
    camera_label: str,
    resumo: dict,
    desempenho: List[dict],
    turnos: List[dict],
) -> bytes:
    """HTML autocontido e imprimível; todo texto vindo do banco passa por html.escape."""
    linhas_mesas = "".join(
        f"<tr><td>{html.escape(str(d['camera_nome']))}</td>"
        f"<td><strong>{html.escape(str(d['mesa_id']))}</strong></td>"
        f"<td>{_num(d['visitas'])}</td><td>{_num(d['permanencia_mediana_min'])}</td>"
        f"<td>{_num(d['espera_mediana_min'])}</td><td>{_num(d['limpeza_mediana_min'])}</td>"
        f"<td>{_num(d['alertas'])}</td><td>{html.escape(fmt_pct(d['sla_ok_pct']))}</td></tr>"
        for d in desempenho
    ) or "<tr><td colspan='8'>Sem dados no período selecionado.</td></tr>"
    linhas_turnos = "".join(
        f"<tr><td>{html.escape(str(t['turno']))}</td><td>{_num(t['ocupacao_media_mesas'])}</td>"
        f"<td>{_num(t['ocupacao_pico_mesas'])}</td><td>{_num(t['giros_visitas'])}</td>"
        f"<td>{_num(t['alertas_disparados'])}</td><td><strong>{html.escape(str(t['escala_sugerida']))}</strong></td></tr>"
        for t in turnos
    )
    esc = html.escape
    doc = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Relatório Gerencial — {esc(loja_nome)}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 28px; color: #1f2937; }}
  .meta {{ color: #4b5563; margin-bottom: 18px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }}
  .card {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 12px; background: #f9fafb; }}
  .card strong {{ display: block; font-size: 1.35rem; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0 24px; font-size: 0.92rem; }}
  th, td {{ border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; }}
  th {{ background: #f3f4f6; }}
</style></head><body>
  <h1>Relatório Gerencial — {esc(loja_nome)}</h1>
  <div class="meta">Período: <strong>{esc(periodo_label)}</strong> | Filtro: <strong>{esc(camera_label)}</strong></div>
  <div class="kpis">
    <div class="card">Visitas Concluídas<strong>{_num(resumo['total_visitas'])}</strong></div>
    <div class="card">SLA Atendimento / Limpeza<strong>{esc(fmt_pct(resumo.get('sla_sem_alerta_pct')))} / {esc(fmt_pct(resumo.get('sla_limpeza_pct')))}</strong></div>
    <div class="card">Permanência Mediana / P90<strong>{esc(fmt_min(resumo.get('permanencia_mediana_min')))} / {esc(fmt_min(resumo.get('permanencia_p90_min')))}</strong></div>
    <div class="card">Espera Mediana Garçom<strong>{esc(fmt_min(resumo.get('espera_mediana_min')))}</strong></div>
    <div class="card">Limpeza Mediana<strong>{esc(fmt_min(resumo.get('limpeza_mediana_min')))}</strong></div>
    <div class="card">Alertas Disparados<strong>{_num(resumo['total_alertas'])}</strong></div>
  </div>
  <h2>Desempenho Operacional por Mesa</h2>
  <table>
    <thead><tr><th>Câmera</th><th>Mesa</th><th>Visitas</th><th>Mediana Perm. (min)</th><th>Espera Garçom (min)</th><th>Limpeza (min)</th><th>Alertas</th><th>SLA OK</th></tr></thead>
    <tbody>{linhas_mesas}</tbody>
  </table>
  <h2>Pico e Dimensionamento Sugerido de Garçons por Turno</h2>
  <table>
    <thead><tr><th>Turno</th><th>Ocupação Média</th><th>Pico Mesas</th><th>Visitas</th><th>Alertas</th><th>Escala Sugerida</th></tr></thead>
    <tbody>{linhas_turnos}</tbody>
  </table>
</body></html>"""
    return doc.encode("utf-8")
