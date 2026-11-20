# ☁️ Portal em Nuvem — Deploy na VPS

Recebe os metadados anônimos enviados pelo `edge-sync` de cada restaurante e os guarda em um PostgreSQL central multi-tenant. O portal de relatórios (Streamlit + login Google) vai ser adicionado a este mesmo compose na próxima etapa.

```text
Restaurante (edge-sync) ──HTTPS + token──▶ Traefik ──▶ api (FastAPI) ──▶ postgres (rede interna)
```

## Pré-requisitos
* VPS com Docker e **Traefik já em execução**, ligado a uma rede Docker externa (padrão: `traefik`).
* Registro DNS `A` do domínio da API (ex: `api.seudominio.com.br`) apontando para a VPS.

## 1. Subir os serviços
```bash
cd cloud
cp .env.example .env      # defina POSTGRES_PASSWORD, API_DOMAIN e os nomes do seu Traefik
docker compose up -d --build
curl https://api.seudominio.com.br/health   # {"status":"ok"}
```

Se o seu Traefik usa outros nomes de rede, entrypoint ou certresolver, ajuste `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT` e `TRAEFIK_CERTRESOLVER` no `.env`.

## 2. Cadastrar um restaurante e gerar o token do Edge
```bash
docker compose exec api python -m app.admin criar-estabelecimento "Restaurante Centro"
```
O token aparece **uma única vez** (o banco guarda só o hash). No Mini PC do restaurante, coloque-o no `.env` ao lado do `docker-compose.yml`:
```bash
CLOUD_API_URL=https://api.seudominio.com.br
EDGE_TOKEN=<token gerado>
```
Depois rode `docker compose up -d` no Mini PC. Acompanhe com `docker compose logs -f edge-sync`.

## 3. Outros comandos de administração
```bash
docker compose exec api python -m app.admin listar                 # restaurantes, último sync e usuários
docker compose exec api python -m app.admin novo-token 3           # rotaciona token (o antigo para de valer; não reativa)
docker compose exec api python -m app.admin desativar 3            # bloqueia a ingestão do restaurante
docker compose exec api python -m app.admin reativar 3             # volta a aceitar a ingestão
docker compose exec api python -m app.admin adicionar-usuario dono@gmail.com gerente --estabelecimento 3
docker compose exec api python -m app.admin adicionar-usuario voce@gmail.com admin
docker compose exec api python -m app.admin remover-usuario dono@gmail.com
```

## Segurança
* O `estabelecimento_id` sai sempre do token no servidor e nunca do payload. Campos extras são rejeitados com `422`.
* Payloads são validados com Pydantic: tipos, faixas e tamanho de lote ≤ 1000. O corpo da requisição é limitado a 5 MB, no Traefik e na API.
* Todo o SQL é parametrizado com psycopg. O Postgres não publica nenhuma porta.
* A ingestão é idempotente, com chave `(estabelecimento_id, instalacao_id, origem_id)`, então reenvios não duplicam dados. A resposta informa `recebidos` e `inseridos` (novos).
* Registros recusados (`422`) são isolados pelo Edge Sync e postos em quarentena (`data/sync_estado.json`) sem travar o restante. O caractere NUL é recusado na validação.
* Pool de conexões esgotado responde `503` (o Edge tenta de novo com backoff).

## Backup do banco
```bash
docker compose exec postgres pg_dump -U monitor monitor_mesas | gzip > backup_$(date +%F).sql.gz
```

## Banco local recriado
Cada SQLite do restaurante tem um `instalacao_id` (UUID). Se o `data/historico_salao.db` for apagado e recriado, o Edge Sync detecta a nova instalação, recomeça o envio do zero e a nuvem guarda o novo histórico sob a nova instalação, sem descartar nada.

## Mudança de esquema (20/11/2026)
As tabelas analíticas ganharam a coluna `instalacao_id` na chave primária. O `schema.sql` usa `CREATE TABLE IF NOT EXISTS` e **não altera tabelas já existentes**: se você subiu um ambiente de teste antes desta data, recrie o banco (`docker compose down -v` apaga o volume do Postgres) antes de reconectar os Edges.
