"""
Agente Edge Sync: envia o histórico analítico local (SQLite) para a API de ingestão na nuvem.

Princípios:
- Envia SOMENTE metadados anônimos (eventos, visitas concluídas e amostras de ocupação).
  Nenhuma imagem, frame ou vídeo sai do restaurante.
- O próprio SQLite funciona como fila: um cursor por tabela (último `id` aceito pela nuvem)
  fica em `data/sync_estado.json`. Sem internet, os dados se acumulam localmente e são
  enviados quando a conexão voltar.
- Envio idempotente: a nuvem grava por (estabelecimento, instalação, id de origem), então
  reenviar um lote após falha de rede não duplica registros.
- Identidade da instalação (`instalacao_id` do SQLite): se o banco local for recriado, os
  ids recomeçam do 1. O agente detecta, zera os cursores e a nuvem guarda os novos
  registros sob a nova instalação, sem descartar nada em silêncio.
- Resiliência: cada tabela sincroniza de forma independente; um registro recusado pela
  nuvem (HTTP 422/413) é isolado por bissecção e posto em quarentena, sem travar a fila.
- Consultas SQL 100% parametrizadas; estado salvo via arquivo temporário + `os.replace`.

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
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

DB_PATH_PADRAO = Path("data/historico_salao.db")
ESTADO_PATH_PADRAO = Path("data/sync_estado.json")
LOTE_MAX_ABSOLUTO = 1000
TIMEOUT_HTTP_SEG = 20
MAX_QUARENTENA = 1000  # registros recusados guardados no estado para diagnóstico
STATUS_REGISTRO_RECUSADO = (413, 422)  # o problema está no conteúdo do lote: dividir e isolar
LIMITE_RECUSAS_SEGUIDAS = 5  # disjuntor: recusas em sequência sem aceite = problema de versão/proxy

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
    def __init__(self, mensagem: str, permanente: bool = False, status: Optional[int] = None):
        super().__init__(mensagem)
        self.permanente = permanente
        self.status = status


class _SemRedirecionamento(urllib.request.HTTPRedirectHandler):
    """Recusa redirecionamentos: o urllib reenviaria o token Bearer ao novo destino."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"Redirecionamento recusado para {newurl}", headers, fp)


_abridor = urllib.request.build_opener(_SemRedirecionamento)


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


# ---------------------------------------------------------------- estado local

def _estado_vazio() -> Dict[str, Any]:
    return {"instalacao_id": None, "cursores": {t: 0 for t in TABELAS_SINCRONIZADAS}, "quarentena": []}


def carregar_estado_completo(caminho: Path) -> Dict[str, Any]:
    """Estado do sync: instalação de origem, cursores por tabela e registros em quarentena."""
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _estado_vazio()
    except (OSError, ValueError) as exc:
        log.warning("Estado de sync ilegível (%s); recomeçando do zero (a nuvem ignora reenvios).", exc)
        return _estado_vazio()
    if not isinstance(dados, dict):
        return _estado_vazio()
    cursores_brutos = dados.get("cursores", {}) if isinstance(dados.get("cursores"), dict) else {}
    cursores = {}
    for t in TABELAS_SINCRONIZADAS:
        try:
            cursores[t] = max(0, int(cursores_brutos.get(t, 0) or 0))
        except (TypeError, ValueError):
            cursores[t] = 0
    quarentena = dados.get("quarentena") if isinstance(dados.get("quarentena"), list) else []
    instalacao = dados.get("instalacao_id") if isinstance(dados.get("instalacao_id"), str) else None
    return {"instalacao_id": instalacao, "cursores": cursores, "quarentena": quarentena[-MAX_QUARENTENA:]}


def carregar_estado(caminho: Path) -> Dict[str, int]:
    """Compatibilidade: só os cursores por tabela."""
    return carregar_estado_completo(caminho)["cursores"]


