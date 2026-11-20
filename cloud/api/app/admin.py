"""
CLI de administração da nuvem (executar dentro do container da API).

    docker compose exec api python -m app.admin criar-estabelecimento "Restaurante Centro"
    docker compose exec api python -m app.admin listar
    docker compose exec api python -m app.admin novo-token 3
    docker compose exec api python -m app.admin desativar 3
    docker compose exec api python -m app.admin reativar 3
    docker compose exec api python -m app.admin definir-fuso 3 America/Manaus
    docker compose exec api python -m app.admin adicionar-usuario dono@gmail.com gerente --estabelecimento 3 4
    docker compose exec api python -m app.admin adicionar-usuario voce@gmail.com admin
    docker compose exec api python -m app.admin vincular dono@gmail.com 5
    docker compose exec api python -m app.admin desvincular dono@gmail.com 4
    docker compose exec api python -m app.admin remover-usuario dono@gmail.com
    docker compose exec api python -m app.admin criar-login-portal

O token do Edge é exibido UMA única vez; o banco guarda apenas o hash SHA-256.
"""

import argparse
import os
import re
import secrets
import sys
import zoneinfo

from psycopg import sql

from . import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGIN_PORTAL_RE = re.compile(r"^[a-z_][a-z0-9_]{2,30}$")


def _exibir_token(est_id: int, nome: str, token: str):
    print(f"\nEstabelecimento #{est_id} - {nome}")
    print("Token do Edge (copie agora; ele NÃO será exibido novamente):\n")
    print(f"    EDGE_TOKEN={token}\n")


