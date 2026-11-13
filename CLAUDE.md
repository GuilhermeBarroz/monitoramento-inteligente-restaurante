# 🤖 CLAUDE.md — Guia do Sistema de Monitoramento de Mesas com IA

Este documento serve como referência técnica primária para desenvolvedores e agentes de IA (Claude, Antigravity, etc.). Ele resume as características fundamentais do software, delimita escopo e arquitetura, e conecta a documentação viva e o planejamento do projeto.

---

## 📚 Documentos de Referência Obrigatórios

Antes de implementar novas funcionalidades, consulte sempre os documentos dedicados do repositório:

1. **[DOCUMENTACAO.md](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/DOCUMENTACAO.md):**
   * Guia técnico vivo, comandos de execução de cada script, histórico detalhado de versões/implementações (**Changelog**) e diretrizes mandatórias de segurança.
2. **[PLANEJAMENTO.md](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/PLANEJAMENTO.md):**
   * Planejamento estratégico, definição da Máquina de Estados Finita (FSM), triplo de métricas de tempo, roadmap em 8 fases e matriz de requisitos de hardware.
3. **[README.md](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/README.md):**
   * Guia rápido de entrada, instruções de setup inicial e comandos básicos de inicialização.
4. **[deploy/README_DEPLOY.md](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/deploy/README_DEPLOY.md):**
   * Roteiro de instalação física em campo em Mini PCs Linux (Ubuntu/Debian) com Docker e acesso remoto seguro via Tailscale.

---

## 🎯 O Que É Característica do Software (Escopo & Funcionalidades Reais)

O projeto é um **Sistema de Visão Computacional para Inteligência Operacional de Restaurantes**, projetado no modelo híbrido **Edge + Cloud**, que transforma câmeras de segurança comuns (CFTV/RTSP) em ferramentas de gestão em tempo real.

### 1. Processamento de Visão Computacional na Borda (Edge AI)
* **Sem sensores físicos:** Utiliza apenas o fluxo de vídeo de câmeras IP convencionais (Intelbras, Hikvision, Dahua, TP-Link, Webcams ou arquivos de vídeo MP4).
* **Detecção Ultralytics YOLOv10 (Nano/Base):** Detecção de pessoas sem necessidade de algoritmo NMS pós-processamento, com licença permissiva comercial Apache 2.0.
* **Geometria Espacial (Point-in-Polygon):** Rastreia os pés e centroides de clientes em relação a polígonos convexos ou livres desenhados sobre as mesas (`cv2.pointPolygonTest` e `shapely`).
* **Filtros Temporais (Debounce):** Exige detecção estável por vários frames consecutivos para confirmar ocupação ou desocupação, evitando falsos positivos por pedestres que apenas cruzam o salão.

### 2. Máquina de Estados Finita (FSM) e o Triplo de Métricas
Cada mesa do salão percorre um ciclo de vida rigorosamente controlado por máquina de estados ([src/fsm_mesas.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/fsm_mesas.py)):
* `LIVRE` ➔ `AGUARDANDO_ATENDIMENTO` ➔ `ANOTANDO_PEDIDO` ➔ `AGUARDANDO_ENTREGA` ➔ `REFEICAO_ANDAMENTO` ➔ `PENDENTE_LIMPEZA` ➔ `LIVRE`.
* **Métrica 1 — Tempo de 1ª Abordagem:** Desde o cliente sentar até o garçom chegar à mesa para anotar o pedido (alerta padrão: > 5 min).
* **Métrica 2 — Tempo de Cozinha / Entrega:** Desde a anotação do pedido até a refeição ser servida aos clientes (alerta padrão: > 20 min).
* **Métrica 3 — Tempo de Giro / Higienização:** Desde o cliente levantar até a mesa ser limpa e liberada para o próximo cliente (alerta padrão: > 3 min).

### 3. Filtros Inteligentes Anti-Falso Positivo
* **Diferenciação de Mise en Place vs Refeição:** Garçons costumam levar pratos vazios, talheres ou bebidas nos primeiros minutos. O sistema ignora visitas relâmpago (< 10 segundos) dentro da janela inicial da cozinha (< 4 a 6 min), evitando encerrar prematuramente o cronômetro do prato principal.
* **Detecção Automática de Garçom:** Identifica a presença do atendente na mesa por **excedente estável de pessoas** acima da linha de base de clientes sentados (com reversão inteligente se for apenas um cliente atrasado que se juntou ao grupo).
* **Classificador de Uniforme HSV (Opcional):** Permite configurar a faixa de cor do uniforme/avental do atendente no espaço de cor HSV para confirmação refinada de atendimento.

