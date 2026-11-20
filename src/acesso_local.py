"""
Controle de acesso do painel local (Mini PC).

O painel local é usado pela equipe do salão (alertas e mesas ao vivo, abertos na
rede local) e pelo técnico/gerente (câmeras, imagens ao vivo, calibração, regras e
relatórios, protegidos por PIN). Não substitui o isolamento de rede: o painel só
deve ser alcançável pela LAN do restaurante e pelo Tailscale do suporte.

Regras:
- PIN em PAINEL_ADMIN_PIN (arquivo .env, gerado pelo install.sh). Sem PIN
  configurado, as áreas de administração ficam FECHADAS (falha segura).
- Comparação em tempo constante (hmac.compare_digest).
- Após MAX_FALHAS erros de um mesmo IP em JANELA_FALHAS_S, o IP fica bloqueado
  por BLOQUEIO_S. O contador é do processo do painel (vale para todas as sessões).
- Sessão de administrador expira após SESSAO_OCIOSA_S sem interação.
"""

import hmac
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

PIN_MIN_CARACTERES = 6
MAX_FALHAS = 5
JANELA_FALHAS_S = 600.0
BLOQUEIO_S = 600.0
# Limite global (todas as origens): contém quem troca de IP na LAN para continuar tentando
MAX_FALHAS_GLOBAL = 30
JANELA_GLOBAL_S = 3600.0
BLOQUEIO_GLOBAL_S = 300.0
SESSAO_OCIOSA_S = 1800.0


def pin_configurado(env: Optional[dict] = None) -> Optional[str]:
    """Retorna o PIN de administrador se configurado e com tamanho mínimo; senão None."""
    valor = (env if env is not None else os.environ).get("PAINEL_ADMIN_PIN", "").strip()
    if len(valor) < PIN_MIN_CARACTERES:
        return None
    return valor


def pin_fraco(pin: Optional[str]) -> bool:
    """PIN trivial: todos os caracteres iguais ou sequência numérica (123456, 987654...)."""
    if not pin:
        return False
    if len(set(pin)) == 1:
        return True
    if pin.isdigit():
        passos = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
        if passos in ({1}, {-1}):
            return True
    return False


