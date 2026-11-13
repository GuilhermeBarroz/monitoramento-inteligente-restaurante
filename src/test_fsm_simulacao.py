"""
Simulação determinística offline da Máquina de Estados (FSM), detecção de garçom,
limpeza breve e reconhecimento de uniformes em HSV (sem câmera e sem pesos YOLO).
"""

import sys
from pathlib import Path
import numpy as np

sys.path.append(str(Path(__file__).parent))
from fsm_mesas import MesaFSM, EstadoMesa, TipoAlerta
from detector_garcom import AnalisadorUniformeHSV, RastreadorAtendimentoMesa

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def _rodar_tick(fsm: MesaFSM, rastreador: RastreadorAtendimentoMesa, t: float, qtd_clientes: int,
                garcom_uniforme: bool = False, usar_excedente: bool = True):
    func, cancelar = rastreador.avaliar(
        estado_valor=fsm.estado.value,
        qtd_clientes=qtd_clientes,
        garcom_uniforme=garcom_uniforme,
        timestamp=t,
        usar_excedente=usar_excedente,
    )
    return fsm.atualizar(
        pessoas_detectadas=qtd_clientes,
        funcionario_presente=func,
        timestamp_atual=t,
        cancelar_visita_falsa=cancelar,
    )


def testar_ciclo_completo_com_excedente_e_limpeza():
    fsm = MesaFSM("Mesa Teste 1", limite_espera_garcom=300.0, limite_cozinha=1200.0,
                  limite_limpeza=180.0, limpeza_min_segundos=5.0, ocupar_apos_suja_segundos=45.0)
    rast = RastreadorAtendimentoMesa(visita_min_s=4.0, visita_max_s=180.0, min_amostras_base=4)
    eventos = []

    for t in range(0, 600):
        # 0..9: livre
        if t < 10:
            n = 0
        # 10..29: 2 clientes chegam e estabilizam
        elif t < 30:
            n = 2
        # 30..40: garçom chega por 10s (excedente = 3)
        elif t < 40:
            n = 3
        # 40..109: garçom sai -> cozinha preparando
        elif t < 110:
            n = 2
        # 110..118: garçom entrega prato (>15s após ANOTANDO_PEDIDO)
        elif t < 118:
            n = 3
        # 118..299: clientes fazendo refeição
        elif t < 300:
            n = 2
        # 300..319: mesa vazia (pendente limpeza)
        elif t < 320:
            n = 0
        # 320..328: funcionário limpa por 8s sem uniforme
        elif t <= 328:
            n = 1
        # 329..: mesa vazia novamente (limpa!)
        else:
            n = 0

        eventos.extend(_rodar_tick(fsm, rast, float(t), n))

    tipos = [e["tipo"] for e in eventos]
    esperado = [
        "MESA_OCUPADA",
        "ATENDIMENTO_INICIADO",
        "PEDIDO_ANOTADO",
        "PEDIDO_ENTREGUE",
        "MESA_DESOCUPADA",
        "MESA_HIGIENIZADA",
    ]
    for item in esperado:
        assert item in tipos, f"Evento esperado '{item}' não ocorreu. Obtidos: {tipos}"
    assert fsm.estado == EstadoMesa.LIVRE, f"Estado final deveria ser LIVRE, foi {fsm.estado}"


def testar_passante_rapido_nao_zera_espera():
    fsm = MesaFSM("Mesa Corredor", limite_espera_garcom=60.0)
    rast = RastreadorAtendimentoMesa(visita_min_s=4.0, min_amostras_base=4)
    eventos = []

    for t in range(0, 90):
        # 2 clientes chegam no t=0, estabilizam e passante cruza por 2s em t=20..21
        n = 3 if 20 <= t <= 21 else 2
        eventos.extend(_rodar_tick(fsm, rast, float(t), n))

    tipos = [e["tipo"] for e in eventos]
    assert "ATENDIMENTO_INICIADO" not in tipos, f"Passante rápido disparou atendimento falso: {eventos}"
    assert any(e.get("alerta") == TipoAlerta.ESPERA_ATENDIMENTO for e in eventos), "Alerta de espera não disparou"
    assert fsm.estado == EstadoMesa.AGUARDANDO_ATENDIMENTO


