# ☁️ Portal em Nuvem — Deploy na VPS

Recebe os metadados anônimos enviados pelo `edge-sync` de cada restaurante, guarda tudo em um PostgreSQL central multi-tenant e serve o portal de relatórios (Streamlit + login Google).

```text
Restaurante (edge-sync) ──HTTPS + token──▶ Traefik ──▶ api (FastAPI) ──▶ postgres (rede interna)
Dono / gerente (navegador) ──HTTPS + Google──▶ Traefik ──▶ portal (Streamlit) ──▶ postgres (somente leitura)
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
docker compose exec api python -m app.admin definir-fuso 3 America/Manaus   # fuso da loja (relatórios)
docker compose exec api python -m app.admin vincular dono@gmail.com 5       # dono com várias lojas
docker compose exec api python -m app.admin desvincular dono@gmail.com 5
docker compose exec api python -m app.admin criar-login-portal              # login Postgres SOMENTE LEITURA do portal
docker compose exec api python -m app.admin adicionar-usuario dono@gmail.com gerente --estabelecimento 3 4
docker compose exec api python -m app.admin adicionar-usuario voce@gmail.com admin
docker compose exec api python -m app.admin remover-usuario dono@gmail.com
```

## 4. Portal de relatórios (login Google)
O dono/gerente acessa os relatórios de qualquer lugar em `https://<PORTAL_DOMAIN>`. Gerente vê **só as lojas vinculadas** a ele; admin escolhe qualquer loja e tem a aba **Saúde das Lojas** (último sync e último dado de ocupação de cada uma).

1. **Credencial Google**: no Google Cloud Console → *APIs e serviços* → *Tela de consentimento OAuth* (tipo Externo, escopos `openid`, `email`, `profile`) → *Credenciais* → *ID do cliente OAuth* do tipo **Aplicativo da Web**, com URI de redirecionamento autorizado `https://<PORTAL_DOMAIN>/oauth2callback`.
2. **DNS**: registro `A` de `PORTAL_DOMAIN` apontando para a VPS.
3. **Login somente leitura do banco** (com a API já no ar):
   ```bash
   docker compose up -d postgres api
   docker compose exec api python -m app.admin criar-login-portal   # mostra PORTAL_DB_PASSWORD uma única vez
   ```
4. Preencha no `.env`: `PORTAL_DOMAIN`, `PORTAL_DB_PASSWORD`, `PORTAL_COOKIE_SECRET` (gere com `python -c "import secrets; print(secrets.token_urlsafe(48))"`), `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET`.
5. Autorize os e-mails e suba o portal:
   ```bash
   docker compose exec api python -m app.admin adicionar-usuario voce@gmail.com admin
   docker compose exec api python -m app.admin adicionar-usuario dono@gmail.com gerente --estabelecimento 3
   docker compose up -d --build portal
   docker compose logs -f portal     # "Login Google configurado" e depois o Streamlit
   ```
   Se faltar alguma variável, o container para com `ERRO: configuração do login do portal inválida: ...` dizendo qual.

Como funciona a segurança do portal:
* Só entra conta Google com **e-mail verificado** que esteja **ativa** na tabela `usuarios`. O perfil é relido do banco a cada clique: `remover-usuario` ou `desvincular` valem na hora.
* Toda consulta de relatório filtra por `estabelecimento_id` de uma loja já autorizada para aquele usuário, com SQL parametrizado. O cache (`st.cache_data`, 5 min) é chaveado pelo id da loja.
* O portal usa o login Postgres `portal` (papel `portal_leitura`): só leitura nas tabelas de relatório, sem acesso ao hash dos tokens, e escrita apenas em `portal_acessos` (auditoria) e `usuarios.ultimo_login_em`.
* Auditoria LGPD em `portal_acessos`: login, acesso negado, loja consultada, exportações e consulta da saúde das lojas.
* O `secrets.toml` do login é gerado na subida do container a partir do `.env` (permissão 0600), nunca versionado nem embutido na imagem. Erros não mostram detalhes no navegador.
* Exportações: CSV com proteção contra injeção de fórmula e relatório HTML imprimível com escape de todo texto. Cada exportação traz no máximo 50.000 linhas; acima disso o portal avisa para reduzir o período.

Testes do portal:
```bash
cd cloud/portal && python -m unittest tests.test_portal -v             # sem banco (usa o .venv do projeto)
# Integração com Postgres real (banco DESCARTÁVEL; precisa de psycopg + pandas):
set TEST_DATABASE_URL=postgresql://postgres:teste@127.0.0.1:55432/postgres
cd cloud/portal && python -m unittest tests.test_portal_postgres -v
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