class LimitadorTentativas:
    """
    Conta tentativas de PIN por IP (e no total) e bloqueia temporariamente após
    excesso. A tentativa é contada ANTES da comparação, sob lock: várias sessões
    em paralelo do mesmo IP não conseguem chutes extras. Sucesso desfaz a contagem.
    """

    def __init__(
        self,
        max_falhas: int = MAX_FALHAS,
        janela_s: float = JANELA_FALHAS_S,
        bloqueio_s: float = BLOQUEIO_S,
        max_falhas_global: int = MAX_FALHAS_GLOBAL,
        janela_global_s: float = JANELA_GLOBAL_S,
        bloqueio_global_s: float = BLOQUEIO_GLOBAL_S,
    ):
        self.max_falhas = max_falhas
        self.janela_s = janela_s
        self.bloqueio_s = bloqueio_s
        self.max_falhas_global = max_falhas_global
        self.janela_global_s = janela_global_s
        self.bloqueio_global_s = bloqueio_global_s
        self._falhas: Dict[str, List[float]] = {}
        self._bloqueado_ate: Dict[str, float] = {}
        self._falhas_global: List[float] = []
        self._bloqueio_global_ate = 0.0
        self._lock = threading.Lock()

    def segundos_bloqueado(self, ip: str, agora: Optional[float] = None) -> float:
        agora = time.monotonic() if agora is None else agora
        with self._lock:
            return self._restante(ip, agora)

    def _restante(self, ip: str, agora: float) -> float:
        fim = max(self._bloqueado_ate.get(ip, 0.0), self._bloqueio_global_ate)
        return max(0.0, fim - agora)

    def _podar(self, agora: float) -> None:
        """Remove registros vencidos (evita crescer sem limite com muitos IPs)."""
        for ip in list(self._falhas):
            recentes = [t for t in self._falhas[ip] if agora - t < self.janela_s]
            if recentes:
                self._falhas[ip] = recentes
            else:
                del self._falhas[ip]
        for ip in [ip for ip, fim in self._bloqueado_ate.items() if fim <= agora]:
            del self._bloqueado_ate[ip]
        self._falhas_global = [t for t in self._falhas_global if agora - t < self.janela_global_s]

    def consumir_tentativa(self, ip: str, agora: Optional[float] = None) -> float:
        """
        Se o IP (ou o painel todo) estiver bloqueado, retorna os segundos restantes
        sem consumir. Senão, conta a tentativa como falha provisória e retorna 0.
        """
        agora = time.monotonic() if agora is None else agora
        with self._lock:
            self._podar(agora)
            restante = self._restante(ip, agora)
            if restante > 0:
                return restante
            self._falhas.setdefault(ip, []).append(agora)
            self._falhas_global.append(agora)
            if len(self._falhas[ip]) >= self.max_falhas:
                self._bloqueado_ate[ip] = agora + self.bloqueio_s
                self._falhas[ip] = []
            if len(self._falhas_global) >= self.max_falhas_global:
                self._bloqueio_global_ate = agora + self.bloqueio_global_s
                self._falhas_global = []
            return 0.0

    def registrar_falha(self, ip: str, agora: Optional[float] = None) -> None:
        """Compatibilidade: conta uma falha (equivale a consumir uma tentativa)."""
        self.consumir_tentativa(ip, agora)

    def registrar_sucesso(self, ip: str) -> None:
        with self._lock:
            self._falhas.pop(ip, None)
            self._bloqueado_ate.pop(ip, None)
            if self._falhas_global:
                self._falhas_global.pop()  # a tentativa correta não conta como falha


def verificar_pin(
    tentativa: str,
    ip: str,
    limitador: LimitadorTentativas,
    pin_correto: Optional[str],
    agora: Optional[float] = None,
) -> Tuple[bool, str]:
    """Valida o PIN informado. Retorna (ok, mensagem para o usuário)."""
    if pin_correto is None:
        return False, "Acesso administrativo desativado: defina PAINEL_ADMIN_PIN (mínimo 6 caracteres) no arquivo .env e reinicie o painel."
    restante = limitador.consumir_tentativa(ip, agora)
    if restante > 0:
        return False, f"Muitas tentativas. Tente novamente em {int(restante // 60) + 1} min."
    if hmac.compare_digest(str(tentativa or "").strip().encode("utf-8"), pin_correto.encode("utf-8")):
        limitador.registrar_sucesso(ip)
        return True, "Acesso de administrador liberado."
    return False, "PIN incorreto."


def sessao_admin_ativa(estado: dict, agora: Optional[float] = None, renovar: bool = True) -> bool:
    """
    `estado` é o session_state da sessão. Encerra após SESSAO_OCIOSA_S sem uso.
    `renovar=True` conta como interação (execução completa da página);
    use `renovar=False` em fragmentos com atualização automática, para que a
    sessão expire mesmo com uma aba de imagens ao vivo aberta e esquecida.
    """
    agora = time.time() if agora is None else agora
    ultimo = estado.get("_admin_ultima_atividade")
    if not estado.get("_admin_ok") or ultimo is None:
        return False
    if agora - float(ultimo) > SESSAO_OCIOSA_S:
        encerrar_sessao_admin(estado)
        return False
    if renovar:
        estado["_admin_ultima_atividade"] = agora
    return True


def iniciar_sessao_admin(estado: dict, agora: Optional[float] = None) -> None:
    estado["_admin_ok"] = True
    estado["_admin_ultima_atividade"] = time.time() if agora is None else agora


def encerrar_sessao_admin(estado: dict) -> None:
    estado["_admin_ok"] = False
    estado.pop("_admin_ultima_atividade", None)