### 4. Orquestração Multi-Câmera & Troca Dinâmica de Fontes
* **Modelo Compartilhado em Memória:** O orquestrador [src/multi_cameras.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/multi_cameras.py) compartilha uma única instância do modelo YOLOv10 na memória RAM/VRAM entre todas as câmeras ativas, economizando recursos computacionais.
* **Hot-Reload de Câmeras:** Cadastro, pausa, ativação e exclusão de câmeras em `data/cameras_config.json` sem necessidade de reiniciar o motor de visão ou recarregar os pesos da IA.
* **Troca Dinâmica de Vídeo/RTSP:** Permite alternar fontes de vídeo (arquivos MP4 de até 500 MB ou fluxos RTSP de câmeras reais) diretamente pela interface web com validação estrita anti-SSRF.

### 5. Editor Visual de Zonas no Navegador
* **Canvas Interativo HTML5:** Componente web via `st.components.v2` ([src/zone_editor_component.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/zone_editor_component.py)) integrado ao dashboard.
* **Sem Reiniciar o Motor:** Permite desenhar retângulos ou polígonos livres, arrastar vértices, renomear mesas e aplicar calibração ao vivo no motor em segundo plano via escrita atômica em `data/mesas_config.json`.

### 6. Motor Estatístico e Analítico (Etapa 2)
* **Previsão de Giro por Mediana Hierárquica:** Estima o tempo restante até a liberação de cada mesa ocupada através de mediana histórica hierárquica resistente a outliers:
  1. Histórico da própria mesa (se $\ge 3$ amostras).
  2. Histórico da câmera/setor (se $\ge 3$ amostras).
  3. Histórico global do restaurante (se $\ge 3$ amostras).
  4. Baseline padrão configurado.
* **Dimensionamento de Equipe & Picos de Fluxo:** Agregações históricas por dia da semana e hora do dia, calculando ocupação média e sugerindo a quantidade ideal de garçons com base na meta de mesas por atendente.
* **Relatórios e Auditoria:** Extrato completo de visitas concluídas, auditoria de alertas disparados, taxas de SLA, exportação de dados em CSV e geração de Relatório Executivo Imprimível em HTML com escape anti-XSS.

### 7. Persistência de Dados e Resiliência
* **SQLite em Modo WAL (`data/historico_salao.db`):** Permite leitura e escrita simultâneas sem bloqueio de banco entre o motor de visão e o dashboard.
* **Consultas 100% Parametrizadas:** Todas as queries em [src/analytics_db.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/analytics_db.py) usam placeholders `?` para eliminação total de riscos de SQL Injection.
* **Persistência Atômica:** O arquivo de estado instantâneo `data/estado_atual.json` e as imagens de visualização ao vivo são salvos via arquivos temporários e `os.replace`, prevenindo condições de corrida (*race conditions*).
* **Ingestão RTSP Resiliente ([src/camera_stream.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/camera_stream.py)):** Thread assíncrona com buffer zerado para eliminar lag, reconexão automática contínua em caso de oscilação de rede e amostragem configurada para 3~5 FPS (reduzindo consumo de CPU em > 80%).

---

## 🚫 O Que NÃO É Característica do Software (Limites e Premissas)

Para preservar a integridade da arquitetura, privacidade (LGPD) e o desempenho, é essencial reconhecer o que o software **não faz e não deve fazer**:

1. **NÃO faz reconhecimento facial nem biometria individual:**
   * O sistema não identifica identidades, CPFs, rostos ou dados pessoais de clientes. Ele detecta unicamente caixas delimitadoras (*bounding boxes*) da classe `person` e localiza o contato dos pés com o solo.
2. **NÃO envia vídeo bruto nem imagens para a nuvem:**
   * Nenhum fluxo de vídeo ou foto de clientes é transmitido pela internet. O processamento de Visão Computacional é **100% local (Edge)** no computador/servidor do restaurante.
