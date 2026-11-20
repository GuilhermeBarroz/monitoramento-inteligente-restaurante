# 📐 Requisitos de Hardware, Padronização do Modelo e Planos de Implantação

Documento de referência para decidir **em que máquina o sistema roda em cada cliente** e **qual modelo de IA é o padrão da plataforma**. Complementa o [PLANEJAMENTO.md](PLANEJAMENTO.md) (roadmap), o [LICENCIAMENTO_IA.md](LICENCIAMENTO_IA.md) (licença do detector) e o [deploy/README_DEPLOY.md](deploy/README_DEPLOY.md) (instalação em campo).

> **Status (20/11/2026):** os requisitos mínimos abaixo são **provisórios**. Os números definitivos saem do benchmark da seção 6, que ainda não foi executado.

---

## 1. Como o monitoramento funciona (IA x código)

| Etapa | Quem faz | O quê |
|---|---|---|
| Detectar pessoas no quadro | **IA**: rede neural YOLO | Desenha uma caixa em volta de cada pessoa. É a única parte "inteligente" e a que mais consome CPU. |
| Em qual mesa a pessoa está | Código (geometria) | Pés da pessoa dentro do polígono da mesa. |
| Filtro anti-falso-positivo | Código (debounce) | Ocupação só é confirmada depois de vários quadros seguidos. |
| Chegada do garçom | Código (regras) | Pessoa excedente estável e, opcionalmente, cor do uniforme (HSV). |
| Ciclo da mesa, cronômetros e alertas | Código (máquina de estados) | `LIVRE → AGUARDANDO_ATENDIMENTO → ANOTANDO_PEDIDO → AGUARDANDO_ENTREGA → REFEICAO_ANDAMENTO → PENDENTE_LIMPEZA`. |
| Relatórios e previsões | Código (estatística) | Medianas, percentis e contagens. |

Não há agente nem LLM, e não há chamada à nuvem para inferência. Dado o mesmo vídeo, o resultado é sempre o mesmo. O **custo de hardware depende quase só da etapa de IA**.

---

## 2. Modelo de IA: situação atual

| Item | Valor atual | Onde |
|---|---|---|
| Modelo | **YOLOv10b** ("Balanced") | `Dockerfile:32` (`ARG YOLO_MODEL=yolov10b.pt`), `src/motor_estados.py:41` |
| Resolução de inferência | 640 px | `src/motor_estados.py` (`imgsz=640`) |
| Classe detectada | só `person` (classe 0) | `src/motor_estados.py` |
| Origem do peso | baixado no build da imagem do repositório oficial do Ultralytics | `Dockerfile` |
| Licença dos pesos | **AGPL-3.0** (não Apache, como dizia a doc antiga) | ver [LICENCIAMENTO_IA.md](LICENCIAMENTO_IA.md) |

**Inconsistência a corrigir:** `PLANEJAMENTO.md` (tabela de tecnologias) e parte do `DOCUMENTACAO.md` ainda citam o YOLOv10 **Nano** e "licença Apache 2.0". O modelo em uso é o **b** desde 11/11/2026, e a licença é AGPL-3.0.

### Tamanhos disponíveis do YOLOv10

| Modelo | Parâmetros (aprox.) | Perfil |
|---|---|---|
| n (Nano) | ~2,3 M | Muito rápido em CPU; perde mais pessoas pequenas ou distantes |
| s (Small) | ~7 M | Meio-termo provável para CPU |
| m (Medium) | ~15 M | |
| **b (Balanced)**, atual | ~19 M | Preciso, mas **provavelmente pesado demais para CPU fraca/N100** |
| l / x | ~24–30 M | Pensados para GPU |

---

## 3. Política de padronização

1. **Um único modelo de IA para todos os clientes**, definido por versão do software. Não existe "modelo por cliente".
   - Os relatórios de lojas diferentes no portal só são comparáveis se todas usarem o mesmo detector.
   - Um único comportamento para validar, calibrar e dar suporte.
2. **Peso fixado na imagem Docker** com verificação de **SHA256** no build. Arquivos `.pt` são pickle e podem executar código ao carregar, por isso nunca se usa um peso de origem não verificada.
3. **Versionamento:** cada release declara `versão do software + modelo + SHA256 do peso`. O Mini PC reporta essa tripla na telemetria da frota (Fase 11).
4. **Troca de modelo = nova release**, validada no benchmark (seção 6) e aplicada a todos os clientes de forma controlada. Nunca é trocado em campo.

### O que PODE variar por cliente (sem trocar o modelo)