def testar_amigo_atrasado_reverte_falso_atendimento():
    fsm = MesaFSM("Mesa Grupo", limite_espera_garcom=300.0)
    rast = RastreadorAtendimentoMesa(visita_min_s=4.0, visita_max_s=180.0, min_amostras_base=4)
    eventos = []

    for t in range(0, 220):
        # 0..19: 2 clientes; t>=20: 3º cliente chega e fica permanentemente (>180s)
        n = 2 if t < 20 else 3
        eventos.extend(_rodar_tick(fsm, rast, float(t), n))

    tipos = [e["tipo"] for e in eventos]
    assert "PEDIDO_ANOTADO" not in tipos, f"Amigo atrasado não poderia gerar PEDIDO_ANOTADO: {tipos}"
    assert fsm.estado == EstadoMesa.AGUARDANDO_ATENDIMENTO, f"Deveria reverter para AGUARDANDO_ATENDIMENTO, foi {fsm.estado}"
    assert rast.base_atual == 3, f"Base deveria atualizar para 3, ficou {rast.base_atual}"


def testar_sentou_sem_limpeza_em_mesa_suja():
    fsm = MesaFSM("Mesa Suja", limite_limpeza=180.0, limpeza_min_segundos=5.0, ocupar_apos_suja_segundos=45.0)
    rast = RastreadorAtendimentoMesa(min_amostras_base=4)
    eventos = []

    # Força estado inicial em PENDENTE_LIMPEZA
    fsm.estado = EstadoMesa.PENDENTE_LIMPEZA
    fsm.timestamp_estado = 0.0

    for t in range(1, 60):
        eventos.extend(_rodar_tick(fsm, rast, float(t), qtd_clientes=2))

    tipos = [e["tipo"] for e in eventos]
    assert "SENTOU_SEM_LIMPEZA" in tipos, f"Deveria disparar SENTOU_SEM_LIMPEZA: {tipos}"
    assert fsm.estado == EstadoMesa.AGUARDANDO_ATENDIMENTO


def testar_analisador_uniforme_hsv():
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    # Caixa azul (BGR: 220, 40, 20) vs caixa vermelha (BGR: 20, 20, 220) vs cinza neutro (128, 128, 128)
    frame[10:150, 10:70] = (220, 40, 20)    # Azul
    frame[10:150, 80:140] = (20, 20, 220)   # Vermelho
    frame[10:150, 145:195] = (128, 128, 128)  # Cinza neutro

    analisador_azul = AnalisadorUniformeHSV("Azul", ratio_min=0.25)
    ok_azul, r_azul = analisador_azul.verificar_pessoa(frame, (10, 10, 70, 150))
    ok_neutro, _ = analisador_azul.verificar_pessoa(frame, (145, 10, 195, 150))
    assert ok_azul and r_azul > 0.9, f"Azul deveria bater, obteve {r_azul}"
    assert not ok_neutro, "Cinza neutro não deveria bater como Azul"

    analisador_verm = AnalisadorUniformeHSV("Vermelho", ratio_min=0.25)
    ok_verm, r_verm = analisador_verm.verificar_pessoa(frame, (80, 10, 140, 150))
    assert ok_verm and r_verm > 0.9, f"Vermelho deveria bater, obteve {r_verm}"


def executar_todos():
    testes = [
        testar_ciclo_completo_com_excedente_e_limpeza,
        testar_passante_rapido_nao_zera_espera,
        testar_amigo_atrasado_reverte_falso_atendimento,
        testar_sentou_sem_limpeza_em_mesa_suja,
        testar_analisador_uniforme_hsv,
    ]
    for fn in testes:
        fn()
        print(f"✅ PASSOU: {fn.__name__}")
    print("\nTESTE OK: todas as simulações de garçom, limpeza e uniforme HSV passaram!")


if __name__ == "__main__":
    executar_todos()

