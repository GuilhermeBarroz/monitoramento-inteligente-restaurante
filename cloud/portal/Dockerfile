FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    TZ=America/Sao_Paulo

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY .streamlit/config.toml ./.streamlit/config.toml
COPY app/ ./app/
RUN useradd --create-home --uid 10002 portal && chown -R portal /srv
USER portal

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"

# Gera o secrets.toml do login Google a partir do ambiente e só então sobe o Streamlit
CMD ["sh", "-c", "python -m app.configurar_auth && exec streamlit run app/portal.py"]