def _email_valido(parser: argparse.ArgumentParser, email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        parser.error("e-mail inválido")
    return email


def _estabelecimentos_existentes(conn, ids) -> list:
    encontrados = {
        r[0] for r in conn.execute("SELECT id FROM estabelecimentos WHERE id = ANY(%s)", (list(ids),)).fetchall()
    }
    faltando = sorted(set(ids) - encontrados)
    if faltando:
        raise SystemExit(f"Estabelecimento(s) não encontrado(s): {faltando}")
    return sorted(encontrados)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("criar-estabelecimento")
    p.add_argument("nome")
    p.add_argument("--fuso", default="America/Sao_Paulo")
    sub.add_parser("listar")
    p = sub.add_parser("novo-token")
    p.add_argument("id", type=int)
    p = sub.add_parser("desativar")
    p.add_argument("id", type=int)
    p = sub.add_parser("reativar")
    p.add_argument("id", type=int)
    p = sub.add_parser("definir-fuso")
    p.add_argument("id", type=int)
    p.add_argument("fuso")
    p = sub.add_parser("adicionar-usuario")
    p.add_argument("email")
    p.add_argument("role", choices=["admin", "gerente"])
    p.add_argument("--estabelecimento", type=int, nargs="+", help="loja(s) do gerente")
    p.add_argument("--nome")
    p = sub.add_parser("vincular")
    p.add_argument("email")
    p.add_argument("id", type=int)
    p = sub.add_parser("desvincular")
    p.add_argument("email")
    p.add_argument("id", type=int)
    p = sub.add_parser("remover-usuario")
    p.add_argument("email")
    p = sub.add_parser("criar-login-portal", help="cria/atualiza o login Postgres somente leitura do portal")
    p.add_argument("--usuario", default="portal")

    args = parser.parse_args(argv)
    db.aplicar_schema()

    def fuso_valido(nome: str) -> str:
        try:
            zoneinfo.ZoneInfo(nome)
        except Exception:
            parser.error(f"fuso inválido: {nome} (use nomes IANA, ex.: America/Sao_Paulo)")
        return nome

    with db.obter_pool().connection() as conn:
        if args.comando == "criar-estabelecimento":
            nome = args.nome.strip()
            if not (1 <= len(nome) <= 120):
                parser.error("nome deve ter entre 1 e 120 caracteres")
            token = db.gerar_token()
            est_id = conn.execute(
                "INSERT INTO estabelecimentos (nome, token_hash, timezone) VALUES (%s, %s, %s) RETURNING id",
                (nome, db.hash_token(token), fuso_valido(args.fuso)),
            ).fetchone()[0]
            _exibir_token(est_id, nome, token)

        elif args.comando == "listar":
            linhas = conn.execute(
                "SELECT id, nome, ativo, timezone, ultimo_sync_em FROM estabelecimentos ORDER BY id"
            ).fetchall()
            for est_id, nome, ativo, fuso, ultimo in linhas:
                print(f"#{est_id:<4} {'ATIVO ' if ativo else 'INATIVO'} {fuso:<20} último sync: {ultimo or '-'}  {nome}")
            usuarios = conn.execute(
                """
                SELECT u.email, u.role, u.ativo,
                       COALESCE(string_agg(ue.estabelecimento_id::text, ',' ORDER BY ue.estabelecimento_id), '')
                FROM usuarios u
                LEFT JOIN usuario_estabelecimentos ue ON ue.email = u.email
                GROUP BY u.email, u.role, u.ativo
                ORDER BY u.role, u.email
                """
            ).fetchall()
            if usuarios:
                print("\nUsuários do portal:")
                for email, role, ativo, lojas in usuarios:
                    escopo = "todas" if role == "admin" else (lojas or "nenhuma")
                    print(f"  {email:<40} {role:<8} lojas={escopo:<12} {'ativo' if ativo else 'inativo'}")

        elif args.comando == "novo-token":
            token = db.gerar_token()
            # Trocar o token NÃO reativa um estabelecimento desativado (use `reativar`).
            linha = conn.execute(
                "UPDATE estabelecimentos SET token_hash = %s WHERE id = %s RETURNING nome, ativo",
                (db.hash_token(token), args.id),
            ).fetchone()
            if linha is None:
                print("Estabelecimento não encontrado.", file=sys.stderr)
                return 1
            _exibir_token(args.id, linha[0], token)
            if not linha[1]:
                print("Atenção: o estabelecimento está DESATIVADO; o novo token só funciona após `reativar`.")

        elif args.comando == "desativar":
            n = conn.execute("UPDATE estabelecimentos SET ativo = FALSE WHERE id = %s", (args.id,)).rowcount
            print("Desativado." if n else "Estabelecimento não encontrado.")

        elif args.comando == "reativar":
            n = conn.execute("UPDATE estabelecimentos SET ativo = TRUE WHERE id = %s", (args.id,)).rowcount
            print("Reativado." if n else "Estabelecimento não encontrado.")

        elif args.comando == "definir-fuso":
            n = conn.execute(
                "UPDATE estabelecimentos SET timezone = %s WHERE id = %s", (fuso_valido(args.fuso), args.id)
            ).rowcount
            print(f"Fuso definido: {args.fuso}." if n else "Estabelecimento não encontrado.")

        elif args.comando == "adicionar-usuario":
            email = _email_valido(parser, args.email)
            if args.role == "gerente" and not args.estabelecimento:
                parser.error("gerente exige --estabelecimento ID [ID ...]")
            lojas = _estabelecimentos_existentes(conn, args.estabelecimento) if args.role == "gerente" else []
            conn.execute(
                """
                INSERT INTO usuarios (email, nome, role) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                   SET nome = COALESCE(EXCLUDED.nome, usuarios.nome), role = EXCLUDED.role, ativo = TRUE
                """,
                (email, args.nome, args.role),
            )
            # Admin vê todas as lojas por papel; vínculos só fazem sentido para gerente
            conn.execute("DELETE FROM usuario_estabelecimentos WHERE email = %s", (email,))
            for est_id in lojas:
                conn.execute(
                    "INSERT INTO usuario_estabelecimentos (email, estabelecimento_id) VALUES (%s, %s)",
                    (email, est_id),
                )
            escopo = "todas as lojas" if args.role == "admin" else f"lojas {lojas}"
            print(f"Usuário {email} autorizado como {args.role} ({escopo}).")

        elif args.comando == "vincular":
            email = _email_valido(parser, args.email)
            _estabelecimentos_existentes(conn, [args.id])
            if conn.execute("SELECT 1 FROM usuarios WHERE email = %s", (email,)).fetchone() is None:
                print("Usuário não encontrado (use adicionar-usuario).", file=sys.stderr)
                return 1
            conn.execute(
                "INSERT INTO usuario_estabelecimentos (email, estabelecimento_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (email, args.id),
            )
            print(f"{email} vinculado à loja #{args.id}.")

        elif args.comando == "desvincular":
            email = _email_valido(parser, args.email)
            n = conn.execute(
                "DELETE FROM usuario_estabelecimentos WHERE email = %s AND estabelecimento_id = %s", (email, args.id)
            ).rowcount
            print("Vínculo removido." if n else "Vínculo não encontrado.")

        elif args.comando == "remover-usuario":
            n = conn.execute(
                "UPDATE usuarios SET ativo = FALSE WHERE email = %s", (_email_valido(parser, args.email),)
            ).rowcount
            print("Acesso revogado." if n else "Usuário não encontrado.")

        elif args.comando == "criar-login-portal":
            usuario = args.usuario.strip().lower()
            if not LOGIN_PORTAL_RE.match(usuario):
                parser.error("usuário inválido (letras minúsculas, números e _; 3 a 31 caracteres)")
            senha = os.environ.get("PORTAL_DB_PASSWORD", "").strip() or secrets.token_urlsafe(32)
            existe = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (usuario,)).fetchone()
            # Comandos de papel não aceitam parâmetros vinculados: composição segura com
            # Identifier/Literal do psycopg (escapados pelo driver, sem concatenação de texto).
            comando = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if existe else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
            conn.execute(sql.SQL(comando).format(sql.Identifier(usuario), sql.Literal(senha)))
            conn.execute(sql.SQL("GRANT portal_leitura TO {}").format(sql.Identifier(usuario)))
            print(f"Login somente leitura '{usuario}' pronto (membro de portal_leitura).")
            if not os.environ.get("PORTAL_DB_PASSWORD"):
                print("Senha gerada (copie agora; NÃO será exibida novamente):\n")
                print(f"    PORTAL_DB_PASSWORD={senha}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
