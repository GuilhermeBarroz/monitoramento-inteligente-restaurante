# 🧪 Relatório — Teste Local Ponta a Ponta (Etapa 1)

**Data:** 22/11/2026 (22:30–23:10, horário de Brasília)
**Branch:** `fix/revisao-p1-p3`
**Roteiro seguido:** [`TESTE_LOCAL.md`](TESTE_LOCAL.md)
**Ambiente:** Windows 11 Pro, Docker Desktop (Engine 29.7.2, Compose v5.5.1), vídeo `sample_restaurante.mp4` em loop, modelo `yolov10b.pt`.

## Resumo

| Bloco | Resultado |
|---|---|
| Subida da nuvem local (postgres, api, portal) | ✅ Todos `healthy` |
| Subida do edge (motor, painel, edge-sync) com cópia `data-teste/` | ✅ Todos no ar |
| Envio edge → nuvem (paridade de contagens) | ✅ Idêntico nas 3 tabelas, 0 duplicatas |
| Relatórios do portal x cálculo local | ✅ Métricas iguais |
| Login somente leitura do portal | ✅ Escrita negada (`InsufficientPrivilege`) |
| Configuração OAuth Google (redirect, escopos, PKCE) | ✅ Google aceita a credencial e o endereço de retorno |
| Resiliência: nuvem fora do ar e volta | ✅ Acumula localmente, reenvia sem duplicar |
| Resiliência: loja desativada / reativada | ✅ Para de insistir no 401. ⚠️ Retomada leva até 30 min (ver Observações) |
| Resiliência: reinício do edge-sync | ✅ Retoma do cursor, sem reenviar tudo |
| Testes automatizados do portal com Postgres real | ✅ 9/9 (primeira execução) |
| Testes unitários do portal | ✅ 31/31 |
| **Login Google interativo no navegador (itens C e D do roteiro)** | ⏳ **Pendente**: exige o usuário no navegador |

Nenhuma falha encontrada no código. Uma observação de comportamento (retomada após reativar a loja) e duas de ambiente estão registradas abaixo.

---

## 1. Subida

* Nuvem: `postgres` + `api` + `portal` com `cloud/docker-compose.teste-local.yml`. A API responde `{"status":"ok"}` em `http://localhost:8000/health`, e o portal responde `HTTP 200` em `http://localhost:8502` com o log `Login Google configurado`.
* Cadastros feitos pelo CLI:
  * login `portal` (papel `portal_leitura`);
  * loja `#1 Restaurante Teste`;
  * um usuário `admin`.

  Nenhuma senha ou token passou por tela ou log: os segredos foram gerados direto nos arquivos `.env.teste-local`, que ficam fora do git, e a senha do portal entrou pelo ambiente (`-e PORTAL_DB_PASSWORD`).
* Edge: override `docker-compose.teste-local.yml`. O motor carregou o YOLOv10 compartilhado e as 5 mesas, e o painel responde `HTTP 200` em `127.0.0.1:8501`.
* Portas: todas publicadas só em `127.0.0.1`. O Postgres não publica porta.

## 2. Envio edge → nuvem

O primeiro ciclo, ~30 s após o motor criar o `instalacao_id`, enviou o histórico copiado de `data/`:

| Tabela | SQLite local | Postgres nuvem |
|---|---|---|
| `eventos_mesas` | 6.411 | 6.411 |
| `visitas_concluidas` | 266 | 266 |
| `amostras_ocupacao` | 715 | 715 |

Quarentena vazia. A partir daí, cada ciclo de 30 s envia só o incremental (ex.: `{'eventos_mesas': 5, 'visitas_concluidas': 0, 'amostras_ocupacao': 1}`).

## 3. Relatórios do portal x painel local

Consultas do portal (`app.consultas`) executadas com o login somente leitura sobre os dados reais do teste, comparadas com o cálculo direto no SQLite local (mesmo recorte de ids):

| Métrica | Portal (Postgres) | Local (SQLite) |
|---|---|---|
| Visitas | 273 | 273 |
| SLA sem alerta | 92,7% | 253/273 = 92,7% |
| Permanência mediana | 1,2 min | 72,9 s |
| Espera do garçom (mediana) | 0,5 min | 32,7 s |
| Preparo (mediana) | 0,6 min | 37,1 s |
| Pessoas por mesa | 4,3 | 4,34 |

O portal também retornou 5 mesas, 191 limpezas (SLA de limpeza 100%) e 20 alertas `ESPERA_ATENDIMENTO`. Uma tentativa de `DELETE` pela conexão do portal foi recusada (`InsufficientPrivilege`).

## 4. Login Google (verificação sem navegador)

