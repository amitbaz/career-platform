-- General ownership contract for the engine's database side (ADR-0002, issue
-- #286, step A: "borders, no code moves").
--
-- job_hunter_shared_writes.sql proves the #179 pattern in depth, table by
-- table and definer function by definer function, against a hand-curated
-- list. This file generalizes the *inventory* half: every `job_hunter_*`
-- table in `public` with no `user_id` column is, by ADR-0002's own
-- definition, engine-owned data with no per-user dimension -- shared, and
-- therefore writable only by the privileged/owner role. A table matching
-- that shape and shipped without the matching grant lockdown fails here
-- automatically, rather than waiting for someone to notice it is missing
-- from a hand-written list (AGENTS.md rule 6: a claim needs a mechanism that
-- fails when it stops being true).
--
-- Five tables are excluded below by name, tracked as issue #292: they already
-- grant anon/authenticated/service_role full write access today, and closing
-- that blind risks breaking a write path that may depend on the service_role
-- grant specifically (a platform-wide ledger with no per-user auth context to
-- read instead). #286 is step A -- borders, no behaviour change -- so
-- deciding that is out of scope here; #292 does the tracing and the fix.
-- Removing an entry from the exclusion list below without first closing its
-- grant in a migration would turn a real, open gap into a false green.
--
-- Until step E splits `public` into engine/app/api schemas, "the api schema
-- is the only exposed schema" has nothing to check yet at the schema-split
-- level. What is checkable today is which schemas app roles can reach at
-- all: this file pins that to Supabase's own fixed set of infrastructure
-- schemas plus `public`, so a new product schema exposed to `anon` or
-- `authenticated` before step E does it deliberately is a decision that
-- fails loudly rather than one nobody notices.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Tracked as issue #292. See the file header before adding or removing an
-- entry here.
create view pg_temp.excluded_from_ownership_check as
select unnest(array[
  'job_hunter_engine_lab_impressions',
  'job_hunter_engine_lab_judgements',
  'job_hunter_platform_ai_quota_state',
  'job_hunter_platform_ai_usage',
  'job_hunter_platform_search_usage'
]) as table_name;

create view pg_temp.engine_tables as
select c.relname::text as table_name
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relkind = 'r'
   and c.relname like 'job\_hunter\_%'
   and not exists (
     select 1 from pg_attribute a
      where a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
        and a.attname = 'user_id'
   )
   and c.relname not in (select table_name from pg_temp.excluded_from_ownership_check);

-- Discovery itself is asserted first: a query that regresses to zero rows
-- must not read as "every engine table passed its ownership check".
select cmp_ok(
  (select count(*)::int from pg_temp.engine_tables), '>=', 10,
  'engine-table discovery (no user_id column) found a plausible number of tables');

select is(
  (select array_agg(t.table_name || ':' || v.verb || ':' || r.role_name
                     order by t.table_name, v.verb, r.role_name)
     from pg_temp.engine_tables t
     cross join (values ('insert'), ('update'), ('delete')) as v(verb)
     cross join (values ('anon'), ('authenticated'), ('service_role')) as r(role_name)
    where has_table_privilege(r.role_name, 'public.' || t.table_name, v.verb)),
  null,
  'no role a user can hold may insert, update or delete an engine-owned table');

-- The owner still writes every one of them. If this fails, the tables above
-- are sealed rather than narrowed, and the refusal above passes for the
-- wrong reason -- the same positive control job_hunter_shared_writes.sql
-- runs for its own list.
select is(
  (select count(*)::int
     from pg_temp.engine_tables t
    where has_table_privilege(current_user, 'public.' || t.table_name, 'insert')),
  (select count(*)::int from pg_temp.engine_tables),
  'the owner (current_user) can still write every engine-owned table');

-- Which schemas app roles can reach at all. `public` plus Supabase's own
-- fixed infrastructure schemas today; nothing else, until step E adds a
-- deliberately-exposed `api` schema.
select is(
  (select array_agg(n.nspname::text order by n.nspname)
     from pg_namespace n
    where n.nspname not like 'pg\_%'
      and n.nspname <> 'information_schema'
      and (has_schema_privilege('anon', n.nspname, 'usage')
        or has_schema_privilege('authenticated', n.nspname, 'usage'))),
  array['auth', 'extensions', 'graphql', 'graphql_public', 'public', 'realtime',
        'storage', 'supabase_functions'],
  'anon/authenticated reach only Supabase''s standard schemas plus public -- '
  'no other product schema is exposed yet (ADR-0002 step E introduces engine/app/api)');

select * from finish();
rollback;
