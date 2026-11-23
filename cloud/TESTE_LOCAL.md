# 🧪 Teste Local Ponta a Ponta (Etapa 1)

Simula no PC do desenvolvedor o caminho completo de produção, o mais próximo possível do real:

```text
sample_restaurante.mp4 ─▶ motor-estados ─▶ data-teste/historico_salao.db
                                                     │
                                         edge-sync (http, token)
                                                     ▼
                     http://localhost:8000  api ─▶ postgres ◀─ portal  http://localhost:8502 (login Google)
```

| Diferença em relação à produção | Por quê |
|---|---|
| Sem Traefik/HTTPS (http em `localhost`) | Não há domínio nem certificado no PC. A homologação na VPS (Etapa 2) cobre isso. |
| Edge usa `data-teste/` (cópia de `data/`) | O histórico e o cursor de sync reais ficam intactos. |
| `CLOUD_PERMITIR_HTTP=1` no edge-sync | O agente só aceita `http://` com esta flag de desenvolvimento. |
| Portas publicadas só em `127.0.0.1` | Nada fica acessível a outros aparelhos da rede. |
| Containers, volume e projetos próprios (`monitor-teste-*`) | Não conflita com a stack normal do edge nem com a da VPS. |

Os comandos abaixo são para **PowerShell**, a partir da raiz do repositório.

> ⚠️ Existem **dois** arquivos `.env.teste-local`, com conteúdos diferentes: o de `cloud/` (senhas do banco, segredo do cookie, credencial Google) e o da raiz (token do edge e PIN do painel). Não copie um sobre o outro. A credencial Google nunca vai no da raiz, e o `EDGE_TOKEN` nunca vai no de `cloud/`.
>
> Os serviços de teste usam `restart: "no"`: depois de reiniciar o PC ou o Docker Desktop, suba-os de novo com os comandos abaixo.

---

## 0. Pré-requisitos

1. **Docker Desktop ligado**, com Compose **2.24 ou mais novo** (o override usa `!override`):
   ```powershell
   docker compose version
   ```
2. **Credencial OAuth de TESTE** no Google Cloud Console (separada da de produção):
   * *Tela de consentimento OAuth*: tipo Externo, escopos `openid`, `email` e `profile`. Em modo "Teste", cadastre seu e-mail como usuário de teste.
   * *Credenciais* → *ID do cliente OAuth* → **Aplicativo da Web**, com URI de redirecionamento autorizado **`http://localhost:8502/oauth2callback`**.
3. As portas **8000, 8501 e 8502** devem estar livres. Se a stack normal do edge estiver rodando, pare-a antes (`docker compose down` na raiz), porque ela também usa a 8501.

Gerar um segredo aleatório (use um diferente para cada campo):
```powershell
.\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 1. Nuvem local: banco e API

```powershell
cd cloud
Copy-Item .env.teste-local.example .env.teste-local
notepad .env.teste-local          # preencha POSTGRES_PASSWORD e PORTAL_COOKIE_SECRET
docker compose -f docker-compose.teste-local.yml --env-file .env.teste-local up -d --build postgres api
curl.exe http://localhost:8000/health
```
✅ Esperado: `{"status":"ok"}`.

Para encurtar os próximos comandos, crie um atalho **nesta janela** do PowerShell:
```powershell
function nuvem { docker compose -f docker-compose.teste-local.yml --env-file .env.teste-local @args }
```

## 2. Cadastros na nuvem

```powershell
nuvem exec api python -m app.admin criar-login-portal
```
➡️ Cole a senha exibida em `PORTAL_DB_PASSWORD` no `.env.teste-local`. **Rode este comando uma vez só:** cada execução troca a senha no banco. Se repetir, atualize o `.env.teste-local` e recrie o portal (`nuvem up -d portal`).

```powershell
nuvem exec api python -m app.admin criar-estabelecimento "Restaurante Teste"
```
➡️ Guarde o `EDGE_TOKEN` exibido (ele aparece **uma única vez**) e anote o número da loja (`#1`).

```powershell
nuvem exec api python -m app.admin adicionar-usuario SEU_EMAIL@gmail.com admin
nuvem exec api python -m app.admin listar
```

## 3. Portal

Preencha `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET` no `.env.teste-local` e suba o portal. Sempre nomeie o serviço: um `nuvem up -d` sem nome também sobe o portal, que falha se o `.env.teste-local` ainda estiver incompleto.
```powershell
nuvem up -d --build portal
nuvem logs -f portal              # Ctrl+C para sair
```
✅ Esperado: `Login Google configurado`, seguido do Streamlit no ar. Se faltar alguma variável, o container para com `ERRO: configuração do login do portal inválida: ...` dizendo qual.

## 4. Edge local com o vídeo de exemplo

Volte para a raiz do repositório e crie a cópia dos dados. A stack normal do edge precisa estar **parada**: o SQLite em modo WAL só é copiado de forma consistente sem ninguém escrevendo nele.
```powershell
cd ..
docker ps --filter name=monitor-mesas    # deve listar NENHUM container; se houver, rode `docker compose down` aqui
if (Test-Path data-teste) { throw "data-teste já existe: rode a limpeza (passo 7) antes de repetir o teste" }
Copy-Item -Recurse data data-teste
Remove-Item data-teste\sync_estado.json -ErrorAction SilentlyContinue
Copy-Item .env.teste-local.example .env.teste-local
notepad .env.teste-local          # EDGE_TOKEN do passo 2 e um PAINEL_ADMIN_PIN de 6+ caracteres
```