* Uma chamada crua a `/auth/login?provider=google` é recusada (302 para `/`), como esperado: o Streamlit exige um token de provedor assinado gerado pelo `st.login`, o que protege contra CSRF.
* Com um token legítimo gerado dentro do container, o portal redireciona para `accounts.google.com` com:
  * `redirect_uri=http://localhost:8502/oauth2callback`;
  * escopos `openid email profile`;
  * PKCE `S256`, `state`, `nonce` e `prompt=select_account`.
* A página do Google abriu como "Sign in - Google Accounts", sem `redirect_uri_mismatch` nem `invalid_client`. A credencial e o endereço de retorno estão aceitos.

## 5. Resiliência do envio

| Cenário | O que aconteceu |
|---|---|
| **API parada** (22:52:52) | O edge-sync registrou `Falha de rede` por tabela e seguiu tentando com backoff. O painel local continuou respondendo `HTTP 200`, e os dados continuaram se acumulando no SQLite (6.411 → 6.438 eventos). |
| **API religada** (22:54:21) | Na próxima tentativa, ~94 s depois por causa do backoff, enviou o acumulado (`37 / 3 / 3`). Nuvem = cursor do edge (`6457 / 269 / 719`), **0 duplicatas**. |
| **Loja desativada** (`desativar 1`) | A API respondeu `401`, e o edge-sync parou na primeira tabela, sem tentar as outras. Só 1 tentativa em 70 s, sem loop. |
| **Loja reativada** (`reativar 1`) | O edge-sync **não** retomou em 7 min, porque após erro permanente a próxima tentativa é em 30 min (comportamento do código, ver Observação 1). |
| **Reinício do edge-sync** | Retomou do cursor e enviou só os 88 eventos novos (não os 6.545). Local = cursor = nuvem (`6545 / 273 / 728`), **0 duplicatas** nas 3 tabelas. |

## 6. Testes automatizados

```text
cloud/portal  tests.test_portal_postgres  Ran 9 tests  OK   (Postgres 16 descartável em 127.0.0.1:55432)
cloud/portal  tests.test_portal           Ran 31 tests OK
```
As linhas `AVISO: falha ao registrar auditoria` na saída dos unitários vêm de testes que simulam falha de banco de propósito.
Para rodar, foram instalados no `.venv` (via `uv`) `psycopg[binary]==3.2.9` e `psycopg-pool==3.2.6`, as mesmas versões do `cloud/portal/requirements.txt`.

---

## Observações

1. **Retomada após reativar uma loja leva até 30 min** (`src/edge_sync.py`, laço principal). Um `401`/`403` é tratado como erro permanente, e a próxima tentativa fica em 1.800 s. Isso é intencional (não martelar a nuvem com token inválido), mas o técnico precisa saber: depois de `reativar` ou de trocar o token, reinicie o `edge-sync` no Mini PC para o efeito ser imediato. O roteiro foi atualizado. Se você quiser, há duas alternativas para decidir:
   * espera menor para 401 (ex.: 5 min);
   * reler o `EDGE_TOKEN` do `.env` a cada tentativa.
2. **O log de subida do portal mostra "External URL" com o IP público do PC.** Para montar essa linha, o Streamlit consulta o IP externo, uma chamada de saída desnecessária. Não há exposição: a porta está presa em `127.0.0.1` local e atrás do Traefik na VPS. Baixa prioridade: avaliar como desligar essa consulta na configuração do Streamlit.
3. **Ambiente:** havia uma stack antiga do edge rodando, a partir da pasta `MonitoramentoCameracom IA` (containers `monitor-mesas-engine` e `monitor-mesas-dashboard`, que montam o `data/` daquela pasta). Ela foi parada para o teste e religada no fim.

## Pendente (depende do usuário)

Com a stack de teste no ar (ver abaixo como religar), abrir `http://localhost:8502` e executar os itens **C** (login admin, abas, exportações, auditoria) e **D** (conta não cadastrada, gerente com loja própria, revogação imediata) do [`TESTE_LOCAL.md`](TESTE_LOCAL.md).

Religar o ambiente de teste (os dados e o banco de teste foram preservados). Pare antes a stack antiga, que também usa o painel:
```powershell
docker stop monitor-mesas-engine monitor-mesas-dashboard
cd cloud;  docker compose -f docker-compose.teste-local.yml --env-file .env.teste-local up -d
cd ..;     docker compose -f docker-compose.yml -f docker-compose.teste-local.yml --env-file .env.teste-local up -d
```
O PIN do painel local de teste está em `PAINEL_ADMIN_PIN` no `.env.teste-local` da raiz.

## Próximo passo

Concluídos os itens C e D, a próxima etapa é a **Etapa 2**: homologação na VPS com subdomínios de teste e `docker compose -p monitor-teste`. Ela precisa do domínio e do acesso à VPS.