3. **NÃO exige internet contínua para a operação local:**
   * Se o restaurante ficar sem internet, o motor de visão, o dashboard local (`:8501`), os cronômetros, os alertas sonoros e a gravação em SQLite continuam funcionando perfeitamente.
4. **NÃO executa inferência de IA na nuvem:**
   * Toda a execução dos modelos de redes neurais roda no processador (CPU) ou acelerador local do restaurante, garantindo latência zero e custo zero de GPU em nuvem por cliente.

---

## ☁️ Arquitetura Híbrida: Local (Edge) vs Portal Remoto (Cloud / SaaS)

O software foi projetado para operar em dois ambientes integrados:

```text
[ RESTAURANTE (EDGE / LOCAL) ]
  Câmeras IP / RTSP
          │
          ▼
  motor_estados.py ──▶ historico_salao.db (SQLite WAL) + estado_atual.json
          │                        │
          ▼                        ▼ (Agente Edge Sync - Apenas JSON Anônimo)
  Dashboard Local (:8501)       Envio em lote via HTTPS (sem fotos / sem vídeos)
  (Caixa / Salão / Balcão)                 │
                                           ▼
[ NUVEM (PORTAL EXTERNO / SAAS) ]
  API de Ingestão Multi-Tenant ──▶ PostgreSQL Central (com tenant_id / estabelecimento_id)
                                           │
                                           ▼
  Portal Gerencial Web (Acesso remoto via Login Google OAuth)
  ├── Visão do Dono/Gerente: KPIs, relatórios e alertas apenas do seu estabelecimento
  └── Visão do Dono da Plataforma (Admin Global): Seleção e auditoria de todos os clientes
```

* **Operação Local:** Roda no Mini PC do restaurante (porta `8501`), voltado ao operador do caixa e garçons.
* **Hospedagem da nuvem:** VPS própria com Docker + Traefik (já existente). A API de ingestão e o `edge-sync` já estão implementados (Fase 9, parte 1). O portal será em Streamlit, com agregações no Postgres e `st.cache_data`.
* **Portal Externo com Login Google (Fase Atual em Desenvolvimento):**
  * O dono do estabelecimento consulta os relatórios históricos e métricas consolidadas pelo celular ou computador de fora do restaurante.
  * Autenticação via Google OAuth 2.0 com lista fechada de e-mails autorizados (*allowlist*).
  * O modelo de dados em nuvem já nasce com `estabelecimento_id` e permissões de perfil (`role`), permitindo que o administrador do sistema tenha acesso global multi-cliente.

---

## 📁 Estrutura de Diretórios e Arquivos-Chave

```text
MonitoramentoCameracom IA/
├── CLAUDE.md                          # Este guia mestre de referência rápida
├── DOCUMENTACAO.md                    # Documentação técnica oficial e changelog
├── PLANEJAMENTO.md                    # Planejamento estratégico, FSM e roadmap
├── README.md                          # Guia de introdução rápida
├── Dockerfile                         # Container multi-stage Python 3.11 com OpenCV/FFmpeg
├── docker-compose.yml                 # Orquestração do Dashboard (:8501) e Engine de IA
├── requirements.txt                   # Dependências Python (ultralytics, opencv, shapely, streamlit)
├── yolov10n.pt / yolov10b.pt          # Pesos locais dos modelos YOLOv10
├── data/
│   ├── cameras_config.json            # Cadastro de câmeras (RTSP, vídeos locais, webcams)
│   ├── mesas_config.json              # Coordenadas dos polígonos das mesas (câmera principal)
│   ├── cameras/                       # Arquivos de polígonos de câmeras secundárias
│   ├── regras_config.json             # Limites de tolerância de tempo configurados
│   ├── estado_atual.json              # Snapshot atômico consolidado do salão em tempo real
│   ├── historico_salao.db             # Banco SQLite analítico WAL (eventos, visitas, ocupação)
│   ├── alertas_log.json               # Histórico estruturado de alertas em JSON
│   └── videos/                        # Vídeos de teste, frames anotados e frames de referência
└── src/
    ├── camera_stream.py               # Ingestão resiliente de RTSP com zero-lag e auto-reconexão
    ├── config_cameras.py              # Validação de câmeras, sanitização anti-SSRF e snapshots
    ├── config_mesas.py                # Validação geométrica e persistência atômica das zonas
    ├── zone_editor_component.py       # Componente interativo de edição de polígonos em Canvas
    ├── detector_garcom.py             # Detecção de garçom por excedente estável e análise HSV
    ├── fsm_mesas.py                   # Máquina de Estados Finita e cronômetros das mesas
    ├── motor_ocupacao.py              # Algoritmo de Point-in-Polygon e contagem de ocupação
    ├── motor_estados.py               # Engine principal que une IA, FSM, SQLite e estado ao vivo
    ├── multi_cameras.py               # Orquestrador multi-câmera com modelo YOLO compartilhado
    ├── analytics_db.py                # Repositório SQLite WAL parametrizado e motor de medianas
    ├── dashboard.py                   # Painel web operacional e gerencial em Streamlit
    ├── test_analytics_etapa2.py       # Testes automatizados do banco analítico e medianas
    ├── edge_sync.py                   # Agente que envia metadados anônimos do SQLite para a API em nuvem
    ├── test_edge_sync.py              # Testes do Edge Sync (cursor, falhas de rede, colunas permitidas)
    ├── test_fsm_simulacao.py          # Simulador determinístico offline da FSM
    └── test_multi_cameras.py          # Testes automatizados de multi-câmeras e segurança SSRF
cloud/                                 # Tudo que roda na VPS (Docker + Traefik existente)
├── docker-compose.yml                 # api + postgres (portal Streamlit entra aqui na próxima etapa)
├── README.md                          # Deploy na VPS e CLI de administração
└── api/                               # FastAPI de ingestão multi-tenant (token por estabelecimento)
    ├── app/main.py, schemas.py, db.py, admin.py, schema.sql
    └── tests/test_api.py              # Testes da API com banco simulado
```

