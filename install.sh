#!/usr/bin/env bash
# ==============================================================================
# Script de Instalação Automatizada (Appliance Linux / Mini PC Restaurante)
# Instala Docker, configura inicialização no boot e sobe os contêineres.
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}================================================================${NC}"
echo -e "${GREEN}🚀 Instalador do Monitor de Mesas com IA para Restaurantes${NC}"
echo -e "${BLUE}================================================================${NC}"

# 1. Verifica se está rodando como root ou com sudo
if [ "$EUID" -ne 0 ]; then
  echo -e "${YELLOW}Solicitando privilégios de administrador (sudo)...${NC}"
  exec sudo bash "$0" "$@"
fi

# Executa sempre a partir da pasta do projeto (onde está o docker-compose.yml)
cd "$(dirname "$(readlink -f "$0")")"

# 2. Atualiza pacotes e instala dependências básicas
echo -e "\n${BLUE}[1/5] Atualizando repositórios e instalando utilitários...${NC}"
apt-get update -qq
apt-get install -y -qq curl git ca-certificates gnupg

# 3. Instala Docker se ainda não estiver instalado
if ! command -v docker &> /dev/null; then
  # Escolhe o repositório oficial do Docker pela distribuição (aceita derivadas
  # como Linux Mint e Raspberry Pi OS via ID_LIKE).
  . /etc/os-release
  case " ${ID} ${ID_LIKE:-} " in
    *" ubuntu "*) DOCKER_DISTRO="ubuntu"; CODINOME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" ;;
    *" debian "*|*" raspbian "*) DOCKER_DISTRO="debian"; CODINOME="${VERSION_CODENAME:-}" ;;
    *)
      echo -e "${RED}Distribuição '${ID}' não suportada. Use Ubuntu ou Debian.${NC}"
      exit 1
      ;;
  esac
  if [ -z "${CODINOME}" ]; then
    echo -e "${RED}Não foi possível identificar a versão da distribuição (VERSION_CODENAME vazio).${NC}"
    exit 1
  fi

  echo -e "${BLUE}[2/5] Instalando Docker Engine (${DOCKER_DISTRO} ${CODINOME})...${NC}"
  mkdir -p /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${DOCKER_DISTRO} \
    ${CODINOME} stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable docker
  systemctl start docker
  echo -e "${GREEN}Docker instalado com sucesso!${NC}"
else
  echo -e "${GREEN}[2/5] Docker já está instalado no sistema.${NC}"
fi

# 4. Constrói e inicializa os contêineres via Docker Compose
echo -e "\n${BLUE}[3/5] Construindo contêineres e baixando o modelo de IA...${NC}"
# Garante a pasta de dados montada como volume (./data) pelos três serviços
mkdir -p data

# Arquivo .env: cria a partir do exemplo e gera o PIN de administrador do painel
if [ ! -f .env ]; then
  cp .env.example .env
fi
PIN_GERADO=""
if ! grep -Eq '^PAINEL_ADMIN_PIN=.{6,}' .env; then
  PIN_GERADO="$(tr -dc '0-9' < /dev/urandom | head -c 8)"
  if grep -q '^PAINEL_ADMIN_PIN=' .env; then
    sed -i "s/^PAINEL_ADMIN_PIN=.*/PAINEL_ADMIN_PIN=${PIN_GERADO}/" .env
  else
    printf '\nPAINEL_ADMIN_PIN=%s\n' "${PIN_GERADO}" >> .env
  fi
fi
# O .env guarda token da nuvem e PIN: legível só pelo root
chmod 600 .env
docker compose build

echo -e "\n${BLUE}[4/5] Inicializando serviços em segundo plano...${NC}"
docker compose up -d

# 5. Obtém IP local da máquina para exibir no terminal
IP_LOCAL=$(hostname -I | awk '{print $1}')

echo -e "\n${BLUE}================================================================${NC}"
echo -e "${GREEN}✅ INSTALAÇÃO CONCLUÍDA COM SUCESSO!${NC}"
echo -e "${BLUE}================================================================${NC}"
echo -e "O sistema está rodando em segundo plano e iniciará automaticamente"
echo -e "sempre que o Mini PC for ligado na tomada."
echo -e ""
echo -e "Acesse o painel no navegador de qualquer dispositivo na rede local:"
echo -e "👉 ${YELLOW}http://${IP_LOCAL}:8501${NC} ou ${YELLOW}http://localhost:8501${NC}"
if [ -n "${PIN_GERADO}" ]; then
  echo -e ""
  echo -e "🔑 PIN do administrador do painel: ${YELLOW}${PIN_GERADO}${NC}"
  echo -e "   Anote em local seguro. Ele fica no arquivo .env (PAINEL_ADMIN_PIN)."
fi
echo -e ""
echo -e "${RED}⚠️  Nunca abra a porta 8501 no roteador.${NC} Acesso remoto do técnico: Tailscale."
echo -e "   Mantenha o Wi-Fi de clientes em rede separada (VLAN/rede de convidados)."
echo -e "${BLUE}================================================================${NC}\n"

