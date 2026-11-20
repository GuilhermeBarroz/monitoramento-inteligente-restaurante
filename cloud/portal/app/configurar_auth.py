"""
Gera `.streamlit/secrets.toml` (login Google do `st.login`) a partir de variáveis de
ambiente, na subida do container. Assim nenhum segredo fica versionado nem na imagem.

Variáveis (definidas em cloud/.env):
    PORTAL_URL             URL pública do portal (https://portal.seudominio.com.br)
    PORTAL_COOKIE_SECRET   segredo aleatório (>= 32 caracteres) que assina o cookie de sessão
    GOOGLE_CLIENT_ID       credencial OAuth "Aplicativo da Web" do Google Cloud
    GOOGLE_CLIENT_SECRET

Uso: python -m app.configurar_auth [caminho_saida]
"""

import json
import os
from pathlib import Path
import sys
from typing import Mapping
from urllib.parse import urlparse

SAIDA_PADRAO = Path(".streamlit/secrets.toml")
GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
MIN_COOKIE_SECRET = 32


class ConfigInvalida(ValueError):
    pass


def _texto(env: Mapping[str, str], nome: str) -> str:
    valor = (env.get(nome) or "").strip()
    if not valor:
        raise ConfigInvalida(f"{nome} não definida")
    if any(ord(c) < 32 or ord(c) == 127 for c in valor):
        raise ConfigInvalida(f"{nome} contém caracteres de controle")
    return valor


def _redirect_uri(portal_url: str) -> str:
    p = urlparse(portal_url)
    local = p.hostname in {"localhost", "127.0.0.1"}
    if p.scheme != "https" and not (p.scheme == "http" and local):
        raise ConfigInvalida("PORTAL_URL deve usar https:// (http só para localhost)")
    if not p.hostname or p.query or p.fragment or p.username or p.password:
        raise ConfigInvalida("PORTAL_URL inválida (use só esquema, domínio e, se houver, caminho)")
    if p.hostname.endswith(".invalid"):
        raise ConfigInvalida("PORTAL_DOMAIN não definido no .env")
    return portal_url.rstrip("/") + "/oauth2callback"


def validar_banco(env: Mapping[str, str]) -> None:
    """O compose monta a URL mesmo sem senha; falhar aqui dá uma mensagem clara em vez de erro de conexão."""
    dsn = (env.get("PORTAL_DATABASE_URL") or "").strip()
    if not dsn:
        raise ConfigInvalida("PORTAL_DATABASE_URL não definida")
    if not urlparse(dsn).password:
        raise ConfigInvalida(
            "PORTAL_DB_PASSWORD vazia: rode `docker compose exec api python -m app.admin criar-login-portal` "
            "e coloque a senha no .env"
        )


def montar_secrets(env: Mapping[str, str]) -> str:
    """Conteúdo TOML. Valores entram como strings JSON (sintaxe válida de string básica TOML)."""
    redirect = _redirect_uri(_texto(env, "PORTAL_URL"))
    cookie = _texto(env, "PORTAL_COOKIE_SECRET")
    if len(cookie) < MIN_COOKIE_SECRET:
        raise ConfigInvalida(f"PORTAL_COOKIE_SECRET deve ter ao menos {MIN_COOKIE_SECRET} caracteres")
    client_id = _texto(env, "GOOGLE_CLIENT_ID")
    client_secret = _texto(env, "GOOGLE_CLIENT_SECRET")
    def q(v: str) -> str:
        # ensure_ascii=False: emoji/acentos saem em UTF-8 (pares surrogate escapados são inválidos em TOML)
        return json.dumps(v, ensure_ascii=False)

    return (
        "# Gerado na subida do container por app/configurar_auth.py. NÃO editar nem versionar.\n"
        "[auth]\n"
        f"redirect_uri = {q(redirect)}\n"
        f"cookie_secret = {q(cookie)}\n\n"
        "[auth.google]\n"
        f"client_id = {q(client_id)}\n"
        f"client_secret = {q(client_secret)}\n"
        f"server_metadata_url = {q(GOOGLE_METADATA_URL)}\n"
    )


def gravar(conteudo: str, destino: Path) -> None:
    """Gravação atômica com permissão 0600 (só o usuário do container lê)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    tmp.unlink(missing_ok=True)  # um .tmp antigo poderia manter permissão mais aberta
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(conteudo)
    os.replace(tmp, destino)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    destino = Path(argv[0]) if argv else SAIDA_PADRAO
    try:
        validar_banco(os.environ)
        gravar(montar_secrets(os.environ), destino)
    except ConfigInvalida as e:
        print(f"ERRO: configuração do login do portal inválida: {e}", file=sys.stderr)
        return 1
    print(f"Login Google configurado ({destino}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
