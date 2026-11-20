-- Esquema central multi-tenant do portal em nuvem.
-- Todos os dados analíticos carregam `estabelecimento_id`. A chave
-- (estabelecimento_id, instalacao_id, origem_id) torna a ingestão idempotente (reenvios do
-- Edge Sync não duplicam registros) e distingue um banco local recriado, cujos ids recomeçam
-- do 1, dos registros antigos (antes eram descartados em silêncio pelo ON CONFLICT).

CREATE TABLE IF NOT EXISTS estabelecimentos (
    id              SERIAL PRIMARY KEY,
    nome            TEXT NOT NULL,
    token_hash      CHAR(64) NOT NULL UNIQUE,
    ativo           BOOLEAN NOT NULL DEFAULT TRUE,
    -- Fuso da loja: dia/hora dos relatórios do portal são derivados de `epoch` neste fuso
    timezone        TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_sync_em  TIMESTAMPTZ
);

-- Usuários do portal (login Google). `admin` vê todas as lojas; `gerente` vê só as lojas
-- vinculadas em usuario_estabelecimentos (um dono pode ter várias lojas).
CREATE TABLE IF NOT EXISTS usuarios (
    email            TEXT PRIMARY KEY CHECK (email = lower(email)),
    nome             TEXT,
    role             TEXT NOT NULL CHECK (role IN ('admin', 'gerente')),
    ativo            BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_login_em  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS usuario_estabelecimentos (
    email              TEXT NOT NULL REFERENCES usuarios(email) ON DELETE CASCADE,
    estabelecimento_id INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
    PRIMARY KEY (email, estabelecimento_id)
);
CREATE INDEX IF NOT EXISTS idx_usuario_est_est ON usuario_estabelecimentos (estabelecimento_id);

-- Trilha de auditoria de acesso ao portal (LGPD: quem viu dados de qual loja e quando)
CREATE TABLE IF NOT EXISTS portal_acessos (
    id                 BIGSERIAL PRIMARY KEY,
    email              TEXT NOT NULL,
    estabelecimento_id INTEGER,
    acao               TEXT NOT NULL,
    em                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_portal_acessos_em ON portal_acessos (em);

CREATE TABLE IF NOT EXISTS eventos_mesas (
    estabelecimento_id INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
    instalacao_id      UUID NOT NULL,
    origem_id          BIGINT NOT NULL,
    epoch              DOUBLE PRECISION NOT NULL,
    iso_datahora       TEXT NOT NULL,
    dia_semana         SMALLINT NOT NULL,
    hora               SMALLINT NOT NULL,
    camera_id          TEXT NOT NULL,
    camera_nome        TEXT NOT NULL,
    mesa_id            TEXT NOT NULL,
    tipo_evento        TEXT NOT NULL,
    detalhes           JSONB NOT NULL DEFAULT '{}'::jsonb,
    recebido_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (estabelecimento_id, instalacao_id, origem_id)
);
CREATE INDEX IF NOT EXISTS idx_eventos_est_epoch ON eventos_mesas (estabelecimento_id, epoch);

CREATE TABLE IF NOT EXISTS visitas_concluidas (
    estabelecimento_id     INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
    instalacao_id          UUID NOT NULL,
    origem_id              BIGINT NOT NULL,
    camera_id              TEXT NOT NULL,
    camera_nome            TEXT NOT NULL,
    mesa_id                TEXT NOT NULL,
    inicio_epoch           DOUBLE PRECISION NOT NULL,
    fim_epoch              DOUBLE PRECISION NOT NULL,
    dia_semana             SMALLINT NOT NULL,
    hora_inicio            SMALLINT NOT NULL,
    duracao_segundos       DOUBLE PRECISION NOT NULL,
    espera_garcom_segundos DOUBLE PRECISION,
    tempo_preparo_segundos DOUBLE PRECISION,
    qtd_pessoas            INTEGER NOT NULL,
    houve_alerta           BOOLEAN NOT NULL,
    recebido_em            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (estabelecimento_id, instalacao_id, origem_id)
);
CREATE INDEX IF NOT EXISTS idx_visitas_est_fim ON visitas_concluidas (estabelecimento_id, fim_epoch);

CREATE TABLE IF NOT EXISTS amostras_ocupacao (
    estabelecimento_id INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
    instalacao_id      UUID NOT NULL,
    origem_id          BIGINT NOT NULL,
    epoch              DOUBLE PRECISION NOT NULL,
    dia_semana         SMALLINT NOT NULL,
    hora               SMALLINT NOT NULL,
    camera_id          TEXT NOT NULL,
    camera_nome        TEXT NOT NULL,
    total_mesas        INTEGER NOT NULL,
    mesas_ocupadas     INTEGER NOT NULL,
    pessoas_sentadas   INTEGER NOT NULL,
    alertas_ativos     INTEGER NOT NULL,
    recebido_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (estabelecimento_id, instalacao_id, origem_id)
);
CREATE INDEX IF NOT EXISTS idx_amostras_est_epoch ON amostras_ocupacao (estabelecimento_id, epoch);

-- Índices dos relatórios do portal (desempenho por mesa, auditoria de alertas, pico por câmera)
CREATE INDEX IF NOT EXISTS idx_visitas_est_cam_mesa_fim ON visitas_concluidas (estabelecimento_id, camera_id, mesa_id, fim_epoch);
CREATE INDEX IF NOT EXISTS idx_eventos_est_tipo_epoch ON eventos_mesas (estabelecimento_id, tipo_evento, epoch);
CREATE INDEX IF NOT EXISTS idx_amostras_est_cam_epoch ON amostras_ocupacao (estabelecimento_id, camera_id, epoch);

-- ---------------------------------------------------------------------------
-- Acesso do portal: papel SOMENTE LEITURA, sem acesso ao hash dos tokens.
-- O login (com senha) é criado pelo CLI: `python -m app.admin criar-login-portal`.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW estabelecimentos_portal AS
    SELECT id, nome, ativo, timezone, criado_em, ultimo_sync_em FROM estabelecimentos;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'portal_leitura') THEN
        CREATE ROLE portal_leitura NOLOGIN;
    END IF;
END
$$;

-- Postgres 15+: um schema `public` recriado não concede USAGE a ninguém; sem isto o
-- portal recebe "relation does not exist" mesmo com SELECT nas tabelas.
GRANT USAGE ON SCHEMA public TO portal_leitura;
GRANT SELECT ON estabelecimentos_portal, usuarios, usuario_estabelecimentos,
                eventos_mesas, visitas_concluidas, amostras_ocupacao TO portal_leitura;
GRANT UPDATE (ultimo_login_em) ON usuarios TO portal_leitura;
GRANT INSERT ON portal_acessos TO portal_leitura;
GRANT USAGE ON SEQUENCE portal_acessos_id_seq TO portal_leitura;
REVOKE ALL ON estabelecimentos FROM portal_leitura;
