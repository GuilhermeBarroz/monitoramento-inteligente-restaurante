"""
Persistência analítica em SQLite (modo WAL) e motor estatístico da Etapa 2:
- Previsão de giro e liberação de mesas via mediana histórica por mesa / câmera / salão.
- Agregações de pico por hora do dia e dia da semana para dimensionamento de equipe (garçons).
- 100% das consultas SQL utilizam queries parametrizadas com placeholders `?`.
"""

from contextlib import contextmanager
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
import zoneinfo

DB_PATH_PADRAO = Path("data/historico_salao.db")
TIMEZONE_PADRAO = "America/Sao_Paulo"

DIAS_SEMANA_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
ESTADOS_OCUPADOS = {
    "AGUARDANDO_ATENDIMENTO",
    "ANOTANDO_PEDIDO",
    "AGUARDANDO_ENTREGA",
    "REFEICAO_ANDAMENTO",
}
TURNOS_PADRAO = [
    ("Madrugada (00h–05h)", 0, 5),
    ("Manhã (06h–10h)", 6, 10),
    ("Almoço (11h–14h)", 11, 14),
    ("Tarde (15h–17h)", 15, 17),
    ("Jantar (18h–23h)", 18, 23),
]


_TZ_FALLBACK_AVISADO = set()


def _resolver_tz(timezone_nome: str = TIMEZONE_PADRAO):
    try:
        return zoneinfo.ZoneInfo(timezone_nome)
    except Exception as e:
        # Sem a base de fusos (pacote tzdata no Windows/imagens slim), dia/hora dos
        # relatórios sairiam no fuso do sistema. Avisa uma vez em vez de falhar calado.
        if timezone_nome not in _TZ_FALLBACK_AVISADO:
            _TZ_FALLBACK_AVISADO.add(timezone_nome)
            print(f"⚠️ Fuso '{timezone_nome}' indisponível ({e}); usando o fuso do sistema. Instale o pacote 'tzdata'.")
        return datetime.now().astimezone().tzinfo


def extrair_tempo_local(epoch: float, timezone_nome: str = TIMEZONE_PADRAO) -> Tuple[str, int, int]:
    """Retorna `(iso_datahora, dia_semana_0_a_6, hora_0_a_23)` no fuso local configurado."""
    tz = _resolver_tz(timezone_nome)
    dt = datetime.fromtimestamp(float(epoch), tz=tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S"), dt.weekday(), dt.hour


def formatar_duracao_curta(segundos: Optional[float]) -> str:
    """Formata segundos em string amigável (`MM:SS` ou `Xh YYm`)."""
    if segundos is None:
        return "-"
    s = max(0, int(round(float(segundos))))
    if s >= 3600:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m:02d}m"
    m = s // 60
    r = s % 60
    return f"{m:02d}:{r:02d}"


def _percentil(valores_ordenados: List[float], p: float) -> float:
    if not valores_ordenados:
        return 0.0
    if len(valores_ordenados) == 1:
        return float(valores_ordenados[0])
    pos = max(0.0, min(1.0, p)) * (len(valores_ordenados) - 1)
    idx_inf = int(math.floor(pos))
    idx_sup = int(math.ceil(pos))
    if idx_inf == idx_sup:
        return float(valores_ordenados[idx_inf])
    frac = pos - idx_inf
    return float(valores_ordenados[idx_inf] * (1.0 - frac) + valores_ordenados[idx_sup] * frac)


def _tipo_alerta(detalhes_json: Optional[str]) -> str:
    """Tipo do alerta a partir do JSON do evento; 'ALERTA' se ausente, nulo ou inválido."""
    try:
        det = json.loads(detalhes_json or "{}")
    except Exception:
        return "ALERTA"
    valor = det.get("alerta") if isinstance(det, dict) else None
    return str(valor) if valor else "ALERTA"


def _media_ponderada_por_amostras(itens: List[dict]) -> float:
    """Média de ocupação ponderada pelo nº de amostras; células sem amostra não entram."""
    peso = sum(int(i.get("amostras", 0)) for i in itens)
    if peso <= 0:
        return 0.0
    return sum(i["ocupacao_media_mesas"] * int(i.get("amostras", 0)) for i in itens) / peso


def _sugerir_garcons(mesas_ocupadas: float, mesas_por_garcom: int, minimo_se_ativo: bool = True) -> int:
    divisor = max(1, int(mesas_por_garcom))
    if mesas_ocupadas <= 0:
        return 0
    estimativa = int(math.ceil(float(mesas_ocupadas) / divisor))
    return max(1, estimativa) if minimo_se_ativo else estimativa


