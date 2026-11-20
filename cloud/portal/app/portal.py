"""
Portal de Relatórios em nuvem (Streamlit + login Google).

- Dono/gerente: vê só as lojas vinculadas a ele.
- Admin da plataforma: escolhe qualquer loja e vê a saúde (último sync) de todas.
Nenhuma imagem passa por aqui: o banco só tem metadados anônimos enviados pelo edge-sync.

Executar: streamlit run app/portal.py  (a partir de cloud/portal, com PYTHONPATH=.)
"""

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import acesso, calculos, consultas  # noqa: E402

LINHAS_EXIBIDAS_TABELA = 300
PERIODOS = {"Últimas 24h": 1, "Últimos 7 dias": 7, "Últimos 30 dias": 30, "Últimos 90 dias": 90, "Todo o histórico": None}
TODAS = "__todas__"

st.set_page_config(page_title="Relatórios — Monitor de Mesas", page_icon="📊", layout="wide")


# ---------------------------------------------------------------------------
# Banco (pool único por processo; caches SEMPRE chaveados pelo id da loja)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _pool():
    from app import db  # import tardio: a tela de login não depende do driver do banco

    return db.criar_pool()


@st.cache_data(ttl=300, max_entries=200, show_spinner=False)
def _cameras(est_id: int) -> list:
    with _pool().connection() as conn:
        return consultas.listar_cameras(conn, est_id)


@st.cache_data(ttl=300, max_entries=100, show_spinner="Calculando relatórios...")
def _dados_banco(est_id: int, tz: str, dias, camera_id) -> dict:
    """Só o que exige SQL. Fica fora da chave a proporção mesas/garçom (cálculo puro)."""
    desde = consultas.desde_epoch(dias)
    with _pool().connection() as conn:
        return {
            "resumo": consultas.resumo_geral(conn, est_id, desde, camera_id),
            "desempenho": consultas.desempenho_por_mesa(conn, est_id, desde, camera_id),
            "por_tipo": consultas.alertas_por_tipo(conn, est_id, desde, camera_id),
            "celulas": consultas.agregados_pico(conn, est_id, tz, desde, camera_id),
        }


def _dados_relatorio(est_id: int, tz: str, dias, camera_id, ratio: int) -> dict:
    b = _dados_banco(est_id, tz, dias, camera_id)
    matriz = calculos.montar_matriz(*b["celulas"], ratio)
    por_hora = calculos.resumo_por_hora(matriz, ratio)
    return {
        "resumo": b["resumo"],
        "desempenho": b["desempenho"],
        "por_tipo": b["por_tipo"],
        "matriz": matriz,
        "por_hora": por_hora,
        "por_dia": calculos.resumo_por_dia_semana(matriz, ratio),
        "turnos": calculos.ranking_turnos(por_hora, ratio),
    }


@st.cache_data(ttl=300, max_entries=8, show_spinner="Carregando extrato...")
def _dados_extrato(est_id: int, tz: str, dias, camera_id) -> dict:
    desde = consultas.desde_epoch(dias)
    with _pool().connection() as conn:
        visitas, truncado = consultas.visitas_detalhadas(conn, est_id, tz, desde, camera_id)
        alertas = consultas.relatorio_alertas(conn, est_id, tz, desde, camera_id)
    return {"visitas": visitas, "visitas_truncado": truncado, "alertas": alertas}


@st.cache_data(ttl=60, show_spinner=False)
def _saude() -> list:
    with _pool().connection() as conn:
        return consultas.saude_lojas(conn)


@st.cache_resource(show_spinner=False)
def _negacoes_recentes() -> dict:
    """email -> instante da última negação auditada (compartilhado entre sessões do processo)."""
    return {}


def _deve_auditar_negacao(email: str, janela_seg: float = 3600.0) -> bool:
    """Uma linha de `acesso_negado` por e-mail por hora: recarregar a página não inunda a auditoria."""
    agora = time.time()
    negacoes = _negacoes_recentes()
    if agora - negacoes.get(email, 0.0) < janela_seg:
        return False
    if len(negacoes) > 10_000:  # teto de memória contra e-mails descartáveis em massa
        negacoes.clear()
    negacoes[email] = agora
    return True


