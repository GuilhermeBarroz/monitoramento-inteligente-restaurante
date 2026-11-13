"""
Máquina de Estados Finitos (FSM) e Gerenciador de Cronômetros por Mesa.
Controla o ciclo de vida de atendimento de cada mesa:
LIVRE -> AGUARDANDO_ATENDIMENTO -> ANOTANDO_PEDIDO -> AGUARDANDO_ENTREGA -> REFEICAO -> PENDENTE_LIMPEZA -> LIVRE
"""

from enum import Enum
import time
from typing import List, Optional


class EstadoMesa(str, Enum):
    LIVRE = "LIVRE"
    AGUARDANDO_ATENDIMENTO = "AGUARDANDO_ATENDIMENTO"
    ANOTANDO_PEDIDO = "ANOTANDO_PEDIDO"
    AGUARDANDO_ENTREGA = "AGUARDANDO_ENTREGA"
    REFEICAO_ANDAMENTO = "REFEICAO_ANDAMENTO"
    PENDENTE_LIMPEZA = "PENDENTE_LIMPEZA"


class TipoAlerta(str, Enum):
    ESPERA_ATENDIMENTO = "ESPERA_ATENDIMENTO"   # Cliente esperando garçom > limite
    ATRASO_COZINHA = "ATRASO_COZINHA"           # Prato demorando > limite
    MESA_SUJA = "MESA_SUJA"                     # Mesa desocupada sem limpar > limite


