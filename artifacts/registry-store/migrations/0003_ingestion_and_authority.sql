BEGIN;

CREATE OR REPLACE FUNCTION registry.upsert_release_v1(
  record_jsonb jsonb,
  idempotency_key text,
  actor_principal text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE
  request_digest registry.sha256_hex;
  immutable_digest registry.sha256_hex;
  existing_audit registry.audit_event%ROWTYPE;
  existing_release registry.app_version%ROWTYPE;
  release_key bigint;
  result jsonb;
  profile_item jsonb;
  platform_item jsonb;
  capability_item jsonb;
  expected_interfaces text[];
  supplied_interfaces text[];
  search_text text;
  embedding_vector public.vector(384);
  model_dimensions integer;
  model_active boolean;
BEGIN
  IF record_jsonb IS NULL OR jsonb_typeof(record_jsonb) <> 'object' OR pg_column_size(record_jsonb) > 262144 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release record must be an object of at most 256 KiB';
  END IF;
  IF idempotency_key IS NULL OR idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR actor_principal IS NULL OR actor_principal !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid idempotency key or actor principal';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended('release-upsert:' || idempotency_key, 0));
  request_digest := registry.jsonb_sha256(record_jsonb);
  SELECT * INTO existing_audit
    FROM registry.audit_event
   WHERE operation = 'release-upsert' AND audit_event.idempotency_key = upsert_release_v1.idempotency_key;
  IF FOUND THEN
    IF existing_audit.request_digest_sha256 <> request_digest THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'idempotency key was reused with a different payload';
    END IF;
    RETURN jsonb_set(existing_audit.result, '{deduplicated}', 'true'::jsonb, true);
  END IF;

  IF NOT registry.jsonb_exact_keys(record_jsonb, ARRAY[
    'schema_version','record_id','app','publisher','contract','compatibility',
    'capabilities','source','search_metadata','embedding'
  ]) OR record_jsonb->>'schema_version' <> 'vibapp.registry-release-upsert.experimental.2026-08-24.1' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release record keys or schema_version are invalid';
  END IF;
  IF NOT registry.jsonb_exact_keys(record_jsonb->'app', ARRAY['id','version','kind','display_name','summary','package_digest_sha256'])
     OR NOT registry.jsonb_exact_keys(record_jsonb->'publisher', ARRAY['publisher_id','display_name'])
     OR NOT registry.jsonb_exact_keys(record_jsonb->'contract', ARRAY['package_format','component_contract','wasi','wit_world','manifest_digest_sha256'])
     OR NOT registry.jsonb_exact_keys(record_jsonb->'compatibility', ARRAY['profiles','platforms'])
     OR NOT registry.jsonb_exact_keys(record_jsonb->'source', ARRAY['visibility','license_spdx','source_digest_sha256','sbom_digest_sha256','provenance_digest_sha256'])
     OR NOT registry.jsonb_exact_keys(record_jsonb->'search_metadata', ARRAY['tags','capability_labels'])
     OR NOT registry.jsonb_exact_keys(record_jsonb->'embedding', ARRAY['model_id','model_version','dimensions','content_digest_sha256','vector']) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release record contains missing or unknown nested fields';
  END IF;

  IF record_jsonb->>'record_id' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR record_jsonb#>>'{app,id}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR record_jsonb#>>'{publisher,publisher_id}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR NOT registry.is_clean_text(record_jsonb#>>'{app,version}', 1, 64)
     OR NOT registry.is_clean_text(record_jsonb#>>'{app,display_name}', 1, 200)
     OR NOT registry.is_clean_text(record_jsonb#>>'{app,summary}', 1, 2000)
     OR NOT registry.is_clean_text(record_jsonb#>>'{publisher,display_name}', 1, 200) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release identity or display metadata is invalid';
  END IF;
  IF record_jsonb#>>'{app,kind}' NOT IN ('ui','service','hybrid')
     OR record_jsonb#>>'{app,package_digest_sha256}' !~ '^[0-9a-f]{64}$'
     OR record_jsonb#>>'{contract,manifest_digest_sha256}' !~ '^[0-9a-f]{64}$'
     OR record_jsonb#>>'{contract,package_format}' <> 'vibapp.package.experimental-v0'
     OR record_jsonb#>>'{contract,component_contract}' <> 'vibapp:experimental-v0@0.0.1'
     OR record_jsonb#>>'{contract,wasi}' <> '0.2'
     OR record_jsonb#>>'{contract,wit_world}' NOT IN ('ui-only-reference','service-only-reference','hybrid-reference','web-preview-reference') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release package or contract metadata is invalid';
  END IF;
  IF record_jsonb#>>'{source,visibility}' NOT IN ('private','public')
     OR NOT registry.is_clean_text(record_jsonb#>>'{source,license_spdx}', 1, 64)
     OR record_jsonb#>>'{source,source_digest_sha256}' !~ '^[0-9a-f]{64}$'
     OR record_jsonb#>>'{source,sbom_digest_sha256}' !~ '^[0-9a-f]{64}$'
     OR record_jsonb#>>'{source,provenance_digest_sha256}' !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release source metadata is invalid';
  END IF;

  IF jsonb_typeof(record_jsonb#>'{compatibility,profiles}') <> 'array'
     OR jsonb_array_length(record_jsonb#>'{compatibility,profiles}') NOT BETWEEN 1 AND 4
     OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(record_jsonb#>'{compatibility,profiles}'))
     OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(record_jsonb#>'{compatibility,profiles}') AS p(value) WHERE value NOT IN ('desktop','web-preview','web-runtime','headless')) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release profiles are invalid or duplicated';
  END IF;
  IF jsonb_typeof(record_jsonb#>'{compatibility,platforms}') <> 'array'
     OR jsonb_array_length(record_jsonb#>'{compatibility,platforms}') NOT BETWEEN 1 AND 16 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release platforms must contain 1..16 rows';
  END IF;
  FOR platform_item IN SELECT value FROM jsonb_array_elements(record_jsonb#>'{compatibility,platforms}')
  LOOP
    IF NOT registry.jsonb_exact_keys(platform_item, ARRAY['os','arch','profile'])
       OR platform_item->>'os' NOT IN ('macos','windows','linux','browser')
       OR platform_item->>'arch' NOT IN ('aarch64','x86-64','wasm32')
       OR platform_item->>'profile' NOT IN ('desktop','web-preview','web-runtime','headless')
       OR NOT (record_jsonb#>'{compatibility,profiles}' @> jsonb_build_array(platform_item->>'profile'))
       OR ((platform_item->>'profile' IN ('web-preview','web-runtime')) <> (platform_item->>'os' = 'browser' AND platform_item->>'arch' = 'wasm32')) THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release platform row is invalid';
    END IF;
  END LOOP;
  IF (SELECT count(*) <> count(DISTINCT value::text) FROM jsonb_array_elements(record_jsonb#>'{compatibility,platforms}')) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release platform rows must be unique';
  END IF;

  IF jsonb_typeof(record_jsonb->'capabilities') <> 'array'
     OR jsonb_array_length(record_jsonb->'capabilities') > 32 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release capabilities must be an array of at most 32 rows';
  END IF;
  FOR capability_item IN SELECT value FROM jsonb_array_elements(record_jsonb->'capabilities')
  LOOP
    IF NOT registry.jsonb_exact_keys(capability_item, ARRAY['interface','necessity','grant','scope_digest_sha256','summary'])
       OR capability_item->>'interface' !~ '^vibapp:experimental-v0/(clock|scheduler|notification|kv|log|host-info|settings|system-metrics|http)@0[.]0[.]1$'
       OR capability_item->>'necessity' NOT IN ('required','degradable')
       OR capability_item->>'grant' NOT IN ('automatic','user')
       OR capability_item->>'scope_digest_sha256' !~ '^[0-9a-f]{64}$'
       OR NOT registry.is_clean_text(capability_item->>'summary', 1, 500) THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release capability row is invalid';
    END IF;
  END LOOP;
  supplied_interfaces := ARRAY(SELECT value->>'interface' FROM jsonb_array_elements(record_jsonb->'capabilities') ORDER BY value->>'interface');
  IF cardinality(supplied_interfaces) <> cardinality(ARRAY(SELECT DISTINCT unnest(supplied_interfaces))) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release capability interfaces must be unique';
  END IF;
  expected_interfaces := CASE record_jsonb#>>'{contract,wit_world}'
    WHEN 'ui-only-reference' THEN ARRAY['vibapp:experimental-v0/clock@0.0.1','vibapp:experimental-v0/host-info@0.0.1','vibapp:experimental-v0/kv@0.0.1','vibapp:experimental-v0/log@0.0.1','vibapp:experimental-v0/settings@0.0.1']
    WHEN 'service-only-reference' THEN ARRAY['vibapp:experimental-v0/clock@0.0.1','vibapp:experimental-v0/host-info@0.0.1','vibapp:experimental-v0/http@0.0.1','vibapp:experimental-v0/kv@0.0.1','vibapp:experimental-v0/log@0.0.1','vibapp:experimental-v0/scheduler@0.0.1','vibapp:experimental-v0/settings@0.0.1','vibapp:experimental-v0/system-metrics@0.0.1']
    WHEN 'hybrid-reference' THEN ARRAY['vibapp:experimental-v0/clock@0.0.1','vibapp:experimental-v0/host-info@0.0.1','vibapp:experimental-v0/kv@0.0.1','vibapp:experimental-v0/log@0.0.1','vibapp:experimental-v0/notification@0.0.1','vibapp:experimental-v0/scheduler@0.0.1','vibapp:experimental-v0/settings@0.0.1']
    WHEN 'web-preview-reference' THEN ARRAY['vibapp:experimental-v0/clock@0.0.1','vibapp:experimental-v0/host-info@0.0.1','vibapp:experimental-v0/http@0.0.1','vibapp:experimental-v0/kv@0.0.1','vibapp:experimental-v0/log@0.0.1','vibapp:experimental-v0/scheduler@0.0.1','vibapp:experimental-v0/settings@0.0.1','vibapp:experimental-v0/system-metrics@0.0.1']
  END;
  IF supplied_interfaces <> expected_interfaces THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'capability interfaces do not exactly match the selected WIT world';
  END IF;

  IF jsonb_typeof(record_jsonb#>'{search_metadata,tags}') <> 'array'
     OR jsonb_typeof(record_jsonb#>'{search_metadata,capability_labels}') <> 'array'
     OR jsonb_array_length(record_jsonb#>'{search_metadata,tags}') > 64
     OR jsonb_array_length(record_jsonb#>'{search_metadata,capability_labels}') > 64
     OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,tags}'))
     OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,capability_labels}'))
     OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,tags}') AS t(value) WHERE NOT registry.is_clean_text(value, 1, 80))
     OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,capability_labels}') AS t(value) WHERE NOT registry.is_clean_text(value, 1, 80)) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release search metadata is invalid or duplicated';
  END IF;

  IF (record_jsonb#>>'{embedding,dimensions}')::integer <> 384
     OR record_jsonb#>>'{embedding,model_id}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR record_jsonb#>>'{embedding,model_version}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR record_jsonb#>>'{embedding,content_digest_sha256}' !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'release embedding metadata is invalid';
  END IF;
  SELECT dimensions, active INTO model_dimensions, model_active
    FROM registry.embedding_model
   WHERE model_id = record_jsonb#>>'{embedding,model_id}'
     AND model_version = record_jsonb#>>'{embedding,model_version}';
  IF NOT FOUND OR model_dimensions <> 384 OR NOT model_active THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'embedding model/version is unknown, inactive, or not 384-dimensional';
  END IF;
  embedding_vector := registry.jsonb_vector_384(record_jsonb#>'{embedding,vector}');

  search_text := concat_ws(E'\n',
    record_jsonb#>>'{app,display_name}', record_jsonb#>>'{app,summary}',
    ARRAY(SELECT value FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,tags}') ORDER BY value)::text,
    ARRAY(SELECT value FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,capability_labels}') ORDER BY value)::text
  );
  IF record_jsonb#>>'{embedding,content_digest_sha256}' <> encode(public.digest(convert_to(search_text, 'UTF8'), 'sha256'), 'hex') THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'embedding content digest does not match the canonical search document';
  END IF;

  immutable_digest := registry.jsonb_sha256(record_jsonb - 'embedding');

  INSERT INTO registry.publisher(publisher_id, display_name)
  VALUES ((record_jsonb#>>'{publisher,publisher_id}')::registry.identifier, record_jsonb#>>'{publisher,display_name}')
  ON CONFLICT (publisher_id) DO NOTHING;
  IF NOT EXISTS (
    SELECT 1 FROM registry.publisher
     WHERE publisher_id = record_jsonb#>>'{publisher,publisher_id}'
       AND display_name = record_jsonb#>>'{publisher,display_name}'
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'publisher identity conflicts with stored metadata';
  END IF;

  INSERT INTO registry.app(app_id, publisher_id)
  VALUES ((record_jsonb#>>'{app,id}')::registry.identifier, (record_jsonb#>>'{publisher,publisher_id}')::registry.identifier)
  ON CONFLICT (app_id) DO NOTHING;
  IF NOT EXISTS (
    SELECT 1 FROM registry.app
     WHERE app_id = record_jsonb#>>'{app,id}'
       AND publisher_id = record_jsonb#>>'{publisher,publisher_id}'
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'app identity conflicts with stored publisher';
  END IF;

  SELECT * INTO existing_release FROM registry.app_version
   WHERE app_id = record_jsonb#>>'{app,id}' AND version = record_jsonb#>>'{app,version}';
  IF FOUND THEN
    IF existing_release.immutable_record_digest_sha256 <> immutable_digest
       OR existing_release.package_digest_sha256 <> record_jsonb#>>'{app,package_digest_sha256}' THEN
      RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'immutable app version metadata cannot be changed';
    END IF;
    release_key := existing_release.release_id;
  ELSE
    INSERT INTO registry.app_version(
      record_id, app_id, version, kind, display_name, summary,
      package_digest_sha256, manifest_digest_sha256, package_format,
      component_contract, wasi_version, wit_world, source_visibility,
      license_spdx, source_digest_sha256, sbom_digest_sha256,
      provenance_digest_sha256, tags, capability_labels, search_document,
      immutable_record_digest_sha256
    ) VALUES (
      (record_jsonb->>'record_id')::registry.identifier,
      (record_jsonb#>>'{app,id}')::registry.identifier,
      record_jsonb#>>'{app,version}', (record_jsonb#>>'{app,kind}')::registry.app_kind,
      record_jsonb#>>'{app,display_name}', record_jsonb#>>'{app,summary}',
      (record_jsonb#>>'{app,package_digest_sha256}')::registry.sha256_hex,
      (record_jsonb#>>'{contract,manifest_digest_sha256}')::registry.sha256_hex,
      record_jsonb#>>'{contract,package_format}', record_jsonb#>>'{contract,component_contract}',
      record_jsonb#>>'{contract,wasi}', record_jsonb#>>'{contract,wit_world}',
      (record_jsonb#>>'{source,visibility}')::registry.source_visibility,
      record_jsonb#>>'{source,license_spdx}',
      (record_jsonb#>>'{source,source_digest_sha256}')::registry.sha256_hex,
      (record_jsonb#>>'{source,sbom_digest_sha256}')::registry.sha256_hex,
      (record_jsonb#>>'{source,provenance_digest_sha256}')::registry.sha256_hex,
      ARRAY(SELECT value FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,tags}') ORDER BY value),
      ARRAY(SELECT value FROM jsonb_array_elements_text(record_jsonb#>'{search_metadata,capability_labels}') ORDER BY value),
      search_text, immutable_digest
    ) RETURNING release_id INTO release_key;

    FOR profile_item IN SELECT value FROM jsonb_array_elements(record_jsonb#>'{compatibility,profiles}')
    LOOP
      INSERT INTO registry.release_profile(release_id, profile) VALUES (release_key, (profile_item#>>'{}')::registry.execution_profile);
    END LOOP;
    FOR platform_item IN SELECT value FROM jsonb_array_elements(record_jsonb#>'{compatibility,platforms}')
    LOOP
      INSERT INTO registry.release_platform(release_id, os, arch, profile)
      VALUES (release_key, (platform_item->>'os')::registry.platform_os, (platform_item->>'arch')::registry.platform_arch, (platform_item->>'profile')::registry.execution_profile);
    END LOOP;
    FOR capability_item IN SELECT value FROM jsonb_array_elements(record_jsonb->'capabilities')
    LOOP
      INSERT INTO registry.release_capability(release_id, interface_name, necessity)
      VALUES (release_key, capability_item->>'interface', (capability_item->>'necessity')::registry.capability_necessity);
      INSERT INTO registry.release_permission(release_id, interface_name, grant_mode, scope_digest_sha256, summary)
      VALUES (release_key, capability_item->>'interface', (capability_item->>'grant')::registry.capability_grant, (capability_item->>'scope_digest_sha256')::registry.sha256_hex, capability_item->>'summary');
    END LOOP;
    INSERT INTO registry.publication(release_id, state, visibility) VALUES (release_key, 'unpublished', 'private');
    INSERT INTO registry.authority_grant(authority_event_id, release_id, authority_kind, principal_id, payload_digest_sha256, granted_at)
    VALUES (
      ('authority.private.' || substr(request_digest::text, 1, 32))::registry.identifier,
      release_key, 'private-store', actor_principal::registry.identifier,
      request_digest, statement_timestamp()
    );
  END IF;

  INSERT INTO registry.release_embedding(release_id, model_id, model_version, content_digest_sha256, embedding)
  VALUES (
    release_key,
    (record_jsonb#>>'{embedding,model_id}')::registry.identifier,
    (record_jsonb#>>'{embedding,model_version}')::registry.identifier,
    (record_jsonb#>>'{embedding,content_digest_sha256}')::registry.sha256_hex,
    embedding_vector
  )
  ON CONFLICT (release_id, model_id, model_version) DO UPDATE
    SET content_digest_sha256 = EXCLUDED.content_digest_sha256,
        embedding = EXCLUDED.embedding,
        embedded_at = statement_timestamp()
    WHERE registry.release_embedding.content_digest_sha256 = EXCLUDED.content_digest_sha256;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'embedding model/version content binding conflicts with stored metadata';
  END IF;

  result := jsonb_build_object(
    'schema_version', 'vibapp.registry-release-upsert-result.experimental.2026-08-24.1',
    'record_id', record_jsonb->>'record_id',
    'app_id', record_jsonb#>>'{app,id}',
    'version', record_jsonb#>>'{app,version}',
    'package_digest_sha256', record_jsonb#>>'{app,package_digest_sha256}',
    'release_id', release_key,
    'publication_state', 'unpublished',
    'deduplicated', false
  );
  INSERT INTO registry.audit_event(
    event_id, operation, idempotency_key, actor_principal, request_digest_sha256,
    outcome, subject_app_id, subject_release_id, result
  ) VALUES (
    ('audit.upsert.' || substr(request_digest::text, 1, 32))::registry.identifier,
    'release-upsert', idempotency_key::registry.identifier, actor_principal::registry.identifier,
    request_digest, 'accepted', (record_jsonb#>>'{app,id}')::registry.identifier,
    release_key, result
  );
  RETURN result;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.record_verification_v1(
  package_digest registry.sha256_hex,
  evidence_id registry.identifier,
  evidence_kind text,
  verifier_id registry.identifier,
  evidence_digest registry.sha256_hex,
  verdict registry.verification_status,
  observed_at timestamptz,
  metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE release_key bigint;
BEGIN
  SELECT release_id INTO STRICT release_key FROM registry.app_version WHERE package_digest_sha256 = package_digest;
  IF evidence_kind NOT IN ('manifest','component','provenance','sbom','build','scan','independent-verification')
     OR jsonb_typeof(metadata) <> 'object' OR pg_column_size(metadata) > 65536 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'verification evidence is invalid';
  END IF;
  INSERT INTO registry.verification_evidence(evidence_id, release_id, evidence_kind, verifier_id, evidence_digest_sha256, verdict, observed_at, metadata)
  VALUES (evidence_id, release_key, evidence_kind, verifier_id, evidence_digest, verdict, observed_at, metadata);
  INSERT INTO registry.release_verification(release_id, status, evidence_id, verified_at)
  VALUES (release_key, verdict, evidence_id, CASE WHEN verdict = 'verified' THEN observed_at END)
  ON CONFLICT (release_id) DO UPDATE SET
    status = EXCLUDED.status, evidence_id = EXCLUDED.evidence_id, verified_at = EXCLUDED.verified_at;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.grant_authority_v1(
  package_digest registry.sha256_hex,
  authority_event_id registry.identifier,
  authority_kind registry.authority_kind,
  principal_id registry.identifier,
  payload_digest registry.sha256_hex,
  granted_at timestamptz,
  expires_at timestamptz DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE release_key bigint;
BEGIN
  SELECT release_id INTO STRICT release_key FROM registry.app_version WHERE package_digest_sha256 = package_digest;
  INSERT INTO registry.authority_grant(authority_event_id, release_id, authority_kind, principal_id, payload_digest_sha256, granted_at, expires_at)
  VALUES (authority_event_id, release_key, authority_kind, principal_id, payload_digest, granted_at, expires_at);
END;
$function$;

CREATE OR REPLACE FUNCTION registry.publish_release_v1(
  package_digest registry.sha256_hex,
  moderation_evidence registry.sha256_hex
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE release_key bigint;
BEGIN
  SELECT release_id INTO STRICT release_key FROM registry.app_version WHERE package_digest_sha256 = package_digest;
  IF NOT EXISTS (SELECT 1 FROM registry.release_verification WHERE release_id = release_key AND status = 'verified')
     OR EXISTS (SELECT 1 FROM registry.revocation_event WHERE release_id = release_key AND effective_at <= statement_timestamp())
     OR NOT EXISTS (SELECT 1 FROM registry.authority_grant WHERE release_id = release_key AND authority_kind = 'public-list' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > statement_timestamp()))
     OR NOT EXISTS (SELECT 1 FROM registry.authority_grant WHERE release_id = release_key AND authority_kind = 'public-install' AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > statement_timestamp())) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'release lacks verification or separate active public authorities';
  END IF;
  UPDATE registry.publication
     SET state = 'published', visibility = 'public', moderation_evidence_digest_sha256 = moderation_evidence, changed_at = statement_timestamp()
   WHERE release_id = release_key;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.revoke_release_v1(
  package_digest registry.sha256_hex,
  event_id registry.identifier,
  authority_id registry.identifier,
  reason_code text,
  reason text,
  evidence_digest registry.sha256_hex,
  effective_at timestamptz
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE release_key bigint;
BEGIN
  SELECT release_id INTO STRICT release_key FROM registry.app_version WHERE package_digest_sha256 = package_digest;
  INSERT INTO registry.revocation_event(event_id, release_id, authority_id, reason_code, reason, evidence_digest_sha256, effective_at)
  VALUES (event_id, release_key, authority_id, reason_code, reason, evidence_digest, effective_at);
  UPDATE registry.publication SET state = 'withdrawn', changed_at = statement_timestamp() WHERE release_id = release_key;
END;
$function$;

COMMIT;