def _auditar(email: str, acao: str, est_id=None):
    with _pool().connection() as conn:
        acesso.registrar_acesso(conn, email, acao, est_id)


# ---------------------------------------------------------------------------
# Login e autorização
# ---------------------------------------------------------------------------
def _tela_login():
    st.title("📊 Relatórios do Salão")
    st.write("Acesse os relatórios do seu restaurante de qualquer lugar.")
    st.button("Entrar com Google", type="primary", on_click=st.login, args=("google",))
    st.caption("Acesso restrito a e-mails autorizados pelo administrador da plataforma.")
    st.stop()


def _negar(mensagem: str):
    st.error(mensagem)
    st.button("Sair", on_click=st.logout)
    st.stop()


def _autenticar() -> acesso.Perfil:
    if not st.user.is_logged_in:
        _tela_login()
    email = acesso.email_da_identidade(st.user.to_dict())
    if email is None:
        _negar("Sua conta Google não tem e-mail verificado. Use outra conta.")
    with _pool().connection() as conn:
        perfil = acesso.carregar_perfil(conn, email)
        if perfil is None:
            if st.session_state.get("_negado_auditado") != email and _deve_auditar_negacao(email):
                acesso.registrar_acesso(conn, email, "acesso_negado")
                st.session_state["_negado_auditado"] = email
            _negar(
                "Este e-mail não está autorizado a acessar o portal. "
                "Peça ao administrador para liberar o seu acesso."
            )
        # Marca guarda o e-mail: outra conta na mesma sessão do navegador também é auditada
        if st.session_state.get("_login_auditado") != email:
            acesso.registrar_login(conn, email)
            st.session_state["_login_auditado"] = email
    return perfil


# ---------------------------------------------------------------------------
# Seções de relatório
# ---------------------------------------------------------------------------
def _kpis(resumo: dict):
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Visitas / Giros", f"{resumo['total_visitas']}")
    k2.metric(
        "SLA Atendimento",
        calculos.fmt_pct(resumo["sla_sem_alerta_pct"]),
        help="Visitas sem alerta de espera do garçom nem de atraso da cozinha.",
    )
    k3.metric(
        "Permanência Mediana",
        calculos.fmt_min(resumo["permanencia_mediana_min"]),
        delta=f"P90: {calculos.fmt_min(resumo['permanencia_p90_min'])}" if resumo["total_visitas"] else None,
        delta_color="off",
    )
    k4.metric("Espera Garçom", calculos.fmt_min(resumo["espera_mediana_min"]))
    k5.metric(
        "Limpeza Mediana",
        calculos.fmt_min(resumo["limpeza_mediana_min"]),
        delta=f"SLA limpeza: {calculos.fmt_pct(resumo['sla_limpeza_pct'])}" if resumo["sla_limpeza_pct"] is not None else None,
        delta_color="off",
        help="SLA de limpeza: mesas higienizadas sem alerta de mesa suja.",
    )
    k6.metric("Alertas no Período", f"{resumo['total_alertas']}")


def _botao_download(rotulo: str, dados: bytes, nome: str, mime: str, chave: str, email: str, est_id: int):
    st.download_button(
        rotulo, data=dados, file_name=nome, mime=mime, key=chave,
        on_click=_auditar, args=(email, "exportar", est_id),
    )


