-- Every crawl reports how many of its postings joined an existing variant
-- group, including zero (issue #61, AGENTS.md rule 5).

alter table public.job_hunter_source_crawls
  add column joined_variant_group integer not null default 0
    check (joined_variant_group >= 0);

comment on column public.job_hunter_source_crawls.joined_variant_group is
  'How many postings this crawl persisted joined an already-existing variant '
  'group (#61), as opposed to founding a new one or having no ATS board to '
  'group by. Written on every crawl, including when it is zero.';
