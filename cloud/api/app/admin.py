"""
CLI de administração da nuvem (executar dentro do container da API).

    docker compose exec api python -m app.admin criar-estabelecimento "Restaurante Centro"
    docker compose exec api python -m app.admin listar
    docker compose exec api python -m app.admin novo-token 3
    docker compose exec api python -m app.admin desativar 3
    docker compose exec api python -m app.admin adicionar-usuario dono@gmail.com gerente --estabelecimento 3
    docker compose exec api python -m app.admin adicionar-usuario voce@gmail.com admin

O token do Edge é exibido UMA única vez; o banco guarda apenas o hash SHA-256.
"""

import argparse
import re
import sys

from . import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _exibir_token(est_id: int, nome: str, token: str):
    print(f"\nEstabelecimento #{est_id} - {nome}")
    print("Token do Edge (copie agora; ele NÃO será exibido novamente):\n")
    print(f"    EDGE_TOKEN={token}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("criar-estabelecimento")
    p.add_argument("nome")
    sub.add_parser("listar")
    p = sub.add_parser("novo-token")
    p.add_argument("id", type=int)
    p = sub.add_parser("desativar")
    p.add_argument("id", type=int)
    p = sub.add_parser("adicionar-usuario")
    p.add_argument("email")
    p.add_argument("role", choices=["admin", "gerente"])
    p.add_argument("--estabelecimento", type=int)
    p.add_argument("--nome")
    p = sub.add_parser("remover-usuario")
    p.add_argument("email")

    args = parser.parse_args(argv)
    db.aplicar_schema()

    with db.obter_pool().connection() as conn:
        if args.comando == "criar-estabelecimento":
            nome = args.nome.strip()
            if not (1 <= len(nome) <= 120):
                parser.error("nome deve ter entre 1 e 120 caracteres")
            token = db.gerar_token()
            est_id = conn.execute(
                "INSERT INTO estabelecimentos (nome, token_hash) VALUES (%s, %s) RETURNING id",
                (nome, db.hash_token(token)),
            ).fetchone()[0]
            _exibir_token(est_id, nome, token)

        elif args.comando == "listar":
            linhas = conn.execute(
                "SELECT id, nome, ativo, ultimo_sync_em FROM estabelecimentos ORDER BY id"
            ).fetchall()
            for est_id, nome, ativo, ultimo in linhas:
                print(f"#{est_id:<4} {'ATIVO ' if ativo else 'INATIVO'} último sync: {ultimo or '-'}  {nome}")
            usuarios = conn.execute(
                "SELECT email, role, estabelecimento_id, ativo FROM usuarios ORDER BY role, email"
            ).fetchall()
            if usuarios:
                print("\nUsuários do portal:")
                for email, role, est_id, ativo in usuarios:
                    print(f"  {email:<40} {role:<8} est={est_id or '*'} {'ativo' if ativo else 'inativo'}")

        elif args.comando == "novo-token":
            token = db.gerar_token()
            linha = conn.execute(
                "UPDATE estabelecimentos SET token_hash = %s, ativo = TRUE WHERE id = %s RETURNING nome",
                (db.hash_token(token), args.id),
            ).fetchone()
            if linha is None:
                print("Estabelecimento não encontrado.", file=sys.stderr)
                return 1
            _exibir_token(args.id, linha[0], token)

        elif args.comando == "desativar":
            n = conn.execute("UPDATE estabelecimentos SET ativo = FALSE WHERE id = %s", (args.id,)).rowcount
            print("Desativado." if n else "Estabelecimento não encontrado.")

        elif args.comando == "adicionar-usuario":
            email = args.email.strip().lower()
            if not EMAIL_RE.match(email):
                parser.error("e-mail inválido")
            if args.role == "gerente" and args.estabelecimento is None:
                parser.error("gerente exige --estabelecimento")
            est_id = args.estabelecimento if args.role == "gerente" else None
            conn.execute(
                """
                INSERT INTO usuarios (email, nome, role, estabelecimento_id) VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                   SET nome = EXCLUDED.nome, role = EXCLUDED.role,
                       estabelecimento_id = EXCLUDED.estabelecimento_id, ativo = TRUE
                """,
                (email, args.nome, args.role, est_id),
            )
            print(f"Usuário {email} autorizado como {args.role}.")

        elif args.comando == "remover-usuario":
            n = conn.execute(
                "UPDATE usuarios SET ativo = FALSE WHERE email = %s", (args.email.strip().lower(),)
            ).rowcount
            print("Acesso revogado." if n else "Usuário não encontrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