class MesaFSM:
    def __init__(
        self,
        mesa_id: str,
        limite_espera_garcom: float = 300.0,       # 5 minutos (em segundos)
        limite_cozinha: float = 1200.0,            # 20 minutos
        limite_limpeza: float = 180.0,             # 3 minutos
        limpeza_min_segundos: float = 5.0,         # Tempo mínimo de limpeza rápida sem uniforme
        ocupar_apos_suja_segundos: float = 45.0,   # Tempo contínuo em mesa suja que caracteriza novo cliente
    ):
        self.mesa_id = mesa_id
        self.limite_espera_garcom = limite_espera_garcom
        self.limite_cozinha = limite_cozinha
        self.limite_limpeza = limite_limpeza
        self.limpeza_min_segundos = limpeza_min_segundos
        self.ocupar_apos_suja_segundos = ocupar_apos_suja_segundos

        # Estado atual
        self.estado = EstadoMesa.LIVRE
        self.qtd_pessoas = 0
        self.timestamp_estado = time.time()
        self.tempo_decorrido_estado = 0.0

        # Histórico de tempos para métricas
        self.timestamp_ocupacao_inicial = None
        self.timestamp_inicio_cozinha = None
        self.timestamp_inicio_limpeza = None
        self.tempo_permanencia_atual = 0.0
        self.pico_pessoas_sessao = 0
        self.tempo_espera_atendimento_ultima: Optional[float] = None
        self.tempo_preparo_ultima: Optional[float] = None
        self.houve_alerta_sessao = False

        # Suporte a reversão caso excedente > visita_max_s (ex: chegou mais um cliente)
        self._estado_anterior_atendimento: Optional[EstadoMesa] = None
        self._timestamp_anterior_atendimento: Optional[float] = None
        self._alertas_anteriores: List[str] = []
        self._espera_anterior_atendimento: Optional[float] = None

        # Controle de visita breve de limpeza em PENDENTE_LIMPEZA
        self._limpeza_visita_inicio: Optional[float] = None
        self._limpeza_visita_ultimo: Optional[float] = None

        # Alertas ativos
        self.alertas_ativos: List[str] = []

    def atualizar(
        self,
        pessoas_detectadas: int,
        funcionario_presente: bool,
        timestamp_atual: Optional[float] = None,
        cancelar_visita_falsa: bool = False,
    ) -> List[dict]:
        """
        Atualiza o estado da mesa com base na presença de clientes e garçons.
        Retorna uma lista de novos eventos/alertas gerados neste ciclo.
        """
        t = timestamp_atual if timestamp_atual is not None else time.time()
        self.tempo_decorrido_estado = max(0.0, t - self.timestamp_estado)
        self.qtd_pessoas = pessoas_detectadas
        novos_eventos: List[dict] = []

        tem_clientes = pessoas_detectadas > 0
        if (
            self.estado in {
                EstadoMesa.AGUARDANDO_ATENDIMENTO,
                EstadoMesa.ANOTANDO_PEDIDO,
                EstadoMesa.AGUARDANDO_ENTREGA,
                EstadoMesa.REFEICAO_ANDAMENTO,
            }
            and self.timestamp_ocupacao_inicial is not None
        ):
            self.tempo_permanencia_atual = max(0.0, t - self.timestamp_ocupacao_inicial)
            self.pico_pessoas_sessao = max(self.pico_pessoas_sessao, int(pessoas_detectadas))
        else:
            self.tempo_permanencia_atual = 0.0

        # === TRANSIÇÕES DE ESTADO ===

        # 1. LIVRE -> AGUARDANDO_ATENDIMENTO
        if self.estado == EstadoMesa.LIVRE:
            if tem_clientes:
                self._limpar_auxiliares()
                self._transicionar(EstadoMesa.AGUARDANDO_ATENDIMENTO, t)
                self.timestamp_ocupacao_inicial = t
                self.tempo_permanencia_atual = 0.0
                self.pico_pessoas_sessao = max(1, int(pessoas_detectadas))
                novos_eventos.append({
                    "tipo": "MESA_OCUPADA",
                    "mesa_id": self.mesa_id,
                    "timestamp": t,
                    "qtd_pessoas": pessoas_detectadas,
                })

        # 2. AGUARDANDO_ATENDIMENTO -> ANOTANDO_PEDIDO ou ALERTA
        elif self.estado == EstadoMesa.AGUARDANDO_ATENDIMENTO:
            if not tem_clientes:
                duracao_ocupacao = (
                    t - self.timestamp_ocupacao_inicial
                    if self.timestamp_ocupacao_inicial is not None
                    else self.tempo_decorrido_estado
                )
                self.alertas_ativos.clear()
                if duracao_ocupacao >= 120.0:
                    # Clientes permaneceram tempo suficiente para sujar a mesa
                    evt_desoc = self._emitir_desocupacao(t, duracao_ocupacao)
                    self._transicionar(EstadoMesa.PENDENTE_LIMPEZA, t)
                    self.timestamp_inicio_limpeza = t
                    self.tempo_permanencia_atual = 0.0
                    novos_eventos.append(evt_desoc)
                else:
                    # Clientes desistiram ou saíram rapidamente
                    self._limpar_auxiliares()
                    self._transicionar(EstadoMesa.LIVRE, t)
                    self.tempo_permanencia_atual = 0.0
            elif funcionario_presente:
                espera_garcom = max(0.0, t - (self.timestamp_ocupacao_inicial or t))
                self._estado_anterior_atendimento = self.estado
                self._timestamp_anterior_atendimento = self.timestamp_estado
                self._alertas_anteriores = list(self.alertas_ativos)
                self._espera_anterior_atendimento = self.tempo_espera_atendimento_ultima
                if self.tempo_espera_atendimento_ultima is None:
                    self.tempo_espera_atendimento_ultima = espera_garcom
                self._transicionar(EstadoMesa.ANOTANDO_PEDIDO, t)
                self.alertas_ativos.clear()
                novos_eventos.append({
                    "tipo": "ATENDIMENTO_INICIADO",
                    "mesa_id": self.mesa_id,
                    "tempo_espera_garcom": round(espera_garcom, 1),
                    "timestamp": t,
                })
            else:
                # Checa estouro de tempo (Alerta de espera)
                if self.tempo_decorrido_estado >= self.limite_espera_garcom:
                    if TipoAlerta.ESPERA_ATENDIMENTO not in self.alertas_ativos:
                        self.alertas_ativos.append(TipoAlerta.ESPERA_ATENDIMENTO)
                        self.houve_alerta_sessao = True
                        novos_eventos.append({
                            "tipo": "ALERTA_DISPARADO",
                            "alerta": TipoAlerta.ESPERA_ATENDIMENTO,
                            "mesa_id": self.mesa_id,
                            "tempo_espera": self.tempo_decorrido_estado,
                            "timestamp": t,
                        })

        # 3. ANOTANDO_PEDIDO -> AGUARDANDO_ENTREGA (quando garçom sai) ou reversão se excedente permanente
        elif self.estado == EstadoMesa.ANOTANDO_PEDIDO:
            if cancelar_visita_falsa and self._estado_anterior_atendimento is not None:
                self.estado = self._estado_anterior_atendimento
                self.timestamp_estado = self._timestamp_anterior_atendimento or t
                self.tempo_decorrido_estado = max(0.0, t - self.timestamp_estado)
                self.alertas_ativos = list(self._alertas_anteriores)
                self.tempo_espera_atendimento_ultima = self._espera_anterior_atendimento
                self._estado_anterior_atendimento = None
                self._timestamp_anterior_atendimento = None
                self._alertas_anteriores = []
                self._espera_anterior_atendimento = None
            elif not tem_clientes and not funcionario_presente:
                evt_desoc = self._emitir_desocupacao(t)
                self._transicionar(EstadoMesa.PENDENTE_LIMPEZA, t)
                self.timestamp_inicio_limpeza = t
                self.tempo_permanencia_atual = 0.0
                novos_eventos.append(evt_desoc)
            elif not funcionario_presente:
                self._estado_anterior_atendimento = None
                self._timestamp_anterior_atendimento = None
                self._alertas_anteriores = []
                self._espera_anterior_atendimento = None
                self._transicionar(EstadoMesa.AGUARDANDO_ENTREGA, t)
                self.timestamp_inicio_cozinha = t
                novos_eventos.append({
                    "tipo": "PEDIDO_ANOTADO",
                    "mesa_id": self.mesa_id,
                    "timestamp": t,
                })

        # 4. AGUARDANDO_ENTREGA -> REFEICAO ou ALERTA COZINHA
        elif self.estado == EstadoMesa.AGUARDANDO_ENTREGA:
            if not tem_clientes:
                self.alertas_ativos.clear()
                evt_desoc = self._emitir_desocupacao(t)
                self._transicionar(EstadoMesa.PENDENTE_LIMPEZA, t)
                self.timestamp_inicio_limpeza = t
                self.tempo_permanencia_atual = 0.0
                novos_eventos.append(evt_desoc)
            elif funcionario_presente and self.tempo_decorrido_estado > 15.0:
                # Filtro anti-falso positivo: só considera entrega após janela mínima
                tempo_preparo = self.tempo_decorrido_estado
                self.tempo_preparo_ultima = tempo_preparo
                self._transicionar(EstadoMesa.REFEICAO_ANDAMENTO, t)
                self.alertas_ativos.clear()
                novos_eventos.append({
                    "tipo": "PEDIDO_ENTREGUE",
                    "mesa_id": self.mesa_id,
                    "tempo_preparo": tempo_preparo,
                    "timestamp": t,
                })
            else:
                if self.tempo_decorrido_estado >= self.limite_cozinha:
                    if TipoAlerta.ATRASO_COZINHA not in self.alertas_ativos:
                        self.alertas_ativos.append(TipoAlerta.ATRASO_COZINHA)
                        self.houve_alerta_sessao = True
                        novos_eventos.append({
                            "tipo": "ALERTA_DISPARADO",
                            "alerta": TipoAlerta.ATRASO_COZINHA,
                            "mesa_id": self.mesa_id,
                            "tempo_cozinha": self.tempo_decorrido_estado,
                            "timestamp": t,
                        })

        # 5. REFEICAO_ANDAMENTO -> PENDENTE_LIMPEZA
        elif self.estado == EstadoMesa.REFEICAO_ANDAMENTO:
            if not tem_clientes:
                evt_desoc = self._emitir_desocupacao(t)
                self._transicionar(EstadoMesa.PENDENTE_LIMPEZA, t)
                self.timestamp_inicio_limpeza = t
                self.tempo_permanencia_atual = 0.0
                novos_eventos.append(evt_desoc)

        # 6. PENDENTE_LIMPEZA -> LIVRE (higienizada) ou SENTOU_SEM_LIMPEZA
        elif self.estado == EstadoMesa.PENDENTE_LIMPEZA:
            if funcionario_presente:
                tempo_limpeza = self.tempo_decorrido_estado
                self._limpar_auxiliares()
                self.alertas_ativos.clear()
                self._transicionar(EstadoMesa.LIVRE, t)
                novos_eventos.append({
                    "tipo": "MESA_HIGIENIZADA",
                    "mesa_id": self.mesa_id,
                    "tempo_limpeza": round(tempo_limpeza, 1),
                    "timestamp": t,
                })
            elif tem_clientes:
                if self._limpeza_visita_inicio is None:
                    self._limpeza_visita_inicio = t
                self._limpeza_visita_ultimo = t

                if (t - self._limpeza_visita_inicio) >= self.ocupar_apos_suja_segundos:
                    self._limpar_auxiliares()
                    self.alertas_ativos.clear()
                    self._transicionar(EstadoMesa.AGUARDANDO_ATENDIMENTO, t)
                    self.timestamp_ocupacao_inicial = t
                    self.pico_pessoas_sessao = max(1, int(pessoas_detectadas))
                    novos_eventos.append({
                        "tipo": "SENTOU_SEM_LIMPEZA",
                        "mesa_id": self.mesa_id,
                        "timestamp": t,
                        "qtd_pessoas": pessoas_detectadas,
                    })
            else:
                if self._limpeza_visita_inicio is not None and self._limpeza_visita_ultimo is not None:
                    dur_visita = self._limpeza_visita_ultimo - self._limpeza_visita_inicio
                    self._limpeza_visita_inicio = None
                    self._limpeza_visita_ultimo = None
                    if dur_visita >= self.limpeza_min_segundos:
                        tempo_limpeza = self.tempo_decorrido_estado
                        self._limpar_auxiliares()
                        self.alertas_ativos.clear()
                        self._transicionar(EstadoMesa.LIVRE, t)
                        novos_eventos.append({
                            "tipo": "MESA_HIGIENIZADA",
                            "mesa_id": self.mesa_id,
                            "tempo_limpeza": round(tempo_limpeza, 1),
                            "timestamp": t,
                        })
                        return novos_eventos

                if self.tempo_decorrido_estado >= self.limite_limpeza:
                    if TipoAlerta.MESA_SUJA not in self.alertas_ativos:
                        self.alertas_ativos.append(TipoAlerta.MESA_SUJA)
                        novos_eventos.append({
                            "tipo": "ALERTA_DISPARADO",
                            "alerta": TipoAlerta.MESA_SUJA,
                            "mesa_id": self.mesa_id,
                            "tempo_espera_limpeza": self.tempo_decorrido_estado,
                            "timestamp": t,
                        })

        return novos_eventos

    def _emitir_desocupacao(self, t: float, duracao_forcada: Optional[float] = None) -> dict:
        duracao = (
            duracao_forcada
            if duracao_forcada is not None
            else (
                max(0.0, t - self.timestamp_ocupacao_inicial)
                if self.timestamp_ocupacao_inicial is not None
                else self.tempo_decorrido_estado
            )
        )
        return {
            "tipo": "MESA_DESOCUPADA",
            "mesa_id": self.mesa_id,
            "timestamp": t,
            "duracao_permanencia": round(duracao, 1),
            "espera_garcom": round(self.tempo_espera_atendimento_ultima, 1)
            if self.tempo_espera_atendimento_ultima is not None
            else None,
            "tempo_preparo": round(self.tempo_preparo_ultima, 1)
            if self.tempo_preparo_ultima is not None
            else None,
            "qtd_pessoas_pico": max(1, int(self.pico_pessoas_sessao)),
            "houve_alerta": bool(self.houve_alerta_sessao),
        }

    def _limpar_auxiliares(self):
        self._limpeza_visita_inicio = None
        self._limpeza_visita_ultimo = None
        self._estado_anterior_atendimento = None
        self._timestamp_anterior_atendimento = None
        self._alertas_anteriores = []
        self._espera_anterior_atendimento = None
        self.pico_pessoas_sessao = 0
        self.tempo_espera_atendimento_ultima = None
        self.tempo_preparo_ultima = None
        self.houve_alerta_sessao = False

    def _transicionar(self, novo_estado: EstadoMesa, timestamp: float):
        self.estado = novo_estado
        self.timestamp_estado = timestamp
        self.tempo_decorrido_estado = 0.0

    def obter_status_dict(self) -> dict:
        """Retorna snapshot do status da mesa para a interface gráfica."""
        return {
            "mesa_id": self.mesa_id,
            "estado": self.estado.value,
            "tempo_segundos": round(self.tempo_decorrido_estado, 1),
            "tempo_permanencia_segundos": round(self.tempo_permanencia_atual, 1),
            "qtd_pessoas": self.qtd_pessoas,
            "alertas": list(self.alertas_ativos),
        }
