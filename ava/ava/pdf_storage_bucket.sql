-- Boltane — Supabase Storage: private bucket for uploaded signup PDFs.
--
-- Run this once against your Supabase project (SQL editor, or as a
-- migration) — Supabase Storage buckets are tracked in the
-- storage.buckets table, so this is the SQL-native way to provision
-- one instead of using the dashboard.

insert into storage.buckets (id, name, public)
values ('signup-pdfs', 'signup-pdfs', false)
on conflict (id) do nothing;

-- No RLS policies are added for storage.objects here on purpose: with
-- public = false and zero policies for the anon/authenticated roles,
-- only the service role key (which api/upload-pdf.ts uses server-side
-- — see lib/supabase.ts) can read or write. That's what "private"
-- means for this bucket in this pass.
--
-- If Raheem's future manual-review UI authenticates via Supabase Auth
-- directly (rather than going through this backend with the service
-- role key), you'll need to add explicit SELECT policies on
-- storage.objects scoped to that bucket/role before it can fetch
-- files — not needed for anything built so far.

-- bucket_id/name are set to 'signup-pdfs' to match the default value
-- of SUPABASE_PDF_BUCKET in .env.example. If you rename the bucket,
-- update that env var to match.
