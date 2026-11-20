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
* **Modelo de IA:** o primeiro build baixa o peso `yolov10b.pt` do repositório oficial do Ultralytics (precisa de internet com acesso ao GitHub). Em rede restrita, copie o arquivo `.pt` para a pasta `models/` antes de rodar o instalador; para outro modelo, use `docker compose build --build-arg YOLO_MODEL=yolov10n.pt`.
* **Primeira execução:** sem o vídeo de exemplo, a câmera `cam_1` é criada **pausada**. Cadastre a URL RTSP real e ative-a na aba **🎥 Câmeras e Vídeos**.

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
3. Restrinja quem alcança o Mini PC com **ACLs/tags do Tailscale** (só a equipe de suporte), em vez de deixar toda a rede Tailscale com acesso.

---

## 🛡️ 4. Acesso ao Painel Local e Segurança

O painel local (`:8501`) é para a **equipe do salão** e para o **técnico**. Relatórios para o dono, de qualquer lugar, ficam no portal em nuvem.

| Área | Acesso |
|---|---|
| 🏠 Salão Ao Vivo, 📜 Eventos | Aberto na rede local (caixa/garçons) |
| 📹 Câmeras Ao Vivo (imagens de clientes), 📊 Relatórios, 🎨 Editar Mesas, 🎥 Câmeras e Vídeos, regras da barra lateral | **PIN de administrador** |

* **PIN:** gerado pelo `install.sh` (8 dígitos) e exibido ao final da instalação; fica em `.env` como `PAINEL_ADMIN_PIN` (arquivo com permissão 600). Para trocar, edite o `.env` e rode `docker compose up -d`. Sem PIN configurado, as áreas de administração ficam bloqueadas.
* **Proteções:** 5 tentativas erradas bloqueiam o IP por 10 minutos; a sessão de administrador expira após 30 minutos sem uso.
* **Rede:**
  * **Nunca** faça redirecionamento de porta (port forward) da `8501` no roteador.
  * Coloque o Wi-Fi de clientes em rede separada (rede de convidados/VLAN) da rede das câmeras e do Mini PC.
  * Opcional: defina `PAINEL_BIND_IP` no `.env` para o painel escutar só numa interface. Prefira o **IP fixo/reservado** do Mini PC na LAN. Usar o IP `100.x` do Tailscale exige que o `tailscaled` suba antes do Docker (drop-in systemd `After=tailscaled.service` no `docker.service`), senão o painel não inicia.
  * **Firewall:** regras do `ufw`/`firewalld` **não** filtram portas publicadas pelo Docker; filtros precisam ir na cadeia `DOCKER-USER` do iptables.
* **Operação:**
  * Troque o PIN ao entregar o equipamento ao cliente (edite `PAINEL_ADMIN_PIN` no `.env` e rode `sudo docker compose up -d`).
  * Rode o `docker compose` sempre com `sudo`: o `.env` (PIN e token da nuvem) só é legível pelo root.
  * Tentativas de login ficam no log do painel: `sudo docker compose logs dashboard | grep "\[acesso\]"`.
* **Limites conhecidos:** o painel na LAN usa HTTP sem TLS (o PIN trafega em texto na rede local; por isso a separação do Wi-Fi de clientes é obrigatória). O acesso pelo Tailscale é criptografado. O PIN é único e compartilhado entre administradores.

