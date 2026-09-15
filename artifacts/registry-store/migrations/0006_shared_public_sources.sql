BEGIN;

-- Preserve existing per-app private receipts. New public-source archives share
-- one organization repository, while still binding exact source/package bytes.
ALTER TABLE registry.github_source_archive
  DROP CONSTRAINT github_source_archive_repository_check;
ALTER TABLE registry.github_source_archive ADD CONSTRAINT github_source_archive_repository_check
  CHECK (repository = 'sources' OR repository ~ '^app-[0-9a-f]{64}$');

CREATE OR REPLACE FUNCTION registry.record_github_source_archive_v1(
  package_digest registry.sha256_hex, receipt jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE
  release_row registry.app_version%ROWTYPE;
  stored registry.github_source_archive%ROWTYPE;
  legacy_repository text;
BEGIN
  SELECT * INTO STRICT release_row FROM registry.app_version
    WHERE package_digest_sha256 = package_digest FOR UPDATE;
  SELECT 'app-' || encode(public.digest(convert_to(a.publisher_id::text || E'\n' || a.app_id::text, 'UTF8'), 'sha256'), 'hex')
    INTO legacy_repository FROM registry.app a WHERE a.app_id = release_row.app_id;
  IF receipt IS NULL OR registry.jsonb_exact_keys(receipt, ARRAY[
    'organization','repository','repository_id','commit_sha','source_digest_sha256','package_digest_sha256'
  ]) IS NOT TRUE OR pg_column_size(receipt) > 4096 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid GitHub source archive receipt';
  END IF;
  IF (receipt->>'organization' = 'vib-app'
      AND receipt->>'repository' IN ('sources', legacy_repository)
      AND jsonb_typeof(receipt->'repository_id') = 'number'
      AND (receipt->>'repository_id') ~ '^[1-9][0-9]{0,15}$'
      AND (receipt->>'repository_id')::numeric <= 9007199254740991
      AND receipt->>'commit_sha' ~ '^[0-9a-f]{40}$'
      AND receipt->>'source_digest_sha256' = release_row.source_digest_sha256
      AND receipt->>'package_digest_sha256' = release_row.package_digest_sha256) IS NOT TRUE THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'GitHub source archive does not match organization, owner, source or package';
  END IF;
  SELECT * INTO stored FROM registry.github_source_archive WHERE release_id = release_row.release_id;
  IF FOUND THEN
    IF stored.repository <> receipt->>'repository'
       OR stored.repository_id <> (receipt->>'repository_id')::bigint
       OR stored.commit_sha <> receipt->>'commit_sha'
       OR stored.source_digest_sha256 <> receipt->>'source_digest_sha256' THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'conflicting GitHub archive receipt';
    END IF;
    RETURN;
  END IF;
  INSERT INTO registry.github_source_archive(
    release_id, source_digest_sha256, package_digest_sha256, organization,
    repository, repository_id, commit_sha
  ) VALUES (release_row.release_id, release_row.source_digest_sha256,
    release_row.package_digest_sha256, 'vib-app', receipt->>'repository',
    (receipt->>'repository_id')::bigint, receipt->>'commit_sha');
END;
$function$;

REVOKE ALL ON FUNCTION registry.record_github_source_archive_v1(registry.sha256_hex,jsonb) FROM PUBLIC;

COMMIT;
