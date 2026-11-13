# 📖 Documentação do Projeto: Monitoramento Inteligente de Mesas com IA

Sistema de Visão Computacional para monitoramento em tempo real do estado de mesas em restaurantes, medindo tempos de espera, atendimento do garçom, entrega de pedidos e ociosidade de mesas desocupadas.

---

## 🧭 Sumário
1. [Visão Geral e Objetivos](#-visão-geral-e-objetivos)
2. [Estrutura do Repositório](#-estrutura-do-repositório)
3. [Tecnologias Utilizadas](#-tecnologias-utilizadas)
4. [Instalação e Ambiente](#-instalação-e-ambiente)
5. [Como Executar os Módulos](#-como-executar-os-módulos)
6. [Regras de Negócio e Estados](#-regras-de-negócio-e-estados)
7. [Diretrizes de Segurança (SQL Injection e Entradas)](#-diretrizes-de-segurança-sql-injection-e-entradas)
8. [Histórico de Implementações e Features (Changelog)](#-histórico-de-implementações-e-features-changelog)
9. [Próximas Funcionalidades](#-próximas-funcionalidades)

---

## 🎯 Visão Geral e Objetivos
O projeto transforma qualquer câmera de segurança (CFTV/RTSP) em uma ferramenta de inteligência operacional para restaurantes:
* **Tempo de 1ª Abordagem:** Alerta se uma mesa ocupada estiver há mais de 5 minutos sem atendimento.
* **Tempo de Cozinha / Entrega:** Monitora o tempo entre a anotação do pedido e a chegada real da refeição à mesa.
* **Giro de Mesas / Limpeza:** Detecta mesas desocupadas e alerta para limpeza rápida, liberando espaço para novos clientes.
* **Filtros Anti-Falso Positivo:** Diferenciação inteligente entre entrega preliminar de talheres/pratos vazios (*mise en place*) e a entrega real da refeição.

---

## 📁 Estrutura do Repositório

```text
MonitoramentoCameracom IA/
├── data/
│   ├── cameras_config.json            # Cadastro e origem (RTSP/vídeo/webcam) das câmeras ativas
│   ├── mesas_config.json              # Coordenadas e polígonos das mesas da câmera principal (cam_1)
│   ├── cameras/                       # Zonas calibradas e previews isolados das câmeras adicionais
│   ├── regras_config.json             # Limites de tempo configuráveis operacionalmente
│   ├── estado_atual.json              # Estado em tempo real consolidado de todas as câmeras
│   ├── historico_salao.db             # Banco SQLite analítico (visitas concluídas, amostras horárias e eventos)
│   ├── alertas_log.json               # Histórico estruturado de eventos e alertas (com origem da câmera)
│   └── videos/
│       ├── sample_restaurante.mp4     # Vídeo de teste (6.304 frames, 720p, 30 FPS)
│       ├── frame_referencia.jpg       # Frame de referência extraído para calibração
│       ├── frame_limpo.jpg            # Frame limpo atual da câmera para o editor web
│       ├── frame_ao_vivo.jpg          # Frame anotado ao vivo exibido no Dashboard
│       ├── calibracao_mesas_preview.jpg # Preview com os polígonos das 5 mesas
│       ├── deteccao_teste.jpg         # Frame anotado resultante do teste do YOLO
│       ├── output_ocupacao.mp4        # Vídeo de saída com ocupação geométrica (Fase 4)
│       └── output_fsm_cronometros.mp4 # Vídeo de saída com cronômetros e FSM (Fase 5)
├── deploy/
│   └── README_DEPLOY.md               # Guia de instalação em campo para técnicos (Fase 8)
├── .streamlit/
│   └── config.toml                    # Configuração Streamlit (limite de upload de vídeo até 500 MB)
├── src/
│   ├── test_video.py                  # Inspeciona o vídeo e extrai frames
│   ├── test_yolo.py                   # Valida a inferência do modelo YOLOv10n
│   ├── calibrador_mesas.py            # Ferramenta interativa para desenhar polígonos das mesas
│   ├── gerar_calibracao.py            # Calibração e exportação das zonas das mesas
│   ├── motor_ocupacao.py              # Motor de detecção geométrica de ocupação (Fase 4)
│   ├── fsm_mesas.py                   # Máquina de Estados FSM e regras de cronômetro
│   ├── detector_garcom.py             # Detecção de atendimento (excedente estável + uniforme HSV)
│   ├── config_mesas.py                # Validação, sanitização e persistência atômica das zonas
│   ├── config_cameras.py              # Cadastro multi-câmera, validação SSRF/upload e captura de frame
│   ├── analytics_db.py                # Persistência SQLite parametrizada, mediana de permanência e pico/escala
│   ├── zone_editor_component.py       # Componente Canvas HTML5 interativo via st.components.v2
│   ├── motor_estados.py               # Motor integrado de cronômetros e alertas (Fase 5)
│   ├── multi_cameras.py               # Orquestrador multi-câmera com YOLOv10 compartilhado e hot-reload
│   ├── dashboard.py                   # Dashboard web interativo em Streamlit (Fase 6)
│   ├── camera_stream.py               # Ingestão resiliente de câmeras RTSP/CFTV (Fase 7)
│   ├── test_fsm_simulacao.py          # Simulador determinístico offline da FSM e uniformes HSV
│   ├── test_multi_cameras.py          # Testes automatizados multi-câmera, segurança de upload e hot-reload
│   └── test_analytics_etapa2.py       # Testes da Etapa 2 (mediana de giro, escala/pico e SQL parametrizado)
├── Dockerfile                         # Imagem Docker multi-plataforma otimizada
├── docker-compose.yml                 # Orquestração do Dashboard e do Motor em segundo plano
├── install.sh                         # Instalador automático em 1 linha para Linux
├── .gitignore                         # Arquivos ignorados pelo Git (.venv, pesos, etc.)
├── DOCUMENTACAO.md                    # Esta documentação técnica viva
├── PLANEJAMENTO.md                    # Arquitetura detalhada, diagrama FSM e roadmap
├── README.md                          # Guia rápido de entrada do projeto
├── requirements.txt                   # Lista de dependências Python
└── yolov10n.pt                        # Pesos locais do modelo YOLOv10 Nano
```

---

## 🛠️ Tecnologias Utilizadas
* **Linguagem:** Python 3.11.18 (gerenciado via `uv`).
* **Visão Computacional:** OpenCV (`opencv-python 5.0+`).
* **Deep Learning / Detecção:** Ultralytics YOLOv10 Nano (`yolov10n.pt`) — licença permissiva comercial Apache 2.0, sem necessidade de NMS pós-processamento.
* **Geometria Espacial:** `shapely` e algoritmos de contenção de ponto em polígono.
* **Processamento:** Otimizado para rodar em CPU convencional (~50 ms/frame) ou GPU NVIDIA.

---

## ⚙️ Instalação e Ambiente

### Pré-requisitos
* Git instalado.
* Python 3.11 ou superior (instalado localmente ou via `uv`).

### Ativação do Ambiente Virtual
No Windows (PowerShell):
```powershell
.\.venv\Scripts\activate
```

Caso queira reinstalar as dependências:
```powershell
pip install -r requirements.txt
```

---

## 🚀 Como Executar os Módulos

### 1. Inspecionar o Vídeo de Teste
Lê o arquivo de vídeo, exibe metadados técnicos (resolução, FPS, duração) e salva o frame base para calibração:
```powershell
.\.venv\Scripts\python.exe src\test_video.py
```

### 2. Testar Detecção de Pessoas (YOLOv10)
Roda a inferência no frame de referência e salva o resultado anotado com caixas delimitadoras em `data/videos/deteccao_teste.jpg`:
```powershell
.\.venv\Scripts\python.exe src\test_yolo.py
```

### 3. Calibrar Regiões das Mesas (ROI)
Abre uma janela visual interativa onde você clica com o mouse para desenhar os polígonos de cada mesa:
```powershell
.\.venv\Scripts\python.exe src\calibrador_mesas.py
```
* **Botão Esquerdo do Mouse:** Marca os vértices da mesa na tela.
* **Tecla `F`:** Finaliza o polígono da mesa atual e solicita o nome no terminal (ex: "Mesa 1").
* **Tecla `C`:** Cancela a mesa em andamento.
* **Tecla `S`:** Salva todas as mesas no arquivo `data/mesas_config.json`.
* **Tecla `Q` ou `ESC`:** Sai da ferramenta.

### 4. Executar o Motor de Detecção de Ocupação (Fase 4)
Processa o vídeo frame a frame, cruza os pés/centroides das pessoas detectadas com os polígonos das mesas e grava o vídeo anotado:
```powershell
.\.venv\Scripts\python.exe src\motor_ocupacao.py
```
* **Verde:** Mesa livre e disponível.
* **Vermelho:** Mesa ocupada com indicação da quantidade de clientes sentados.
* **Barra superior:** Contagem total de mesas ocupadas, livres e percentual de ocupação do salão.
* **Saída gerada:** Salvo em `data/videos/output_ocupacao.mp4`.

### 5. Executar o Motor de Estados e Cronômetros (Fase 5)
Roda a máquina de estados completa (FSM), calculando cronômetros de espera de garçom, preparo e limpeza, com emissão de alertas e log de eventos:
```powershell
.\.venv\Scripts\python.exe src\motor_estados.py
```
* **Placas dinâmicas:** Exibe o estado da mesa e o cronômetro formatado em `MM:SS`.
* **Banner de Alerta:** Exibe faixa vermelha superior quando clientes estão aguardando além do tempo limite.
* **Logs gerados:** Registro estruturado de todos os eventos em `data/alertas_log.json`.
* **Vídeo anotado:** Salvo em `data/videos/output_fsm_cronometros.mp4`.

### 6. Executar o Dashboard Operacional em Tempo Real (Fase 6)
Inicia o painel visual web gerencial em Streamlit para o gerente e o caixa:
```powershell
.\.venv\Scripts\streamlit.exe run src\dashboard.py
```
* **KPIs em tempo real:** Mesas livres, ocupadas, alertas críticos e taxa de ocupação.
* **Grid de Mesas:** Cartões visuais coloridos com cronômetros individuais e pessoas sentadas.
* **Fila de Alertas:** Destaque imediato no topo de clientes esperando atendimento ou pratos atrasados.
* **Ajuste Dinâmico:** Sliders na barra lateral para mudar tempos limites (salvos em `data/regras_config.json`).
* **Editor de Mesas e Zonas no Navegador:** Aba interativa com `<canvas>` HTML5 para adicionar retângulos/polígonos, arrastar vértices, renomear IDs/descrições e aplicar ao vivo sem reiniciar o motor.
* **Auditoria:** Tabela completa de eventos de atendimento e histórico.

### 7. Ingestão Resiliente de Câmeras RTSP / CFTV (Fase 7)
Módulo universal para conexão com qualquer câmera IP do mercado (Intelbras, Hikvision, Dahua, TP-Link, Webcam ou arquivo):
```powershell
.\.venv\Scripts\python.exe src\camera_stream.py
```
* **Zero Lag:** Thread dedicada que drena buffers de rede e descarta frames atrasados.
* **Auto-Reconexão:** Recuperação contínua automática se o Wi-Fi ou cabo de rede oscilar.
* **Amostragem FPS:** Limita o fluxo a 3~5 FPS, economizando mais de 80% de CPU/GPU.
* **Sanitização de URL:** Validação contra injeções de rede e comandos.

### 8. Deploy Automatizado com Docker e Appliance Linux (Fase 8)
Para implantação definitiva em restaurantes em Mini PCs (Ubuntu/Debian):
```bash
# Instalação automática em 1 linha
sudo bash install.sh
```
Ou manualmente via Docker Compose:
```bash
docker compose up -d
```
* **Auto-Inicialização:** Reinício automático em caso de reboot ou queda de energia.
* **Guia de Campo:** Consulte [`deploy/README_DEPLOY.md`](deploy/README_DEPLOY.md) para o passo a passo com suporte remoto via Tailscale.

### 9. Sincronização com o Portal em Nuvem (Fase 9)
Envia o histórico anônimo (eventos, visitas e amostras; sem imagens) para a API na VPS. Configure `CLOUD_API_URL` e `EDGE_TOKEN` no `.env` (veja `.env.example`). Sem configuração, o serviço fica ocioso.
```powershell
.\.venv\Scripts\python.exe src\edge_sync.py --uma-vez   # um ciclo manual
.\.venv\Scripts\python.exe src\test_edge_sync.py        # testes
```
Deploy da nuvem (API + Postgres com Traefik): consulte [`cloud/README.md`](cloud/README.md).

---

## 📊 Regras de Negócio e Estados

Cada mesa monitorada percorre o seguinte fluxo:
1. 🟢 **LIVRE:** Mesa desocupada e pronta para receber clientes.
2. 🔴 **OCUPADA / AGUARDANDO ATENDIMENTO:** Clientes sentaram. Inicia cronômetro da 1ª abordagem.
   * *Alerta se tempo > 5 minutos sem garçom.*
3. 🟡 **ANOTANDO PEDIDO:** Garçom chega à mesa e anota o pedido.
4. 🔵 **AGUARDANDO ENTREGA:** Garçom sai. Inicia cronômetro da cozinha/bar.
   * *Alerta se tempo > limite de cozinha (ex: 20 minutos).*
   * *Filtro anti-falso positivo:* idas rápidas para levar talheres/pratos vazios não contam como entrega do prato.
5. 🍽️ **REFEIÇÃO EM ANDAMENTO:** Prato entregue aos clientes.
6. 🟠 **PENDENTE DE LIMPEZA:** Clientes levantaram e foram embora.
   * *Alerta se tempo > 3 minutos sem limpeza.*
7. 🟢 **LIVRE (HIGIENIZADA):** Funcionário higienizou a mesa; o sistema a coloca como verde novamente.

---

## 🛡️ Diretrizes de Segurança (SQL Injection e Entradas)

> [!CAUTION]
> **Regra de Segurança Obrigatória (Consultas Externas e Banco de Dados):**
> Sempre que qualquer componente do sistema receber entradas externas ou realizar consultas (banco de dados, internet, webhooks ou APIs):
> 
> 1. **Prevenção Estrita contra SQL Injection:**
>    * É terminantemente proibido o uso de concatenação de strings ou formatação de strings (`f"SELECT ... WHERE campo = '{valor}'"`) para montagem de comandos SQL.
>    * Toda e qualquer operação de banco de dados **DEVE** utilizar queries parametrizadas (*Prepared Statements* / placeholders como `?` no SQLite ou `:param` no SQLAlchemy/PostgreSQL).
>    * Checagem ativa e filtros contra padrões maliciosos (ex: `' OR '1'='1`, caracteres de terminação `;`, `--`, UNION attacks).
> 2. **Sanitização e Validação de Tipos:**
>    * Qualquer dado recebido externamente (como webhooks de PDV/KDS de restaurantes ou comandos de API) deve ser validado estritamente através de esquemas tipados (ex: Pydantic) antes do processamento.
>    * Proteção contra SSRF na ingestão de URLs de câmeras ou webhooks de terceiros.

---

## 📝 Histórico de Implementações e Features (Changelog)

> [!IMPORTANT]
> **Regra de Atualização Contínua:** Sempre que uma nova funcionalidade, script, refatoração ou regra de negócio for adicionada ao projeto, esta seção deve ser atualizada.

| Data | Feature / Módulo | Descrição Detalhada | Arquivos Impactados |
| :--- | :--- | :--- | :--- |
| **31/10/2026** | **Planejamento e Arquitetura** | Criação do documento mestre de arquitetura, máquina de estados finitos (FSM), triplo de métricas e regras de negócio. | `PLANEJAMENTO.md` |
| **31/10/2026** | **Setup do Ambiente e Dependências** | Instalação do Python 3.11 isolado via `uv`, criação do `.venv` e instalação de `ultralytics`, `opencv-python`, `shapely`, `torch`. | `.venv/`, `requirements.txt`, `.gitignore` |
| **31/10/2026** | **Dataset de Teste (Vídeo Gravado)** | Download do vídeo de teste `sample_restaurante.mp4` (720p, 6.304 frames) para desenvolvimento sem necessidade de câmera física imediata. | `data/videos/sample_restaurante.mp4` |
| **31/10/2026** | **Script de Inspeção e Extração** | Módulo que extrai metadados do vídeo e gera o `frame_referencia.jpg` para ser base de calibração. | `src/test_video.py` |
| **31/10/2026** | **Inferência YOLOv10n Validada** | Validação do modelo `yolov10n` sem NMS, executando detecção de pessoas e mobiliário a ~50ms/frame em CPU. | `src/test_yolo.py`, `yolov10n.pt` |
| **31/10/2026** | **Calibrador Visual de Zonas (ROIs)** | Ferramenta interativa em OpenCV para marcação de polígonos de mesas com o mouse e exportação para JSON. | `src/calibrador_mesas.py` |
| **02/11/2026** | **Documentação Técnica Viva** | Criação do documento oficial técnico do projeto com instruções de execução, regras de negócio e changelog. | `DOCUMENTACAO.md` |
| **10/11/2026** | **Políticas de Segurança e Sanitização** | Inclusão de diretrizes mandatórias contra SQL Injection, validação de entradas externas e uso exclusivo de queries parametrizadas. | `DOCUMENTACAO.md`, `PLANEJAMENTO.md` |
| **10/11/2026** | **Calibração das Mesas (Fase 3 Concluída)** | Mapeamento das 5 mesas do salão em polígonos, persistência das coordenadas em JSON e geração do preview visual. | `data/mesas_config.json`, `src/gerar_calibracao.py`, `data/videos/calibracao_mesas_preview.jpg` |
| **10/11/2026** | **Motor de Ocupação e Geometria (Fase 4 Concluída)** | Algoritmo de Point-in-Polygon (cv2.pointPolygonTest), debounce temporal e renderização dinâmica das mesas ocupadas/livres a ~19 FPS. | `src/motor_ocupacao.py`, `data/videos/output_ocupacao.mp4` |
| **10/11/2026** | **FSM, Cronômetros e Alertas (Fase 5 Concluída)** | Máquina de Estados FSM (`src/fsm_mesas.py`), cronômetros em tempo real (espera, cozinha, limpeza), banner dinâmico de alertas e log de eventos JSON. | `src/fsm_mesas.py`, `src/motor_estados.py`, `data/alertas_log.json`, `data/videos/output_fsm_cronometros.mp4` |
| **10/11/2026** | **Dashboard Web Gerencial (Fase 6 Concluída)** | Painel interativo em Streamlit com KPIs do salão, grid de mesas com badges coloridos e cronômetros, fila de alertas, visualização da câmera e ajuste de regras persistente. | `src/dashboard.py`, `data/regras_config.json`, `requirements.txt` |
| **10/11/2026** | **Ingestão Universal de Câmeras (Fase 7 Concluída)** | Módulo resiliente em thread assíncrona (`src/camera_stream.py`) com zero lag, reconexão automática, amostragem inteligente a 4 FPS e sanitização estrita de URLs. | `src/camera_stream.py` |
| **10/11/2026** | **Empacotamento e Deploy Plug & Play (Fase 8 Concluída)** | Dockerfile, docker-compose.yml orquestrando Dashboard e IA, instalador automático `install.sh` e guia de campo `deploy/README_DEPLOY.md` com Tailscale. | `Dockerfile`, `docker-compose.yml`, `install.sh`, `deploy/README_DEPLOY.md` |
| **11/11/2026** | **Sincronização Ao Vivo & Precisão YOLOv10b** | Remoção de dados simulados do Dashboard, criação de estado ao vivo (`estado_atual.json`) e frame ao vivo (`frame_ao_vivo.jpg`) via escrita atômica, atualização do modelo para `yolov10b.pt`, eliminação de sobreposição entre polígonos das mesas e auto-refresh reativo via `@st.fragment`. | `src/motor_estados.py`, `src/motor_ocupacao.py`, `src/dashboard.py`, `src/gerar_calibracao.py`, `data/mesas_config.json`, `Dockerfile` |
| **11/11/2026** | **Editor Visual de Mesas no Navegador & Hot-Reload** | Novo editor interativo em `<canvas>` HTML5 via `st.components.v2` para desenhar/ajustar zonas, arrastar cantos, criar polígonos livres ou retângulos, renomear IDs/descrições direto pelo navegador com validação estrita e recarga automática (`hot-reload`) no motor de IA sem reiniciar containers. | `src/zone_editor_component.py`, `src/config_mesas.py`, `src/dashboard.py`, `src/motor_estados.py` |
| **11/11/2026** | **Detecção Automática de Garçom, Limpeza Rápida & RTSP TCP** | Implementação de presença automática de garçom via excedente estável sobre a linha de base (com reversão de cliente atrasado), detecção de limpeza rápida (`PENDENTE_LIMPEZA -> LIVRE` ou `SENTOU_SEM_LIMPEZA`), classificador opcional de uniforme/avental em HSV com margem configurável no Dashboard, RTSP sobre TCP no `StreamCamera` e simulação determinística offline (`test_fsm_simulacao.py`). | `src/detector_garcom.py`, `src/fsm_mesas.py`, `src/motor_estados.py`, `src/camera_stream.py`, `src/dashboard.py`, `src/test_fsm_simulacao.py` |
| **11/11/2026** | **Suporte Multi-Câmeras & Troca Dinâmica de Vídeo / RTSP** | Orquestrador `GerenciadorMultiCameras` compartilhando um único modelo YOLOv10b na memória entre várias câmeras ativas, hot-reload de `data/cameras_config.json`, leitura não-bloqueante (`ler_se_pronto()`), nova aba **🎥 Câmeras e Vídeos** (cadastro/edição/pausa/remoção, troca/upload de `.mp4` até 500 MB e URLs RTSP/HTTP/webcam com bloqueio SSRF e mascaramento de senha), editor visual multi-câmera por seleção e painel agregado por salão. | `src/config_cameras.py`, `src/multi_cameras.py`, `src/motor_estados.py`, `src/camera_stream.py`, `src/dashboard.py`, `src/test_multi_cameras.py`, `docker-compose.yml`, `.streamlit/config.toml` |
| **13/11/2026** | **Fase 9 (parte 1): Edge Sync + API de Ingestão em Nuvem** | Novo serviço local `edge-sync` que envia de forma incremental (cursor por tabela em `data/sync_estado.json`, escrita atômica) apenas metadados anônimos do SQLite para a nuvem, com backoff e tolerância a falta de internet. Nova pasta `cloud/` para a VPS: API FastAPI multi-tenant com token Bearer por estabelecimento (hash SHA-256), validação Pydantic estrita, ingestão idempotente em PostgreSQL com SQL parametrizado, CLI de administração e compose integrado ao Traefik existente. | `src/edge_sync.py`, `src/test_edge_sync.py`, `docker-compose.yml`, `.env.example`, `.gitignore`, `cloud/` |
| **11/11/2026** | **Etapa 2: Previsão por Mediana & Tela Completa de Relatórios** | Persistência histórica em SQLite (`data/historico_salao.db`, modo WAL e queries 100% parametrizadas) em `analytics_db.py`, cálculo hierárquico de mediana de permanência resistente a outliers, contagem regressiva **"Próximas Mesas a Liberar"**, nova aba dedicada **📊 Relatórios** com 4 visões (`Desempenho & SLA por Mesa`, `Pico & Escala de Garçons`, `Previsão de Giro (Medianas)` e `Extrato de Visitas & Alertas`), exportação CSV + Relatório Gerencial Imprimível (`.html` com escape XSS), fuso `America/Sao_Paulo` e healthcheck dedicado no Docker. | `src/analytics_db.py`, `src/fsm_mesas.py`, `src/motor_estados.py`, `src/multi_cameras.py`, `src/dashboard.py`, `src/test_analytics_etapa2.py`, `docker-compose.yml` |

---

## 🔮 Melhorias Futuras (Pós-Roadmap)
- [ ] **Integração KDS / PDV:** Webhook de pedidos prontos da cozinha para zerar margem de erro na entrega de pratos.
- [x] **Classificação Uniforme (Garçom vs Cliente):** Detecção de cor de uniforme/avental em HSV para automatizar a identificação de abordagem do atendente (integrado ao `detector_garcom.py`).
- [x] **Suporte Multi-Câmeras no Dashboard:** Alternância, cadastro e exibição simultânea de múltiplas câmeras/salões com troca de vídeo no mesmo painel web.

