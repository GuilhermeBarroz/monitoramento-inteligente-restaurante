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

# 2. Atualiza pacotes e instala dependências básicas
echo -e "\n${BLUE}[1/5] Atualizando repositórios e instalando utilitários...${NC}"
apt-get update -qq
apt-get install -y -qq curl git ca-certificates gnupg lsb-release

# 3. Instala Docker se ainda não estiver instalado
if ! command -v docker &> /dev/null; then
  echo -e "${BLUE}[2/5] Instalando Docker Engine...${NC}"
  mkdir -p /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable docker
  systemctl start docker
  echo -e "${GREEN}Docker instalado com sucesso!${NC}"
else
  echo -e "${GREEN}[2/5] Docker já está instalado no sistema.${NC}"
fi

# 4. Constrói e inicializa os contêineres via Docker Compose
echo -e "\n${BLUE}[3/5] Construindo contêineres e baixando modelos...${NC}"
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
echo -e "${BLUE}================================================================${NC}\n"

