"""
API de ingestão multi-tenant do portal em nuvem.

Recebe do Edge Sync de cada restaurante apenas metadados anônimos (eventos, visitas e amostras),
autenticados por token Bearer exclusivo do estabelecimento. O `estabelecimento_id` é SEMPRE
derivado do token no servidor, nunca do payload, isolando os dados entre clientes.
"""

from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .schemas import LoteAmostras, LoteEventos, LoteVisitas

log = logging.getLogger("api_ingestao")
TAMANHO_MAX_CORPO = 5 * 1024 * 1024  # 5 MB


@asynccontextmanager
async def ciclo_vida(_app: FastAPI):
    db.aplicar_schema()
    yield
    db.fechar_pool()


app = FastAPI(
    title="Monitor de Mesas - API de Ingestão",
    version="1.0.0",
    lifespan=ciclo_vida,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def limitar_corpo(request: Request, call_next):
    tamanho = request.headers.get("content-length")
    if tamanho is not None and (not tamanho.isdigit() or int(tamanho) > TAMANHO_MAX_CORPO):
        return JSONResponse({"detail": "Corpo da requisição grande demais."}, status_code=413)
    return await call_next(request)


def estabelecimento_autenticado(cred: HTTPAuthorizationCredentials = Depends(_bearer)) -> int:
    nao_autorizado = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if cred is None or cred.scheme.lower() != "bearer" or not (32 <= len(cred.credentials) <= 128):
        raise nao_autorizado
    with db.obter_pool().connection() as conn:
        linha = conn.execute(
            "SELECT id FROM estabelecimentos WHERE token_hash = %s AND ativo",
            (db.hash_token(cred.credentials),),
        ).fetchone()
    if linha is None:
        raise nao_autorizado
    return int(linha[0])


def _gravar(est_id: int, sql: str, linhas: list) -> None:
    with db.obter_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, linhas)
        conn.execute("UPDATE estabelecimentos SET ultimo_sync_em = now() WHERE id = %s", (est_id,))


SQL_EVENTOS = """
    INSERT INTO eventos_mesas
        (estabelecimento_id, origem_id, epoch, iso_datahora, dia_semana, hora,
         camera_id, camera_nome, mesa_id, tipo_evento, detalhes)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (estabelecimento_id, origem_id) DO NOTHING
"""

SQL_VISITAS = """
    INSERT INTO visitas_concluidas
        (estabelecimento_id, origem_id, camera_id, camera_nome, mesa_id, inicio_epoch, fim_epoch,
         dia_semana, hora_inicio, duracao_segundos, espera_garcom_segundos, tempo_preparo_segundos,
         qtd_pessoas, houve_alerta)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (estabelecimento_id, origem_id) DO NOTHING
"""

SQL_AMOSTRAS = """
    INSERT INTO amostras_ocupacao
        (estabelecimento_id, origem_id, epoch, dia_semana, hora, camera_id, camera_nome,
         total_mesas, mesas_ocupadas, pessoas_sentadas, alertas_ativos)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (estabelecimento_id, origem_id) DO NOTHING
"""


@app.get("/health")
def health():
    with db.obter_pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/v1/ingest/eventos_mesas")
def ingerir_eventos(lote: LoteEventos, est_id: int = Depends(estabelecimento_autenticado)):
    linhas = [
        (est_id, r.id, r.epoch, r.iso_datahora, r.dia_semana, r.hora,
         r.camera_id, r.camera_nome, r.mesa_id, r.tipo_evento, r.detalhes_json)
        for r in lote.registros
    ]
    _gravar(est_id, SQL_EVENTOS, linhas)
    return {"recebidos": len(linhas), "ultimo_id": lote.registros[-1].id}


@app.post("/api/v1/ingest/visitas_concluidas")
def ingerir_visitas(lote: LoteVisitas, est_id: int = Depends(estabelecimento_autenticado)):
    linhas = [
        (est_id, r.id, r.camera_id, r.camera_nome, r.mesa_id, r.inicio_epoch, r.fim_epoch,
         r.dia_semana, r.hora_inicio, r.duracao_segundos, r.espera_garcom_segundos,
         r.tempo_preparo_segundos, r.qtd_pessoas, bool(r.houve_alerta))
        for r in lote.registros
    ]
    _gravar(est_id, SQL_VISITAS, linhas)
    return {"recebidos": len(linhas), "ultimo_id": lote.registros[-1].id}


@app.post("/api/v1/ingest/amostras_ocupacao")
def ingerir_amostras(lote: LoteAmostras, est_id: int = Depends(estabelecimento_autenticado)):
    linhas = [
        (est_id, r.id, r.epoch, r.dia_semana, r.hora, r.camera_id, r.camera_nome,
         r.total_mesas, r.mesas_ocupadas, r.pessoas_sentadas, r.alertas_ativos)
        for r in lote.registros
    ]
    _gravar(est_id, SQL_AMOSTRAS, linhas)
    return {"recebidos": len(linhas), "ultimo_id": lote.registros[-1].id}