```powershell
function edge { docker compose -f docker-compose.yml -f docker-compose.teste-local.yml --env-file .env.teste-local @args }
edge up -d --build
edge ps
edge logs -f edge-sync
```
✅ Esperado no `edge-sync`: `Sincronizados: {...}` a cada ciclo (30 s). O histórico que já estava no `historico_salao.db` copiado sobe no primeiro ciclo. São os dados que você já tinha em `data/`, e eles ficam só no Postgres local de teste, apagado na limpeza. Visitas novas aparecem conforme o vídeo, que roda em loop, completa ciclos de mesa.

---

## 5. Roteiro de verificação

Marque cada item. Onde aparecer "auditoria", confira com:
```powershell
cd cloud
nuvem exec postgres psql -U monitor monitor_mesas -c "SELECT * FROM portal_acessos ORDER BY 1 DESC LIMIT 10"
```

### A. Operação local (Mini PC simulado)
- [ ] `http://localhost:8501` abre o painel do salão, com o vídeo processado e as mesas mudando de estado.
- [ ] As áreas de administração pedem o PIN e liberam com o PIN do `.env.teste-local`.

### B. Envio para a nuvem
- [ ] `nuvem exec api python -m app.admin listar` mostra o "Restaurante Teste" com último sync recente.
- [ ] Contagem no banco cresce ao longo do tempo: `nuvem exec postgres psql -U monitor monitor_mesas -c "SELECT (SELECT count(*) FROM eventos_mesas) AS eventos, (SELECT count(*) FROM visitas_concluidas) AS visitas, (SELECT count(*) FROM amostras_ocupacao) AS amostras"`.

### C. Portal (dono/admin)
- [ ] `http://localhost:8502` → **Entrar com Google** com o e-mail admin → entra.
- [ ] Abas **📊 Relatórios** (Desempenho & SLA, Pico & Escala, Extrato & Alertas) e **🩺 Saúde das Lojas** com dados da loja de teste.
- [ ] Exportar CSV e relatório HTML: os arquivos abrem e os números batem com a tela.
- [ ] Auditoria registra o login, a loja consultada e as exportações.

### D. Controle de acesso
- [ ] Entrar com uma conta Google **não cadastrada** → acesso negado (e registrado na auditoria).
- [ ] Gerente vê só a própria loja. Crie uma segunda loja e um gerente vinculado só a ela:
  ```powershell
  nuvem exec api python -m app.admin criar-estabelecimento "Loja Vazia"
  nuvem exec api python -m app.admin adicionar-usuario OUTRO_EMAIL@gmail.com gerente --estabelecimento 2
  ```
  Ao entrar com `OUTRO_EMAIL`, só a "Loja Vazia" aparece e não há aba Saúde das Lojas.
- [ ] Revogação imediata: com o gerente logado, rode `nuvem exec api python -m app.admin remover-usuario OUTRO_EMAIL@gmail.com`. O próximo clique no portal deve bloquear o acesso.

### E. Resiliência do envio
- [ ] **Nuvem fora do ar:** `docker stop monitor-teste-api`. O `edge-sync` registra avisos e tenta de novo, enquanto o painel local (8501) segue normal.
- [ ] **Volta da nuvem:** `docker start monitor-teste-api`. O próximo ciclo envia o acumulado e a contagem no banco não duplica.
- [ ] **Loja desativada:** `nuvem exec api python -m app.admin desativar 1`. O `edge-sync` recebe recusa (401/403) e para de insistir. Depois de `reativar 1`, o envio volta na próxima tentativa, que após erro permanente leva até **30 min**. Para acelerar, use `edge restart edge-sync`.
- [ ] **Reinício do edge:** `edge restart edge-sync`. Retoma do cursor sem reenviar tudo (`data-teste/sync_estado.json`).

---

## 6. Teste automatizado do portal com Postgres real

Usa um banco **descartável e separado**: o teste apaga e recria o schema, então nunca aponte para o banco do passo 1.

```powershell
.\.venv\Scripts\python -m pip install "psycopg[binary]" pandas
docker run --rm -d --name pg-teste -e POSTGRES_PASSWORD=teste -p 127.0.0.1:55432:5432 postgres:16-alpine
$env:TEST_DATABASE_URL = "postgresql://postgres:teste@127.0.0.1:55432/postgres"
cd cloud\portal
..\..\.venv\Scripts\python -m unittest tests.test_portal_postgres -v
cd ..\..
docker stop pg-teste              # --rm apaga o container
Remove-Item Env:TEST_DATABASE_URL # evita reaproveitar a variável por engano (o teste apaga o schema)
```

---

## 7. Limpeza

```powershell
edge down                         # na raiz
Remove-Item -Recurse -Force data-teste
cd cloud
nuvem down -v                     # -v apaga o banco de teste
```
Os arquivos `.env.teste-local` ficam no disco (ignorados pelo git). Apague-os se não for repetir o teste.

---

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `edge-sync`: `CLOUD_API_URL deve usar https://` | O override de teste não foi aplicado. Confira os dois `-f` do atalho `edge`. |
| `edge-sync`: erro de conexão com `host.docker.internal` | A API não está no ar (`curl.exe http://localhost:8000/health`). |
| Google: `redirect_uri_mismatch` | A URI na credencial deve ser exatamente `http://localhost:8502/oauth2callback`. |
| Google: "acesso bloqueado / app em teste" | Cadastre o e-mail em *Usuários de teste* na tela de consentimento. |
| Portal: `PORTAL_DB_PASSWORD vazia` | Faltou o passo 2 (`criar-login-portal`) ou recriar o container depois de editar o `.env` (`nuvem up -d portal`). |
| `docker compose`: erro com `!override` | Compose anterior à 2.24. Atualize o Docker Desktop. |
| Porta 8501 ocupada | A stack normal do edge está rodando (`docker compose down` na raiz). |