def calcular_previsao_liberacao(
    camera_id: str,
    mesa_id: str,
    estado_valor: str,
    tempo_permanencia_seg: float,
    stats: dict,
    padrao_segundos: float = 2700.0,
    min_amostras: int = 3,
) -> Optional[dict]:
    """
    Estima o tempo restante até a mesa liberar usando mediana histórica hierárquica:
    1) Mesma mesa >= min_amostras -> 2) Mesma câmera >= min_amostras -> 3) Global >= min_amostras -> 4) Baseline padrão.
    """
    if estado_valor not in ESTADOS_OCUPADOS:
        return None

    por_mesa = stats.get("por_mesa", {})
    por_camera = stats.get("por_camera", {})
    global_info = stats.get("global", {})

    chave_mesa = f"{camera_id}::{mesa_id}"
    escolhido = None
    fonte = "estimativa_padrao"

    if chave_mesa in por_mesa and por_mesa[chave_mesa]["n"] >= min_amostras:
        escolhido = por_mesa[chave_mesa]
        fonte = "mesa"
    elif camera_id in por_camera and por_camera[camera_id]["n"] >= min_amostras:
        escolhido = por_camera[camera_id]
        fonte = "camera"
    elif global_info.get("n", 0) >= min_amostras:
        escolhido = global_info
        fonte = "global"

    if escolhido is not None:
        mediana_seg = float(escolhido["mediana_seg"])
        amostras_base = int(escolhido["n"])
    else:
        mediana_seg = max(60.0, float(padrao_segundos))
        amostras_base = 0

    atual = max(0.0, float(tempo_permanencia_seg))
    restante_seg = max(0.0, mediana_seg - atual)
    excedente_seg = max(0.0, atual - mediana_seg)
    excedeu = atual > mediana_seg
    progresso_pct = min(100, int(round((atual / mediana_seg) * 100))) if mediana_seg > 0 else 0

    return {
        "mediana_prevista_seg": round(mediana_seg, 1),
        "mediana_prevista_formatada": formatar_duracao_curta(mediana_seg),
        "tempo_restante_seg": round(restante_seg, 1),
        "tempo_restante_formatado": formatar_duracao_curta(restante_seg),
        "excedente_seg": round(excedente_seg, 1),
        "excedeu_mediana": excedeu,
        "progresso_pct": progresso_pct,
        "amostras_base": amostras_base,
        "fonte_previsao": fonte,
    }


