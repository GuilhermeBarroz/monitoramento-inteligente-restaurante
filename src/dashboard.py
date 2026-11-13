"""
Dashboard Operacional em Tempo Real para Gestão de Mesas com IA (Fase 6+).
Totalmente sincronizado com o Motor de IA (src/motor_estados.py):
- Lê o estado real de cada mesa em 'data/estado_atual.json' (Zero dados fictícios)
- Exibe o frame processado AO VIVO em 'data/videos/frame_ao_vivo.jpg'
- Atualiza automaticamente a cada 2 segundos via @st.fragment
- Permite ajustar limites operacionais dinamicamente em 'data/regras_config.json'
"""

import sys
import json
import os
import base64
import html
from pathlib import Path
from typing import Optional
import cv2
import numpy as np
import streamlit as st
import pandas as pd

# Ajusta path para importar módulos locais
sys.path.append(str(Path(__file__).parent))
from analytics_db import (
    DB_PATH_PADRAO,
    DIAS_SEMANA_PT,
    RepositorioAnalytics,
    calcular_previsao_liberacao,
    formatar_duracao_curta,
)
from fsm_mesas import EstadoMesa
from detector_garcom import PRESETS_UNIFORME_HSV
from config_mesas import salvar_configuracao_mesas
from config_cameras import (
    caminhos_derivados_camera,
    carregar_cameras_config,
    extrair_frame_limpo_fonte,
    listar_videos_disponiveis,
    mascarar_fonte_segura,
    sanitizar_id_camera,
    salvar_cameras_config,
    salvar_upload_video,
)
from zone_editor_component import renderizar_editor_zonas

CONFIG_MESAS_PATH = Path("data/mesas_config.json")
CONFIG_REGRAS_PATH = Path("data/regras_config.json")
ESTADO_ATUAL_PATH = Path("data/estado_atual.json")
LOG_EVENTOS_PATH = Path("data/alertas_log.json")
HISTORICO_DB_PATH = DB_PATH_PADRAO
FRAME_AO_VIVO_PATH = Path("data/videos/frame_ao_vivo.jpg")
FRAME_LIMPO_PATH = Path("data/videos/frame_limpo.jpg")
FRAME_REF_PATH = Path("data/videos/frame_referencia.jpg")
PREVIEW_IMG_PATH = Path("data/videos/calibracao_mesas_preview.jpg")

