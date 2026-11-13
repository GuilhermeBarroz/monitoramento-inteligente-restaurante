"""
Agente Edge Sync: envia o histórico analítico local (SQLite) para a API de ingestão na nuvem.

Princípios:
- Envia SOMENTE metadados anônimos (eventos, visitas concluídas e amostras de ocupação).
  Nenhuma imagem, frame ou vídeo sai do restaurante.
- O próprio SQLite funciona como fila: um cursor por tabela (último `id` aceito pela nuvem)
  fica em `data/sync_estado.json`. Sem internet, os dados se acumulam localmente e são
  enviados quando a conexão voltar.
- Envio idempotente: a nuvem faz upsert por (estabelecimento, id de origem), então reenviar
  um lote após falha de rede não duplica registros.
- Consultas SQL 100% parametrizadas; cursor salvo via arquivo temporário + `os.replace`.

Configuração (variáveis de ambiente):
    CLOUD_API_URL         URL base da API (ex: https://api.seudominio.com.br). Obrigatória.
    EDGE_TOKEN            Token do estabelecimento gerado no painel/CLI da nuvem. Obrigatório.
    SYNC_INTERVALO_SEG    Intervalo entre ciclos (padrão: 60).
    SYNC_LOTE_MAX         Registros por requisição (padrão: 500, máx. 1000).
    CLOUD_PERMITIR_HTTP   "1" para aceitar http:// (apenas desenvolvimento local).

Uso:
    python src/edge_sync.py            # loop contínuo
    python src/edge_sync.py --uma-vez  # executa um único ciclo e sai
"""

import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

DB_PATH_PADRAO = Path("data/historico_salao.db")
ESTADO_PATH_PADRAO = Path("data/sync_estado.json")
LOTE_MAX_ABSOLUTO = 1000
TIMEOUT_HTTP_SEG = 20

# Colunas enviadas por tabela (lista fechada: nada fora daqui sai do restaurante).
# Os nomes de tabela/coluna são constantes do código, nunca entrada externa.
TABELAS_SINCRONIZADAS: Dict[str, List[str]] = {
    "eventos_mesas": [
        "id", "epoch", "iso_datahora", "dia_semana", "hora",
        "camera_id", "camera_nome", "mesa_id", "tipo_evento", "detalhes_json",
    ],
    "visitas_concluidas": [
        "id", "camera_id", "camera_nome", "mesa_id", "inicio_epoch", "fim_epoch",
        "dia_semana", "hora_inicio", "duracao_segundos", "espera_garcom_segundos",
        "tempo_preparo_segundos", "qtd_pessoas", "houve_alerta",
    ],
    "amostras_ocupacao": [
        "id", "epoch", "dia_semana", "hora", "camera_id", "camera_nome",
        "total_mesas", "mesas_ocupadas", "pessoas_sentadas", "alertas_ativos",
    ],
}

log = logging.getLogger("edge_sync")


class ErroConfiguracao(ValueError):
    pass


class ErroEnvio(RuntimeError):
    def __init__(self, mensagem: str, permanente: bool = False):
        super().__init__(mensagem)
        self.permanente = permanente