class RepositorioAnalytics:
    """Repositório SQLite thread/process-safe com queries estritamente parametrizadas."""

    def __init__(self, db_path: Path = DB_PATH_PADRAO, timezone_nome: str = TIMEZONE_PADRAO):
        self.db_path = Path(db_path)
        self.timezone_nome = timezone_nome
        # Memoização da matriz de pico dentro da vida desta instância. O painel cria
        # um repositório por renderização e pede a matriz várias vezes (hora, dia,
        # turno, matriz): sem isso, as mesmas consultas pesadas rodavam ~5 vezes.
        self._cache_matriz: Dict[Tuple[Optional[int], Optional[str], int], List[dict]] = {}
        self.inicializar_banco()

    @contextmanager
    def _conectar(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def inicializar_banco(self):
        with self._conectar() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eventos_mesas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch REAL NOT NULL,
                    iso_datahora TEXT NOT NULL,
                    dia_semana INTEGER NOT NULL,
                    hora INTEGER NOT NULL,
                    camera_id TEXT NOT NULL,
                    camera_nome TEXT NOT NULL,
                    mesa_id TEXT NOT NULL,
                    tipo_evento TEXT NOT NULL,
                    detalhes_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eventos_epoch ON eventos_mesas (epoch)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eventos_cam_tipo ON eventos_mesas (camera_id, tipo_evento)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS visitas_concluidas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL,
                    camera_nome TEXT NOT NULL,
                    mesa_id TEXT NOT NULL,
                    inicio_epoch REAL NOT NULL,
                    fim_epoch REAL NOT NULL,
                    dia_semana INTEGER NOT NULL,
                    hora_inicio INTEGER NOT NULL,
                    duracao_segundos REAL NOT NULL,
                    espera_garcom_segundos REAL,
                    tempo_preparo_segundos REAL,
                    qtd_pessoas INTEGER NOT NULL DEFAULT 1,
                    houve_alerta INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_visitas_fim ON visitas_concluidas (fim_epoch)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_visitas_cam_mesa ON visitas_concluidas (camera_id, mesa_id)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS amostras_ocupacao (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch REAL NOT NULL,
                    dia_semana INTEGER NOT NULL,
                    hora INTEGER NOT NULL,
                    camera_id TEXT NOT NULL,
                    camera_nome TEXT NOT NULL,
                    total_mesas INTEGER NOT NULL,
                    mesas_ocupadas INTEGER NOT NULL,
                    pessoas_sentadas INTEGER NOT NULL,
                    alertas_ativos INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_amostras_epoch ON amostras_ocupacao (epoch)")
            # Identidade desta instalação do banco. Se o arquivo for apagado e recriado,
            # nasce um id novo: o edge-sync detecta e a nuvem não confunde os ids
            # reiniciados (1, 2, 3...) com registros antigos.
            conn.execute("CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
            conn.execute(
                "INSERT OR IGNORE INTO meta (chave, valor) VALUES (?, ?)",
                ("instalacao_id", str(uuid.uuid4())),
            )

    def obter_instalacao_id(self) -> str:
        with self._conectar() as conn:
            linha = conn.execute("SELECT valor FROM meta WHERE chave = ?", ("instalacao_id",)).fetchone()
        return str(linha["valor"])

    @staticmethod
    def _clausula_filtro(
        coluna_tempo: str,
        dias: Optional[int],
        camera_id: Optional[str],
        agora_epoch: Optional[float] = None,
    ) -> Tuple[str, List[Any]]:
        if coluna_tempo not in {"epoch", "fim_epoch"}:
            raise ValueError(f"Coluna de tempo inválida: {coluna_tempo}")

        filtros: List[str] = []
        params: List[Any] = []
        agora = agora_epoch if agora_epoch is not None else time.time()

        if dias is not None and int(dias) > 0:
            cutoff = agora - (int(dias) * 86400.0)
            filtros.append(f"{coluna_tempo} >= ?")
            params.append(float(cutoff))

        if camera_id and str(camera_id).strip() and camera_id != "__todas__":
            filtros.append("camera_id = ?")
            params.append(str(camera_id).strip())

        if not filtros:
            return "", params
        return " WHERE " + " AND ".join(filtros), params

    def registrar_evento(
        self,
        epoch: float,
        camera_id: str,
        camera_nome: str,
        mesa_id: str,
        tipo_evento: str,
        detalhes: Optional[dict] = None,
    ):
        iso_dt, dia_sem, hora = extrair_tempo_local(epoch, self.timezone_nome)
        payload_json = json.dumps(detalhes or {}, ensure_ascii=False)
        with self._conectar() as conn:
            conn.execute(
                """
                INSERT INTO eventos_mesas
                (epoch, iso_datahora, dia_semana, hora, camera_id, camera_nome, mesa_id, tipo_evento, detalhes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(epoch),
                    iso_dt,
                    int(dia_sem),
                    int(hora),
                    str(camera_id),
                    str(camera_nome),
                    str(mesa_id),
                    str(tipo_evento),
                    payload_json,
                ),
            )

    def registrar_visita(
        self,
        camera_id: str,
        camera_nome: str,
        mesa_id: str,
        fim_epoch: float,
        duracao_segundos: float,
        espera_garcom_segundos: Optional[float] = None,
        tempo_preparo_segundos: Optional[float] = None,
        qtd_pessoas: int = 1,
        houve_alerta: bool = False,
    ) -> bool:
        dur = float(duracao_segundos)
        if dur < 30.0:
            return False

        fim = float(fim_epoch)
        inicio = max(0.0, fim - dur)
        _, dia_sem, hora_ini = extrair_tempo_local(inicio, self.timezone_nome)

        with self._conectar() as conn:
            conn.execute(
                """
                INSERT INTO visitas_concluidas
                (camera_id, camera_nome, mesa_id, inicio_epoch, fim_epoch, dia_semana, hora_inicio,
                 duracao_segundos, espera_garcom_segundos, tempo_preparo_segundos, qtd_pessoas, houve_alerta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(camera_id),
                    str(camera_nome),
                    str(mesa_id),
                    inicio,
                    fim,
                    int(dia_sem),
                    int(hora_ini),
                    dur,
                    float(espera_garcom_segundos) if espera_garcom_segundos is not None else None,
                    float(tempo_preparo_segundos) if tempo_preparo_segundos is not None else None,
                    max(1, int(qtd_pessoas)),
                    1 if houve_alerta else 0,
                ),
            )
        return True

    def registrar_amostra_ocupacao(
        self,
        epoch: float,
        camera_id: str,
        camera_nome: str,
        total_mesas: int,
        mesas_ocupadas: int,
        pessoas_sentadas: int,
        alertas_ativos: int,
    ):
        _, dia_sem, hora = extrair_tempo_local(epoch, self.timezone_nome)
        with self._conectar() as conn:
            conn.execute(
                """
                INSERT INTO amostras_ocupacao
                (epoch, dia_semana, hora, camera_id, camera_nome, total_mesas, mesas_ocupadas, pessoas_sentadas, alertas_ativos)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(epoch),
                    int(dia_sem),
                    int(hora),
                    str(camera_id),
                    str(camera_nome),
                    max(0, int(total_mesas)),
                    max(0, int(mesas_ocupadas)),
                    max(0, int(pessoas_sentadas)),
                    max(0, int(alertas_ativos)),
                ),
            )

    def obter_estatisticas_permanencia(self, dias: int = 90, limite_registros: int = 5000) -> dict:
        """Calcula medianas históricas recentes agrupadas por mesa, câmera e global."""
        where_sql, params = self._clausula_filtro("fim_epoch", dias, None)
        query = (
            "SELECT camera_id, mesa_id, duracao_segundos FROM visitas_concluidas"
            + where_sql
            + " ORDER BY fim_epoch DESC LIMIT ?"
        )
        params.append(int(limite_registros))

        por_mesa_vals: Dict[str, List[float]] = {}
        por_cam_vals: Dict[str, List[float]] = {}
        globais: List[float] = []

        with self._conectar() as conn:
            rows = conn.execute(query, params).fetchall()

        for row in rows:
            dur = float(row["duracao_segundos"])
            cid = str(row["camera_id"])
            mid = str(row["mesa_id"])
            chave = f"{cid}::{mid}"
            por_mesa_vals.setdefault(chave, []).append(dur)
            por_cam_vals.setdefault(cid, []).append(dur)
            globais.append(dur)

        return {
            "por_mesa": {
                k: {"mediana_seg": float(statistics.median(v)), "n": len(v)}
                for k, v in por_mesa_vals.items()
            },
            "por_camera": {
                k: {"mediana_seg": float(statistics.median(v)), "n": len(v)}
                for k, v in por_cam_vals.items()
            },
            "global": {
                "mediana_seg": float(statistics.median(globais)) if globais else 0.0,
                "n": len(globais),
            },
        }

    def consultar_eventos_recentes(self, limite: int = 200, camera_id: Optional[str] = None) -> List[dict]:
        where_sql, params = self._clausula_filtro("epoch", None, camera_id)
        query = (
            "SELECT iso_datahora, camera_id, camera_nome, mesa_id, tipo_evento, detalhes_json "
            "FROM eventos_mesas" + where_sql + " ORDER BY epoch DESC, id DESC LIMIT ?"
        )
        params.append(int(limite))
        with self._conectar() as conn:
            rows = conn.execute(query, params).fetchall()

        saida: List[dict] = []
        for r in rows:
            try:
                det = json.loads(r["detalhes_json"] or "{}")
            except Exception:
                det = {}
            det_limpo = {k: v for k, v in det.items() if k not in {"camera_id", "camera_nome", "mesa_id", "tipo", "timestamp"}}
            saida.append({
                "data_hora": r["iso_datahora"],
                "camera_id": r["camera_id"],
                "camera_nome": r["camera_nome"],
                "mesa_id": r["mesa_id"],
                "evento": r["tipo_evento"],
                "detalhes": json.dumps(det_limpo, ensure_ascii=False) if det_limpo else "-",
            })
        return saida

    def consultar_resumo_geral(self, dias: Optional[int] = 30, camera_id: Optional[str] = None) -> dict:
        where_v, params_v = self._clausula_filtro("fim_epoch", dias, camera_id)
        where_e, params_e = self._clausula_filtro("epoch", dias, camera_id)

        with self._conectar() as conn:
            visitas = conn.execute(
                "SELECT duracao_segundos, espera_garcom_segundos, tempo_preparo_segundos, qtd_pessoas, houve_alerta "
                "FROM visitas_concluidas" + where_v,
                params_v,
            ).fetchall()

            sql_alertas = "SELECT COUNT(*) AS qtd FROM eventos_mesas" + (
                where_e + " AND tipo_evento = ?" if where_e else " WHERE tipo_evento = ?"
            )
            qtd_alertas = conn.execute(sql_alertas, params_e + ["ALERTA_DISPARADO"]).fetchone()["qtd"]

            sql_limpezas = "SELECT detalhes_json FROM eventos_mesas" + (
                where_e + " AND tipo_evento = ?" if where_e else " WHERE tipo_evento = ?"
            )
            limpezas_rows = conn.execute(sql_limpezas, params_e + ["MESA_HIGIENIZADA"]).fetchall()

        durs = sorted(float(v["duracao_segundos"]) for v in visitas)
        esperas = [float(v["espera_garcom_segundos"]) for v in visitas if v["espera_garcom_segundos"] is not None]
        preparos = [float(v["tempo_preparo_segundos"]) for v in visitas if v["tempo_preparo_segundos"] is not None]
        pessoas = [int(v["qtd_pessoas"]) for v in visitas]
        sem_atraso = sum(1 for v in visitas if int(v["houve_alerta"]) == 0)

        tempos_limpeza: List[float] = []
        # SLA de limpeza (Métrica 3) por EPISÓDIO: cada MESA_HIGIENIZADA traz `com_alerta`
        # (o alerta de mesa suja já tinha disparado naquela limpeza). O alerta dispara
        # depois que a visita foi fechada, por isso não entra no `houve_alerta` da visita.
        # Eventos antigos, sem o campo, ficam fora do SLA (não há como saber).
        limpezas_avaliaveis = 0
        limpezas_no_prazo = 0
        for lr in limpezas_rows:
            try:
                det = json.loads(lr["detalhes_json"] or "{}")
            except Exception:
                continue
            if det.get("tempo_limpeza") is not None:
                try:
                    tempos_limpeza.append(float(det["tempo_limpeza"]))
                except (TypeError, ValueError):
                    pass
            if isinstance(det.get("com_alerta"), bool):
                limpezas_avaliaveis += 1
                if not det["com_alerta"]:
                    limpezas_no_prazo += 1

        total_limpezas = len(limpezas_rows)
        return {
            "total_visitas": len(visitas),
            # SLA de atendimento (espera do garçom + cozinha). None sem visitas: não exibir "100%" sem dado.
            "sla_sem_alerta_pct": round((sem_atraso / len(visitas)) * 100.0, 1) if visitas else None,
            # SLA de limpeza: higienizações concluídas sem alerta de "mesa suja" naquele episódio
            "total_limpezas": total_limpezas,
            "sla_limpeza_pct": round((limpezas_no_prazo / limpezas_avaliaveis) * 100.0, 1)
            if limpezas_avaliaveis
            else None,
            "permanencia_mediana_min": round(statistics.median(durs) / 60.0, 1) if durs else 0.0,
            "permanencia_p90_min": round(_percentil(durs, 0.90) / 60.0, 1) if durs else 0.0,
            "espera_mediana_min": round(statistics.median(esperas) / 60.0, 1) if esperas else None,
            "preparo_mediano_min": round(statistics.median(preparos) / 60.0, 1) if preparos else None,
            "limpeza_mediana_min": round(statistics.median(tempos_limpeza) / 60.0, 1) if tempos_limpeza else None,
            "media_pessoas_por_mesa": round(sum(pessoas) / len(pessoas), 1) if pessoas else 0.0,
            "total_alertas": int(qtd_alertas),
        }

    def consultar_desempenho_por_mesa(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
    ) -> List[dict]:
        """Consolida visitas, tempos medianos, limpeza e alertas por mesa para relatório gerencial."""
        where_v, params_v = self._clausula_filtro("fim_epoch", dias, camera_id)
        where_e, params_e = self._clausula_filtro("epoch", dias, camera_id)

        with self._conectar() as conn:
            visitas = conn.execute(
                "SELECT camera_id, camera_nome, mesa_id, duracao_segundos, espera_garcom_segundos, houve_alerta "
                "FROM visitas_concluidas" + where_v,
                params_v,
            ).fetchall()

            sql_ev = "SELECT camera_id, camera_nome, mesa_id, tipo_evento, detalhes_json FROM eventos_mesas" + (
                where_e + " AND tipo_evento IN (?, ?)" if where_e else " WHERE tipo_evento IN (?, ?)"
            )
            eventos_rows = conn.execute(sql_ev, params_e + ["ALERTA_DISPARADO", "MESA_HIGIENIZADA"]).fetchall()

        grupos: Dict[Tuple[str, str], dict] = {}

        def _obter(cid: str, cnome: str, mid: str) -> dict:
            chave = (cid, mid)
            return grupos.setdefault(
                chave,
                {"camera_id": cid, "camera_nome": cnome, "mesa_id": mid, "durs": [], "esperas": [], "limpezas": [], "sem_alerta": 0, "alertas": 0},
            )

        for r in visitas:
            g = _obter(str(r["camera_id"]), str(r["camera_nome"]), str(r["mesa_id"]))
            g["durs"].append(float(r["duracao_segundos"]))
            if r["espera_garcom_segundos"] is not None:
                g["esperas"].append(float(r["espera_garcom_segundos"]))
            if int(r["houve_alerta"]) == 0:
                g["sem_alerta"] += 1

        for er in eventos_rows:
            g = _obter(str(er["camera_id"]), str(er["camera_nome"]), str(er["mesa_id"]))
            if er["tipo_evento"] == "ALERTA_DISPARADO":
                g["alertas"] += 1
            elif er["tipo_evento"] == "MESA_HIGIENIZADA":
                try:
                    det = json.loads(er["detalhes_json"] or "{}")
                    if det.get("tempo_limpeza") is not None:
                        g["limpezas"].append(float(det["tempo_limpeza"]))
                except Exception:
                    pass

        linhas: List[dict] = []
        for g in sorted(grupos.values(), key=lambda x: (-len(x["durs"]), x["camera_nome"], x["mesa_id"])):
            durs = sorted(g["durs"])
            n_vis = len(durs)
            linhas.append({
                "camera_id": g["camera_id"],
                "camera_nome": g["camera_nome"],
                "mesa_id": g["mesa_id"],
                "visitas": n_vis,
                "permanencia_mediana_min": round(statistics.median(durs) / 60.0, 1) if durs else None,
                "permanencia_p90_min": round(_percentil(durs, 0.90) / 60.0, 1) if durs else None,
                "espera_mediana_min": round(statistics.median(g["esperas"]) / 60.0, 1) if g["esperas"] else None,
                "limpeza_mediana_min": round(statistics.median(g["limpezas"]) / 60.0, 1) if g["limpezas"] else None,
                "alertas": g["alertas"],
                "sla_ok_pct": round((g["sem_alerta"] / n_vis) * 100.0, 1) if n_vis > 0 else None,
            })
        return linhas

    def consultar_visitas_detalhadas(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
        limite: Optional[int] = 300,
    ) -> List[dict]:
        """
        Lista extrato detalhado das visitas concluídas. `limite=None` traz o período
        inteiro (uso: exportação CSV, que não pode sair truncada).
        """
        where_v, params_v = self._clausula_filtro("fim_epoch", dias, camera_id)
        query = (
            "SELECT camera_nome, mesa_id, inicio_epoch, fim_epoch, duracao_segundos, "
            "espera_garcom_segundos, tempo_preparo_segundos, qtd_pessoas, houve_alerta "
            "FROM visitas_concluidas" + where_v + " ORDER BY fim_epoch DESC"
        )
        if limite is not None:
            query += " LIMIT ?"
            params_v.append(int(limite))
        with self._conectar() as conn:
            rows = conn.execute(query, params_v).fetchall()

        saida: List[dict] = []
        for r in rows:
            ent_iso, _, _ = extrair_tempo_local(float(r["inicio_epoch"]), self.timezone_nome)
            sai_iso, _, _ = extrair_tempo_local(float(r["fim_epoch"]), self.timezone_nome)
            saida.append({
                "entrada": ent_iso,
                "saida": sai_iso,
                "camera_nome": r["camera_nome"],
                "mesa_id": r["mesa_id"],
                "pessoas": int(r["qtd_pessoas"]),
                "permanencia_min": round(float(r["duracao_segundos"]) / 60.0, 1),
                "espera_garcom_min": round(float(r["espera_garcom_segundos"]) / 60.0, 1)
                if r["espera_garcom_segundos"] is not None
                else None,
                "preparo_cozinha_min": round(float(r["tempo_preparo_segundos"]) / 60.0, 1)
                if r["tempo_preparo_segundos"] is not None
                else None,
                "sla": "⚠️ Com alerta" if int(r["houve_alerta"]) else "✅ Dentro da meta",
            })
        return saida

    def consultar_relatorio_alertas(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
        limite: Optional[int] = 300,
    ) -> dict:
        """
        Resumo por tipo de alerta (sempre sobre o PERÍODO INTEIRO, coerente com o
        total de alertas) e listagem das ocorrências (`limite=None` = todas).
        """
        where_e, params_e = self._clausula_filtro("epoch", dias, camera_id)
        filtro_tipo = where_e + " AND tipo_evento = ?" if where_e else " WHERE tipo_evento = ?"
        sql = (
            "SELECT iso_datahora, camera_nome, mesa_id, detalhes_json FROM eventos_mesas"
            + filtro_tipo
            + " ORDER BY epoch DESC"
        )
        params_lista = params_e + ["ALERTA_DISPARADO"]
        if limite is not None:
            sql += " LIMIT ?"
            params_lista.append(int(limite))
        # Contagem por tipo sobre o período inteiro, em Python e tolerante a JSON
        # inválido (json_extract no SQL falharia a consulta toda por uma linha ruim
        # e não existe no Postgres do portal).
        sql_todos = "SELECT detalhes_json FROM eventos_mesas" + filtro_tipo
        with self._conectar() as conn:
            rows = conn.execute(sql, params_lista).fetchall()
            todos = conn.execute(sql_todos, params_e + ["ALERTA_DISPARADO"]).fetchall()

        por_tipo: Dict[str, int] = {}
        for r in todos:
            tipo = _tipo_alerta(r["detalhes_json"])
            por_tipo[tipo] = por_tipo.get(tipo, 0) + 1

        alertas: List[dict] = []
        for r in rows:
            try:
                det = json.loads(r["detalhes_json"] or "{}")
            except Exception:
                det = {}
            tipo_alerta = _tipo_alerta(r["detalhes_json"])

            tempo_seg = det.get("tempo_espera") or det.get("tempo_cozinha") or det.get("tempo_espera_limpeza")
            alertas.append({
                "data_hora": r["iso_datahora"],
                "camera_nome": r["camera_nome"],
                "mesa_id": r["mesa_id"],
                "tipo_alerta": tipo_alerta,
                "tempo_decorrido": formatar_duracao_curta(float(tempo_seg)) if tempo_seg is not None else "-",
            })

        return {"por_tipo": por_tipo, "alertas": alertas}

    def consultar_medianas_detalhadas(
        self,
        dias: Optional[int] = 90,
        camera_id: Optional[str] = None,
    ) -> List[dict]:
        where_v, params_v = self._clausula_filtro("fim_epoch", dias, camera_id)
        with self._conectar() as conn:
            rows = conn.execute(
                "SELECT camera_id, camera_nome, mesa_id, duracao_segundos, espera_garcom_segundos "
                "FROM visitas_concluidas" + where_v,
                params_v,
            ).fetchall()

        grupos: Dict[Tuple[str, str, str], dict] = {}
        for r in rows:
            chave = (str(r["camera_id"]), str(r["camera_nome"]), str(r["mesa_id"]))
            item = grupos.setdefault(chave, {"durs": [], "esperas": []})
            item["durs"].append(float(r["duracao_segundos"]))
            if r["espera_garcom_segundos"] is not None:
                item["esperas"].append(float(r["espera_garcom_segundos"]))

        resultado: List[dict] = []
        for (cid, cnome, mid), vals in sorted(grupos.items(), key=lambda x: (x[0][1], x[0][2])):
            durs = sorted(vals["durs"])
            med_s = float(statistics.median(durs))
            p90_s = _percentil(durs, 0.90)
            esp_s = float(statistics.median(vals["esperas"])) if vals["esperas"] else None
            resultado.append({
                "camera_id": cid,
                "camera_nome": cnome,
                "mesa_id": mid,
                "visitas": len(durs),
                "permanencia_mediana_min": round(med_s / 60.0, 1),
                "permanencia_p90_min": round(p90_s / 60.0, 1),
                "espera_mediana_min": round(esp_s / 60.0, 1) if esp_s is not None else None,
                "confiavel": len(durs) >= 3,
            })
        return resultado

    def consultar_matriz_pico_hora_dia(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
        mesas_por_garcom: int = 4,
    ) -> List[dict]:
        """
        Retorna matriz `(dia_semana, hora)` combinando amostras de ocupação, visitas iniciadas e alertas.
        """
        chave_cache = (dias, camera_id, int(mesas_por_garcom))
        if chave_cache in self._cache_matriz:
            return [dict(item) for item in self._cache_matriz[chave_cache]]
        matriz = self._calcular_matriz_pico_hora_dia(dias, camera_id, mesas_por_garcom)
        self._cache_matriz[chave_cache] = matriz
        return [dict(item) for item in matriz]

    def _calcular_matriz_pico_hora_dia(
        self,
        dias: Optional[int],
        camera_id: Optional[str],
        mesas_por_garcom: int,
    ) -> List[dict]:
        where_a, params_a = self._clausula_filtro("epoch", dias, camera_id)
        where_v, params_v = self._clausula_filtro("fim_epoch", dias, camera_id)
        where_e, params_e = self._clausula_filtro("epoch", dias, camera_id)

        with self._conectar() as conn:
            amostras_rows = conn.execute(
                """
                SELECT dia_semana, hora, epoch, camera_id, total_mesas, mesas_ocupadas, pessoas_sentadas
                FROM amostras_ocupacao
                """
                + where_a,
                params_a,
            ).fetchall()

            visitas_rows = conn.execute(
                "SELECT dia_semana, hora_inicio AS hora, duracao_segundos FROM visitas_concluidas" + where_v,
                params_v,
            ).fetchall()

            sql_alertas = (
                "SELECT dia_semana, hora, COUNT(*) AS qtd FROM eventos_mesas"
                + (where_e + " AND tipo_evento = ?" if where_e else " WHERE tipo_evento = ?")
                + " GROUP BY dia_semana, hora"
            )
            alertas_rows = conn.execute(sql_alertas, params_e + ["ALERTA_DISPARADO"]).fetchall()

        # Agrega amostras por timestamp aproximado (minuto) somando múltiplas câmeras se filtro='todas'
        buckets_instantaneos: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        for r in amostras_rows:
            ts_min = int(float(r["epoch"]) // 60)
            chave_b = (int(r["dia_semana"]), int(r["hora"]), ts_min)
            b = buckets_instantaneos.setdefault(chave_b, {"ocupadas": 0, "total": 0, "pessoas": 0})
            b["ocupadas"] += int(r["mesas_ocupadas"])
            b["total"] += int(r["total_mesas"])
            b["pessoas"] += int(r["pessoas_sentadas"])

        amostras_por_celula: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for (dia, hora, _), vals in buckets_instantaneos.items():
            amostras_por_celula.setdefault((dia, hora), []).append(vals)

        visitas_por_celula: Dict[Tuple[int, int], List[float]] = {}
        for r in visitas_rows:
            visitas_por_celula.setdefault((int(r["dia_semana"]), int(r["hora"])), []).append(float(r["duracao_segundos"]))

        alertas_por_celula: Dict[Tuple[int, int], int] = {
            (int(r["dia_semana"]), int(r["hora"])): int(r["qtd"]) for r in alertas_rows
        }

        celulas_ativas = set(amostras_por_celula.keys()) | set(visitas_por_celula.keys()) | set(alertas_por_celula.keys())
        matriz: List[dict] = []

        for dia in range(7):
            for hora in range(24):
                chave = (dia, hora)
                if chave not in celulas_ativas:
                    continue
                lista_am = amostras_por_celula.get(chave, [])
                lista_vis = sorted(visitas_por_celula.get(chave, []))
                qtd_alertas = alertas_por_celula.get(chave, 0)

                if lista_am:
                    oc_media = sum(x["ocupadas"] for x in lista_am) / len(lista_am)
                    oc_pico = max(x["ocupadas"] for x in lista_am)
                    tot_media = sum(x["total"] for x in lista_am) / len(lista_am)
                    pes_media = sum(x["pessoas"] for x in lista_am) / len(lista_am)
                    taxa_pct = (oc_media / tot_media * 100.0) if tot_media > 0 else 0.0
                else:
                    # Sem amostras de ocupação nesta hora: NÃO há como saber quantas mesas
                    # estavam ocupadas ao mesmo tempo. Antes o total de visitas do período
                    # inteiro era usado como "mesas ocupadas", inflando ocupação e escala.
                    oc_media = 0.0
                    oc_pico = 0
                    pes_media = 0.0
                    taxa_pct = 0.0

                matriz.append({
                    "dia_semana": dia,
                    "dia_nome": DIAS_SEMANA_PT[dia],
                    "hora": hora,
                    "hora_label": f"{hora:02d}:00",
                    "amostras": len(lista_am),
                    "ocupacao_media_mesas": round(oc_media, 2),
                    "ocupacao_pico_mesas": int(oc_pico),
                    "taxa_ocupacao_media_pct": round(taxa_pct, 1),
                    "pessoas_media": round(pes_media, 1),
                    "giros_visitas": len(lista_vis),
                    "permanencia_mediana_min": round(statistics.median(lista_vis) / 60.0, 1) if lista_vis else None,
                    "alertas_disparados": qtd_alertas,
                    "garcons_recomendados_medio": _sugerir_garcons(oc_media, mesas_por_garcom, minimo_se_ativo=oc_media > 0),
                    "garcons_recomendados_pico": _sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0),
                })

        return matriz

    def consultar_resumo_por_hora(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
        mesas_por_garcom: int = 4,
    ) -> List[dict]:
        matriz = self.consultar_matriz_pico_hora_dia(dias=dias, camera_id=camera_id, mesas_por_garcom=mesas_por_garcom)
        por_hora: Dict[int, List[dict]] = {h: [] for h in range(24)}
        for item in matriz:
            por_hora[item["hora"]].append(item)

        linhas: List[dict] = []
        for h in range(24):
            itens = por_hora[h]
            if not itens:
                linhas.append({
                    "hora": h,
                    "hora_label": f"{h:02d}:00",
                    "ocupacao_media_mesas": 0.0,
                    "ocupacao_pico_mesas": 0,
                    "giros_visitas": 0,
                    "alertas_disparados": 0,
                    "garcons_recomendados_medio": 0,
                    "garcons_recomendados_pico": 0,
                })
                continue

            oc_media = _media_ponderada_por_amostras(itens)
            oc_pico = max(i["ocupacao_pico_mesas"] for i in itens)
            giros = sum(i["giros_visitas"] for i in itens)
            alertas = sum(i["alertas_disparados"] for i in itens)
            linhas.append({
                "hora": h,
                "hora_label": f"{h:02d}:00",
                "ocupacao_media_mesas": round(oc_media, 2),
                "ocupacao_pico_mesas": int(oc_pico),
                "giros_visitas": int(giros),
                "alertas_disparados": int(alertas),
                "garcons_recomendados_medio": _sugerir_garcons(oc_media, mesas_por_garcom, minimo_se_ativo=oc_media > 0),
                "garcons_recomendados_pico": _sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0),
            })
        return linhas

    def consultar_resumo_por_dia_semana(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
        mesas_por_garcom: int = 4,
    ) -> List[dict]:
        matriz = self.consultar_matriz_pico_hora_dia(dias=dias, camera_id=camera_id, mesas_por_garcom=mesas_por_garcom)
        por_dia: Dict[int, List[dict]] = {d: [] for d in range(7)}
        for item in matriz:
            por_dia[item["dia_semana"]].append(item)

        linhas: List[dict] = []
        for d in range(7):
            itens = por_dia[d]
            if not itens:
                linhas.append({
                    "dia_semana": d,
                    "dia_nome": DIAS_SEMANA_PT[d],
                    "ocupacao_media_mesas": 0.0,
                    "ocupacao_pico_mesas": 0,
                    "giros_visitas": 0,
                    "alertas_disparados": 0,
                    "garcons_recomendados_pico": 0,
                })
                continue

            oc_media = _media_ponderada_por_amostras(itens)
            oc_pico = max(i["ocupacao_pico_mesas"] for i in itens)
            linhas.append({
                "dia_semana": d,
                "dia_nome": DIAS_SEMANA_PT[d],
                "ocupacao_media_mesas": round(oc_media, 2),
                "ocupacao_pico_mesas": int(oc_pico),
                "giros_visitas": sum(i["giros_visitas"] for i in itens),
                "alertas_disparados": sum(i["alertas_disparados"] for i in itens),
                "garcons_recomendados_pico": _sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0),
            })
        return linhas

    def consultar_ranking_turnos(
        self,
        dias: Optional[int] = 30,
        camera_id: Optional[str] = None,
        mesas_por_garcom: int = 4,
    ) -> List[dict]:
        por_hora = {item["hora"]: item for item in self.consultar_resumo_por_hora(dias, camera_id, mesas_por_garcom)}
        turnos: List[dict] = []
        for nome_turno, h_ini, h_fim in TURNOS_PADRAO:
            fatias = [por_hora[h] for h in range(h_ini, h_fim + 1)]
            ativas = [f for f in fatias if f["ocupacao_media_mesas"] > 0 or f["giros_visitas"] > 0 or f["alertas_disparados"] > 0]
            oc_media = sum(f["ocupacao_media_mesas"] for f in ativas) / len(ativas) if ativas else 0.0
            oc_pico = max((f["ocupacao_pico_mesas"] for f in fatias), default=0)
            garcons_med = _sugerir_garcons(oc_media, mesas_por_garcom, minimo_se_ativo=oc_media > 0)
            garcons_pico = _sugerir_garcons(oc_pico, mesas_por_garcom, minimo_se_ativo=oc_pico > 0)
            turnos.append({
                "turno": nome_turno,
                "ocupacao_media_mesas": round(oc_media, 2),
                "ocupacao_pico_mesas": int(oc_pico),
                "giros_visitas": sum(f["giros_visitas"] for f in fatias),
                "alertas_disparados": sum(f["alertas_disparados"] for f in fatias),
                "garcons_medios": garcons_med,
                "garcons_pico": garcons_pico,
                "escala_sugerida": (
                    f"{garcons_med} garçom(ns) ({garcons_pico} no pico)"
                    if garcons_pico > garcons_med
                    else (f"{garcons_med} garçom(ns)" if garcons_med > 0 else "Sem movimento registrado")
                ),
            })
        return turnos
