BEGIN;

-- Only the trusted source-archive service may attest a completed GitHub upload.
-- Pending/failed jobs deliberately have no row in this table.
CREATE TABLE registry.github_source_archive (
  release_id bigint PRIMARY KEY REFERENCES registry.app_version(release_id) ON DELETE RESTRICT,
  source_digest_sha256 registry.sha256_hex NOT NULL,
  package_digest_sha256 registry.sha256_hex NOT NULL,
  organization text NOT NULL CHECK (organization = 'vib-app'),
  repository text NOT NULL CHECK (repository ~ '^app-[0-9a-f]{64}$'),
  repository_id bigint NOT NULL CHECK (repository_id > 0 AND repository_id <= 9007199254740991),
  commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  verified_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  UNIQUE (release_id, source_digest_sha256, package_digest_sha256)
);

CREATE OR REPLACE FUNCTION registry.record_github_source_archive_v1(
  package_digest registry.sha256_hex,
  receipt jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE
  release_row registry.app_version%ROWTYPE;
  stored registry.github_source_archive%ROWTYPE;
  expected_repository text;
BEGIN
  SELECT * INTO STRICT release_row FROM registry.app_version
    WHERE package_digest_sha256 = package_digest FOR UPDATE;
  SELECT 'app-' || encode(public.digest(convert_to(a.publisher_id::text || E'\n' || a.app_id::text, 'UTF8'), 'sha256'), 'hex')
    INTO expected_repository FROM registry.app a WHERE a.app_id = release_row.app_id;
  IF receipt IS NULL OR registry.jsonb_exact_keys(receipt, ARRAY[
    'organization','repository','repository_id','commit_sha','source_digest_sha256','package_digest_sha256'
  ]) IS NOT TRUE OR pg_column_size(receipt) > 4096 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid GitHub source archive receipt';
  END IF;
  IF (receipt->>'organization' = 'vib-app'
      AND receipt->>'repository' = expected_repository
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
    IF stored.repository_id <> (receipt->>'repository_id')::bigint
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
    release_row.package_digest_sha256, 'vib-app', expected_repository,
    (receipt->>'repository_id')::bigint, receipt->>'commit_sha');
END;
$function$;

ALTER TABLE registry.publication ADD COLUMN source_archive_release_id bigint
  REFERENCES registry.github_source_archive(release_id) ON DELETE RESTRICT;

-- Legacy public rows have no attestation. Keep their data and moderation evidence,
-- but require archive backfill + a fresh publish operation before public listing.
UPDATE registry.publication SET state = 'review', visibility = 'private', changed_at = statement_timestamp()
  WHERE state = 'published';

ALTER TABLE registry.publication ADD CONSTRAINT public_requires_github_archive
  CHECK (state <> 'published' OR
    (source_archive_release_id IS NOT NULL AND source_archive_release_id = release_id));

CREATE OR REPLACE FUNCTION registry.require_github_archive_for_publication_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, registry
AS $function$
BEGIN
  IF NEW.state = 'published' THEN
    IF NOT EXISTS (
      SELECT 1 FROM registry.github_source_archive s
      JOIN registry.app_version v ON v.release_id = s.release_id
      WHERE s.release_id = NEW.release_id AND s.organization = 'vib-app'
        AND s.source_digest_sha256 = v.source_digest_sha256
        AND s.package_digest_sha256 = v.package_digest_sha256
    ) THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'source-archive-required: upload verified source to vib-app before public publication';
    END IF;
    NEW.source_archive_release_id := NEW.release_id;
  END IF;
  RETURN NEW;
END;
$function$;

CREATE TRIGGER public_source_archive_gate BEFORE INSERT OR UPDATE ON registry.publication
  FOR EACH ROW EXECUTE FUNCTION registry.require_github_archive_for_publication_v1();

CREATE OR REPLACE VIEW registry.github_source_archive_facts_v1 AS
  SELECT release_id, jsonb_build_object(
    'organization', organization, 'repository', repository, 'repository_id', repository_id,
    'commit_sha', commit_sha, 'source_digest_sha256', source_digest_sha256,
    'package_digest_sha256', package_digest_sha256
  ) AS github_archive FROM registry.github_source_archive;

-- The authenticated publication service resolves immutable source input here;
-- it does not need SELECT on all Registry tables or receipt-write authority.
CREATE VIEW registry.source_archive_input_v1 AS
  SELECT a.publisher_id, v.app_id, v.source_digest_sha256, v.package_digest_sha256
  FROM registry.app_version v JOIN registry.app a USING (app_id);

REVOKE ALL ON registry.github_source_archive, registry.github_source_archive_facts_v1 FROM PUBLIC;
REVOKE ALL ON registry.source_archive_input_v1 FROM PUBLIC;
REVOKE ALL ON FUNCTION registry.record_github_source_archive_v1(registry.sha256_hex,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION registry.require_github_archive_for_publication_v1() FROM PUBLIC;

COMMIT;