# Configuração da página Streamlit
st.set_page_config(
    page_title="Monitor de Mesas com IA | Restaurante",
    page_icon="🍽️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilos CSS personalizados
st.markdown("""
<style>
    .status-badge {
        font-weight: bold;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.85rem;
        display: inline-block;
        margin-bottom: 8px;
    }
    .badge-livre { background-color: #1e7e34; color: white; }
    .badge-aguardando { background-color: #bd2130; color: white; }
    .badge-atendendo { background-color: #d39e00; color: black; }
    .badge-cozinha { background-color: #117a8b; color: white; }
    .badge-refeicao { background-color: #6f42c1; color: white; }
    .badge-limpeza { background-color: #fd7e14; color: white; }
</style>
""", unsafe_allow_html=True)

def carregar_regras() -> dict:
    if CONFIG_REGRAS_PATH.exists():
        try:
            with open(CONFIG_REGRAS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "limite_espera_garcom_minutos": 5.0,
        "limite_cozinha_minutos": 20.0,
        "limite_limpeza_minutos": 3.0,
        "alerta_sonoro": True
    }

def salvar_regras(regras: dict):
    CONFIG_REGRAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_REGRAS_PATH.with_suffix(".tmp.json")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(regras, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CONFIG_REGRAS_PATH)

def carregar_mesas(mesas_path: Path = CONFIG_MESAS_PATH) -> list:
    if Path(mesas_path).exists():
        try:
            with open(mesas_path, "r", encoding="utf-8") as f:
                return json.load(f).get("mesas", [])
        except Exception:
            pass
    return []

def carregar_estado_ao_vivo() -> dict:
    """Lê o estado real e sincronizado gravado pelo Motor de IA."""
    if ESTADO_ATUAL_PATH.exists():
        try:
            with open(ESTADO_ATUAL_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def carregar_historico_eventos() -> list:
    if LOG_EVENTOS_PATH.exists():
        try:
            with open(LOG_EVENTOS_PATH, "r", encoding="utf-8") as f:
                dados = json.load(f)
                return dados.get("eventos", [])
        except Exception:
            pass
    return []

# === BARRA LATERAL (CONFIGURAÇÕES E FILTROS) ===
st.sidebar.title("⚙️ Painel de Controle")
st.sidebar.caption("Configurações dos Limites Operacionais")

regras = carregar_regras()

novo_limite_espera = st.sidebar.slider(
    "⏱️ Tempo Limite de Espera (Garçom)",
    min_value=0.5, max_value=15.0,
    value=float(regras.get("limite_espera_garcom_minutos", 5.0)),
    step=0.5,
    help="Tempo máximo (em minutos) que um cliente pode aguardar atendimento."
)

novo_limite_cozinha = st.sidebar.slider(
    "🍳 Tempo Limite de Cozinha / Entrega",
    min_value=5.0, max_value=45.0,
    value=float(regras.get("limite_cozinha_minutos", 20.0)),
    step=1.0,
    help="Tempo máximo (em minutos) entre pedido e entrega na mesa."
)

novo_limite_limpeza = st.sidebar.slider(
    "🧹 Tempo Limite de Limpeza (Giro)",
    min_value=1.0, max_value=10.0,
    value=float(regras.get("limite_limpeza_minutos", 3.0)),
    step=0.5,
    help="Tempo máximo (em minutos) para higienizar uma mesa vazia."
)

alerta_sonoro = st.sidebar.checkbox("🔔 Alerta Sonoro no Caixa", value=regras.get("alerta_sonoro", True))

with st.sidebar.expander("👔 Garçom e Limpeza Automática"):
    deteccao_excedente = st.checkbox(
        "Detectar garçom por excedente na mesa",
        value=bool(regras.get("deteccao_garcom_excedente", True)),
        help="Identifica visita de garçom quando entra mais alguém na mesa acima da base estável.",
    )
    visita_min_seg = st.number_input(
        "Tempo mín. visita de garçom (s)",
        min_value=1.0, max_value=30.0,
        value=float(regras.get("visita_min_segundos", 4.0)),
        step=0.5,
        help="Filtra passantes rápidos no corredor.",
    )
    limpeza_min_seg = st.number_input(
        "Tempo mín. visita de limpeza (s)",
        min_value=1.0, max_value=30.0,
        value=float(regras.get("limpeza_min_segundos", 5.0)),
        step=0.5,
        help="Tempo breve de higienização na mesa vazia antes de liberá-la.",
    )
    uniforme_ativo = st.checkbox(
        "Reconhecer uniforme da equipe (HSV)",
        value=bool(regras.get("uniforme_ativo", False)),
        help="Usa cor da camisa/avental no tórax para distinguir garçons de clientes.",
    )
    presets_cores = list(PRESETS_UNIFORME_HSV.keys())
    cor_atual = str(regras.get("uniforme_cor_preset", "Preto / Escuro"))
    idx_cor = presets_cores.index(cor_atual) if cor_atual in presets_cores else 0
    uniforme_cor = st.selectbox("Cor do uniforme / avental", presets_cores, index=idx_cor, disabled=not uniforme_ativo)
    uniforme_margem = st.slider(
        "Margem ao redor da mesa (px)",
        min_value=0, max_value=80,
        value=int(regras.get("uniforme_margem_px", 35)),
        step=5,
        disabled=not uniforme_ativo,
    )
    uniforme_ratio = st.slider(
        "Sensibilidade cor uniforme (%)",
        min_value=10, max_value=60,
        value=int(round(float(regras.get("uniforme_ratio_min", 0.25)) * 100)),
        step=5,
        disabled=not uniforme_ativo,
    ) / 100.0

with st.sidebar.expander("📊 Previsão de Giro & Equipe"):
    permanencia_padrao_min = st.slider(
        "Permanência padrão inicial (min)",
        min_value=15.0,
        max_value=120.0,
        value=float(regras.get("permanencia_padrao_minutos", 45.0)),
        step=5.0,
        help="Usado para prever liberação de mesas antes de completar 3 visitas reais na mesa.",
    )
    mesas_por_garcom = st.number_input(
        "Mesas por garçom (escala)",
        min_value=1,
        max_value=12,
        value=int(regras.get("mesas_por_garcom", 4)),
        step=1,
        help="Base usada para recomendar quantidade de garçons nos horários de pico.",
    )

if st.sidebar.button("💾 Salvar Alterações"):
    regras_atualizadas = {
        **regras,
        "limite_espera_garcom_minutos": novo_limite_espera,
        "limite_cozinha_minutos": novo_limite_cozinha,
        "limite_limpeza_minutos": novo_limite_limpeza,
        "alerta_sonoro": alerta_sonoro,
        "deteccao_garcom_excedente": deteccao_excedente,
        "visita_min_segundos": float(visita_min_seg),
        "limpeza_min_segundos": float(limpeza_min_seg),
        "uniforme_ativo": uniforme_ativo,
        "uniforme_cor_preset": uniforme_cor,
        "uniforme_margem_px": int(uniforme_margem),
        "uniforme_ratio_min": float(uniforme_ratio),
        "permanencia_padrao_minutos": float(permanencia_padrao_min),
        "mesas_por_garcom": int(mesas_por_garcom),
    }
    salvar_regras(regras_atualizadas)
    st.sidebar.success("Regras atualizadas! O motor aplicará imediatamente.")

st.sidebar.divider()
auto_refresh = st.sidebar.toggle("🔄 Atualização em Tempo Real (2s)", value=True)
st.sidebar.info("💡 **Status:** O relatório abaixo reflete exatamente o frame processado pela câmera de IA.")

# === CABEÇALHO PRINCIPAL ===
st.title("🍽️ Monitoramento de Salão com IA")
st.caption("Visão Computacional e Inteligência Operacional Sincronizada em Tempo Real")

aba_visao_geral, aba_camera, aba_relatorios, aba_historico, aba_editor, aba_cameras = st.tabs([
    "🏠 Salão Ao Vivo",
    "📹 Câmeras Ao Vivo",
    "📊 Relatórios",
    "📜 Eventos",
    "🎨 Editar Mesas",
    "🎥 Câmeras e Vídeos",
])

classes_badge = {
    EstadoMesa.LIVRE.value: ("badge-livre", "🟢 Livre"),
    EstadoMesa.AGUARDANDO_ATENDIMENTO.value: ("badge-aguardando", "🔴 Ocupada / Aguardando Garçom"),
    EstadoMesa.ANOTANDO_PEDIDO.value: ("badge-atendendo", "🟡 Anotando Pedido"),
    EstadoMesa.AGUARDANDO_ENTREGA.value: ("badge-cozinha", "🔵 Preparo Cozinha"),
    EstadoMesa.REFEICAO_ANDAMENTO.value: ("badge-refeicao", "🍽️ Em Refeição"),
    EstadoMesa.PENDENTE_LIMPEZA.value: ("badge-limpeza", "🟠 Pendente Limpeza")
}


def _preview_path_para_camera(cam_id: str) -> Path:
    if cam_id == "cam_1":
        return PREVIEW_IMG_PATH
    return Path(f"data/cameras/{cam_id}_preview.jpg")


def _status_camera_badge(cam_cfg: dict, cam_status: dict) -> str:
    if not cam_cfg.get("ativa", True):
        return "⏸️ Pausada"
    if cam_status.get("conectada"):
        return "🟢 Online"
    if cam_status.get("erro"):
        return "🔴 Offline / Erro"
    return "🟡 Conectando"


def _obter_cameras_e_estados():
    cameras_cfg = carregar_cameras_config()
    estado_vivo = carregar_estado_ao_vivo()
    cameras_estado = dict(estado_vivo.get("cameras", {}))
    if not cameras_estado and cameras_cfg and estado_vivo.get("mesas"):
        cid_padrao = cameras_cfg[0]["id"]
        cameras_estado[cid_padrao] = {
            "id": cid_padrao,
            "nome": cameras_cfg[0]["nome"],
            "ativa": cameras_cfg[0].get("ativa", True),
            "conectada": True,
            "mesas": estado_vivo.get("mesas", {}),
        }
    return cameras_cfg, cameras_estado, estado_vivo


def _rerun_escopo(escopo: str = "fragment"):
    try:
        if escopo:
            st.rerun(scope=escopo)
        else:
            st.rerun()
    except Exception:
        st.rerun()


def _renderizar_card_mesa(mesa: dict, dados_m: dict):
    m_id = mesa["id"]
    est_str = dados_m.get("estado", EstadoMesa.LIVRE.value)
    badge_cls, badge_label = classes_badge.get(est_str, ("badge-livre", "🟢 Livre"))

    with st.container(border=True):
        st.markdown(f"### {m_id}")
        st.caption(mesa.get("descricao", "Mesa Salão"))
        st.markdown(f"<span class='status-badge {badge_cls}'>{badge_label}</span>", unsafe_allow_html=True)

        c_p, c_t = st.columns(2)
        c_p.metric("Pessoas Detectadas", f"{dados_m.get('qtd_pessoas', 0)}")
        c_t.metric("Cronômetro", f"{dados_m.get('tempo_formatado', '00:00')}")

        prev = dados_m.get("previsao_liberacao")
        if isinstance(prev, dict):
            pct = max(0, min(100, int(prev.get("progresso_pct", 0))))
            st.progress(pct / 100.0)
            perm_fmt = dados_m.get(
                "tempo_permanencia_formatado",
                formatar_duracao_curta(dados_m.get("tempo_permanencia_segundos", 0.0)),
            )
            origem = f"{prev['amostras_base']} visitas" if prev.get("amostras_base", 0) > 0 else "padrão"
            if prev.get("excedeu_mediana"):
                st.caption(f"⏳ Permanência {perm_fmt} • Acima da mediana ({prev.get('mediana_prevista_formatada', '-')})")
            else:
                st.caption(
                    f"⏱️ Previsão liberar: ~{prev.get('tempo_restante_formatado', '-')} • "
                    f"Mediana {prev.get('mediana_prevista_formatada', '-')} ({origem})"
                )

        if dados_m.get("garcom_presente"):
            st.info("👔 Garçom detectado na mesa")

        if dados_m.get("alertas"):
            st.warning(f"⚠️ {', '.join(dados_m['alertas'])}")


def renderizar_painel_ao_vivo():
    cameras_cfg, cameras_estado, estado_vivo = _obter_cameras_e_estados()
    atualizado_texto = estado_vivo.get("atualizado_em_texto", "Aguardando motor...")

    total = 0
    ocupadas = 0
    alertas_lista = []
    previsoes_liberacao = []
    cameras_ativas = sum(1 for c in cameras_cfg if c.get("ativa", True))
    cameras_online = 0
    tem_alguma_leitura = False

    blocos_cameras = []
    for cam in cameras_cfg:
        cid = cam["id"]
        cam_st = cameras_estado.get(cid, {})
        mesas_cfg = carregar_mesas(Path(cam["config_mesas_path"]))
        mesas_estado = cam_st.get("mesas", {})
        if mesas_estado:
            tem_alguma_leitura = True
        if cam.get("ativa", True) and cam_st.get("conectada"):
            cameras_online += 1

        total_cam = len(mesas_cfg) if mesas_cfg else len(mesas_estado)
        total += total_cam
        for m_id, m_dados in mesas_estado.items():
            if m_dados.get("estado") != EstadoMesa.LIVRE.value:
                ocupadas += 1
            for alerta in m_dados.get("alertas", []):
                alertas_lista.append((cam["nome"], m_id, alerta, m_dados.get("tempo_formatado", "00:00")))
            prev = m_dados.get("previsao_liberacao")
            if isinstance(prev, dict):
                previsoes_liberacao.append({
                    "restante_seg": float(prev.get("tempo_restante_seg", 99999.0)),
                    "perm_seg": float(m_dados.get("tempo_permanencia_segundos", 0.0)),
                    "cam_nome": cam["nome"],
                    "mesa_id": m_id,
                    "restante_fmt": prev.get("tempo_restante_formatado", "-"),
                    "mediana_fmt": prev.get("mediana_prevista_formatada", "-"),
                    "perm_fmt": m_dados.get(
                        "tempo_permanencia_formatado",
                        formatar_duracao_curta(m_dados.get("tempo_permanencia_segundos", 0.0)),
                    ),
                    "excedeu": bool(prev.get("excedeu_mediana")),
                    "amostras": int(prev.get("amostras_base", 0)),
                })

        blocos_cameras.append((cam, cam_st, mesas_cfg, mesas_estado))

    if not tem_alguma_leitura:
        st.warning("⏳ Aguardando a primeira leitura do motor de IA (`src/motor_estados.py`)...")

    livres = max(total - ocupadas, 0)
    taxa = (ocupadas / total) * 100 if total > 0 else 0

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Total de Mesas", f"{total}")
    col2.metric("Mesas Ocupadas", f"{ocupadas}", delta=f"{taxa:.0f}% taxa")
    col3.metric("Mesas Livres", f"{livres}")
    col4.metric("Alertas Ativos", f"{len(alertas_lista)}", delta="Atenção" if alertas_lista else "Normal", delta_color="inverse")
    col5.metric("Câmeras Online", f"{cameras_online}/{cameras_ativas}")
    col6.metric("Última Sincronização", atualizado_texto)

    st.divider()

    if alertas_lista:
        st.subheader("🚨 Fila Prioritária de Alertas")
        for cam_nome, m_id, tipo_alerta, tempo_fmt in alertas_lista:
            st.error(f"**[{cam_nome}] {m_id}:** Alerta de `{tipo_alerta}` ativo! (Tempo decorrido: **{tempo_fmt}**)")

    if previsoes_liberacao:
        previsoes_ordenadas = sorted(previsoes_liberacao, key=lambda x: (x["restante_seg"], -x["perm_seg"]))
        st.subheader("⏱️ Próximas Mesas a Liberar (Previsão por Mediana)")
        top_prev = previsoes_ordenadas[:4]
        cols_prev = st.columns(len(top_prev))
        for col_p, item in zip(cols_prev, top_prev):
            delta_txt = (
                "Acima da mediana"
                if item["excedeu"]
                else (f"Base: {item['amostras']} visitas" if item["amostras"] > 0 else f"Mediana padrão ({item['mediana_fmt']})")
            )
            val_txt = "Prestes a vagar" if item["excedeu"] else f"~{item['restante_fmt']}"
            col_p.metric(f"{item['mesa_id']} ({item['cam_nome']})", val_txt, delta=delta_txt, delta_color="off")
        st.divider()

    st.subheader("🪑 Status Real das Mesas por Câmera")
    if len(cameras_cfg) > 1:
        opcoes_filtro = ["__todas__"] + [c["id"] for c in cameras_cfg]
        mapa_filtro = {"__todas__": "Todas as Câmeras"} | {c["id"]: f"📷 {c['nome']}" for c in cameras_cfg}
        filtro_sel = st.selectbox(
            "Filtrar visualização por câmera:",
            opcoes_filtro,
            format_func=lambda x: mapa_filtro[x],
            key="painel_filtro_camera",
        )
        if filtro_sel != "__todas__":
            blocos_cameras = [b for b in blocos_cameras if b[0]["id"] == filtro_sel]

    for cam, cam_st, mesas_cfg, mesas_estado in blocos_cameras:
        status_txt = _status_camera_badge(cam, cam_st)
        st.markdown(f"#### 📷 {cam['nome']} — {status_txt}")
        if cam_st.get("erro"):
            st.warning(f"⚠️ Câmera `{cam['nome']}`: {cam_st['erro']}")
        if not mesas_cfg:
            st.info(
                f"Nenhuma mesa mapeada para **{cam['nome']}**. Acesse a aba **🎨 Editor de Mesas e Zonas** "
                "e selecione esta câmera para desenhar suas zonas."
            )
            continue

        cols = st.columns(3)
        for idx, mesa in enumerate(mesas_cfg):
            dados_m = mesas_estado.get(mesa["id"], {
                "estado": EstadoMesa.LIVRE.value,
                "tempo_formatado": "00:00",
                "qtd_pessoas": 0,
                "alertas": [],
            })
            with cols[idx % 3]:
                _renderizar_card_mesa(mesa, dados_m)


def _renderizar_feed_camera(cam: dict, cam_st: dict):
    cid = cam["id"]
    status_txt = _status_camera_badge(cam, cam_st)
    fonte_segura = mascarar_fonte_segura(cam["fonte"])

    with st.container(border=True):
        st.markdown(f"#### 📹 {cam['nome']} — {status_txt}")
        st.caption(f"ID: `{cid}` | FPS alvo: `{cam.get('fps_alvo', 2.5)}` | Fonte: `{fonte_segura}`")
        if cam_st.get("erro"):
            st.error(f"Erro na captura: {cam_st['erro']}")

        frame_vivo = Path(cam["frame_vivo_path"])
        preview_path = _preview_path_para_camera(cid)
        frame_limpo = Path(cam["frame_limpo_path"])

        if frame_vivo.exists():
            try:
                st.image(frame_vivo.read_bytes(), caption=f"{cam['nome']} — Detecção ao vivo", width="stretch")
            except Exception:
                st.info("Atualizando frame da câmera...")
        elif preview_path.exists():
            st.image(preview_path.read_bytes(), caption=f"{cam['nome']} — Preview de calibração", width="stretch")
        elif frame_limpo.exists():
            st.image(frame_limpo.read_bytes(), caption=f"{cam['nome']} — Frame inicial capturado", width="stretch")
        else:
            st.warning("Nenhuma imagem disponível para esta câmera ainda.")


def renderizar_camera_ao_vivo():
    st.subheader("📹 Visão Ao Vivo Processada pela IA")
    cameras_cfg, cameras_estado, _ = _obter_cameras_e_estados()

    if len(cameras_cfg) == 1:
        cam = cameras_cfg[0]
        _renderizar_feed_camera(cam, cameras_estado.get(cam["id"], {}))
    else:
        cols = st.columns(2)
        for idx, cam in enumerate(cameras_cfg):
            with cols[idx % 2]:
                _renderizar_feed_camera(cam, cameras_estado.get(cam["id"], {}))

    st.caption("ℹ️ As caixas e status exibidos nas imagens acima refletem em tempo real o processamento de cada câmera.")


def renderizar_historico():
    st.subheader("📜 Histórico de Eventos e Auditoria")
    eventos_db = []
    try:
        eventos_db = RepositorioAnalytics(db_path=HISTORICO_DB_PATH).consultar_eventos_recentes(limite=200)
    except Exception:
        eventos_db = []

    eventos = eventos_db if eventos_db else carregar_historico_eventos()
    if eventos:
        df_eventos = pd.DataFrame(eventos)
        colunas_pref = ["data_hora", "timestamp", "camera_nome", "mesa_id", "evento", "tipo", "detalhes"]
        ordenadas = [c for c in colunas_pref if c in df_eventos.columns]
        restantes = [c for c in df_eventos.columns if c not in ordenadas]
        df_eventos = df_eventos[ordenadas + restantes]
        st.dataframe(df_eventos, width="stretch", hide_index=True)
        origem_lbl = "banco persistente SQLite" if eventos_db else "sessão atual"
        st.caption(f"Exibindo {len(eventos)} evento(s) recentes ({origem_lbl}).")
    else:
        st.info("Nenhum evento registrado ainda pelo motor de monitoramento.")


def _renderizar_previsao_mediana_tabela(cameras_cfg: list, cameras_estado: dict, medianas_hist: list):
    st.markdown("### ⏱️ Previsão de Liberação & Mediana Histórica de Permanência")
    linhas_ao_vivo = []
    for cam in cameras_cfg:
        cam_st = cameras_estado.get(cam["id"], {})
        for m_id, m_dados in cam_st.get("mesas", {}).items():
            prev = m_dados.get("previsao_liberacao")
            if not isinstance(prev, dict):
                continue
            origem = f"Mediana ({prev['amostras_base']} visitas)" if prev.get("amostras_base", 0) > 0 else "Estimativa padrão"
            linhas_ao_vivo.append({
                "Câmera": cam["nome"],
                "Mesa": m_id,
                "Status": m_dados.get("estado", "-"),
                "Permanência Atual": m_dados.get("tempo_permanencia_formatado", "-"),
                "Mediana Base": prev.get("mediana_prevista_formatada", "-"),
                "Previsão Restante": "Acima da mediana" if prev.get("excedeu_mediana") else f"~{prev.get('tempo_restante_formatado', '-')}",
                "Progresso (%)": int(prev.get("progresso_pct", 0)),
                "Fonte da Previsão": origem,
            })

    if linhas_ao_vivo:
        st.dataframe(
            pd.DataFrame(linhas_ao_vivo),
            width="stretch",
            hide_index=True,
            column_config={"Progresso (%)": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%")},
        )
    else:
        st.info("Nenhuma mesa ocupada neste momento para estimativa regressiva.")

    if medianas_hist:
        df_med = pd.DataFrame([
            {
                "Câmera": item["camera_nome"],
                "Mesa": item["mesa_id"],
                "Visitas Concluídas": item["visitas"],
                "Mediana Permanência (min)": item["permanencia_mediana_min"],
                "P90 Permanência (min)": item["permanencia_p90_min"],
                "Mediana Espera Garçom (min)": item["espera_mediana_min"],
                "Confiabilidade": "✅ Base histórica ativa" if item["confiavel"] else "⏳ Coletando (<3 visitas)",
            }
            for item in medianas_hist
        ])
        st.markdown("#### 📊 Tempos Medianos Registrados por Mesa")
        st.dataframe(
            df_med,
            width="stretch",
            hide_index=True,
            column_config={"Mediana Espera Garçom (min)": st.column_config.NumberColumn(format="%.1f")},
        )
    else:
        st.info(
            "Ainda não há visitas concluídas no banco (`data/historico_salao.db`). "
            "Assim que os clientes finalizarem um ciclo completo na mesa, a mediana real substituirá automaticamente a estimativa inicial."
        )


def _renderizar_pico_e_escala(repo: RepositorioAnalytics, dias: Optional[int], cam_filtro: Optional[str], ratio_garcom: int):
    st.markdown("### 👔 Pico por Hora / Dia e Dimensionamento de Garçons")
    turnos = repo.consultar_ranking_turnos(dias=dias, camera_id=cam_filtro, mesas_por_garcom=ratio_garcom)
    por_hora = repo.consultar_resumo_por_hora(dias=dias, camera_id=cam_filtro, mesas_por_garcom=ratio_garcom)
    por_dia = repo.consultar_resumo_por_dia_semana(dias=dias, camera_id=cam_filtro, mesas_por_garcom=ratio_garcom)
    matriz = repo.consultar_matriz_pico_hora_dia(dias=dias, camera_id=cam_filtro, mesas_por_garcom=ratio_garcom)

    tem_dados = any(h["ocupacao_media_mesas"] > 0 or h["giros_visitas"] > 0 or h["alertas_disparados"] > 0 for h in por_hora)
    if not tem_dados:
        st.info(
            "Aguardando acúmulo de amostras operacionais (gravadas automaticamente a cada 60s enquanto o motor está em execução) "
            "para gerar curvas de pico por hora, dia da semana e escala de garçons."
        )
        return

    st.markdown("#### 🕐 Recomendação de Escala por Turno")
    df_turnos = pd.DataFrame([
        {
            "Turno": t["turno"],
            "Ocupação Média (mesas)": t["ocupacao_media_mesas"],
            "Pico (mesas)": t["ocupacao_pico_mesas"],
            "Visitas Iniciadas": t["giros_visitas"],
            "Alertas": t["alertas_disparados"],
            "Escala Sugerida": t["escala_sugerida"],
        }
        for t in turnos
    ])
    st.dataframe(df_turnos, width="stretch", hide_index=True)

    df_hora = pd.DataFrame(por_hora)
    df_hora_ativas = df_hora[(df_hora["ocupacao_media_mesas"] > 0) | (df_hora["giros_visitas"] > 0) | (df_hora["alertas_disparados"] > 0)]
    if not df_hora_ativas.empty:
        c_graf1, c_graf2 = st.columns(2)
        with c_graf1:
            st.markdown("#### 📈 Ocupação de Mesas por Hora")
            chart_oc = df_hora.set_index("hora_label")[["ocupacao_media_mesas", "ocupacao_pico_mesas"]].rename(
                columns={"ocupacao_media_mesas": "Média Ocupadas", "ocupacao_pico_mesas": "Pico Ocupadas"}
            )
            st.bar_chart(chart_oc)
        with c_graf2:
            st.markdown(f"#### 👔 Garçons Necessários / Hora (1 para cada {ratio_garcom} mesas)")
            chart_g = df_hora.set_index("hora_label")[["garcons_recomendados_medio", "garcons_recomendados_pico"]].rename(
                columns={"garcons_recomendados_medio": "Escala Média", "garcons_recomendados_pico": "Escala Pico"}
            )
            st.bar_chart(chart_g)

    st.markdown("#### 📅 Movimento Consolidado por Dia da Semana")
    df_dia = pd.DataFrame([
        {
            "Dia": d["dia_nome"],
            "Ocupação Média (mesas)": d["ocupacao_media_mesas"],
            "Pico (mesas)": d["ocupacao_pico_mesas"],
            "Visitas": d["giros_visitas"],
            "Alertas": d["alertas_disparados"],
            "Garçons no Pico": d["garcons_recomendados_pico"],
        }
        for d in por_dia
    ])
    st.dataframe(df_dia, width="stretch", hide_index=True)

    if matriz:
        st.markdown("#### 🗓️ Matriz Dia da Semana × Hora (Ocupação Média de Mesas)")
        df_m = pd.DataFrame(matriz)
        piv = df_m.pivot(index="dia_nome", columns="hora_label", values="ocupacao_media_mesas")
        ordem_dias = [d for d in DIAS_SEMANA_PT if d in piv.index]
        piv = piv.reindex(ordem_dias).fillna(0.0)
        st.dataframe(piv, width="stretch")

        csv_bytes = df_m.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 Baixar Relatório Completo de Pico e Escala (CSV)",
            data=csv_bytes,
            file_name="relatorio_pico_equipe.csv",
            mime="text/csv",
            key="dl_pico_csv",
        )


def _gerar_relatorio_html(
    periodo_label: str,
    camera_label: str,
    resumo: dict,
    desempenho: list,
    turnos: list,
) -> bytes:
    """Gera relatório executivo HTML autocontido e imprimível com escape estrito contra XSS."""
    linhas_mesas = "".join(
        f"<tr><td>{html.escape(str(item['camera_nome']))}</td>"
        f"<td><strong>{html.escape(str(item['mesa_id']))}</strong></td>"
        f"<td>{item['visitas']}</td>"
        f"<td>{item['permanencia_mediana_min'] if item['permanencia_mediana_min'] is not None else '-'}</td>"
        f"<td>{item['espera_mediana_min'] if item['espera_mediana_min'] is not None else '-'}</td>"
        f"<td>{item['limpeza_mediana_min'] if item['limpeza_mediana_min'] is not None else '-'}</td>"
        f"<td>{item['alertas']}</td>"
        f"<td>{item['sla_ok_pct']:.1f}%</td></tr>"
        for item in desempenho
    ) or "<tr><td colspan='8'>Sem dados no período selecionado.</td></tr>"

    linhas_turnos = "".join(
        f"<tr><td>{html.escape(str(t['turno']))}</td>"
        f"<td>{t['ocupacao_media_mesas']}</td>"
        f"<td>{t['ocupacao_pico_mesas']}</td>"
        f"<td>{t['giros_visitas']}</td>"
        f"<td>{t['alertas_disparados']}</td>"
        f"<td><strong>{html.escape(str(t['escala_sugerida']))}</strong></td></tr>"
        for t in turnos
    )

    doc = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Relatório Gerencial — Monitor de Mesas IA</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 28px; color: #1f2937; }}
  h1, h2 {{ color: #111827; }}
  .meta {{ color: #4b5563; margin-bottom: 18px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }}
  .card {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 12px; background: #f9fafb; }}
  .card strong {{ display: block; font-size: 1.35rem; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0 24px; font-size: 0.92rem; }}
  th, td {{ border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; }}
  th {{ background: #f3f4f6; }}
</style></head><body>
  <h1>Relatório Gerencial — Monitoramento de Salão com IA</h1>
  <div class="meta">Período: <strong>{html.escape(periodo_label)}</strong> | Filtro: <strong>{html.escape(camera_label)}</strong></div>
  <div class="kpis">
    <div class="card">Visitas Concluídas<strong>{resumo['total_visitas']}</strong></div>
    <div class="card">SLA Dentro da Meta<strong>{resumo.get('sla_sem_alerta_pct', 100.0):.1f}%</strong></div>
    <div class="card">Permanência Mediana / P90<strong>{resumo['permanencia_mediana_min']:.1f} min / {resumo['permanencia_p90_min']:.1f} min</strong></div>
    <div class="card">Espera Mediana Garçom<strong>{resumo['espera_mediana_min'] if resumo['espera_mediana_min'] is not None else '-'} min</strong></div>
    <div class="card">Limpeza Mediana<strong>{resumo.get('limpeza_mediana_min') if resumo.get('limpeza_mediana_min') is not None else '-'} min</strong></div>
    <div class="card">Alertas Disparados<strong>{resumo['total_alertas']}</strong></div>
  </div>
  <h2>Desempenho Operacional por Mesa</h2>
  <table>
    <thead><tr><th>Câmera</th><th>Mesa</th><th>Visitas</th><th>Mediana Perm. (min)</th><th>Espera Garçom (min)</th><th>Limpeza (min)</th><th>Alertas</th><th>SLA OK</th></tr></thead>
    <tbody>{linhas_mesas}</tbody>
  </table>
  <h2>Pico e Dimensionamento Sugerido de Garçons por Turno</h2>
  <table>
    <thead><tr><th>Turno</th><th>Ocupação Média</th><th>Pico Mesas</th><th>Visitas</th><th>Alertas</th><th>Escala Sugerida</th></tr></thead>
    <tbody>{linhas_turnos}</tbody>
  </table>
</body></html>"""
    return doc.encode("utf-8")


def _renderizar_desempenho_e_sla(
    desempenho: list,
    rel_alertas: dict,
    resumo: dict,
    turnos: list,
    periodo_label: str,
    camera_label: str,
):
    st.markdown("### 🏆 Ranking e Desempenho Operacional por Mesa")
    if desempenho:
        df_des = pd.DataFrame([
            {
                "Câmera": d["camera_nome"],
                "Mesa": d["mesa_id"],
                "Visitas": d["visitas"],
                "Mediana Perm. (min)": d["permanencia_mediana_min"],
                "P90 Perm. (min)": d["permanencia_p90_min"],
                "Espera Garçom (min)": d["espera_mediana_min"],
                "Limpeza (min)": d["limpeza_mediana_min"],
                "Alertas": d["alertas"],
                "SLA OK (%)": float(d["sla_ok_pct"]),
            }
            for d in desempenho
        ])
        st.dataframe(
            df_des,
            width="stretch",
            hide_index=True,
            column_config={
                "Mediana Perm. (min)": st.column_config.NumberColumn(format="%.1f"),
                "P90 Perm. (min)": st.column_config.NumberColumn(format="%.1f"),
                "Espera Garçom (min)": st.column_config.NumberColumn(format="%.1f"),
                "Limpeza (min)": st.column_config.NumberColumn(format="%.1f"),
                "SLA OK (%)": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            },
        )
        c_dl1, c_dl2 = st.columns(2)
        c_dl1.download_button(
            "📥 Baixar Desempenho por Mesa (CSV)",
            data=df_des.to_csv(index=False).encode("utf-8-sig"),
            file_name="relatorio_desempenho_mesas.csv",
            mime="text/csv",
            key="dl_mesas_csv",
        )
        c_dl2.download_button(
            "🖨️ Baixar Relatório Gerencial Imprimível (.html)",
            data=_gerar_relatorio_html(periodo_label, camera_label, resumo, desempenho, turnos),
            file_name="relatorio_gerencial_salao.html",
            mime="text/html",
            key="dl_exec_html",
        )
    else:
        st.info("Ainda não há histórico suficiente por mesa no período selecionado.")

    st.markdown("### 🚨 Distribuição de Alertas de SLA por Tipo")
    por_tipo = rel_alertas.get("por_tipo", {})
    if por_tipo:
        df_tipos = pd.DataFrame(
            [{"Tipo de Alerta": k, "Ocorrências": v} for k, v in sorted(por_tipo.items(), key=lambda x: -x[1])]
        )
        col_t1, col_t2 = st.columns([1.2, 1.8])
        col_t1.dataframe(df_tipos, width="stretch", hide_index=True)
        col_t2.bar_chart(df_tipos.set_index("Tipo de Alerta"))
    else:
        st.success("✅ Nenhum alerta de atraso ou limpeza pendente registrado neste período!")


def _renderizar_extrato_visitas_e_alertas(visitas_det: list, rel_alertas: dict):
    st.markdown("### 🧾 Extrato de Visitas / Giros Concluídos")
    if visitas_det:
        df_v = pd.DataFrame([
            {
                "Entrada": v["entrada"],
                "Saída": v["saida"],
                "Câmera": v["camera_nome"],
                "Mesa": v["mesa_id"],
                "Pessoas": v["pessoas"],
                "Permanência (min)": v["permanencia_min"],
                "Espera Garçom (min)": v["espera_garcom_min"],
                "Preparo (min)": v["preparo_cozinha_min"],
                "SLA": v["sla"],
            }
            for v in visitas_det
        ])
        st.dataframe(
            df_v,
            width="stretch",
            hide_index=True,
            column_config={
                "Permanência (min)": st.column_config.NumberColumn(format="%.1f"),
                "Espera Garçom (min)": st.column_config.NumberColumn(format="%.1f"),
                "Preparo (min)": st.column_config.NumberColumn(format="%.1f"),
            },
        )
        st.download_button(
            "📥 Baixar Extrato de Visitas (CSV)",
            data=df_v.to_csv(index=False).encode("utf-8-sig"),
            file_name="relatorio_visitas.csv",
            mime="text/csv",
            key="dl_visitas_csv",
        )
    else:
        st.info("Nenhuma visita concluída registrada no período selecionado.")

    st.divider()
    st.markdown("### ⚠️ Ocorrências Detalhadas de Alertas")
    lista_al = rel_alertas.get("alertas", [])
    if lista_al:
        df_al = pd.DataFrame([
            {
                "Data/Hora": a["data_hora"],
                "Câmera": a["camera_nome"],
                "Mesa": a["mesa_id"],
                "Tipo de Alerta": a["tipo_alerta"],
                "Tempo Decorrido": a["tempo_decorrido"],
            }
            for a in lista_al
        ])
        st.dataframe(df_al, width="stretch", hide_index=True)
        st.download_button(
            "📥 Baixar Log de Alertas (CSV)",
            data=df_al.to_csv(index=False).encode("utf-8-sig"),
            file_name="relatorio_alertas.csv",
            mime="text/csv",
            key="dl_alertas_csv",
        )
    else:
        st.info("Nenhum alerta registrado no período selecionado.")


def renderizar_relatorios_etapa2():
    st.subheader("📊 Relatórios Gerenciais e Inteligência Operacional")
    st.caption(
        "Indicadores de SLA, ranking por mesa, previsão de giro via mediana histórica, fluxo por hora/dia e dimensionamento de garçons."
    )

    cameras_cfg, cameras_estado, _ = _obter_cameras_e_estados()
    regras_atual = carregar_regras()

    c_per, c_cam, c_ratio, c_btn = st.columns([1.2, 1.4, 1.0, 0.8])
    periodos = {"Hoje (24h)": 1, "Últimos 7 dias": 7, "Últimos 30 dias": 30, "Todo o histórico": None}
    per_label = c_per.selectbox("Período de análise:", list(periodos.keys()), index=2, key="ana_periodo")
    dias_filtro = periodos[per_label]

    opcoes_cam = ["__todas__"] + [c["id"] for c in cameras_cfg]
    mapa_cam = {"__todas__": "Todas as Câmeras"} | {c["id"]: f"📷 {c['nome']}" for c in cameras_cfg}
    cam_sel = c_cam.selectbox("Filtrar câmera / salão:", opcoes_cam, format_func=lambda x: mapa_cam[x], key="ana_cam")

    ratio_val = int(
        c_ratio.number_input(
            "Mesas / garçom:",
            min_value=1,
            max_value=12,
            value=int(regras_atual.get("mesas_por_garcom", 4)),
            step=1,
            key="ana_ratio_garcom",
        )
    )
    c_btn.write("")
    if c_btn.button("🔄 Atualizar", key="ana_refresh_btn", width="stretch"):
        _rerun_escopo("fragment")

    repo = RepositorioAnalytics(db_path=HISTORICO_DB_PATH)
    resumo = repo.consultar_resumo_geral(dias=dias_filtro, camera_id=cam_sel)
    desempenho = repo.consultar_desempenho_por_mesa(dias=dias_filtro, camera_id=cam_sel)
    medianas_hist = repo.consultar_medianas_detalhadas(dias=dias_filtro, camera_id=cam_sel)
    turnos = repo.consultar_ranking_turnos(dias=dias_filtro, camera_id=cam_sel, mesas_por_garcom=ratio_val)
    visitas_det = repo.consultar_visitas_detalhadas(dias=dias_filtro, camera_id=cam_sel)
    rel_alertas = repo.consultar_relatorio_alertas(dias=dias_filtro, camera_id=cam_sel)

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Visitas / Giros", f"{resumo['total_visitas']}")
    k2.metric("SLA Sem Atraso", f"{resumo.get('sla_sem_alerta_pct', 100.0):.1f}%")
    k3.metric(
        "Permanência Mediana",
        f"{resumo['permanencia_mediana_min']:.1f} min" if resumo["total_visitas"] else "-",
        delta=f"P90: {resumo['permanencia_p90_min']:.1f} min" if resumo["total_visitas"] else None,
        delta_color="off",
    )
    k4.metric(
        "Espera Garçom",
        f"{resumo['espera_mediana_min']:.1f} min" if resumo["espera_mediana_min"] is not None else "-",
    )
    k5.metric(
        "Limpeza Mediana",
        f"{resumo['limpeza_mediana_min']:.1f} min" if resumo.get("limpeza_mediana_min") is not None else "-",
    )
    k6.metric("Alertas no Período", f"{resumo['total_alertas']}")

    st.divider()
    sub_desempenho, sub_escala, sub_previsao, sub_extrato = st.tabs([
        "📋 Desempenho & SLA",
        "👔 Pico & Escala de Equipe",
        "⏱️ Previsão de Giro (Medianas)",
        "🧾 Extrato de Visitas & Alertas",
    ])

    with sub_desempenho:
        _renderizar_desempenho_e_sla(desempenho, rel_alertas, resumo, turnos, per_label, mapa_cam[cam_sel])

    with sub_escala:
        _renderizar_pico_e_escala(repo, dias_filtro, cam_sel, ratio_val)

    with sub_previsao:
        _renderizar_previsao_mediana_tabela(cameras_cfg, cameras_estado, medianas_hist)

    with sub_extrato:
        _renderizar_extrato_visitas_e_alertas(visitas_det, rel_alertas)


def _carregar_imagem_base_editor(cam_cfg: dict, preferir_limpo_ao_vivo: bool = True):
    """Carrega a imagem base e suas dimensões nativas para o editor de polígonos da câmera."""
    frame_limpo = Path(cam_cfg["frame_limpo_path"])
    frame_vivo = Path(cam_cfg["frame_vivo_path"])
    candidatos = [frame_limpo, frame_vivo] if preferir_limpo_ao_vivo else []
    if cam_cfg["id"] == "cam_1":
        candidatos = (
            [frame_limpo, FRAME_REF_PATH, frame_vivo]
            if preferir_limpo_ao_vivo
            else [FRAME_REF_PATH, frame_limpo, frame_vivo]
        )
    elif not preferir_limpo_ao_vivo:
        candidatos = [frame_limpo, frame_vivo]

    if not any(p.exists() for p in candidatos):
        extrair_frame_limpo_fonte(cam_cfg["fonte"], frame_limpo)

    for caminho in candidatos:
        if caminho.exists():
            raw = caminho.read_bytes()
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                alt, larg, _ = img.shape
                b64 = base64.b64encode(raw).decode("ascii")
                return f"data:image/jpeg;base64,{b64}", larg, alt, caminho
    return None, 1280, 720, None


@st.fragment
def renderizar_editor_navegador():
    st.subheader("🎨 Editor Visual de Mesas e Zonas")
    st.caption(
        "Selecione uma câmera, desenhe novas zonas por cliques, adicione retângulos e personalize nomes/descrições direto no navegador. "
        "Ao salvar, o motor de IA aplica as alterações automaticamente sem reiniciar os containers."
    )

    feedback = st.session_state.pop("ze_feedback", None)
    if feedback:
        nivel, msg = feedback
        if nivel == "success":
            st.success(msg)
        else:
            st.error(msg)

    cameras_cfg = carregar_cameras_config()
    mapa_nomes = {c["id"]: f"📷 {c['nome']} ({c['id']})" for c in cameras_cfg}
    cam_id_sel = st.selectbox(
        "Câmera para calibrar zonas:",
        list(mapa_nomes.keys()),
        format_func=lambda cid: mapa_nomes[cid],
        key="ze_camera_select",
    )
    cam_sel = next(c for c in cameras_cfg if c["id"] == cam_id_sel)
    cfg_mesas_path = Path(cam_sel["config_mesas_path"])
    frame_limpo_path = Path(cam_sel["frame_limpo_path"])
    preview_out = _preview_path_para_camera(cam_id_sel)

    col_fonte, col_captura, col_refresh = st.columns([3, 1, 1])
    with col_fonte:
        opcoes_bg = ["📷 Frame Limpo da Câmera"]
        if cam_id_sel == "cam_1":
            opcoes_bg.append("🖼️ Frame de Referência Padrão")
        fonte_bg = st.radio(
            "Imagem de fundo para calibração:",
            opcoes_bg,
            horizontal=True,
            key=f"ze_bg_source_{cam_id_sel}",
        )
    with col_captura:
        st.write("")
        if st.button("📸 Capturar da Fonte", key=f"ze_cap_{cam_id_sel}", width="stretch"):
            dims = extrair_frame_limpo_fonte(cam_sel["fonte"], frame_limpo_path)
            if dims:
                st.session_state["ze_feedback"] = ("success", f"✅ Frame capturado ({dims[0]}x{dims[1]}).")
            else:
                st.session_state["ze_feedback"] = ("error", "❌ Não foi possível ler frame desta fonte.")
            _rerun_escopo("fragment")
    with col_refresh:
        st.write("")
        if st.button("🔄 Atualizar Frame", key=f"ze_ref_{cam_id_sel}", width="stretch"):
            _rerun_escopo("fragment")

    preferir_ao_vivo = fonte_bg.startswith("📷")
    img_b64, largura, altura, caminho_img = _carregar_imagem_base_editor(cam_sel, preferir_ao_vivo)

    if img_b64 is None or caminho_img is None:
        st.warning(f"Nenhum frame encontrado para `{cam_sel['nome']}`. Verifique a fonte de vídeo na aba **🎥 Câmeras e Vídeos**.")
        return

    mesas_atuais = carregar_mesas(cfg_mesas_path)
    rev_key = f"ze_revision_{cam_id_sel}"
    ack_key = f"ze_ack_{cam_id_sel}"
    nonce_key = f"ze_last_nonce_{cam_id_sel}"

    rev = int(st.session_state.get(rev_key, 0))
    ack = int(st.session_state.get(ack_key, 0))

    result = renderizar_editor_zonas(
        mesas=mesas_atuais,
        largura=largura,
        altura=altura,
        img_b64=img_b64,
        revision=rev,
        ack=ack,
        key=f"editor_zonas_{cam_id_sel}",
    )

    payload = getattr(result, "save_payload", None)
    if isinstance(payload, dict) and payload.get("nonce") != st.session_state.get(nonce_key):
        st.session_state[nonce_key] = payload.get("nonce")
        try:
            mesas_salvas = salvar_configuracao_mesas(
                mesas_raw=payload.get("mesas"),
                largura=int(payload.get("largura", largura)),
                altura=int(payload.get("altura", altura)),
                imagem_referencia=caminho_img.name,
                config_path=cfg_mesas_path,
                img_base_path=caminho_img,
                preview_out_path=preview_out,
            )
            st.session_state[rev_key] = rev + 1
            st.session_state[ack_key] = ack + 1
            st.session_state["ze_feedback"] = (
                "success",
                f"✅ Configuração de `{cam_sel['nome']}` salva com {len(mesas_salvas)} mesa(s)! O motor aplicará automaticamente.",
            )
        except Exception as e:
            st.session_state[ack_key] = ack + 1
            st.session_state["ze_feedback"] = ("error", f"❌ Não foi possível salvar: {e}")

        _rerun_escopo("fragment")

    with st.expander(f"📋 Resumo das Mesas Salvas ({cfg_mesas_path.as_posix()})"):
        if mesas_atuais:
            resumo = [
                {
                    "Nome / ID": m.get("id", ""),
                    "Descrição": m.get("descricao", ""),
                    "Qtd. Vértices": len(m.get("pontos", [])),
                    "Coordenadas": str(m.get("pontos", [])),
                }
                for m in mesas_atuais
            ]
            st.dataframe(pd.DataFrame(resumo), width="stretch", hide_index=True)
        else:
            st.info(f"Nenhuma mesa salva em `{cfg_mesas_path.as_posix()}`.")


def _seletor_fonte_camera(prefixo_key: str, fonte_atual: str, videos_disponiveis: list):
    """Renderiza seletor reutilizável de vídeo existente, upload ou URL RTSP/Webcam."""
    opcoes = [
        "🎬 Vídeo na pasta data/videos",
        "📤 Enviar Novo Vídeo (.mp4/.avi/.mov)",
        "🌐 RTSP / HTTP / Webcam",
    ]
    if fonte_atual in videos_disponiveis:
        idx_padrao = 0
    elif fonte_atual:
        idx_padrao = 2
    else:
        idx_padrao = 0 if videos_disponiveis else 2

    modo = st.radio(
        "Origem do vídeo / stream:",
        opcoes,
        index=idx_padrao,
        horizontal=True,
        key=f"{prefixo_key}_modo",
    )

    if modo == opcoes[0]:
        if not videos_disponiveis:
            st.warning("Nenhum vídeo encontrado em `data/videos/`. Envie um novo vídeo ao lado.")
            return "video", None
        idx_v = videos_disponiveis.index(fonte_atual) if fonte_atual in videos_disponiveis else 0
        sel = st.selectbox("Arquivo de vídeo", videos_disponiveis, index=idx_v, key=f"{prefixo_key}_vid")
        return "video", sel

    if modo == opcoes[1]:
        up = st.file_uploader(
            "Enviar vídeo de restaurante",
            type=["mp4", "avi", "mov", "mkv", "webm"],
            key=f"{prefixo_key}_up",
        )
        return "upload", up

    mascarada = mascarar_fonte_segura(fonte_atual) if fonte_atual and fonte_atual not in videos_disponiveis else ""
    url_digitada = st.text_input(
        "URL RTSP/HTTP ou índice da webcam (0..10)",
        value=mascarada,
        placeholder="rtsp://usuario:senha@192.168.1.100:554/stream1",
        key=f"{prefixo_key}_url",
    )
    valor_final = fonte_atual if (mascarada and url_digitada == mascarada) else url_digitada
    return "url", valor_final


def _resolver_fonte_selecionada(tipo: str, valor) -> str:
    if tipo == "video":
        if not valor:
            raise ValueError("Selecione um arquivo de vídeo em `data/videos/`.")
        return str(valor)
    if tipo == "upload":
        if valor is None:
            raise ValueError("Envie um arquivo de vídeo antes de salvar.")
        return salvar_upload_video(valor.name, valor.getvalue())
    return str(valor or "").strip()


@st.fragment
def renderizar_aba_cameras():
    st.subheader("🎥 Gerenciamento de Câmeras e Fontes de Vídeo")
    st.caption(
        "Conecte múltiplas câmeras IP (RTSP/HTTP), webcams ou troque vídeos de teste em tempo real. "
        "Todas as câmeras ativas compartilham o mesmo modelo YOLOv10 na memória e aplicam alterações sem reiniciar o Docker."
    )

    feedback = st.session_state.pop("cam_feedback", None)
    if feedback:
        nivel, msg = feedback
        if nivel == "success":
            st.success(msg)
        else:
            st.error(msg)

    cameras_cfg = carregar_cameras_config()
    videos_disponiveis = listar_videos_disponiveis()

    for idx, cam in enumerate(cameras_cfg):
        cid = cam["id"]
        estado_txt = "🟢 Ativa" if cam.get("ativa", True) else "⏸️ Pausada"
        with st.container(border=True):
            st.markdown(f"### 📷 {cam['nome']} (`{cid}`) — {estado_txt}")
            st.caption(f"Fonte atual: `{mascarar_fonte_segura(cam['fonte'])}` | Arquivo de zonas: `{cam['config_mesas_path']}`")

            c1, c2, c3 = st.columns([2.2, 1.2, 0.8])
            novo_nome = c1.text_input("Nome da câmera / área", value=cam["nome"], key=f"cam_nome_{cid}")
            novo_fps = c2.slider(
                "FPS alvo",
                min_value=0.5,
                max_value=10.0,
                value=float(cam.get("fps_alvo", 2.5)),
                step=0.5,
                key=f"cam_fps_{cid}",
            )
            c3.write("")
            nova_ativa = c3.checkbox("Ativa", value=bool(cam.get("ativa", True)), key=f"cam_ativa_{cid}")

            tipo_sel, val_sel = _seletor_fonte_camera(f"edit_{cid}", cam["fonte"], videos_disponiveis)

            b1, b2, b3 = st.columns([1.2, 1.4, 1.0])
            if b1.button("💾 Salvar Alterações", key=f"btn_save_{cid}", width="stretch"):
                try:
                    nova_fonte = _resolver_fonte_selecionada(tipo_sel, val_sel)
                    atualizada = {
                        **cam,
                        "nome": novo_nome.strip(),
                        "fonte": nova_fonte,
                        "ativa": nova_ativa,
                        "fps_alvo": novo_fps,
                    }
                    lista_nova = [atualizada if c["id"] == cid else c for c in cameras_cfg]
                    salvar_cameras_config(lista_nova)
                    if nova_fonte != cam["fonte"] or not Path(cam["frame_limpo_path"]).exists():
                        extrair_frame_limpo_fonte(nova_fonte, Path(cam["frame_limpo_path"]))
                    st.session_state["cam_feedback"] = ("success", f"✅ Câmera `{atualizada['nome']}` atualizada! O motor já está sincronizando.")
                except Exception as e:
                    st.session_state["cam_feedback"] = ("error", f"❌ Erro ao salvar `{cam['nome']}`: {e}")
                _rerun_escopo(None)

            if b2.button("📸 Capturar Frame p/ Editor", key=f"btn_cap_{cid}", width="stretch"):
                try:
                    dims = extrair_frame_limpo_fonte(cam["fonte"], Path(cam["frame_limpo_path"]))
                    if dims:
                        st.session_state["cam_feedback"] = ("success", f"✅ Frame de `{cam['nome']}` capturado ({dims[0]}x{dims[1]}).")
                    else:
                        st.session_state["cam_feedback"] = ("error", f"❌ Não foi possível ler frame de `{cam['nome']}`.")
                except Exception as e:
                    st.session_state["cam_feedback"] = ("error", f"❌ Falha ao capturar frame: {e}")
                _rerun_escopo("fragment")

            if b3.button("🗑️ Remover", key=f"btn_del_{cid}", disabled=len(cameras_cfg) <= 1, width="stretch"):
                try:
                    restantes = [c for c in cameras_cfg if c["id"] != cid]
                    salvar_cameras_config(restantes)
                    st.session_state["cam_feedback"] = ("success", f"🗑️ Câmera `{cam['nome']}` removida.")
                except Exception as e:
                    st.session_state["cam_feedback"] = ("error", f"❌ Erro ao remover câmera: {e}")
                _rerun_escopo(None)

    st.divider()
    with st.container(border=True):
        st.markdown("### ➕ Adicionar Nova Câmera / Fonte")
        col_n, col_f, col_a = st.columns([2.2, 1.2, 0.8])
        add_nome = col_n.text_input("Nome da nova área (ex: Varanda, Salão Superior)", value="", key="add_cam_nome")
        add_fps = col_f.slider("FPS alvo", min_value=0.5, max_value=10.0, value=2.0, step=0.5, key="add_cam_fps")
        col_a.write("")
        add_ativa = col_a.checkbox("Ativa", value=True, key="add_cam_ativa")

        add_tipo, add_val = _seletor_fonte_camera("add_new", "", videos_disponiveis)

        if st.button("➕ Adicionar Câmera", type="primary"):
            try:
                if not add_nome.strip():
                    raise ValueError("Informe o nome da nova câmera/área.")
                fonte_nova = _resolver_fonte_selecionada(add_tipo, add_val)

                base_id = sanitizar_id_camera(add_nome)
                ids_existentes = {c["id"] for c in cameras_cfg}
                novo_id = base_id
                sufixo = 2
                while novo_id in ids_existentes:
                    novo_id = f"{base_id[:28]}_{sufixo}"
                    sufixo += 1

                nova_lista = cameras_cfg + [{
                    "id": novo_id,
                    "nome": add_nome.strip(),
                    "fonte": fonte_nova,
                    "ativa": add_ativa,
                    "fps_alvo": add_fps,
                }]
                salvas = salvar_cameras_config(nova_lista)
                recem_criada = next(c for c in salvas if c["id"] == novo_id)
                extrair_frame_limpo_fonte(recem_criada["fonte"], Path(recem_criada["frame_limpo_path"]))
                st.session_state["cam_feedback"] = (
                    "success",
                    f"✅ Câmera `{recem_criada['nome']}` adicionada! Acesse **🎨 Editor de Mesas e Zonas** para mapear suas mesas.",
                )
            except Exception as e:
                st.session_state["cam_feedback"] = ("error", f"❌ Não foi possível adicionar câmera: {e}")
            _rerun_escopo(None)

    with st.expander("📡 Guia Rápido: URLs RTSP (Intelbras / Hikvision) e Dicas"):
        st.markdown(
            """
            - **Intelbras / Dahua:** `rtsp://admin:SENHA@IP_DO_DVR:554/cam/realmonitor?channel=1&subtype=0`
            - **Hikvision:** `rtsp://admin:SENHA@IP_CAMERA:554/Streaming/Channels/101`
            - **Webcam USB local:** informe apenas o índice numérico `0` (ou `1`, `2`).
            - **Senhas com símbolos:** codifique caracteres especiais na URL (`@` → `%40`, `#` → `%23`, `:` → `%3A`).
            - **Dica de CPU:** para mais de 2 câmeras simultâneas em CPU, reduza o *FPS alvo* para `1.5` ou `2.0`.
            """
        )


with aba_visao_geral:
    if auto_refresh:
        @st.fragment(run_every=2)
        def frag_visao_geral():
            renderizar_painel_ao_vivo()
        frag_visao_geral()
    else:
        renderizar_painel_ao_vivo()

with aba_camera:
    if auto_refresh:
        @st.fragment(run_every=2)
        def frag_camera():
            renderizar_camera_ao_vivo()
        frag_camera()
    else:
        renderizar_camera_ao_vivo()

with aba_relatorios:
    @st.fragment
    def frag_relatorios():
        renderizar_relatorios_etapa2()
    frag_relatorios()

with aba_historico:
    if auto_refresh:
        @st.fragment(run_every=5)
        def frag_historico():
            renderizar_historico()
        frag_historico()
    else:
        renderizar_historico()

with aba_editor:
    renderizar_editor_navegador()

with aba_cameras:
    renderizar_aba_cameras()
