# ==============================================================================
# Dockerfile: Sistema de Monitoramento de Mesas com Visão Computacional (IA)
# Base otimizada em Python 3.11 Slim para execução em Mini PCs e Servidores Linux
# ==============================================================================

FROM python:3.11-slim

# Evita geração de arquivos .pyc e força stdout não-bufferizado
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Instala bibliotecas de sistema requeridas pelo OpenCV e FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia e instala as dependências Python
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pesos do YOLO. Os arquivos .pt não vão para o git; se `models/` já tiver o peso
# escolhido, ele é usado; senão é baixado no build pelo mecanismo oficial do
# Ultralytics (repositório github.com/ultralytics/assets). O build falha de forma
# explícita se o peso não estiver presente ao final.
ARG YOLO_MODEL=yolov10b.pt
ENV YOLO_MODEL_PATH=/app/models/${YOLO_MODEL}
COPY models/ /app/models/
RUN if [ ! -f "$YOLO_MODEL_PATH" ]; then \
        cd /app/models && \
        python -c "import sys; from ultralytics.utils.downloads import attempt_download_asset; attempt_download_asset(sys.argv[1])" "$YOLO_MODEL"; \
    fi && \
    test -f "$YOLO_MODEL_PATH" || (echo "ERRO: peso $YOLO_MODEL_PATH ausente" && exit 1)

# Código da aplicação
COPY src/ /app/src/

# Cria pasta de dados persistente
RUN mkdir -p /app/data

# Porta de acesso do Dashboard Streamlit
EXPOSE 8501

# Healthcheck para monitorar integridade da aplicação
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Inicializa o Dashboard por padrão
CMD ["streamlit", "run", "src/dashboard.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]

