BEGIN;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE SCHEMA IF NOT EXISTS registry;

CREATE TYPE registry.app_kind AS ENUM ('ui', 'service', 'hybrid');
CREATE TYPE registry.execution_profile AS ENUM ('desktop', 'web-preview', 'web-runtime', 'headless');
CREATE TYPE registry.platform_os AS ENUM ('macos', 'windows', 'linux', 'browser');
CREATE TYPE registry.platform_arch AS ENUM ('aarch64', 'x86-64', 'wasm32');
CREATE TYPE registry.capability_necessity AS ENUM ('required', 'degradable');
CREATE TYPE registry.capability_grant AS ENUM ('automatic', 'user');
CREATE TYPE registry.source_visibility AS ENUM ('private', 'public');
CREATE TYPE registry.publisher_verification AS ENUM ('unverified', 'pending', 'verified', 'suspended');
CREATE TYPE registry.trust_tier AS ENUM ('unknown', 'community', 'verified', 'first-party');
CREATE TYPE registry.verification_status AS ENUM ('pending', 'verified', 'failed');
CREATE TYPE registry.publication_state AS ENUM ('unpublished', 'review', 'published', 'withdrawn');
CREATE TYPE registry.authority_kind AS ENUM ('private-store', 'remote-process', 'public-list', 'public-install');
CREATE TYPE registry.audit_outcome AS ENUM ('accepted', 'deduplicated', 'rejected');

CREATE DOMAIN registry.identifier AS text
  CHECK (
    VALUE ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    AND VALUE !~ '[[:cntrl:]]'
  );

CREATE DOMAIN registry.sha256_hex AS text
  CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE OR REPLACE FUNCTION registry.is_clean_text(value text, minimum_length integer, maximum_length integer)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $function$
  SELECT value IS NOT NULL
    AND char_length(value) BETWEEN minimum_length AND maximum_length
    AND value !~ '[[:cntrl:]]';
$function$;

CREATE OR REPLACE FUNCTION registry.jsonb_exact_keys(value jsonb, expected text[])
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $function$
  SELECT jsonb_typeof(value) = 'object'
    AND ARRAY(SELECT key FROM jsonb_object_keys(value) AS key ORDER BY key)
        = ARRAY(SELECT key FROM unnest(expected) AS key ORDER BY key);
$function$;

CREATE OR REPLACE FUNCTION registry.jsonb_sha256(value jsonb)
RETURNS registry.sha256_hex
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $function$
  SELECT encode(public.digest(convert_to(value::text, 'UTF8'), 'sha256'), 'hex')::registry.sha256_hex;
$function$;

CREATE OR REPLACE FUNCTION registry.jsonb_vector_384(value jsonb)
RETURNS public.vector(384)
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
DECLARE
  result public.vector(384);
BEGIN
  IF jsonb_typeof(value) <> 'array' OR jsonb_array_length(value) <> 384 THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'embedding must contain exactly 384 numbers';
  END IF;
  BEGIN
    result := value::text::public.vector(384);
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'embedding contains an invalid or non-finite value';
  END;
  IF public.vector_norm(result) <= 0 THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'embedding must have a non-zero finite norm';
  END IF;
  RETURN result;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.cursor_encode(value jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
  SELECT encode(convert_to(value::text, 'UTF8'), 'hex');
$function$;

CREATE OR REPLACE FUNCTION registry.cursor_decode(value text)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
DECLARE
  result jsonb;
BEGIN
  IF char_length(value) > 2048 OR value !~ '^[0-9a-f]+$' OR char_length(value) % 2 <> 0 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registry cursor';
  END IF;
  BEGIN
    result := convert_from(decode(value, 'hex'), 'UTF8')::jsonb;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registry cursor';
  END;
  RETURN result;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.or_tsquery(value text)
RETURNS tsquery
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $function$
  SELECT COALESCE(
    pg_catalog.to_tsquery(
      'simple'::regconfig,
      (SELECT string_agg(quote_literal(lexeme), ' | ' ORDER BY lexeme)
         FROM unnest(pg_catalog.tsvector_to_array(pg_catalog.to_tsvector('simple'::regconfig, value))) AS lexeme
        WHERE lexeme <> ALL(ARRAY[
          'a','an','and','as','at','be','for','in','is','it','of','on','or','the','this','to','with','without'
        ]::text[]))
    ),
    pg_catalog.to_tsquery('simple'::regconfig, quote_literal('__vibapp_no_lexeme__'))
  );
$function$;

COMMIT;
