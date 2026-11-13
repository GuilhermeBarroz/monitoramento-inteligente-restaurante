-- Esquema central multi-tenant do portal em nuvem.
-- Todos os dados analíticos carregam `estabelecimento_id`; a chave (estabelecimento_id, origem_id)
-- torna a ingestão idempotente (reenvios do Edge Sync não duplicam registros).

CREATE TABLE IF NOT EXISTS estabelecimentos (
    id              SERIAL PRIMARY KEY,
    nome            TEXT NOT NULL,
    token_hash      CHAR(64) NOT NULL UNIQUE,
    ativo           BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_sync_em  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS usuarios (
    email              TEXT PRIMARY KEY,
    nome               TEXT,
    role               TEXT NOT NULL CHECK (role IN ('admin', 'gerente')),
    estabelecimento_id INTEGER REFERENCES estabelecimentos(id) ON DELETE CASCADE,
    ativo              BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (role = 'admin' OR estabelecimento_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS eventos_mesas (
    estabelecimento_id INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
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
    PRIMARY KEY (estabelecimento_id, origem_id)
);
CREATE INDEX IF NOT EXISTS idx_eventos_est_epoch ON eventos_mesas (estabelecimento_id, epoch);

CREATE TABLE IF NOT EXISTS visitas_concluidas (
    estabelecimento_id     INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
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
    PRIMARY KEY (estabelecimento_id, origem_id)
);
CREATE INDEX IF NOT EXISTS idx_visitas_est_fim ON visitas_concluidas (estabelecimento_id, fim_epoch);

CREATE TABLE IF NOT EXISTS amostras_ocupacao (
    estabelecimento_id INTEGER NOT NULL REFERENCES estabelecimentos(id) ON DELETE CASCADE,
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
    PRIMARY KEY (estabelecimento_id, origem_id)
);
CREATE INDEX IF NOT EXISTS idx_amostras_est_epoch ON amostras_ocupacao (estabelecimento_id, epoch);