def salvar_estado(caminho: Path, estado: Dict[str, Any]):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    payload = {
        "instalacao_id": estado.get("instalacao_id"),
        "cursores": estado["cursores"],
        "quarentena": estado.get("quarentena", [])[-MAX_QUARENTENA:],
        "atualizado_em": time.time(),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


# ---------------------------------------------------------------- leitura do SQLite

def _conectar_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def ler_instalacao_id(db_path: Path) -> Optional[str]:
    """`instalacao_id` gravado pelo analytics_db; None se o banco/tabela ainda não existir."""
    if not db_path.exists():
        return None
    conn = _conectar_ro(db_path)
    try:
        linha = conn.execute("SELECT valor FROM meta WHERE chave = ?", ("instalacao_id",)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return str(linha["valor"]) if linha else None


def maior_id(db_path: Path, tabela: str) -> int:
    if tabela not in TABELAS_SINCRONIZADAS or not db_path.exists():
        return 0
    conn = _conectar_ro(db_path)
    try:
        # Identificador vem da lista fechada; não há valor externo na consulta.
        linha = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {tabela}").fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
    return int(linha["m"])


def ler_lote(db_path: Path, tabela: str, apos_id: int, limite: int) -> List[dict]:
    if tabela not in TABELAS_SINCRONIZADAS:
        raise ValueError(f"Tabela não sincronizável: {tabela}")
    if not db_path.exists():
        return []
    colunas = TABELAS_SINCRONIZADAS[tabela]
    # Identificadores vêm exclusivamente da lista fechada acima; valores sempre via `?`.
    sql = f"SELECT {', '.join(colunas)} FROM {tabela} WHERE id > ? ORDER BY id LIMIT ?"
    conn = _conectar_ro(db_path)
    try:
        try:
            linhas = conn.execute(sql, (int(apos_id), int(limite))).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
    finally:
        conn.close()
    return [dict(linha) for linha in linhas]


# ---------------------------------------------------------------- envio

def enviar_lote(api_url: str, token: str, tabela: str, registros: List[dict], instalacao_id: str) -> int:
    """Envia o lote e retorna o maior id de origem confirmado pela nuvem."""
    corpo = json.dumps({"instalacao_id": instalacao_id, "registros": registros}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/api/v1/ingest/{tabela}",
        data=corpo,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "monitor-mesas-edge-sync/1.1",
        },
    )
    try:
        with _abridor.open(req, timeout=TIMEOUT_HTTP_SEG) as resp:
            resposta = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        exc.close()  # libera o socket da resposta (inclusive a do redirecionamento recusado)
        # 401/403/404: configuração (token, URL); 413/422: conteúdo do lote (tratado por bissecção);
        # 3xx: redirecionamento recusado; 5xx: transitório.
        permanente = exc.code in (400, 401, 403, 404, 413, 422) or 300 <= exc.code < 400
        raise ErroEnvio(f"HTTP {exc.code} ao enviar {tabela}", permanente=permanente, status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ErroEnvio(f"Falha de rede ao enviar {tabela}: {exc}") from exc
    except ValueError as exc:
        raise ErroEnvio(f"Resposta inválida da nuvem para {tabela}") from exc

    if not isinstance(resposta, dict):
        raise ErroEnvio(f"Resposta inválida da nuvem para {tabela}")
    ultimo = resposta.get("ultimo_id")
    esperado = int(registros[-1]["id"])
    if not isinstance(ultimo, int) or ultimo != esperado:
        raise ErroEnvio(f"Confirmação inconsistente para {tabela}: esperado {esperado}, recebido {ultimo}")
    return ultimo


def _efetivar_quarentena(tabela: str, pendentes: List[dict], estado: Dict[str, Any]) -> None:
    for item in pendentes:
        log.error("Registro %s #%s recusado pela nuvem (HTTP %s); posto em quarentena.", tabela, item["id"], item["status"])
        estado.setdefault("quarentena", []).append({"tabela": tabela, **item, "em": time.time()})
    pendentes.clear()


def _enviar_recursivo(
    api_url: str,
    token: str,
    tabela: str,
    registros: List[dict],
    estado: Dict[str, Any],
    estado_path: Path,
    ctx: Dict[str, Any],
) -> None:
    """Envia; em recusa de conteúdo (422/413) divide ao meio e acumula os isolados em ctx."""
    try:
        ultimo = enviar_lote(api_url, token, tabela, registros, estado["instalacao_id"])
    except ErroEnvio as exc:
        if exc.status not in STATUS_REGISTRO_RECUSADO:
            raise
        if len(registros) == 1:
            ctx["pendentes"].append({"id": registros[0].get("id"), "status": exc.status})
            if len(ctx["pendentes"]) >= LIMITE_RECUSAS_SEGUIDAS:
                raise ErroEnvio(
                    f"{tabela}: {len(ctx['pendentes'])} registros seguidos recusados (HTTP {exc.status}) sem "
                    "nenhum aceite: provável incompatibilidade de versão com a nuvem ou proxy. "
                    "Sincronização desta tabela suspensa; nada foi descartado.",
                    permanente=True,
                    status=exc.status,
                ) from exc
            return
        meio = len(registros) // 2
        _enviar_recursivo(api_url, token, tabela, registros[:meio], estado, estado_path, ctx)
        _enviar_recursivo(api_url, token, tabela, registros[meio:], estado, estado_path, ctx)
        return
    # Aceite: confirma os recusados anteriores como problema de conteúdo
    ctx["houve_aceite"] = True
    _efetivar_quarentena(tabela, ctx["pendentes"], estado)
    estado["cursores"][tabela] = ultimo
    salvar_estado(estado_path, estado)


def _enviar_com_isolamento(
    api_url: str,
    token: str,
    tabela: str,
    registros: List[dict],
    estado: Dict[str, Any],
    estado_path: Path,
) -> None:
    """
    Envia `registros` avançando o cursor a cada parte confirmada. Se a nuvem recusar o
    CONTEÚDO (422/413), divide o lote ao meio até isolar o(s) registro(s) problemático(s).

    Disjuntor (evita descartar o histórico em silêncio):
    - um registro isolado só vai para a quarentena quando a nuvem ACEITA outro registro
      do mesmo lote (prova de que o problema é daquele registro, não da nuvem/versão);
    - LIMITE_RECUSAS_SEGUIDAS recusas em sequência sem aceite → erro permanente, cursor parado;
    - lote só com recusas (abaixo do limite) → erro transitório, cursor parado; o próximo
      ciclo, com registros novos, serve de comparação.
    """
    ctx: Dict[str, Any] = {"pendentes": [], "houve_aceite": False}
    _enviar_recursivo(api_url, token, tabela, registros, estado, estado_path, ctx)
    if not ctx["pendentes"]:
        return
    if ctx["houve_aceite"]:
        # Recusas no fim do lote, com aceites no mesmo lote: isola e segue
        ultimo_pendente = int(ctx["pendentes"][-1]["id"])
        _efetivar_quarentena(tabela, ctx["pendentes"], estado)
        estado["cursores"][tabela] = max(estado["cursores"][tabela], ultimo_pendente)
        salvar_estado(estado_path, estado)
        return
    raise ErroEnvio(
        f"{tabela}: registro(s) {[p['id'] for p in ctx['pendentes']]} recusado(s) pela nuvem sem nenhum "
        "aceite para comparação; aguardando novos registros antes de isolar.",
        permanente=False,
        status=ctx["pendentes"][-1]["status"],
    )


def _conferir_instalacao(db_path: Path, estado: Dict[str, Any], estado_path: Path) -> Optional[str]:
    """
    Garante que os cursores pertencem ao banco atual. Zera-os se o banco foi recriado
    (instalacao_id diferente, ou maior id local abaixo do cursor em estados antigos).
    Retorna o instalacao_id atual (None se o banco ainda não existe).
    """
    atual = ler_instalacao_id(db_path)
    if atual is None:
        if db_path.exists():
            # Banco existe mas sem a identidade (versão antiga ou motor ainda não iniciou):
            # avisa em vez de "sincronizar" zero registros em silêncio.
            log.warning("Banco %s sem instalacao_id (motor de visão ainda não atualizou o esquema); aguardando.", db_path)
        return None
    anterior = estado.get("instalacao_id")
    recriado = anterior is not None and anterior != atual
    if anterior is None:
        # Estado de versão anterior (sem instalacao_id): detecta recriação pelo maior id
        recriado = any(estado["cursores"][t] > maior_id(db_path, t) for t in TABELAS_SINCRONIZADAS)
    if recriado:
        log.warning(
            "Banco local recriado (instalação %s → %s). Cursores zerados; a nuvem receberá "
            "o novo histórico sob a nova instalação.", anterior or "?", atual,
        )
        estado["cursores"] = {t: 0 for t in TABELAS_SINCRONIZADAS}
    if recriado or anterior != atual:
        estado["instalacao_id"] = atual
        salvar_estado(estado_path, estado)
    return atual


def executar_ciclo(
    api_url: str,
    token: str,
    db_path: Path = DB_PATH_PADRAO,
    estado_path: Path = ESTADO_PATH_PADRAO,
    lote_max: int = 500,
    max_lotes_por_tabela: int = 50,
) -> Dict[str, int]:
    """
    Drena as três tabelas em lotes. Retorna quantos registros foram processados por tabela.
    Uma falha numa tabela não impede as demais; ao final, se houve falha, levanta a
    primeira ErroEnvio (permanente só se todas as falhas forem permanentes).
    """
    estado = carregar_estado_completo(estado_path)
    enviados = {t: 0 for t in TABELAS_SINCRONIZADAS}
    if _conferir_instalacao(db_path, estado, estado_path) is None:
        return enviados

    erros: List[ErroEnvio] = []
    for tabela in TABELAS_SINCRONIZADAS:
        try:
            for _ in range(max_lotes_por_tabela):
                lote = ler_lote(db_path, tabela, estado["cursores"][tabela], lote_max)
                if not lote:
                    break
                _enviar_com_isolamento(api_url, token, tabela, lote, estado, estado_path)
                enviados[tabela] += len(lote)
                if len(lote) < lote_max:
                    break
        except ErroEnvio as exc:
            log.warning("Tabela %s: %s", tabela, exc)
            erros.append(exc)
            if exc.status in (401, 403):
                break  # token inválido vale para todas as tabelas: não insiste
        except sqlite3.Error as exc:
            log.warning("Tabela %s: erro lendo o banco local: %s", tabela, exc)
            erros.append(ErroEnvio(f"Erro lendo {tabela}: {exc}"))

    if erros:
        primeiro = erros[0]
        primeiro.permanente = all(e.permanente for e in erros)
        raise primeiro
    return enviados


def carregar_config() -> Tuple[str, str, int, int]:
    permitir_http = os.environ.get("CLOUD_PERMITIR_HTTP", "").strip() == "1"
    api_url = validar_url_api(os.environ.get("CLOUD_API_URL", ""), permitir_http)
    token = os.environ.get("EDGE_TOKEN", "").strip()
    if len(token) < 32:
        raise ErroConfiguracao("EDGE_TOKEN ausente ou curto demais (gere-o no CLI de administração da nuvem).")
    try:
        intervalo = max(10, int(os.environ.get("SYNC_INTERVALO_SEG", "60")))
        lote = min(LOTE_MAX_ABSOLUTO, max(1, int(os.environ.get("SYNC_LOTE_MAX", "500"))))
    except ValueError as exc:
        raise ErroConfiguracao(f"SYNC_INTERVALO_SEG/SYNC_LOTE_MAX inválidos: {exc}") from exc
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
        except Exception:  # noqa: BLE001 — o agente 24/7 não pode morrer por erro inesperado
            log.exception("Erro inesperado no ciclo de sincronização")
            if args.uma_vez:
                return 1
            espera_erro = min(1800, espera_erro * 2)
            espera = espera_erro
        if args.uma_vez:
            return 0
        time.sleep(espera)


if __name__ == "__main__":
    sys.exit(main())
