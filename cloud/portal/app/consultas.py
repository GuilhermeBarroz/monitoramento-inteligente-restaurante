"""
Consultas dos relatórios do portal no PostgreSQL central.

Regras:
- Toda consulta filtra por `estabelecimento_id` (isolamento entre lojas). O id vem
  da loja já autorizada para o usuário (ver `acesso.py`), nunca de texto livre.
- 100% parametrizado (`%(nome)s` do psycopg). Os únicos trechos de SQL compostos
  são fragmentos FIXOS desta função (nomes de coluna de uma lista branca).
- Agregação no banco: medianas e P90 via `percentile_cont` (mesma interpolação
  linear de `statistics.median` / `_percentil` do painel local).
- Dia da semana e hora saem de `epoch` no fuso da loja (`estabelecimentos.timezone`).

A conexão deve entregar linhas como dict (`row_factory=dict_row`).
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from . import calculos

LIMITE_EXPORTACAO = 50_000  # teto de linhas por exportação CSV (protege a memória do portal)
JANELA_CAMERAS_DIAS = 90

_COLUNAS_TEMPO = {"epoch", "fim_epoch"}

# Expressões fixas (sem dado de usuário). `%(tz)s` é sempre parâmetro.
# Aceita número JSON ou texto numérico (o painel local faz float("120")); o resto é ignorado
_TEMPO_LIMPEZA = (
    "CASE WHEN jsonb_typeof(detalhes->'tempo_limpeza') = 'number' "
    "OR (jsonb_typeof(detalhes->'tempo_limpeza') = 'string' "
    "AND detalhes->>'tempo_limpeza' ~ '^[0-9]{1,9}([.][0-9]+)?$') "
    "THEN (detalhes->>'tempo_limpeza')::float8 END"
)
_TIPO_ALERTA = (
    "CASE WHEN jsonb_typeof(detalhes->'alerta') = 'string' AND detalhes->>'alerta' <> '' "
    "THEN detalhes->>'alerta' ELSE 'ALERTA' END"
)


def desde_epoch(dias: Optional[int], agora: Optional[float] = None) -> Optional[float]:
    """Início da janela móvel (agora − dias); None = todo o histórico."""
    if dias is None or int(dias) <= 0:
        return None
    return (agora if agora is not None else time.time()) - int(dias) * 86400.0


def _filtro(coluna_tempo: str, est_id: int, desde: Optional[float], camera_id: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if coluna_tempo not in _COLUNAS_TEMPO:
        raise ValueError(f"Coluna de tempo inválida: {coluna_tempo}")
    partes = ["estabelecimento_id = %(est)s"]
    params: Dict[str, Any] = {"est": int(est_id)}
    if desde is not None:
        partes.append(coluna_tempo + " >= %(desde)s")
        params["desde"] = float(desde)
    if camera_id:
        partes.append("camera_id = %(cam)s")
        params["cam"] = str(camera_id)
    return " WHERE " + " AND ".join(partes), params


def _f(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


# ---------------------------------------------------------------------------
# Filtros da tela
# ---------------------------------------------------------------------------
def listar_cameras(conn, est_id: int, agora: Optional[float] = None) -> List[dict]:
    """
    Câmeras com dados nos últimos 90 dias (para o filtro), com o nome do registro mais
    recente (câmera renomeada mostra o nome atual), ordenadas pelo nome.
    """
    params = {"est": int(est_id), "desde": desde_epoch(JANELA_CAMERAS_DIAS, agora)}
    linhas = conn.execute(
        """
        SELECT DISTINCT ON (camera_id) camera_id, camera_nome FROM (
            SELECT camera_id, camera_nome, epoch AS t FROM amostras_ocupacao
             WHERE estabelecimento_id = %(est)s AND epoch >= %(desde)s
            UNION ALL
            SELECT camera_id, camera_nome, fim_epoch AS t FROM visitas_concluidas
             WHERE estabelecimento_id = %(est)s AND fim_epoch >= %(desde)s
        ) u
        ORDER BY camera_id, t DESC
        """,
        params,
    ).fetchall()
    cams = [{"camera_id": r["camera_id"], "camera_nome": r["camera_nome"]} for r in linhas]
    return sorted(cams, key=lambda c: (str(c["camera_nome"]).lower(), str(c["camera_id"])))


# ---------------------------------------------------------------------------
# KPIs do período
# ---------------------------------------------------------------------------
def resumo_geral(conn, est_id: int, desde: Optional[float], camera_id: Optional[str] = None) -> dict:
    where_v, p_v = _filtro("fim_epoch", est_id, desde, camera_id)
    where_e, p_e = _filtro("epoch", est_id, desde, camera_id)
    v = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE NOT houve_alerta) AS sem_alerta, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY duracao_segundos) AS perm_med, "
        "percentile_cont(0.9) WITHIN GROUP (ORDER BY duracao_segundos) AS perm_p90, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY espera_garcom_segundos) AS espera_med, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY tempo_preparo_segundos) AS preparo_med, "
        "AVG(qtd_pessoas) AS media_pessoas "
        "FROM visitas_concluidas" + where_v,
        p_v,
    ).fetchone()
    # SLA de limpeza por episódio: só eventos com `com_alerta` booleano entram (como no local)
    e = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE tipo_evento = 'ALERTA_DISPARADO') AS alertas, "
        "COUNT(*) FILTER (WHERE tipo_evento = 'MESA_HIGIENIZADA') AS limpezas, "
        "COUNT(*) FILTER (WHERE tipo_evento = 'MESA_HIGIENIZADA' "
        "AND jsonb_typeof(detalhes->'com_alerta') = 'boolean') AS limpezas_avaliaveis, "
        "COUNT(*) FILTER (WHERE tipo_evento = 'MESA_HIGIENIZADA' "
        "AND detalhes->'com_alerta' = 'false'::jsonb) AS limpezas_no_prazo, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY CASE WHEN tipo_evento = 'MESA_HIGIENIZADA' "
        "THEN " + _TEMPO_LIMPEZA + " END) AS limpeza_med "
        "FROM eventos_mesas" + where_e + " AND tipo_evento IN ('ALERTA_DISPARADO', 'MESA_HIGIENIZADA')",
        p_e,
    ).fetchone()

    total = int(v["total"] or 0)
    avaliaveis = int(e["limpezas_avaliaveis"] or 0)
    return {
        "total_visitas": total,
        "sla_sem_alerta_pct": round(int(v["sem_alerta"]) / total * 100.0, 1) if total else None,
        "total_limpezas": int(e["limpezas"] or 0),
        "sla_limpeza_pct": round(int(e["limpezas_no_prazo"]) / avaliaveis * 100.0, 1) if avaliaveis else None,
        "permanencia_mediana_min": calculos.seg_para_min(v["perm_med"]),
        "permanencia_p90_min": calculos.seg_para_min(v["perm_p90"]),
        "espera_mediana_min": calculos.seg_para_min(v["espera_med"]),
        "preparo_mediano_min": calculos.seg_para_min(v["preparo_med"]),
        "limpeza_mediana_min": calculos.seg_para_min(e["limpeza_med"]),
        "media_pessoas_por_mesa": round(float(v["media_pessoas"]), 1) if v["media_pessoas"] is not None else None,
        "total_alertas": int(e["alertas"] or 0),
    }


# ---------------------------------------------------------------------------
# Desempenho por mesa
# ---------------------------------------------------------------------------
def desempenho_por_mesa(conn, est_id: int, desde: Optional[float], camera_id: Optional[str] = None) -> List[dict]:
    where_v, p_v = _filtro("fim_epoch", est_id, desde, camera_id)
    where_e, p_e = _filtro("epoch", est_id, desde, camera_id)
    visitas = conn.execute(
        "SELECT camera_id, MAX(camera_nome) AS camera_nome, mesa_id, COUNT(*) AS visitas, "
        "COUNT(*) FILTER (WHERE NOT houve_alerta) AS sem_alerta, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY duracao_segundos) AS perm_med, "
        "percentile_cont(0.9) WITHIN GROUP (ORDER BY duracao_segundos) AS perm_p90, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY espera_garcom_segundos) AS espera_med "
        "FROM visitas_concluidas" + where_v + " GROUP BY camera_id, mesa_id",
        p_v,
    ).fetchall()
    eventos = conn.execute(
        "SELECT camera_id, MAX(camera_nome) AS camera_nome, mesa_id, "
        "COUNT(*) FILTER (WHERE tipo_evento = 'ALERTA_DISPARADO') AS alertas, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY CASE WHEN tipo_evento = 'MESA_HIGIENIZADA' "
        "THEN " + _TEMPO_LIMPEZA + " END) AS limpeza_med "
        "FROM eventos_mesas" + where_e + " AND tipo_evento IN ('ALERTA_DISPARADO', 'MESA_HIGIENIZADA') "
        "GROUP BY camera_id, mesa_id",
        p_e,
    ).fetchall()

    grupos: Dict[Tuple[str, str], dict] = {}

    def _obter(r) -> dict:
        return grupos.setdefault(
            (r["camera_id"], r["mesa_id"]),
            {
                "camera_id": r["camera_id"], "camera_nome": r["camera_nome"], "mesa_id": r["mesa_id"],
                "visitas": 0, "permanencia_mediana_min": None, "permanencia_p90_min": None,
                "espera_mediana_min": None, "limpeza_mediana_min": None, "alertas": 0, "sla_ok_pct": None,
            },
        )

    for r in visitas:
        g = _obter(r)
        n = int(r["visitas"])
        g.update({
            "visitas": n,
            "permanencia_mediana_min": calculos.seg_para_min(r["perm_med"]),
            "permanencia_p90_min": calculos.seg_para_min(r["perm_p90"]),
            "espera_mediana_min": calculos.seg_para_min(r["espera_med"]),
            "sla_ok_pct": round(int(r["sem_alerta"]) / n * 100.0, 1) if n else None,
        })
    for r in eventos:
        g = _obter(r)
        g["alertas"] = int(r["alertas"] or 0)
        g["limpeza_mediana_min"] = calculos.seg_para_min(r["limpeza_med"])

    return sorted(grupos.values(), key=lambda g: (-g["visitas"], str(g["camera_nome"]), str(g["mesa_id"])))


# ---------------------------------------------------------------------------
# Extratos (tela e exportação)
# ---------------------------------------------------------------------------
def visitas_detalhadas(
    conn, est_id: int, tz: str, desde: Optional[float], camera_id: Optional[str] = None, limite: int = LIMITE_EXPORTACAO
) -> Tuple[List[dict], bool]:
    """Visitas mais recentes primeiro. Retorna (linhas, truncado)."""
    where_v, params = _filtro("fim_epoch", est_id, desde, camera_id)
    params.update({"tz": tz, "lim": int(limite) + 1})
    rows = conn.execute(
        "SELECT camera_nome, mesa_id, "
        "to_char(to_timestamp(inicio_epoch) AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI:SS') AS entrada, "
        "to_char(to_timestamp(fim_epoch) AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI:SS') AS saida, "
        "qtd_pessoas, duracao_segundos, espera_garcom_segundos, tempo_preparo_segundos, houve_alerta "
        "FROM visitas_concluidas" + where_v + " ORDER BY fim_epoch DESC LIMIT %(lim)s",
        params,
    ).fetchall()
    truncado = len(rows) > int(limite)
    saida = [
        {
            "entrada": r["entrada"],
            "saida": r["saida"],
            "camera_nome": r["camera_nome"],
            "mesa_id": r["mesa_id"],
            "pessoas": int(r["qtd_pessoas"]),
            "permanencia_min": calculos.seg_para_min(r["duracao_segundos"]),
            "espera_garcom_min": calculos.seg_para_min(r["espera_garcom_segundos"]),
            "preparo_cozinha_min": calculos.seg_para_min(r["tempo_preparo_segundos"]),
            "sla": "⚠️ Com alerta" if r["houve_alerta"] else "✅ Dentro da meta",
        }
        for r in rows[: int(limite)]
    ]
    return saida, truncado


def alertas_por_tipo(conn, est_id: int, desde: Optional[float], camera_id: Optional[str] = None) -> Dict[str, int]:
    """Quantidade de alertas por tipo no período (consulta leve, agregada no banco)."""
    where_e, params = _filtro("epoch", est_id, desde, camera_id)
    rows = conn.execute(
        "SELECT " + _TIPO_ALERTA + " AS tipo, COUNT(*) AS qtd FROM eventos_mesas" + where_e
        + " AND tipo_evento = 'ALERTA_DISPARADO' GROUP BY 1",
        params,
    ).fetchall()
    return {r["tipo"]: int(r["qtd"]) for r in rows}


def relatorio_alertas(
    conn, est_id: int, tz: str, desde: Optional[float], camera_id: Optional[str] = None, limite: int = LIMITE_EXPORTACAO
) -> dict:
    """Contagem por tipo (período inteiro) + ocorrências mais recentes."""
    where_e, params = _filtro("epoch", est_id, desde, camera_id)
    filtro = where_e + " AND tipo_evento = 'ALERTA_DISPARADO'"
    params_lista = dict(params, tz=tz, lim=int(limite) + 1)
    rows = conn.execute(
        "SELECT to_char(to_timestamp(epoch) AT TIME ZONE %(tz)s, 'YYYY-MM-DD HH24:MI:SS') AS data_hora, "
        "camera_nome, mesa_id, detalhes FROM eventos_mesas" + filtro + " ORDER BY epoch DESC LIMIT %(lim)s",
        params_lista,
    ).fetchall()
    return {
        "por_tipo": alertas_por_tipo(conn, est_id, desde, camera_id),
        "alertas": [
            {
                "data_hora": r["data_hora"],
                "camera_nome": r["camera_nome"],
                "mesa_id": r["mesa_id"],
                "tipo_alerta": calculos.tipo_alerta(r["detalhes"]),
                "tempo_decorrido": calculos.tempo_decorrido_alerta(r["detalhes"]),
            }
            for r in rows[: int(limite)]
        ],
        "truncado": len(rows) > int(limite),
    }


# ---------------------------------------------------------------------------
# Pico por dia/hora (agregados por célula; montagem em calculos.montar_matriz)
# ---------------------------------------------------------------------------
def agregados_pico(conn, est_id: int, tz: str, desde: Optional[float], camera_id: Optional[str] = None):
    """Retorna (amostras, visitas, alertas) por célula (dia 0=Seg..6, hora 0..23) no fuso da loja."""
    where_a, p_a = _filtro("epoch", est_id, desde, camera_id)
    p_a["tz"] = tz
    # Instantes de 1 minuto somam as câmeras (ocupação do salão inteiro naquele minuto)
    amostras = conn.execute(
        "WITH base AS ("
        "  SELECT to_timestamp(epoch) AT TIME ZONE %(tz)s AS lt, floor(epoch / 60) AS minuto, "
        "         mesas_ocupadas, total_mesas, pessoas_sentadas FROM amostras_ocupacao" + where_a +
        "), instantes AS ("
        "  SELECT (EXTRACT(ISODOW FROM MIN(lt))::int - 1) AS dia, EXTRACT(HOUR FROM MIN(lt))::int AS hora, "
        "         SUM(mesas_ocupadas) AS oc, SUM(total_mesas) AS tot, SUM(pessoas_sentadas) AS pes "
        "    FROM base GROUP BY minuto"
        ") "
        "SELECT dia, hora, COUNT(*) AS amostras, AVG(oc) AS oc_media, MAX(oc) AS oc_pico, "
        "       AVG(tot) AS tot_media, AVG(pes) AS pes_media FROM instantes GROUP BY dia, hora",
        p_a,
    ).fetchall()

    where_v, p_v = _filtro("fim_epoch", est_id, desde, camera_id)
    p_v["tz"] = tz
    visitas = conn.execute(
        "SELECT (EXTRACT(ISODOW FROM lt)::int - 1) AS dia, EXTRACT(HOUR FROM lt)::int AS hora, "
        "COUNT(*) AS giros, percentile_cont(0.5) WITHIN GROUP (ORDER BY duracao_segundos) AS perm_med "
        "FROM (SELECT to_timestamp(inicio_epoch) AT TIME ZONE %(tz)s AS lt, duracao_segundos "
        "FROM visitas_concluidas" + where_v + ") v GROUP BY 1, 2",
        p_v,
    ).fetchall()

    where_e, p_e = _filtro("epoch", est_id, desde, camera_id)
    p_e["tz"] = tz
    alertas = conn.execute(
        "SELECT (EXTRACT(ISODOW FROM lt)::int - 1) AS dia, EXTRACT(HOUR FROM lt)::int AS hora, COUNT(*) AS qtd "
        "FROM (SELECT to_timestamp(epoch) AT TIME ZONE %(tz)s AS lt FROM eventos_mesas"
        + where_e + " AND tipo_evento = 'ALERTA_DISPARADO') a GROUP BY 1, 2",
        p_e,
    ).fetchall()

    cel_am = {
        (int(r["dia"]), int(r["hora"])): {
            "amostras": int(r["amostras"]),
            "oc_media": float(r["oc_media"]),
            "oc_pico": int(r["oc_pico"]),
            "tot_media": float(r["tot_media"]),
            "pes_media": float(r["pes_media"]),
        }
        for r in amostras
    }
    cel_vi = {
        (int(r["dia"]), int(r["hora"])): {"giros": int(r["giros"]), "perm_mediana_seg": _f(r["perm_med"])}
        for r in visitas
    }
    cel_al = {(int(r["dia"]), int(r["hora"])): int(r["qtd"]) for r in alertas}
    return cel_am, cel_vi, cel_al


# ---------------------------------------------------------------------------
# Saúde das lojas (somente admin)
# ---------------------------------------------------------------------------
def saude_lojas(conn) -> List[dict]:
    """Último sync e último dado de ocupação por loja (o motor parado deixa de gerar amostras)."""
    rows = conn.execute(
        """
        SELECT e.id, e.nome, e.ativo, e.timezone,
               EXTRACT(EPOCH FROM now() - e.ultimo_sync_em) AS seg_desde_sync,
               to_char(e.ultimo_sync_em AT TIME ZONE e.timezone, 'YYYY-MM-DD HH24:MI') AS ultimo_sync_local,
               ult.ultima_amostra
          FROM estabelecimentos_portal e
          LEFT JOIN LATERAL (
               SELECT MAX(a.epoch) AS ultima_amostra FROM amostras_ocupacao a
                WHERE a.estabelecimento_id = e.id
          ) ult ON TRUE
         ORDER BY e.nome
        """
    ).fetchall()
    agora = time.time()
    return [
        {
            "id": int(r["id"]),
            "nome": r["nome"],
            "status": calculos.status_sync(_f(r["seg_desde_sync"]), bool(r["ativo"])),
            "ultimo_sync": r["ultimo_sync_local"] or "-",
            "min_desde_ultima_amostra": round((agora - float(r["ultima_amostra"])) / 60.0)
            if r["ultima_amostra"] is not None
            else None,
            "fuso": r["timezone"],
        }
        for r in rows
    ]
