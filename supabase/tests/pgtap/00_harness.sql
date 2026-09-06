-- Proves the pgTAP harness itself works: the extension loads, the test
-- runs inside a transaction that is rolled back, and the shared auth
-- schema the isolation tests seed into is present.
begin;
create extension if not exists pgtap with schema extensions;
select plan(2);

select has_extension('pgtap', 'pgtap extension is installed');
select has_table('auth', 'users', 'auth.users exists for seeding test users');

select * from finish();
rollback;