def _secao_desempenho(d: dict, rel_alertas: dict, loja: acesso.Loja, periodo: str, camera_label: str, email: str):
    st.markdown("### 🏆 Desempenho Operacional por Mesa")
    desempenho = d["desempenho"]
    if desempenho:
        df = pd.DataFrame([
            {
                "Câmera": x["camera_nome"],
                "Mesa": x["mesa_id"],
                "Visitas": x["visitas"],
                "Mediana Perm. (min)": x["permanencia_mediana_min"],
                "P90 Perm. (min)": x["permanencia_p90_min"],
                "Espera Garçom (min)": x["espera_mediana_min"],
                "Limpeza (min)": x["limpeza_mediana_min"],
                "Alertas": x["alertas"],
                "SLA OK (%)": x["sla_ok_pct"],
            }
            for x in desempenho
        ])
        st.dataframe(
            df, width="stretch", hide_index=True,
            column_config={
                "Mediana Perm. (min)": st.column_config.NumberColumn(format="%.1f"),
                "P90 Perm. (min)": st.column_config.NumberColumn(format="%.1f"),
                "Espera Garçom (min)": st.column_config.NumberColumn(format="%.1f"),
                "Limpeza (min)": st.column_config.NumberColumn(format="%.1f"),
                "SLA OK (%)": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            },
        )
        c1, c2 = st.columns(2)
        with c1:
            _botao_download("📥 Desempenho por Mesa (CSV)", calculos.csv_seguro(df),
                            "desempenho_mesas.csv", "text/csv", "dl_mesas", email, loja.id)
        with c2:
            html_rel = calculos.gerar_relatorio_html(loja.nome, periodo, camera_label, d["resumo"], desempenho, d["turnos"])
            _botao_download("🖨️ Relatório Gerencial Imprimível (.html)", html_rel,
                            "relatorio_gerencial.html", "text/html", "dl_html", email, loja.id)
    else:
        st.info("Ainda não há histórico por mesa no período selecionado.")

    st.markdown("### 🚨 Alertas por Tipo")
    por_tipo = rel_alertas["por_tipo"]
    if por_tipo:
        df_t = pd.DataFrame(
            [{"Tipo de Alerta": k, "Ocorrências": v} for k, v in sorted(por_tipo.items(), key=lambda x: -x[1])]
        )
        c1, c2 = st.columns([1.2, 1.8])
        c1.dataframe(df_t, width="stretch", hide_index=True)
        c2.bar_chart(df_t.set_index("Tipo de Alerta"))
    else:
        st.success("✅ Nenhum alerta registrado neste período.")


def _secao_pico(d: dict, ratio: int, email: str, est_id: int):
    st.markdown("### 👔 Pico por Hora / Dia e Escala de Garçons")
    por_hora = d["por_hora"]
    if not any(h["ocupacao_media_mesas"] > 0 or h["giros_visitas"] > 0 or h["alertas_disparados"] > 0 for h in por_hora):
        st.info("Ainda não há amostras de ocupação suficientes no período para as curvas de pico e escala.")
        return

    st.markdown("#### 🕐 Escala Sugerida por Turno")
    st.dataframe(pd.DataFrame([
        {
            "Turno": t["turno"],
            "Ocupação Média (mesas)": t["ocupacao_media_mesas"],
            "Pico (mesas)": t["ocupacao_pico_mesas"],
            "Visitas Iniciadas": t["giros_visitas"],
            "Alertas": t["alertas_disparados"],
            "Escala Sugerida": t["escala_sugerida"],
        }
        for t in d["turnos"]
    ]), width="stretch", hide_index=True)

    df_h = pd.DataFrame(por_hora).set_index("hora_label")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### 📈 Mesas Ocupadas por Hora")
        st.bar_chart(df_h[["ocupacao_media_mesas", "ocupacao_pico_mesas"]].rename(
            columns={"ocupacao_media_mesas": "Média", "ocupacao_pico_mesas": "Pico"}))
    with c2:
        st.markdown(f"#### 👔 Garçons por Hora (1 a cada {ratio} mesas)")
        st.bar_chart(df_h[["garcons_recomendados_medio", "garcons_recomendados_pico"]].rename(
            columns={"garcons_recomendados_medio": "Média", "garcons_recomendados_pico": "Pico"}))

    st.markdown("#### 📅 Movimento por Dia da Semana")
    st.dataframe(pd.DataFrame([
        {
            "Dia": x["dia_nome"],
            "Ocupação Média (mesas)": x["ocupacao_media_mesas"],
            "Pico (mesas)": x["ocupacao_pico_mesas"],
            "Visitas": x["giros_visitas"],
            "Alertas": x["alertas_disparados"],
            "Garçons no Pico": x["garcons_recomendados_pico"],
        }
        for x in d["por_dia"]
    ]), width="stretch", hide_index=True)

    if d["matriz"]:
        st.markdown("#### 🗓️ Dia da Semana × Hora (ocupação média de mesas)")
        df_m = pd.DataFrame(d["matriz"])
        piv = df_m.pivot(index="dia_nome", columns="hora_label", values="ocupacao_media_mesas")
        piv = piv.reindex([x for x in calculos.DIAS_SEMANA_PT if x in piv.index]).fillna(0.0)
        st.dataframe(piv, width="stretch")
        _botao_download("📥 Pico e Escala Completo (CSV)", calculos.csv_seguro(df_m),
                        "pico_escala.csv", "text/csv", "dl_pico", email, est_id)