def validar_url_api(url: str, permitir_http: bool = False) -> str:
    """Aceita apenas https:// (ou http:// em desenvolvimento) com host definido, sem credenciais."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ErroConfiguracao("CLOUD_API_URL não configurada.")
    partes = urllib.parse.urlsplit(url)
    esquemas = {"https", "http"} if permitir_http else {"https"}
    if partes.scheme not in esquemas:
        raise ErroConfiguracao(
            f"CLOUD_API_URL deve usar {' ou '.join(sorted(esquemas))}:// (recebido: {partes.scheme or 'vazio'})."
        )
    if not partes.hostname:
        raise ErroConfiguracao("CLOUD_API_URL sem host válido.")
    if partes.username or partes.password:
        raise ErroConfiguracao("CLOUD_API_URL não pode conter usuário/senha embutidos.")
    if partes.query or partes.fragment:
        raise ErroConfiguracao("CLOUD_API_URL não pode conter query string ou fragmento.")
    return url


def carregar_estado(caminho: Path) -> Dict[str, int]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {t: 0 for t in TABELAS_SINCRONIZADAS}
    except (OSError, ValueError) as exc:
        log.warning("Estado de sync ilegível (%s); recomeçando do zero (upsert evita duplicatas).", exc)
        return {t: 0 for t in TABELAS_SINCRONIZADAS}
    cursores = dados.get("cursores", {}) if isinstance(dados, dict) else {}
    return {t: max(0, int(cursores.get(t, 0) or 0)) for t in TABELAS_SINCRONIZADAS}


def salvar_estado(caminho: Path, cursores: Dict[str, int]):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    payload = {"cursores": cursores, "atualizado_em": time.time()}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


def ler_lote(db_path: Path, tabela: str, apos_id: int, limite: int) -> List[dict]:
    if tabela not in TABELAS_SINCRONIZADAS:
        raise ValueError(f"Tabela não sincronizável: {tabela}")
    if not db_path.exists():
        return []
    colunas = TABELAS_SINCRONIZADAS[tabela]
    # Identificadores vêm exclusivamente da lista fechada acima; valores sempre via `?`.
    sql = f"SELECT {', '.join(colunas)} FROM {tabela} WHERE id > ? ORDER BY id LIMIT ?"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        try:
            linhas = conn.execute(sql, (int(apos_id), int(limite))).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
    finally:
        conn.close()
    return [dict(linha) for linha in linhas]


def enviar_lote(api_url: str, token: str, tabela: str, registros: List[dict]) -> int:
    """Envia o lote e retorna o maior id de origem confirmado pela nuvem."""
    corpo = json.dumps({"registros": registros}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/api/v1/ingest/{tabela}",
        data=corpo,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "monitor-mesas-edge-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_HTTP_SEG) as resp:
            resposta = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # 401/403/404/422: problema de configuração ou payload; não adianta repetir em loop rápido.
        permanente = exc.code in (400, 401, 403, 404, 413, 422)
        raise ErroEnvio(f"HTTP {exc.code} ao enviar {tabela}", permanente=permanente) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ErroEnvio(f"Falha de rede ao enviar {tabela}: {exc}") from exc
    except ValueError as exc:
        raise ErroEnvio(f"Resposta inválida da nuvem para {tabela}") from exc

    ultimo = resposta.get("ultimo_id")
    esperado = int(registros[-1]["id"])
    if not isinstance(ultimo, int) or ultimo != esperado:
        raise ErroEnvio(f"Confirmação inconsistente para {tabela}: esperado {esperado}, recebido {ultimo}")
    return ultimo


def executar_ciclo(
    api_url: str,
    token: str,
    db_path: Path = DB_PATH_PADRAO,
    estado_path: Path = ESTADO_PATH_PADRAO,
    lote_max: int = 500,
    max_lotes_por_tabela: int = 50,
) -> Dict[str, int]:
    """Drena as três tabelas em lotes. Retorna quantos registros foram enviados por tabela."""
    cursores = carregar_estado(estado_path)
    enviados = {t: 0 for t in TABELAS_SINCRONIZADAS}

    for tabela in TABELAS_SINCRONIZADAS:
        for _ in range(max_lotes_por_tabela):
            lote = ler_lote(db_path, tabela, cursores[tabela], lote_max)
            if not lote:
                break
            cursores[tabela] = enviar_lote(api_url, token, tabela, lote)
            salvar_estado(estado_path, cursores)
            enviados[tabela] += len(lote)
            if len(lote) < lote_max:
                break
    return enviados


def carregar_config() -> Tuple[str, str, int, int]:
    permitir_http = os.environ.get("CLOUD_PERMITIR_HTTP", "").strip() == "1"
    api_url = validar_url_api(os.environ.get("CLOUD_API_URL", ""), permitir_http)
    token = os.environ.get("EDGE_TOKEN", "").strip()
    if len(token) < 32:
        raise ErroConfiguracao("EDGE_TOKEN ausente ou curto demais (gere-o no CLI de administração da nuvem).")
    intervalo = max(10, int(os.environ.get("SYNC_INTERVALO_SEG", "60")))
    lote = min(LOTE_MAX_ABSOLUTO, max(1, int(os.environ.get("SYNC_LOTE_MAX", "500"))))
    return api_url, token, intervalo, lote


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sincroniza o histórico local com o portal em nuvem.")
    parser.add_argument("--uma-vez", action="store_true", help="Executa um único ciclo e sai.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [edge_sync] %(levelname)s %(message)s")

    try:
        api_url, token, intervalo, lote = carregar_config()
    except ErroConfiguracao as exc:
        log.error("%s", exc)
        if args.uma_vez:
            return 2
        # Em modo serviço, não derruba o container em loop de restart: aguarda configuração.
        log.error("Sincronização com a nuvem desativada até a configuração ser corrigida.")
        while True:
            time.sleep(3600)

    espera_erro = intervalo
    while True:
        try:
            enviados = executar_ciclo(api_url, token, lote_max=lote)
            total = sum(enviados.values())
            if total:
                log.info("Sincronizados: %s", enviados)
            espera_erro = intervalo
            espera = intervalo
        except ErroEnvio as exc:
            log.warning("%s", exc)
            if args.uma_vez:
                return 1
            # Backoff exponencial até 30 min (erros permanentes já começam alto).
            espera_erro = 1800 if exc.permanente else min(1800, espera_erro * 2)
            espera = espera_erro
        if args.uma_vez:
            return 0
        time.sleep(espera)


if __name__ == "__main__":
    sys.exit(main())
