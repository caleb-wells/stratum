-- Tables Chainlit's built-in Postgres data layer reads and writes (it does not create them).
CREATE TABLE IF NOT EXISTS "User" (
    id          TEXT PRIMARY KEY,
    identifier  TEXT NOT NULL UNIQUE,
    metadata    JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMP NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMP NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS "Thread" (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',
    tags        TEXT[] NOT NULL DEFAULT '{}',
    "userId"    TEXT REFERENCES "User"(id) ON DELETE CASCADE,
    "createdAt" TIMESTAMP NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMP NOT NULL DEFAULT now(),
    "deletedAt" TIMESTAMP
);
CREATE INDEX IF NOT EXISTS thread_user_updated ON "Thread" ("userId", "updatedAt" DESC);
CREATE TABLE IF NOT EXISTS "Step" (
    id          TEXT PRIMARY KEY,
    "threadId"  TEXT REFERENCES "Thread"(id) ON DELETE CASCADE,
    "parentId"  TEXT,
    name        TEXT,
    type        TEXT NOT NULL DEFAULT 'run',
    input       TEXT,
    output      TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',
    "showInput" TEXT,
    "isError"   BOOLEAN DEFAULT false,
    "startTime" TIMESTAMP,
    "endTime"   TIMESTAMP,
    "createdAt" TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS step_thread_start ON "Step" ("threadId", "startTime");
CREATE TABLE IF NOT EXISTS "Element" (
    id            TEXT PRIMARY KEY,
    "threadId"    TEXT REFERENCES "Thread"(id) ON DELETE CASCADE,
    "stepId"      TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}',
    mime          TEXT,
    name          TEXT,
    "objectKey"   TEXT,
    url           TEXT,
    "chainlitKey" TEXT,
    display       TEXT,
    size          TEXT,
    language      TEXT,
    page          INT,
    props         JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS element_thread ON "Element" ("threadId");
CREATE TABLE IF NOT EXISTS "Feedback" (
    id       TEXT PRIMARY KEY,
    "stepId" TEXT REFERENCES "Step"(id) ON DELETE CASCADE,
    name     TEXT,
    value    DOUBLE PRECISION,
    comment  TEXT
);
