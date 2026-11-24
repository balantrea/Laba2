CREATE TABLE public.accounts (
    id          bigint PRIMARY KEY,
    owner_name  text NOT NULL,
    effective_at timestamptz NOT NULL
);

CREATE TABLE public.orders (
    id           bigint PRIMARY KEY,
    status       text NOT NULL CHECK (status IN ('CREATED', 'PAID', 'CANCELLED')),
    effective_at timestamptz NOT NULL
);

INSERT INTO public.accounts VALUES (101, 'Анна Орлова', '2026-02-24T08:00:00Z');
INSERT INTO public.orders VALUES (7001, 'CREATED', '2026-02-26T14:00:00Z');

-- Для событий DELETE нужны прежние значения колонок.
ALTER TABLE public.accounts REPLICA IDENTITY FULL;
ALTER TABLE public.orders REPLICA IDENTITY FULL;

CREATE ROLE cdc LOGIN REPLICATION PASSWORD 'cdc';
GRANT CONNECT ON DATABASE operations TO cdc;
GRANT USAGE ON SCHEMA public TO cdc;
GRANT SELECT ON public.accounts, public.orders TO cdc;
CREATE PUBLICATION p2_publication FOR TABLE public.accounts, public.orders;