def _secao_extrato(ext: dict, email: str, est_id: int):
    st.markdown("### 🧾 Extrato de Visitas Concluídas")
    visitas = ext["visitas"]
    if visitas:
        df_v = pd.DataFrame([
            {
                "Entrada": v["entrada"], "Saída": v["saida"], "Câmera": v["camera_nome"], "Mesa": v["mesa_id"],
                "Pessoas": v["pessoas"], "Permanência (min)": v["permanencia_min"],
                "Espera Garçom (min)": v["espera_garcom_min"], "Preparo (min)": v["preparo_cozinha_min"], "SLA": v["sla"],
            }
            for v in visitas
        ])
        if len(df_v) > LINHAS_EXIBIDAS_TABELA:
            st.caption(f"Exibindo as {LINHAS_EXIBIDAS_TABELA} visitas mais recentes de {len(df_v)}. O CSV traz todas.")
        if ext["visitas_truncado"]:
            st.warning(f"Período muito grande: o CSV traz as {consultas.LIMITE_EXPORTACAO} visitas mais recentes. Reduza o período.")
        st.dataframe(df_v.head(LINHAS_EXIBIDAS_TABELA), width="stretch", hide_index=True)
        _botao_download("📥 Extrato de Visitas (CSV)", calculos.csv_seguro(df_v),
                        "visitas.csv", "text/csv", "dl_visitas", email, est_id)
    else:
        st.info("Nenhuma visita concluída no período selecionado.")

    st.divider()
    st.markdown("### ⚠️ Ocorrências de Alertas")
    lista = ext["alertas"]["alertas"]
    if lista:
        df_a = pd.DataFrame([
            {"Data/Hora": a["data_hora"], "Câmera": a["camera_nome"], "Mesa": a["mesa_id"],
             "Tipo de Alerta": a["tipo_alerta"], "Tempo Decorrido": a["tempo_decorrido"]}
            for a in lista
        ])
        if len(df_a) > LINHAS_EXIBIDAS_TABELA:
            st.caption(f"Exibindo os {LINHAS_EXIBIDAS_TABELA} alertas mais recentes de {len(df_a)}. O CSV traz todos.")
        if ext["alertas"]["truncado"]:
            st.warning(f"Período muito grande: o CSV traz os {consultas.LIMITE_EXPORTACAO} alertas mais recentes.")
        st.dataframe(df_a.head(LINHAS_EXIBIDAS_TABELA), width="stretch", hide_index=True)
        _botao_download("📥 Log de Alertas (CSV)", calculos.csv_seguro(df_a),
                        "alertas.csv", "text/csv", "dl_alertas", email, est_id)
    else:
        st.info("Nenhum alerta no período selecionado.")


