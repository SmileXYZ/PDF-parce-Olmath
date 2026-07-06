-- Supabase: схема под олимпиадные задачи + bucket для чертежей.
-- Темы и метрики сложности остаются под ручную выверку (verified=false).

-- ---------- таблица тем (справочник) ----------
create table if not exists topics (
  id          bigint generated always as identity primary key,
  name        text not null,
  slug        text unique not null,
  description text
);

-- ---------- задачи ----------
create table if not exists olympiad_tasks (
  id             uuid primary key default gen_random_uuid(),
  olympiad_title text,                 -- напр. "ММО-2025"
  olympiad_year  int,
  grade          int,                  -- класс (9, 10, 11 ...)
  task_number    int,
  author         text,
  statement_md   text not null,        -- условие: Markdown + $KaTeX$, {{FIG:id}} -> ![](url)
  answer_md      text,
  solutions      jsonb not null default '[]'::jsonb,  -- [{title, body_md}]
  comments       jsonb not null default '[]'::jsonb,  -- [{title, body_md}]
  figures        jsonb not null default '[]'::jsonb,  -- [{id, page, bbox, url}]
  meta           jsonb not null default '{}'::jsonb,  -- метрики сложности — заполняет человек/LLM позже
  source_pdf     text,
  verified       boolean not null default false,      -- прошло выверку на фронте?
  created_at     timestamptz not null default now()
);

create index if not exists olympiad_tasks_unverified_idx on olympiad_tasks (verified) where verified = false;
create index if not exists olympiad_tasks_title_idx       on olympiad_tasks (olympiad_title, grade, task_number);

-- ---------- связь задача<->тема (many-to-many) ----------
create table if not exists task_topics (
  task_id      uuid not null references olympiad_tasks(id) on delete cascade,
  topic_id     bigint not null references topics(id) on delete cascade,
  confidence   real default 1.0,      -- если темы предлагает эвристика/LLM
  is_confirmed boolean not null default false,
  primary key (task_id, topic_id)
);

-- ---------- bucket под чертежи (public read) ----------
insert into storage.buckets (id, name, public)
values ('olympiad-figures', 'olympiad-figures', true)
on conflict (id) do nothing;

-- политика на чтение (если RLS на storage.objects включён)
-- create policy "public read olympiad-figures" on storage.objects
--   for select using (bucket_id = 'olympiad-figures');
