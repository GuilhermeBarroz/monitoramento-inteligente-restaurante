# 📦 Guia Prático de Implantação em Restaurantes (Appliance Plug & Play)

Este guia orienta técnicos e instaladores na implantação rápida do sistema de monitoramento inteligente em novos restaurantes em **menos de 15 minutos**.

---

## 🖥️ 1. Hardware Recomendado (Mini PC de Borda)

Para processar o salão localmente sem consumir internet do restaurante:
* **Processador:** Intel N100 (4 cores, 3.4 GHz) ou Intel Core i5 (10ª geração ou superior).
* **Memória RAM:** 8 GB DDR4/DDR5.
* **Armazenamento:** SSD NVMe 128 GB ou 256 GB.
* **Consumo de Energia:** ~15W (custo de energia inferior a R$ 10/mês ligado 24/7).
* **Custo Estimado do Aparelho:** R$ 1.100 a R$ 1.600 (ex: Beelink Mini S12 Pro, GMKtec NucBox, ou similar).

---

## 🔌 2. Passo a Passo de Instalação no Local

### Passo 1: Conexão Física
1. Conecte o cabo de rede Ethernet (RJ45) do Mini PC diretamente no roteador/switch das câmeras do restaurante.
2. Ligue a fonte de energia na tomada.

### Passo 2: Execução do Instalador Automático
No terminal do Mini PC (Ubuntu Server ou Debian), execute o comando único:
```bash
sudo bash install.sh
```
* O script instala o Docker, baixa as dependências, compila os contêineres e inicializa os serviços.
* A flag `restart: unless-stopped` garante que o sistema suba sozinho mesmo após quedas de energia.

### Passo 3: Configuração da Câmera (RTSP)
Localize a URL RTSP da câmera do salão (geralmente fixada no gravador DVR/NVR ou câmera IP):
* **Intelbras:** `rtsp://admin:senha@192.168.1.108:554/cam/realmonitor?channel=1&subtype=0`
* **Hikvision:** `rtsp://admin:senha@192.168.1.64:554/Streaming/Channels/101`
* **Dahua:** `rtsp://admin:senha@192.168.1.50:554/cam/realmonitor?channel=1&subtype=1`
* **TP-Link Tapo:** `rtsp://admin:senha@192.168.1.20:554/stream1`

### Passo 4: Calibração das Mesas
1. Abra o navegador em qualquer tablet, celular ou computador conectado na mesma rede:
   `http://<IP_DO_MINI_PC>:8501`
2. Calibre as mesas desenhando os polígonos correspondentes a cada área.
3. Defina os limites de tolerância de atendimento na aba lateral.

---

## 🔒 3. Acesso Remoto Seguro sem Abrir Portas (Tailscale)

Para prestar suporte remoto, atualizar modelos ou solucionar dúvidas do cliente sem precisar mexer no roteador ou firewall do restaurante:

1. Instale o Tailscale no Mini PC:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. O Mini PC entrará na sua rede privada segura. Você poderá acessar o painel ou terminal SSH de qualquer lugar do mundo com um clique.

