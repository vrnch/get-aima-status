create table if not exists public.aima_bot_sessions (
    telegram_user_id bigint primary key,
    chat_id bigint not null,
    flow text not null check (flow in ('passport', 'residence')),
    step text not null,
    data jsonb not null default '{}'::jsonb,
    expires_at timestamptz not null
);

create index if not exists aima_bot_sessions_expires_at_idx
    on public.aima_bot_sessions (expires_at);

alter table public.aima_bot_sessions enable row level security;

-- No client policies are created. Only the server-side service-role key can
-- access this table. Never expose that key to Telegram users or a browser.
revoke all on public.aima_bot_sessions from anon, authenticated;

create or replace function public.delete_expired_aima_bot_sessions()
returns void
language sql
security definer
set search_path = public
as $$
    delete from public.aima_bot_sessions
    where expires_at < now();
$$;

-- Supabase includes pg_cron. This removes abandoned sessions every 10 minutes.
create extension if not exists pg_cron with schema extensions;

do $$
begin
    if not exists (
        select 1
        from cron.job
        where jobname = 'delete-expired-aima-bot-sessions'
    ) then
        perform cron.schedule(
            'delete-expired-aima-bot-sessions',
            '*/10 * * * *',
            'select public.delete_expired_aima_bot_sessions();'
        );
    end if;
end
$$;
