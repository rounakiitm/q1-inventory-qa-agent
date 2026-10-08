-- Q1 Inventory Intelligence — Supabase schema
-- Backend uses the service-role key and filters by user_id.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- documents (workbook library; files live in Storage bucket `workbooks`)
-- ---------------------------------------------------------------------------
create table if not exists public.documents (
  id            text primary key,
  user_id       uuid not null references auth.users (id) on delete cascade,
  name          text not null,
  filename      text not null,
  storage_path  text not null,
  fingerprint   text,
  n_rows        int  not null default 0,
  n_cols        int  not null default 0,
  schema_card   text,
  audit         jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  unique (user_id, name)
);

create index if not exists documents_user_id_idx on public.documents (user_id);

-- ---------------------------------------------------------------------------
-- sessions / messages / tool_runs / usage / session_docs
-- ---------------------------------------------------------------------------
create table if not exists public.sessions (
  id          text primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  created_at  timestamptz not null default now()
);
create index if not exists sessions_user_id_idx on public.sessions (user_id);

create table if not exists public.messages (
  id          bigserial primary key,
  session_id  text not null references public.sessions (id) on delete cascade,
  user_id     uuid not null references auth.users (id) on delete cascade,
  role        text not null,
  content     text not null,
  meta        jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists messages_session_id_idx on public.messages (session_id);

create table if not exists public.tool_runs (
  id          bigserial primary key,
  session_id  text not null references public.sessions (id) on delete cascade,
  user_id     uuid not null references auth.users (id) on delete cascade,
  tool        text not null,
  args        text,
  result      text,
  ok          boolean not null default false,
  latency_ms  double precision,
  created_at  timestamptz not null default now()
);
create index if not exists tool_runs_session_id_idx on public.tool_runs (session_id);

create table if not exists public.usage (
  id                 bigserial primary key,
  session_id         text not null references public.sessions (id) on delete cascade,
  user_id            uuid not null references auth.users (id) on delete cascade,
  prompt_tokens      int not null default 0,
  completion_tokens  int not null default 0,
  created_at         timestamptz not null default now()
);
create index if not exists usage_session_id_idx on public.usage (session_id);

create table if not exists public.session_docs (
  session_id  text not null references public.sessions (id) on delete cascade,
  doc_id      text not null references public.documents (id) on delete cascade,
  user_id     uuid not null references auth.users (id) on delete cascade,
  primary key (session_id, doc_id)
);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------
alter table public.documents    enable row level security;
alter table public.sessions     enable row level security;
alter table public.messages     enable row level security;
alter table public.tool_runs    enable row level security;
alter table public.usage        enable row level security;
alter table public.session_docs enable row level security;

drop policy if exists documents_own on public.documents;
create policy documents_own on public.documents
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists sessions_own on public.sessions;
create policy sessions_own on public.sessions
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists messages_own on public.messages;
create policy messages_own on public.messages
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists tool_runs_own on public.tool_runs;
create policy tool_runs_own on public.tool_runs
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists usage_own on public.usage;
create policy usage_own on public.usage
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists session_docs_own on public.session_docs;
create policy session_docs_own on public.session_docs
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Storage bucket for workbooks
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('workbooks', 'workbooks', false)
on conflict (id) do nothing;

drop policy if exists workbooks_own_read on storage.objects;
create policy workbooks_own_read on storage.objects
  for select using (
    bucket_id = 'workbooks' and auth.uid()::text = (storage.foldername(name))[1]
  );

drop policy if exists workbooks_own_insert on storage.objects;
create policy workbooks_own_insert on storage.objects
  for insert with check (
    bucket_id = 'workbooks' and auth.uid()::text = (storage.foldername(name))[1]
  );

drop policy if exists workbooks_own_update on storage.objects;
create policy workbooks_own_update on storage.objects
  for update using (
    bucket_id = 'workbooks' and auth.uid()::text = (storage.foldername(name))[1]
  );

drop policy if exists workbooks_own_delete on storage.objects;
create policy workbooks_own_delete on storage.objects
  for delete using (
    bucket_id = 'workbooks' and auth.uid()::text = (storage.foldername(name))[1]
  );
