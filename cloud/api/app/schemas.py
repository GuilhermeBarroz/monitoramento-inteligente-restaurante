"""Esquemas Pydantic estritos para o payload enviado pelo Edge Sync (validação antes de qualquer SQL)."""

import json
from typing import Annotated, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

LOTE_MAX = 1000
# O caractere NUL não é aceito pelo Postgres (TEXT/JSONB): recusado aqui com 422, para o
# Edge Sync isolar o registro, em vez de virar erro 500 reenviado para sempre.
SEM_NUL = r"^[^\x00]*$"
Texto = Annotated[str, Field(min_length=1, max_length=120, pattern=SEM_NUL)]
InstalacaoId = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]
Epoch = Annotated[float, Field(ge=1_000_000_000, le=10_000_000_000)]  # ~2001 a ~2286
DiaSemana = Annotated[int, Field(ge=0, le=6)]
Hora = Annotated[int, Field(ge=0, le=23)]
OrigemId = Annotated[int, Field(ge=1)]
Segundos = Annotated[float, Field(ge=0, le=7 * 86400)]
Contagem = Annotated[int, Field(ge=0, le=10_000)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)


class EventoMesa(_Base):
    id: OrigemId
    epoch: Epoch
    iso_datahora: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")]
    dia_semana: DiaSemana
    hora: Hora
    camera_id: Texto
    camera_nome: Texto
    mesa_id: Texto
    tipo_evento: Annotated[str, Field(min_length=1, max_length=60, pattern=SEM_NUL)]
    # validate_default: sem o campo, o default None também passa pelo validador e vira "{}"
    # (antes inseria NULL numa coluna NOT NULL → erro 500).
    detalhes_json: Optional[str] = Field(default=None, max_length=8000, validate_default=True)

    @field_validator("detalhes_json", mode="before")
    @classmethod
    def _json_objeto(cls, valor: Optional[str]) -> str:
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            return "{}"
        if not isinstance(valor, str):
            raise ValueError("detalhes_json deve ser texto JSON")
        try:
            dados = json.loads(valor)
        except ValueError as exc:
            raise ValueError("detalhes_json não é JSON válido") from exc
        if not isinstance(dados, dict):
            raise ValueError("detalhes_json deve ser um objeto JSON")
        normalizado = json.dumps(dados, ensure_ascii=False)
        if "\x00" in normalizado or "\\u0000" in normalizado:
            raise ValueError("detalhes_json contém caractere NUL")
        return normalizado


class VisitaConcluida(_Base):
    id: OrigemId
    camera_id: Texto
    camera_nome: Texto
    mesa_id: Texto
    inicio_epoch: Epoch
    fim_epoch: Epoch
    dia_semana: DiaSemana
    hora_inicio: Hora
    duracao_segundos: Segundos
    espera_garcom_segundos: Optional[Segundos] = None
    tempo_preparo_segundos: Optional[Segundos] = None
    qtd_pessoas: Annotated[int, Field(ge=1, le=100)]
    houve_alerta: Annotated[int, Field(ge=0, le=1)]


class AmostraOcupacao(_Base):
    id: OrigemId
    epoch: Epoch
    dia_semana: DiaSemana
    hora: Hora
    camera_id: Texto
    camera_nome: Texto
    total_mesas: Contagem
    mesas_ocupadas: Contagem
    pessoas_sentadas: Contagem
    alertas_ativos: Contagem


class _LoteBase(_Base):
    @field_validator("registros", check_fields=False)
    @classmethod
    def _ids_crescentes_unicos(cls, registros: list) -> list:
        ids = [r.id for r in registros]
        if any(b <= a for a, b in zip(ids, ids[1:])):
            raise ValueError("registros devem ter ids únicos em ordem crescente")
        return registros


class LoteEventos(_LoteBase):
    instalacao_id: InstalacaoId
    registros: Annotated[List[EventoMesa], Field(min_length=1, max_length=LOTE_MAX)]


class LoteVisitas(_LoteBase):
    instalacao_id: InstalacaoId
    registros: Annotated[List[VisitaConcluida], Field(min_length=1, max_length=LOTE_MAX)]


class LoteAmostras(_LoteBase):
    instalacao_id: InstalacaoId
    registros: Annotated[List[AmostraOcupacao], Field(min_length=1, max_length=LOTE_MAX)]
