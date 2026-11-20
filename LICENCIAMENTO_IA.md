# ⚖️ Licenciamento do Modelo de IA — Pesquisa e Opções

> **Data da pesquisa:** 19/11/2026
> **Aviso:** este documento é um levantamento técnico, **não é parecer jurídico**. Antes de comercializar o produto, valide a decisão com um advogado especializado em software/propriedade intelectual.

---

## 1. O problema

O projeto usa o pacote **`ultralytics`** e os pesos **YOLOv10** (`yolov10b.pt` / `yolov10n.pt`). A documentação do projeto (`CLAUDE.md`, `PLANEJAMENTO.md`) afirma que o YOLOv10 tem "licença permissiva comercial Apache 2.0". **Isso está incorreto.**

| Componente | Licença | Fonte |
|---|---|---|
| Pacote `ultralytics` (código) | **AGPL-3.0** | [Ultralytics — License](https://www.ultralytics.com/license) |
| Modelos treinados com Ultralytics | **AGPL-3.0** por padrão (a Ultralytics considera que a licença cobre o código de treino **e os modelos gerados**) | [Ultralytics — License](https://www.ultralytics.com/license) |
| Pesos YOLOv10 (THU-MIG) | **AGPL-3.0** | [Model card yolov10n (Hugging Face)](https://huggingface.co/jameslahm/yolov10n/blob/main/README.md), [Roboflow — YOLOv10 licensing](https://roboflow.com/model-licenses/yolov10) |

### O que a AGPL-3.0 exige, na prática
- Se você **distribui** o software (ex.: entrega um Mini PC com o sistema instalado ao cliente) **ou** o disponibiliza para uso **pela rede**, precisa disponibilizar o **código-fonte completo da aplicação** sob AGPL-3.0.
- No nosso caso, isso alcançaria o motor de visão, o dashboard e, possivelmente, partes que se comunicam com eles. **Não é compatível com um produto comercial fechado.**
- A própria Ultralytics afirma que a licença Enterprise é necessária para usar o YOLO "sem abrir o código do projeto inteiro", inclusive em ferramentas internas e qualquer produto ou serviço comercial ([Ultralytics — License](https://www.ultralytics.com/license)).
- Os termos da Ultralytics colocam no usuário a responsabilidade de determinar qual licença se aplica ([Termos de Serviço](https://ultralytics.com/legal/terms-of-service)).

---

## 2. Opções

### Opção A — Comprar a licença comercial (Ultralytics Enterprise)
- **Como funciona:** permite modificar e embutir código e modelos Ultralytics YOLO em produtos proprietários, sem a obrigação de abrir o código.
- **Preço:** **não é público**. É sob consulta, por formulário ([Request License](https://www.ultralytics.com/request-license), [Pricing](https://ultralytics.com/pricing)).
- **Ponto em aberto:** **não confirmei** se a licença Enterprise da Ultralytics cobre os **pesos YOLOv10 da THU-MIG** (projeto de terceiros, também AGPL). Com a licença Enterprise, o caminho natural seria trocar para um modelo da própria Ultralytics (ex.: YOLO11/YOLO26). **Pergunte isso explicitamente na cotação.**
- **Alternativa de revenda:** a Roboflow se apresenta como licenciadora autorizada da Ultralytics e inclui licença comercial de modelos YOLO26 em seus planos, porém atrelada ao uso da plataforma deles ([Roboflow — YOLO26 licensing](https://roboflow.com/model-licenses/yolo26)).
- **Prós:** menor mudança no código; ecossistema maduro.
- **Contras:** custo recorrente e desconhecido; dependência de fornecedor; a licença precisa acompanhar o crescimento (número de lojas/dispositivos).

### Opção B — Trocar o detector por um modelo de licença permissiva (Apache 2.0)
Nosso uso do modelo é simples: **detectar a classe "pessoa" e obter a caixa delimitadora**. Não usamos rastreamento, segmentação nem treino. Isso torna a troca **viável e contida** (um adaptador `Detector` no lugar das chamadas `YOLO(...)` e `model.predict(...)` em `motor_estados.py` e `multi_cameras.py`).

| Modelo | Licença declarada | Observações | Fonte |
|---|---|---|---|
| **YOLOX** (Megvii) | Apache 2.0 | Maduro (2021), leve, tem versões pequenas (nano/tiny) e exportação ONNX; exige manter aviso de copyright | [LICENSE no GitHub](https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE), [Roboflow](https://playground.roboflow.com/models/megvii/yolox) |
| **RF-DETR** (Roboflow) | Apache 2.0 **nos tamanhos Nano a Large**; tamanhos Atto/Femto/Pico/XL/2XL usam licença própria (PML 1.0) | Estado da arte recente; **confirmar a licença por tamanho** no repositório antes de usar | [Roboflow — RF-DETR licensing](https://roboflow.com/model-licenses/rf-detr), [Blog Roboflow](https://blog.roboflow.com/rf-detr-is-free-to-use-commercially/) |
| **RT-DETR** (Baidu, implementação original) | Apache 2.0 | Atenção: a versão **dentro do pacote `ultralytics` é AGPL**; usar a implementação original | [Roboflow — D-FINE vs RT-DETR](https://playground.roboflow.com/models/compare/d-fine-vs-rt-detr) |
| **D-FINE** / **DEIM** | Apache 2.0 (código) | Um model card de pesos de D-FINE traz ressalva de "uso de pesquisa"; **verificar a licença dos pesos** | [Hugging Face — D-FINE](https://huggingface.co/fastforks/dfine), [Roboflow — DEIM](https://roboflow.com/model/deim) |
| **OpenVINO Open Model Zoo** — `person-detection-0200/0201/0202` (Intel) | Repositório Apache 2.0; **licença específica dos pesos não confirmada** | Detector **só de pessoas**, MobileNetV2 + SSD, muito leve e **otimizado para CPUs/iGPU Intel como o N100**. Verificar a seção "Legal Information" do modelo | [OpenVINO docs — person-detection-0200](https://docs.openvino.ai/2024/omz_models_model_person_detection_0200.html) |

**Regras importantes para a Opção B:**
1. **Remover o pacote `ultralytics` por completo.** Qualquer modelo carregado *através* dele herda a AGPL do código. Rodar o modelo escolhido com **ONNX Runtime** ou **OpenVINO Runtime** (ambos Apache 2.0) a partir de um arquivo `.onnx`/OpenVINO IR.
2. **Verificar a licença dos pesos, não só a do código.** Em vários projetos elas diferem.
3. **Manter avisos de copyright** (exigência da Apache 2.0) num arquivo `THIRD_PARTY_NOTICES.md`.
4. **Dataset de pré-treino:** a maioria desses modelos é pré-treinada no COCO (anotações CC BY 4.0, imagens do Flickr com licenças diversas). Isso é prática comum de mercado, mas vale citar ao advogado.

- **Prós:** sem custo de licença; sem dependência de fornecedor; modelos leves rodam melhor no N100 (casa com a P6 de desempenho).
- **Contras:** trabalho de engenharia (adaptador, exportação, testes de precisão com nossos vídeos); possível diferença de precisão a validar.

### Opção C — Abrir o código sob AGPL-3.0
- Tecnicamente cumpre a licença, mas expõe todo o produto a concorrentes. **Não recomendado** para o modelo de negócio planejado.

---

## 3. Recomendação

**Opção B, com YOLOX (ou o detector de pessoas do OpenVINO) rodando via OpenVINO/ONNX Runtime, sem o pacote `ultralytics`.**

Motivos:
- O uso é só "detectar pessoas": não precisamos de nada exclusivo do ecossistema Ultralytics.
- Elimina um custo recorrente desconhecido e o risco jurídico de uma vez.
- Resolve junto o problema de desempenho no N100 (o `yolov10b` atual deve ser pesado demais; modelos pequenos com OpenVINO são muito mais rápidos em CPU Intel).
- Remove o `torch` da imagem Docker (vários GB a menos).

**Plano sugerido (encaixa na P6 — desempenho):**
1. Criar a interface `Detector` (entrada: frame; saída: lista de caixas de pessoas com confiança).
2. Implementar `DetectorYOLOX` (ONNX/OpenVINO) e, opcionalmente, `DetectorOpenVINOPessoa`.
3. Comparar com o YOLOv10 atual nos vídeos de teste: precisão (pessoas detectadas por mesa) e velocidade no Mini PC.
4. Remover `ultralytics`/`torch` do `requirements.txt` e do Dockerfile; adicionar `THIRD_PARTY_NOTICES.md`.
5. Corrigir as afirmações de licença em `CLAUDE.md` e `PLANEJAMENTO.md`.

**Enquanto isso:** não distribua o sistema comercialmente com o `ultralytics`/YOLOv10 atual sem a licença Enterprise ou sem concluir a troca.

**Se preferir a Opção A:** ao pedir a cotação, pergunte (1) se cobre pesos YOLOv10 da THU-MIG ou exige migrar para YOLO11/YOLO26, (2) como o preço escala por loja/dispositivo, (3) se cobre distribuição em hardware entregue ao cliente (edge) e uso via portal web.

---

## 4. Pendências de verificação (antes da decisão final)
- [ ] Ler o `LICENSE` oficial do repositório escolhido (YOLOX: [GitHub](https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE)) e a licença dos **pesos** pré-treinados.
- [ ] Confirmar a "Legal Information" do `person-detection-0200` na documentação do OpenVINO.
- [ ] Confirmar a licença por tamanho do RF-DETR no repositório oficial, caso seja considerado.
- [ ] Validação jurídica final.

## Fontes
- [Ultralytics — License](https://www.ultralytics.com/license)
- [Ultralytics — Request License](https://www.ultralytics.com/request-license)
- [Ultralytics — Pricing](https://ultralytics.com/pricing)
- [Ultralytics — Terms of Service](https://ultralytics.com/legal/terms-of-service)
- [Roboflow — Why AGPL-3.0 Is a Risk for Computer Vision Teams](https://blog.roboflow.com/why-agpl-3-0-is-a-risk-for-computer-vision-teams/)
- [Roboflow — YOLO26 Model Licensing](https://roboflow.com/model-licenses/yolo26)
- [Roboflow — YOLOv10 Model Licensing](https://roboflow.com/model-licenses/yolov10)
- [Hugging Face — jameslahm/yolov10n](https://huggingface.co/jameslahm/yolov10n/blob/main/README.md)
- [Roboflow — RF-DETR Licensing](https://roboflow.com/model-licenses/rf-detr)
- [Roboflow Blog — RF-DETR is free to use commercially](https://blog.roboflow.com/rf-detr-is-free-to-use-commercially/)
- [Roboflow — D-FINE vs RT-DETR](https://playground.roboflow.com/models/compare/d-fine-vs-rt-detr)
- [Roboflow — DEIM](https://roboflow.com/model/deim)
- [Hugging Face — D-FINE](https://huggingface.co/fastforks/dfine)
- [OpenVINO — person-detection-0200](https://docs.openvino.ai/2024/omz_models_model_person_detection_0200.html)
- [GitHub — Megvii-BaseDetection/YOLOX LICENSE](https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE)
- [Roboflow — YOLOX](https://playground.roboflow.com/models/megvii/yolox)