| Parâmetro | Efeito | Observação |
|---|---|---|
| Quadros analisados por segundo | Principal alavanca de CPU | Ocupação de mesa muda em minutos; 1–2 FPS deve bastar (validar no benchmark) |
| Câmeras por máquina | Carga proporcional ao nº de câmeras | Definido pelo teste de capacidade na instalação |
| Aceleração de execução (OpenVINO em CPU Intel, ONNX Runtime) | Mais rápido com o **mesmo modelo** | O resultado deve ser praticamente idêntico; verificar no benchmark |

---

## 4. Planos de hardware

### Plano Padrão (recomendado): Mini PC fornecido pela plataforma

- Mini PC Intel **N100** (ou superior), **16 GB** de RAM, SSD, rede cabeada, Linux (Ubuntu/Debian) + Docker.
- Pré-gravado, já registrado na nuvem (Fase 10), sob controle da plataforma: atualizações, segurança e suporte padronizados.
- Liga sozinho após queda de energia (configurar na BIOS) e roda 24/7.

### Plano Alternativo: computador que o restaurante já tem

Para clientes que não querem pagar pelo Mini PC. Só é aceito se passar no **checklist de compatibilidade** e no **teste automático de capacidade**.

**Requisitos mínimos provisórios:**

| Item | Mínimo provisório | Recomendado |
|---|---|---|
| CPU | x86-64, 4 núcleos, **com AVX2** | Intel 8ª geração+ / Ryzen 2000+ |
| RAM | 8 GB | 16 GB |
| Disco | SSD com 30 GB livres | SSD 128 GB+ |
| Rede | Cabeada, mesma rede das câmeras | Cabeada gigabit |
| Sistema | Linux com Docker; ou Windows 10/11 Pro com WSL2 + Docker Desktop | Linux dedicado |
| Disponibilidade | Ligado durante todo o expediente | Ligado 24/7, sem suspensão |

**Riscos do plano alternativo (o cliente deve ser informado):**
- **PC do caixa:** concorre com o software de vendas e pode ser desligado ou reiniciado por funcionários.
- **Windows:** atualizações automáticas, antivírus e economia de energia derrubam o sistema; o Docker exige virtualização ativa na BIOS.
- **Segurança:** a máquina não é controlada pela plataforma e pode estar comprometida; ela terá acesso às câmeras e ao token de envio. O token é por loja e revogável (`novo-token`).
- **Suporte:** cada PC é diferente, o que vai contra o objetivo de implantação simples.
- **SLA reduzido:** a plataforma não garante disponibilidade nesse plano.

**Checklist de compatibilidade (a automatizar na Fase 10):**
- [ ] CPU x86-64 com AVX2
- [ ] RAM e disco livres acima do mínimo
- [ ] Virtualização ativa (Windows) / Docker funcional
- [ ] Acesso RTSP às câmeras pela rede
- [ ] Saída HTTPS para a API da nuvem
- [ ] Suspensão e hibernação desativadas
- [ ] **Teste de capacidade:** roda o modelo padrão por alguns minutos e informa quantas câmeras e quantos FPS a máquina sustenta

### Planos comerciais (rascunho)

| Plano | Hardware | Câmeras | Suporte |
|---|---|---|---|
| Padrão | Mini PC da plataforma | Até o limite medido no benchmark | Completo, remoto (Tailscale) |
| Alternativo | PC do cliente aprovado no checklist | Conforme teste de capacidade | Melhor esforço |

---

## 5. Decisões pendentes (bloqueiam a definição do padrão)

1. **Licença do detector:** manter YOLOv10 (AGPL-3.0, com as obrigações descritas em `LICENCIAMENTO_IA.md`) ou migrar para um detector Apache 2.0 (ex.: YOLOX). Decidir **antes** de fixar o padrão, para não fixá-lo duas vezes.
2. **Modelo padrão e FPS padrão:** saem do benchmark.
3. **Requisito mínimo definitivo** do plano alternativo: sai do benchmark num PC antigo típico.

---

## 6. Plano de benchmark (fase "Desempenho no N100")

**Candidatos:** YOLOv10 n / s / b e, se a licença pesar, YOLOX nano / tiny / s. Cada um em PyTorch e em OpenVINO.

**Máquinas:** Mini PC N100 (plano padrão), PC antigo típico de restaurante (i3/i5 de 4–6 anos atrás, 8 GB) e a máquina de desenvolvimento como referência.

**Vídeos:** `data/videos/sample_restaurante.mp4` e os vídeos externos aprovados (seção 7), com câmera fixa do alto mostrando o salão.