---

## 🛠️ Comandos Mais Utilizados

### 1. Executar Testes Automatizados
```bash
# Executa a suíte de testes analíticos da Etapa 2
python src/test_analytics_etapa2.py

# Executa testes do orquestrador multi-câmeras e segurança SSRF
python src/test_multi_cameras.py

# Executa simulação determinística offline da FSM e uniforme HSV
python src/test_fsm_simulacao.py
```

### 2. Execução Local com Python
```powershell
# Ativar ambiente virtual
.\.venv\Scripts\activate

# Iniciar o motor de visão em loop contínuo
python src/motor_estados.py --loop

# Iniciar o Dashboard Streamlit (porta 8501)
streamlit run src/dashboard.py
```

### 3. Execução em Produção via Docker
```bash
# Subir ambos os serviços (motor de IA + dashboard) em background
docker compose up -d

# Visualizar logs em tempo real
docker compose logs -f

# Verificar status e healthcheck
docker compose ps
```

---

## 🛡️ Diretrizes Críticas para Desenvolvedores e Agentes de IA

Ao criar ou editar qualquer código neste projeto, **siga obrigatoriamente estas regras**:

1. **Prevenção Estrita de SQL Injection:**
   * **Nunca** use concatenação de strings ou `f-strings` para montar comandos SQL (`f"SELECT ... WHERE id = '{id}'"` é terminantemente proibido).
   * **Sempre** utilize queries parametrizadas com placeholders `?` no SQLite ou vinculação tipada no banco de destino.
2. **Gravação Atômica de Arquivos:**
   * Arquivos JSON compartilhados entre processos (`estado_atual.json`, `mesas_config.json`, etc.) devem ser gravados primeiro em um arquivo temporário (`.tmp`) e depois renomeados atomicamente com `os.replace`.
3. **Proteção Anti-SSRF em URLs de Câmeras:**
   * Qualquer adição de fluxo RTSP ou HTTP externo deve ser validada por [src/config_cameras.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/config_cameras.py) para barrar faixas de loopback ou esquemas perigosos (`file://`, portas de metadados de nuvem, etc.).
4. **Respeito ao Fuso Horário Local:**
   * O sistema adota `America/Sao_Paulo` como padrão para datas, turnos e relatórios em [src/analytics_db.py](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/src/analytics_db.py) e no Docker.
5. **Atualização do Changelog:**
   * Ao finalizar uma nova implementação relevante ou refatoração, documente a alteração na seção de Changelog de [DOCUMENTACAO.md](file:///c:/Users/Guilherme/Documents/Antigravity/MonitoramentoCameracom%20IA/DOCUMENTACAO.md).