def _pagina_relatorios(perfil: acesso.Perfil):
    if not perfil.lojas:
        st.info("Nenhuma loja ativa está vinculada ao seu acesso. Fale com o administrador da plataforma.")
        return

    nomes = {l.id: (l.nome if l.ativo else f"{l.nome} (desativada)") for l in perfil.lojas}
    c_loja, c_per, c_cam, c_ratio = st.columns([1.6, 1.1, 1.4, 0.9])
    est_sel = c_loja.selectbox("Restaurante:", list(nomes), format_func=nomes.get, key="loja")
    loja = acesso.loja_autorizada(perfil, est_sel)
    if loja is None:  # defesa em profundidade: nunca consultar loja fora do escopo
        st.error("Loja não autorizada.")
        return

    if st.session_state.get("_loja_auditada") != loja.id:
        _auditar(perfil.email, "ver_relatorios", loja.id)
        st.session_state["_loja_auditada"] = loja.id

    periodo = c_per.selectbox("Período:", list(PERIODOS), index=2, key="periodo")
    cams = _cameras(loja.id)
    mapa_cam = {TODAS: "Todas as câmeras"} | {c["camera_id"]: f"📷 {c['camera_nome']}" for c in cams}
    cam_sel = c_cam.selectbox("Câmera / salão:", list(mapa_cam), format_func=mapa_cam.get, key=f"cam_{loja.id}")
    ratio = int(c_ratio.number_input("Mesas / garçom:", min_value=1, max_value=12, value=4, step=1, key="ratio"))
    camera_id = None if cam_sel == TODAS else cam_sel

    d = _dados_relatorio(loja.id, loja.timezone, PERIODOS[periodo], camera_id, ratio)
    st.caption(f"Horários no fuso da loja ({loja.timezone}). Dados atualizados a cada 5 minutos.")
    _kpis(d["resumo"])
    st.divider()

    aba_des, aba_pico, aba_ext = st.tabs(["📋 Desempenho & SLA", "👔 Pico & Escala", "🧾 Extrato & Alertas"])
    with aba_des:
        _secao_desempenho(d, {"por_tipo": d["por_tipo"]}, loja, periodo, mapa_cam[cam_sel], perfil.email)
    with aba_pico:
        _secao_pico(d, ratio, perfil.email, loja.id)
    with aba_ext:
        # O st.tabs executa todas as abas a cada clique: o extrato (até 50 mil linhas)
        # só é consultado e exportado quando o usuário pede.
        if st.toggle("Carregar extrato detalhado de visitas e alertas", key="carregar_extrato"):
            ext = _dados_extrato(loja.id, loja.timezone, PERIODOS[periodo], camera_id)
            _secao_extrato(ext, perfil.email, loja.id)
        else:
            st.caption("Ative para listar e exportar cada visita e cada alerta do período.")


def _pagina_saude(perfil: acesso.Perfil):
    st.markdown("### 🩺 Saúde das Lojas")
    st.caption("Último envio do edge-sync e último dado de ocupação recebido (motor parado = sem amostras novas).")
    if not st.session_state.get("_saude_auditada"):
        _auditar(perfil.email, "ver_saude")
        st.session_state["_saude_auditada"] = True
    lojas = _saude()
    if not lojas:
        st.info("Nenhuma loja cadastrada.")
        return
    st.dataframe(pd.DataFrame([
        {
            "#": x["id"], "Loja": x["nome"], "Status": x["status"], "Último sync": x["ultimo_sync"],
            "Último dado (min atrás)": x["min_desde_ultima_amostra"], "Fuso": x["fuso"],
        }
        for x in lojas
    ]), width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------
def main():
    perfil = _autenticar()
    with st.sidebar:
        st.text(perfil.nome or perfil.email)  # texto puro: nome cadastrado não é interpretado como markdown
        st.caption(f"{perfil.email} · {'Administrador' if perfil.is_admin else 'Gerente'}")
        st.button("Sair", on_click=st.logout, width="stretch")

    st.title("📊 Relatórios do Salão")
    if perfil.is_admin:
        aba_rel, aba_saude = st.tabs(["📊 Relatórios", "🩺 Saúde das Lojas"])
        with aba_rel:
            _pagina_relatorios(perfil)
        with aba_saude:
            _pagina_saude(perfil)
    else:
        _pagina_relatorios(perfil)


try:
    main()
except Exception as e:  # noqa: BLE001
    # st.stop()/rerun usam exceções próprias do Streamlit: deixar passar
    if type(e).__module__.startswith("streamlit"):
        raise
    print(f"Erro no portal: {type(e).__name__}: {e}")
    st.error("Não foi possível carregar os relatórios agora. Tente novamente em instantes.")
