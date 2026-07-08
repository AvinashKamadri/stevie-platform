-- Migration 019 — blog_posts.slug is NOT unique.
--
-- slug is the URL's trailing segment, a convenience/display field — `url` is the
-- stable unique natural key. Legacy HubSpot URLs collide on the trailing slug
-- across distinct urls (e.g. /blog/bid/<id>/<slug> vs /blog/<slug>), so the
-- UNIQUE on slug (from 018) is wrong and aborted the full ingestion with a
-- duplicate-key error. Drop it; keep a plain index. Forward-only fix (018 is
-- already shipped/banked). Found during full ingestion 2026-07-08.
alter table blog_posts drop constraint if exists blog_posts_slug_key;
create index if not exists blog_posts_slug_idx on blog_posts (slug);
