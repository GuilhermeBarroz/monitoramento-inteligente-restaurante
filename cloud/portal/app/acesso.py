"""
Autorização do portal: quem entrou com o Google pode ver o quê.

Fluxo: identidade do Google (e-mail VERIFICADO) → allowlist `usuarios` (ativo) →
lojas permitidas (admin: todas; gerente: só as vinculadas e ativas) → cada consulta
de relatório usa apenas um id que esteja nessa lista.

O perfil é recarregado do banco a cada execução da página (revogar um usuário ou um
vínculo vale no próximo clique, sem esperar a sessão expirar).
"""

from dataclasses import dataclass
import re
from typing import Any, Mapping, Optional, Tuple

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACOES_AUDITADAS = {"login", "acesso_negado", "ver_relatorios", "exportar", "ver_saude"}


@dataclass(frozen=True)
class Loja:
    id: int
    nome: str
    timezone: str
    ativo: bool


@dataclass(frozen=True)
class Perfil:
    email: str
    nome: Optional[str]
    role: str
    lojas: Tuple[Loja, ...]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def email_da_identidade(usuario: Mapping[str, Any]) -> Optional[str]:
    """
    E-mail normalizado do usuário logado, ou None se não houver login ou se o Google
    não confirmar o e-mail (`email_verified` precisa ser exatamente True).
    """
    if not usuario.get("is_logged_in"):
        return None
    if usuario.get("email_verified") is not True:
        return None
    email = usuario.get("email")
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return None
    return email


def _loja(r) -> Loja:
    return Loja(id=int(r["id"]), nome=str(r["nome"]), timezone=str(r["timezone"]), ativo=bool(r["ativo"]))


def carregar_perfil(conn, email: str) -> Optional[Perfil]:
    """Perfil autorizado ou None (fora da allowlist ou desativado)."""
    u = conn.execute(
        "SELECT email, nome, role FROM usuarios WHERE email = %(email)s AND ativo",
        {"email": email},
    ).fetchone()
    if u is None:
        return None
    if u["role"] == "admin":
        # Admin vê também lojas desativadas (histórico, suporte)
        rows = conn.execute(
            "SELECT id, nome, timezone, ativo FROM estabelecimentos_portal ORDER BY nome, id"
        ).fetchall()
    elif u["role"] == "gerente":
        rows = conn.execute(
            """
            SELECT e.id, e.nome, e.timezone, e.ativo
              FROM estabelecimentos_portal e
              JOIN usuario_estabelecimentos ue ON ue.estabelecimento_id = e.id
             WHERE ue.email = %(email)s AND e.ativo
             ORDER BY e.nome, e.id
            """,
            {"email": email},
        ).fetchall()
    else:
        return None
    return Perfil(email=u["email"], nome=u["nome"], role=u["role"], lojas=tuple(_loja(r) for r in rows))


def loja_autorizada(perfil: Perfil, est_id: Any) -> Optional[Loja]:
    """A loja pedida, somente se estiver no escopo do perfil."""
    try:
        alvo = int(est_id)
    except (TypeError, ValueError):
        return None
    for loja in perfil.lojas:
        if loja.id == alvo:
            return loja
    return None


def registrar_acesso(conn, email: str, acao: str, est_id: Optional[int] = None) -> None:
    """Trilha de auditoria (LGPD). Falha de auditoria não derruba a página."""
    if acao not in ACOES_AUDITADAS:
        raise ValueError(f"Ação de auditoria desconhecida: {acao}")
    try:
        conn.execute(
            "INSERT INTO portal_acessos (email, estabelecimento_id, acao) VALUES (%(email)s, %(est)s, %(acao)s)",
            {"email": email[:254], "est": est_id, "acao": acao},
        )
    except Exception as e:  # noqa: BLE001 - auditoria é melhor esforço, mas deixa rastro no log
        print(f"AVISO: falha ao registrar auditoria ({acao}): {type(e).__name__}")


def registrar_login(conn, email: str) -> None:
    try:
        conn.execute("UPDATE usuarios SET ultimo_login_em = now() WHERE email = %(email)s", {"email": email})
    except Exception as e:  # noqa: BLE001
        print(f"AVISO: falha ao atualizar ultimo_login_em: {type(e).__name__}")
    registrar_acesso(conn, email, "login")