**Medidas:**
- Velocidade: ms por quadro, FPS sustentado, uso de CPU e RAM, temperatura em 1 h contínua.
- Precisão de detecção: pessoas detectadas x reais em quadros anotados à mão, principalmente as sentadas e parcialmente encobertas.
- Precisão de negócio: os eventos da máquina de estados (sentou, garçom chegou, levantou, limpou) batem com o vídeo?
- Capacidade: quantas câmeras a 1 e a 2 FPS cada máquina sustenta.

**Critério de escolha:** o menor modelo cuja precisão de negócio fique dentro da margem aceitável (definir, ex.: ≤ 5% de eventos errados) e que rode **pelo menos 4 câmeras a 1–2 FPS no N100**.

---

## 7. Vídeos de teste externos

**Regras de segurança e LGPD:**
- Só fontes com licença clara para uso comercial: Pexels, Pixabay e datasets com licença declarada.
- **YouTube:** vale como referência visual de ângulo e cenário. **Não baixar**: os termos de uso proíbem, e os vídeos de CCTV publicados mostram pessoas reais sem consentimento.
- No download: só domínios oficiais; conferir que o arquivo é mesmo um vídeo pelo conteúdo, não pela extensão; recusar compactados ou executáveis; limitar o tamanho; validar com OpenCV/ffprobe; guardar em `data/videos/externos/` com registro de origem e licença.
- **Critério de utilidade:** câmera **fixa, alta, mostrando várias mesas** por vários minutos. Vídeos cinematográficos (câmera na mão, close, cortes) não servem para validar a máquina de estados.

**Candidatos encontrados em 20/11/2026 (precisam de triagem visual antes de qualquer download):**

| Fonte | Link | Uso possível |
|---|---|---|
| Pexels | [People At The Restaurant](https://www.pexels.com/video/people-at-the-restaurant-852179/) | Licença livre; provável close, verificar enquadramento |
| Pexels | [People Eating a Meal at the Restaurant](https://www.pexels.com/video/people-eating-a-meal-at-the-restaurant-5101342/) | Idem |
| Pexels | [People Eating on the Table](https://www.pexels.com/video/people-eating-on-the-table-5963647/) | Grupo à mesa |
| Pexels | [Footage Of People Eating Together](https://www.pexels.com/video/footage-of-people-eating-together-3760749/) | Grupo à mesa |
| Pexels | [Two Men Eating Food at a Restaurant](https://www.pexels.com/video/two-men-eating-food-at-a-restaurant-5834309/) | Mesa de 2 |
| Pexels | [People Eating Together](https://www.pexels.com/video/people-eating-together-7927063/) | Grupo à mesa |
| Pexels (busca) | [restaurant interior](https://www.pexels.com/search/videos/restaurant%20interior/) | Filtrar por planos abertos e estáticos |
| Pixabay (busca) | [restaurant](https://pixabay.com/videos/search/restaurant/) · [dining](https://pixabay.com/videos/search/dining/) | Idem |
| Roboflow (dataset) | [restaurant surveillance](https://universe.roboflow.com/restsurv/restaurant-surveillance) | Imagens de CCTV de restaurante; verificar licença e consentimento |
| YouTube (só referência) | [Security camera inside Pepper Grill restaurant](https://www.youtube.com/watch?v=oi1ohiysZdw) | Ângulo real de CCTV de salão |
| YouTube (só referência) | [IP Dome CCTV Camera (Restaurant View)](https://www.youtube.com/watch?v=DBl7MmHlQK0) | Demo de câmera em restaurante |
| YouTube (só referência) | [2h43 de câmera de segurança em restaurante chinês](https://www.youtube.com/watch?v=uw4ZEJuj54U) | Longa duração, ângulo real (VHS) |
| YouTube (só referência) | [Restaurant Timelapse](https://www.youtube.com/watch?v=22Fflc-YkBc) | Fluxo de clientes acelerado |
| YouTube (só referência) | [Italian Family Dining Table Live 24/7 – Fixed Camera](https://www.youtube.com/watch?v=D92ObC_AECQ) | Câmera fixa sobre mesa |
| YouTube (só referência) | [We Put 19 Cameras in a Michelin-Starred Restaurant](https://www.youtube.com/watch?v=klfxQuXT66s) | Operação de salão |

**Melhor alternativa:** gravar com consentimento um vídeo próprio (câmera IP fixa no alto de um ambiente com mesas, encenando o ciclo completo: sentar, garçom, refeição, levantar, limpeza). É o único vídeo que valida a máquina de estados com gabarito conhecido.
