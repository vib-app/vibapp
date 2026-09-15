BEGIN;

CREATE OR REPLACE FUNCTION registry.set_publisher_trust_v1(
  target_publisher registry.identifier,
  verification registry.publisher_verification,
  tier registry.trust_tier,
  evidence_digest registry.sha256_hex
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
BEGIN
  IF verification = 'verified' AND (tier NOT IN ('verified','first-party') OR evidence_digest IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'verified publisher requires verified trust tier and evidence';
  END IF;
  UPDATE registry.publisher
     SET verification_state = verification,
         trust_tier = tier,
         authority_evidence_digest_sha256 = evidence_digest,
         updated_at = statement_timestamp()
   WHERE publisher_id = target_publisher;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'publisher not found';
  END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.validate_search_request_v1(request_jsonb jsonb)
RETURNS void
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
  need jsonb;
  constraints_json jsonb;
  item jsonb;
BEGIN
  IF request_jsonb IS NULL OR jsonb_typeof(request_jsonb) <> 'object' OR pg_column_size(request_jsonb) > 98304
     OR NOT registry.jsonb_exact_keys(request_jsonb, ARRAY['need','constraints','limit']) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'search request must contain exactly need, constraints and limit and fit 96 KiB';
  END IF;
  need := request_jsonb->'need';
  constraints_json := request_jsonb->'constraints';
  IF NOT registry.jsonb_exact_keys(need, ARRAY[
    'schema_version','document_type','need_id','owner','goal','requirements',
    'negative_constraints','platforms','profiles','permission_ceiling',
    'privacy_requirement','created_at_utc','revision'
  ]) OR need->>'schema_version' <> 'vibapp.need-spec.product-v0.0.1'
     OR need->>'document_type' <> 'need-spec'
     OR need->>'need_id' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR NOT registry.jsonb_exact_keys(need->'owner', ARRAY['principal_id','principal_kind'])
     OR need#>>'{owner,principal_id}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR need#>>'{owner,principal_kind}' NOT IN ('user','service','auditor')
     OR NOT registry.is_clean_text(need->>'goal', 1, 4000)
     OR need->>'privacy_requirement' NOT IN ('local-private','remote-private','public')
     OR need->>'created_at_utc' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
     OR jsonb_typeof(need->'revision') <> 'number' OR (need->>'revision')::numeric < 1
     OR (need->>'revision')::numeric <> trunc((need->>'revision')::numeric) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec identity, goal, privacy or revision is invalid';
  END IF;
  IF jsonb_typeof(need->'requirements') <> 'array' OR jsonb_array_length(need->'requirements') NOT BETWEEN 1 AND 64
     OR jsonb_typeof(need->'negative_constraints') <> 'array' OR jsonb_array_length(need->'negative_constraints') > 64
     OR jsonb_typeof(need->'platforms') <> 'array' OR jsonb_array_length(need->'platforms') NOT BETWEEN 1 AND 16
     OR jsonb_typeof(need->'profiles') <> 'array' OR jsonb_array_length(need->'profiles') NOT BETWEEN 1 AND 4 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec arrays exceed their bounds';
  END IF;
  IF (SELECT count(*) <> count(DISTINCT value->>'requirement_id') FROM jsonb_array_elements(need->'requirements'))
     OR (SELECT count(*) <> count(DISTINCT value->>'constraint_id') FROM jsonb_array_elements(need->'negative_constraints'))
     OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(need->'profiles'))
     OR (SELECT count(*) <> count(DISTINCT value::text) FROM jsonb_array_elements(need->'platforms')) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec contains duplicate identifiers, profiles or platforms';
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(need->'requirements') LOOP
    IF NOT registry.jsonb_exact_keys(item, ARRAY['requirement_id','text','priority','acceptance_examples'])
       OR item->>'requirement_id' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
       OR NOT registry.is_clean_text(item->>'text', 1, 1000)
       OR item->>'priority' NOT IN ('must-have','nice-to-have')
       OR jsonb_typeof(item->'acceptance_examples') <> 'array' OR jsonb_array_length(item->'acceptance_examples') > 16
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(item->'acceptance_examples') e(value) WHERE NOT registry.is_clean_text(value,1,1000)) THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec requirement is invalid';
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(need->'profiles') p(value) WHERE value NOT IN ('desktop','web-preview','web-runtime','headless')) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec profile is invalid';
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(need->'negative_constraints') LOOP
    IF NOT registry.jsonb_exact_keys(item, ARRAY['constraint_id','kind','value','source'])
       OR item->>'constraint_id' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
       OR item->>'kind' NOT IN ('forbidden-capability','forbidden-publisher','forbidden-package','privacy','platform','profile','other')
       OR NOT registry.is_clean_text(item->>'value', 1, 500)
       OR item->>'source' NOT IN ('user','rejection-feedback','policy') THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec negative constraint is invalid';
    END IF;
  END LOOP;
  FOR item IN SELECT value FROM jsonb_array_elements(need->'platforms') LOOP
    IF NOT registry.jsonb_exact_keys(item, ARRAY['os','arch','profile'])
       OR item->>'os' NOT IN ('macos','windows','linux','browser')
       OR item->>'arch' NOT IN ('aarch64','x86-64','wasm32')
       OR item->>'profile' NOT IN ('desktop','web-preview','web-runtime','headless')
       OR NOT (need->'profiles' @> jsonb_build_array(item->>'profile')) THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec platform is invalid';
    END IF;
  END LOOP;
  IF NOT registry.jsonb_exact_keys(need->'permission_ceiling', ARRAY['allowed_interfaces','forbidden_interfaces','maximum_scope_digests'])
     OR jsonb_typeof(need#>'{permission_ceiling,allowed_interfaces}') <> 'array'
     OR jsonb_typeof(need#>'{permission_ceiling,forbidden_interfaces}') <> 'array'
     OR jsonb_typeof(need#>'{permission_ceiling,maximum_scope_digests}') <> 'array'
     OR jsonb_array_length(need#>'{permission_ceiling,allowed_interfaces}') > 32
     OR jsonb_array_length(need#>'{permission_ceiling,forbidden_interfaces}') > 32
     OR jsonb_array_length(need#>'{permission_ceiling,maximum_scope_digests}') > 32 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec permission ceiling is invalid';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements_text(need#>'{permission_ceiling,allowed_interfaces}') a(value)
    JOIN jsonb_array_elements_text(need#>'{permission_ceiling,forbidden_interfaces}') f(value) USING (value)
  ) OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(need#>'{permission_ceiling,allowed_interfaces}'))
    OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(need#>'{permission_ceiling,forbidden_interfaces}'))
    OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(need#>'{permission_ceiling,maximum_scope_digests}'))
    OR EXISTS (
      SELECT 1 FROM jsonb_array_elements_text(need#>'{permission_ceiling,allowed_interfaces}') i(value)
      WHERE value !~ '^vibapp:experimental-v0/(clock|scheduler|notification|kv|log|host-info|settings|system-metrics|http)@0[.]0[.]1$'
    ) OR EXISTS (
      SELECT 1 FROM jsonb_array_elements_text(need#>'{permission_ceiling,forbidden_interfaces}') i(value)
      WHERE value !~ '^vibapp:experimental-v0/(clock|scheduler|notification|kv|log|host-info|settings|system-metrics|http)@0[.]0[.]1$'
    ) OR EXISTS (
    SELECT 1 FROM jsonb_array_elements_text(need#>'{permission_ceiling,maximum_scope_digests}') s(value)
    WHERE value !~ '^[0-9a-f]{64}$'
  ) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'NeedSpec permission ceiling conflicts or contains an invalid digest';
  END IF;

  IF NOT registry.jsonb_exact_keys(constraints_json, ARRAY[
    'acceptable_app_kinds','required_interfaces','minimum_publisher_verification',
    'acceptable_publication_states','require_verified_artifact','require_not_revoked',
    'embedding_consent'
  ]) OR constraints_json->>'minimum_publisher_verification' <> 'verified'
     OR constraints_json->'acceptable_publication_states' <> '["published"]'::jsonb
     OR constraints_json->'require_verified_artifact' <> 'true'::jsonb
     OR constraints_json->'require_not_revoked' <> 'true'::jsonb
     OR constraints_json->>'embedding_consent' NOT IN ('granted','not-granted')
     OR jsonb_typeof(constraints_json->'acceptable_app_kinds') <> 'array'
     OR jsonb_array_length(constraints_json->'acceptable_app_kinds') NOT BETWEEN 1 AND 3
     OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(constraints_json->'acceptable_app_kinds'))
     OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(constraints_json->'acceptable_app_kinds') k(value) WHERE value NOT IN ('ui','service','hybrid'))
     OR jsonb_typeof(constraints_json->'required_interfaces') <> 'array'
     OR jsonb_array_length(constraints_json->'required_interfaces') > 32
     OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(constraints_json->'required_interfaces'))
     OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(constraints_json->'required_interfaces') i(value) WHERE value !~ '^vibapp:experimental-v0/(clock|scheduler|notification|kv|log|host-info|settings|system-metrics|http)@0[.]0[.]1$') THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'search constraints are invalid or relax mandatory safety filters';
  END IF;
  IF jsonb_typeof(request_jsonb->'limit') <> 'number'
     OR (request_jsonb->>'limit')::numeric <> trunc((request_jsonb->>'limit')::numeric)
     OR (request_jsonb->>'limit')::integer NOT BETWEEN 1 AND 20 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'search limit must be an integer in 1..20';
  END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.hard_filter_reasons_v1(
  target_release_id bigint,
  request_jsonb jsonb,
  embedding_model_id text,
  embedding_model_version text
)
RETURNS text[]
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $function$
DECLARE
  reasons text[] := '{}';
  need jsonb := request_jsonb->'need';
  constraints_json jsonb := request_jsonb->'constraints';
  release_row registry.app_version%ROWTYPE;
  publisher_row registry.publisher%ROWTYPE;
  negative_item jsonb;
BEGIN
  SELECT * INTO STRICT release_row FROM registry.app_version WHERE release_id = target_release_id;
  SELECT p.* INTO STRICT publisher_row
    FROM registry.publisher p JOIN registry.app a ON a.publisher_id = p.publisher_id
   WHERE a.app_id = release_row.app_id;

  IF release_row.package_format <> 'vibapp.package.experimental-v0' THEN reasons := array_append(reasons, 'package-format'); END IF;
  IF release_row.component_contract <> 'vibapp:experimental-v0@0.0.1' THEN reasons := array_append(reasons, 'contract'); END IF;
  IF release_row.wasi_version <> '0.2' THEN reasons := array_append(reasons, 'wasi'); END IF;
  IF release_row.wit_world NOT IN ('ui-only-reference','service-only-reference','hybrid-reference','web-preview-reference') THEN reasons := array_append(reasons, 'wit-world'); END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(need->'platforms') requested
     WHERE NOT EXISTS (
       SELECT 1 FROM registry.release_platform available
        WHERE available.release_id = target_release_id
          AND available.os::text = requested->>'os'
          AND available.arch::text = requested->>'arch'
          AND available.profile::text = requested->>'profile'
     )
  ) THEN reasons := array_append(reasons, 'platform'); END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements_text(need->'profiles') requested(profile)
     WHERE NOT EXISTS (SELECT 1 FROM registry.release_profile available WHERE available.release_id = target_release_id AND available.profile::text = requested.profile)
  ) THEN reasons := array_append(reasons, 'profile'); END IF;
  IF NOT (request_jsonb#>'{constraints,acceptable_app_kinds}' @> jsonb_build_array(release_row.kind::text)) THEN reasons := array_append(reasons, 'app-kind'); END IF;

  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements_text(request_jsonb#>'{constraints,required_interfaces}') requested(interface_name)
     WHERE NOT EXISTS (SELECT 1 FROM registry.release_capability capability WHERE capability.release_id = target_release_id AND capability.interface_name = requested.interface_name)
  ) THEN reasons := array_append(reasons, 'required-capability'); END IF;
  IF EXISTS (
    SELECT 1 FROM registry.release_capability capability
     WHERE capability.release_id = target_release_id
       AND (
         NOT (need#>'{permission_ceiling,allowed_interfaces}' @> jsonb_build_array(capability.interface_name))
         OR need#>'{permission_ceiling,forbidden_interfaces}' @> jsonb_build_array(capability.interface_name)
       )
  ) OR (
    jsonb_array_length(need#>'{permission_ceiling,maximum_scope_digests}') > 0
    AND EXISTS (
      SELECT 1 FROM registry.release_permission permission
       WHERE permission.release_id = target_release_id
         AND NOT (need#>'{permission_ceiling,maximum_scope_digests}' @> jsonb_build_array(permission.scope_digest_sha256::text))
    )
  ) THEN reasons := array_append(reasons, 'permission-ceiling'); END IF;

  FOR negative_item IN SELECT value FROM jsonb_array_elements(need->'negative_constraints') LOOP
    IF (negative_item->>'kind' = 'forbidden-capability' AND EXISTS (
          SELECT 1 FROM registry.release_capability c WHERE c.release_id = target_release_id AND lower(c.interface_name) = lower(negative_item->>'value')
        ))
       OR (negative_item->>'kind' = 'forbidden-publisher' AND lower(publisher_row.publisher_id::text) = lower(negative_item->>'value'))
       OR (negative_item->>'kind' = 'forbidden-package' AND lower(negative_item->>'value') IN (lower(release_row.app_id::text), release_row.package_digest_sha256::text))
       OR (negative_item->>'kind' = 'profile' AND EXISTS (SELECT 1 FROM registry.release_profile rp WHERE rp.release_id = target_release_id AND lower(rp.profile::text) = lower(negative_item->>'value')))
       OR (negative_item->>'kind' = 'platform' AND EXISTS (SELECT 1 FROM registry.release_platform rp WHERE rp.release_id = target_release_id AND lower(concat_ws('/',rp.os::text,rp.arch::text,rp.profile::text)) LIKE '%' || lower(negative_item->>'value') || '%'))
       OR (negative_item->>'kind' = 'privacy' AND lower(negative_item->>'value') IN ('network','remote-processing','http') AND EXISTS (SELECT 1 FROM registry.release_capability c WHERE c.release_id = target_release_id AND c.interface_name = 'vibapp:experimental-v0/http@0.0.1'))
       OR (negative_item->>'kind' = 'other' AND lower(negative_item->>'value') = ANY(SELECT lower(value) FROM unnest(release_row.tags || release_row.capability_labels) value)) THEN
      IF NOT ('negative-constraint' = ANY(reasons)) THEN reasons := array_append(reasons, 'negative-constraint'); END IF;
    END IF;
  END LOOP;

  IF publisher_row.verification_state <> 'verified' OR publisher_row.trust_tier NOT IN ('verified','first-party') THEN reasons := array_append(reasons, 'publisher-trust'); END IF;
  IF NOT EXISTS (SELECT 1 FROM registry.release_verification v WHERE v.release_id = target_release_id AND v.status = 'verified') THEN reasons := array_append(reasons, 'verification'); END IF;
  IF EXISTS (SELECT 1 FROM registry.revocation_event r WHERE r.release_id = target_release_id AND r.effective_at <= statement_timestamp()) THEN reasons := array_append(reasons, 'revocation'); END IF;
  IF NOT EXISTS (
    SELECT 1 FROM registry.publication p
     WHERE p.release_id = target_release_id AND p.state = 'published' AND p.visibility = 'public'
  ) OR NOT EXISTS (
    SELECT 1 FROM registry.authority_grant a
     WHERE a.release_id = target_release_id AND a.authority_kind = 'public-list' AND a.revoked_at IS NULL
       AND (a.expires_at IS NULL OR a.expires_at > statement_timestamp())
  ) OR NOT EXISTS (
    SELECT 1 FROM registry.authority_grant a
     WHERE a.release_id = target_release_id AND a.authority_kind = 'public-install' AND a.revoked_at IS NULL
       AND (a.expires_at IS NULL OR a.expires_at > statement_timestamp())
  ) THEN reasons := array_append(reasons, 'publication-state'); END IF;
  IF need->>'privacy_requirement' = 'local-private' AND EXISTS (
    SELECT 1 FROM registry.release_capability c WHERE c.release_id = target_release_id AND c.interface_name = 'vibapp:experimental-v0/http@0.0.1'
  ) THEN reasons := array_append(reasons, 'privacy'); END IF;
  IF NOT EXISTS (
    SELECT 1 FROM registry.release_embedding e
    JOIN registry.embedding_model m USING (model_id, model_version)
     WHERE e.release_id = target_release_id
       AND e.model_id = embedding_model_id AND e.model_version = embedding_model_version
       AND m.active AND m.dimensions = 384
  ) THEN reasons := array_append(reasons, 'embedding-model'); END IF;

  RETURN reasons;
END;
$function$;

CREATE OR REPLACE FUNCTION registry.safe_rejections_v1(
  request_jsonb jsonb,
  embedding_model_id text,
  embedding_model_version text
)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $function$
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'app_id', v.app_id,
    'display_name', v.display_name,
    'package_digest_sha256', v.package_digest_sha256,
    'rejected_reasons', registry.hard_filter_reasons_v1(v.release_id, request_jsonb, embedding_model_id, embedding_model_version),
    'checked_facts', jsonb_build_array(
      '/contract/package_format','/contract/component_contract','/contract/wasi','/contract/wit_world',
      '/compatibility/platforms','/compatibility/profiles','/permissions',
      '/app/publisher/verification_state','/verification/status','/verification/revocation','/publication/state'
    )
  ) ORDER BY v.app_id, v.version, v.package_digest_sha256), '[]'::jsonb)
  FROM registry.app_version v
  JOIN registry.publication p ON p.release_id = v.release_id
  WHERE cardinality(registry.hard_filter_reasons_v1(v.release_id, request_jsonb, embedding_model_id, embedding_model_version)) > 0
    AND p.visibility = 'public';
$function$;

CREATE OR REPLACE FUNCTION registry.search_preflight_v1(
  request_jsonb jsonb,
  embedding_model_id text,
  embedding_model_version text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE
  eligible_count integer;
  rejected jsonb;
  request_digest registry.sha256_hex;
BEGIN
  PERFORM registry.validate_search_request_v1(request_jsonb);
  IF embedding_model_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
     OR embedding_model_version !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'embedding model identity is invalid';
  END IF;
  SELECT count(*) INTO eligible_count FROM registry.app_version v
   WHERE cardinality(registry.hard_filter_reasons_v1(v.release_id, request_jsonb, embedding_model_id, embedding_model_version)) = 0;
  rejected := registry.safe_rejections_v1(request_jsonb, embedding_model_id, embedding_model_version);
  request_digest := registry.jsonb_sha256(request_jsonb || jsonb_build_object('embedding_model_id', embedding_model_id, 'embedding_model_version', embedding_model_version));

  IF eligible_count = 0 THEN
    RETURN jsonb_build_object(
      'needs_embedding', false,
      'response', jsonb_build_object(
        'schema_version','vibapp.registry-route.experimental.2026-08-24.1',
        'status','experimental-product-hold','route','refinement',
        'request_id','registry.' || substr(request_digest::text,1,24),
        'need_id',request_jsonb#>>'{need,need_id}','recommendations','[]'::jsonb,
        'refinement',jsonb_build_object('reason_code','no-hard-filter-match','message','No candidate passed every compatibility, permission, trust, revocation and publication constraint.'),
        'retrieval',jsonb_build_object(
          'mode','hard-filter-then-fulltext-plus-pgvector',
          'embedding',jsonb_build_object('status','not-needed','provider','not-called','model',embedding_model_id,'model_version',embedding_model_version,'dimensions',384),
          'eligible_candidates',0,'rejected_candidates',jsonb_array_length(rejected),
          'pagination',jsonb_build_object('next_cursor',NULL,'stable_order','score-desc-app-version-digest-asc')
        ),
        'rejected',rejected,
        'codeagent_handoff',jsonb_build_object('created',false,'permitted',false,'reason','Registry only recommends or requests refinement.')
      )
    );
  END IF;
  IF request_jsonb#>>'{constraints,embedding_consent}' <> 'granted' THEN
    RETURN jsonb_build_object(
      'needs_embedding', false,
      'response', jsonb_build_object(
        'schema_version','vibapp.registry-route.experimental.2026-08-24.1',
        'status','experimental-product-hold','route','refinement',
        'request_id','registry.' || substr(request_digest::text,1,24),
        'need_id',request_jsonb#>>'{need,need_id}','recommendations','[]'::jsonb,
        'refinement',jsonb_build_object('reason_code','embedding-consent-required','message','Semantic retrieval requires explicit embedding consent; no candidate was recommended.'),
        'retrieval',jsonb_build_object(
          'mode','hard-filter-then-fulltext-plus-pgvector',
          'embedding',jsonb_build_object('status','not-called','provider','not-authorized','model',embedding_model_id,'model_version',embedding_model_version,'dimensions',384),
          'eligible_candidates',eligible_count,'rejected_candidates',jsonb_array_length(rejected),
          'pagination',jsonb_build_object('next_cursor',NULL,'stable_order','score-desc-app-version-digest-asc')
        ),
        'rejected',rejected,
        'codeagent_handoff',jsonb_build_object('created',false,'permitted',false,'reason','Registry only recommends or requests refinement.')
      )
    );
  END IF;
  RETURN jsonb_build_object('needs_embedding', true, 'response', NULL, 'eligible_candidates', eligible_count, 'request_digest_sha256', request_digest);
END;
$function$;

CREATE OR REPLACE FUNCTION registry.search_v1(
  request_jsonb jsonb,
  embedding_model_id text,
  embedding_model_version text,
  query_embedding public.vector(384),
  requirement_embeddings jsonb,
  cursor_token text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, registry
AS $function$
DECLARE
  preflight jsonb;
  request_digest registry.sha256_hex;
  cursor_json jsonb;
  rejected jsonb;
  response jsonb;
  required_ids text[];
  embedding_ids text[];
BEGIN
  preflight := registry.search_preflight_v1(request_jsonb, embedding_model_id, embedding_model_version);
  IF NOT (preflight->>'needs_embedding')::boolean THEN RETURN preflight->'response'; END IF;
  IF public.vector_dims(query_embedding) <> 384 OR public.vector_norm(query_embedding) <= 0
     OR jsonb_typeof(requirement_embeddings) <> 'object' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'query and requirement embeddings must be 384-dimensional';
  END IF;
  required_ids := ARRAY(SELECT value->>'requirement_id' FROM jsonb_array_elements(request_jsonb#>'{need,requirements}') ORDER BY value->>'requirement_id');
  embedding_ids := ARRAY(SELECT key FROM jsonb_object_keys(requirement_embeddings) key ORDER BY key);
  IF required_ids <> embedding_ids THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'requirement embeddings must exactly match requirement IDs';
  END IF;
  PERFORM registry.jsonb_vector_384(value) FROM jsonb_each(requirement_embeddings);

  request_digest := (preflight->>'request_digest_sha256')::registry.sha256_hex;
  IF cursor_token IS NOT NULL THEN
    cursor_json := registry.cursor_decode(cursor_token);
    IF NOT registry.jsonb_exact_keys(cursor_json, ARRAY['request_digest_sha256','model_id','model_version','score','app_id','version','package_digest_sha256'])
       OR cursor_json->>'request_digest_sha256' <> request_digest::text
       OR cursor_json->>'model_id' <> embedding_model_id
       OR cursor_json->>'model_version' <> embedding_model_version THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'registry cursor is stale or bound to another request/model';
    END IF;
  END IF;
  rejected := registry.safe_rejections_v1(request_jsonb, embedding_model_id, embedding_model_version);

  WITH
  eligible AS MATERIALIZED (
    SELECT v.release_id
      FROM registry.app_version v
     WHERE cardinality(registry.hard_filter_reasons_v1(v.release_id, request_jsonb, embedding_model_id, embedding_model_version)) = 0
  ),
  requirement_rows AS MATERIALIZED (
    SELECT
      r.value->>'requirement_id' AS requirement_id,
      r.value->>'priority' AS priority,
      concat_ws(E'\n', r.value->>'text', ARRAY(SELECT x FROM jsonb_array_elements_text(r.value->'acceptance_examples') x)::text) AS requirement_text,
      registry.jsonb_vector_384(requirement_embeddings->(r.value->>'requirement_id')) AS requirement_embedding
    FROM jsonb_array_elements(request_jsonb#>'{need,requirements}') WITH ORDINALITY r(value, ordinal)
  ),
  base_scored AS MATERIALIZED (
    SELECT
      v.*, p.publisher_id, p.verification_state, p.trust_tier,
      e.embedding,
      LEAST(1.0, GREATEST(0.0,
        pg_catalog.ts_rank_cd(v.search_tsv, registry.or_tsquery(
          concat_ws(E'\n', request_jsonb#>>'{need,goal}', ARRAY(SELECT rr->>'text' FROM jsonb_array_elements(request_jsonb#>'{need,requirements}') rr)::text)
        ), 32) * 4.0
      ))::double precision AS keyword_score,
      GREATEST(0.0, LEAST(1.0, 1.0 - (e.embedding <=> query_embedding)))::double precision AS vector_score,
      (SELECT count(*) FROM registry.release_permission rp WHERE rp.release_id = v.release_id) AS permission_count
    FROM eligible h
    JOIN registry.app_version v ON v.release_id = h.release_id
    JOIN registry.app a ON a.app_id = v.app_id
    JOIN registry.publisher p ON p.publisher_id = a.publisher_id
    JOIN registry.release_embedding e ON e.release_id = v.release_id
      AND e.model_id = embedding_model_id AND e.model_version = embedding_model_version
  ),
  with_coverage AS MATERIALIZED (
    SELECT b.*,
      coverage.rows AS coverage_rows,
      coverage.must_have_complete,
      coverage.coverage_score
    FROM base_scored b
    CROSS JOIN LATERAL (
      SELECT
        COALESCE(jsonb_agg(jsonb_build_object(
          'requirement_id', q.requirement_id,
          'state', CASE WHEN q.covered THEN 'covered' ELSE 'uncovered' END,
          'lexical_score', round(q.lexical_score::numeric, 6),
          'semantic_score', round(q.semantic_score::numeric, 6),
          'evidence_fields', jsonb_build_array('/app/summary','/search_metadata/tags','/search_metadata/capability_labels')
        ) ORDER BY q.requirement_id), '[]'::jsonb) AS rows,
        bool_and(q.priority <> 'must-have' OR q.covered) AS must_have_complete,
        avg(CASE WHEN q.covered THEN 1.0 ELSE 0.0 END)::double precision AS coverage_score
      FROM (
        SELECT rr.*,
          LEAST(1.0, GREATEST(0.0, pg_catalog.ts_rank_cd(b.search_tsv, registry.or_tsquery(rr.requirement_text), 32) * 4.0))::double precision AS lexical_score,
          GREATEST(0.0, LEAST(1.0, 1.0 - (b.embedding <=> rr.requirement_embedding)))::double precision AS semantic_score,
          (
            LEAST(1.0, GREATEST(0.0, pg_catalog.ts_rank_cd(b.search_tsv, registry.or_tsquery(rr.requirement_text), 32) * 4.0)) >= 0.30
            OR GREATEST(0.0, LEAST(1.0, 1.0 - (b.embedding <=> rr.requirement_embedding))) >= 0.55
          ) AS covered
        FROM requirement_rows rr
      ) q
    ) coverage
  ),
  ranked AS MATERIALIZED (
    SELECT c.*,
      round((
        0.45 * c.vector_score + 0.25 * c.keyword_score + 0.20 * c.coverage_score
        + 0.05 * CASE c.trust_tier WHEN 'first-party' THEN 1.0 WHEN 'verified' THEN 0.9 ELSE 0.0 END
        + 0.05 * GREATEST(0.0, 1.0 - LEAST(c.permission_count, 32)::double precision / 32.0)
      )::numeric, 6) AS final_score
    FROM with_coverage c
  ),
  acceptable AS MATERIALIZED (
    SELECT * FROM ranked r
     WHERE r.must_have_complete AND r.final_score >= 0.50
       AND (
         cursor_json IS NULL
         OR r.final_score < (cursor_json->>'score')::numeric
         OR (r.final_score = (cursor_json->>'score')::numeric AND (r.app_id::text, r.version, r.package_digest_sha256::text) > (cursor_json->>'app_id', cursor_json->>'version', cursor_json->>'package_digest_sha256'))
       )
     ORDER BY r.final_score DESC, r.app_id, r.version, r.package_digest_sha256
     LIMIT ((request_jsonb->>'limit')::integer + 1)
  ),
  page AS MATERIALIZED (
    SELECT * FROM acceptable ORDER BY final_score DESC, app_id, version, package_digest_sha256 LIMIT (request_jsonb->>'limit')::integer
  ),
  considered AS MATERIALIZED (
    SELECT * FROM ranked ORDER BY final_score DESC, app_id, version, package_digest_sha256 LIMIT (request_jsonb->>'limit')::integer
  ),
  aggregate_result AS (
    SELECT
      (SELECT count(*) FROM eligible) AS eligible_count,
      (SELECT count(*) FROM acceptable) > (request_jsonb->>'limit')::integer AS has_more,
      (SELECT count(*) FROM page) AS page_count,
      (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'app', jsonb_build_object(
          'id', p.app_id, 'version', p.version, 'kind', p.kind,
          'display_name', p.display_name, 'summary', p.summary,
          'publisher', jsonb_build_object('publisher_id',p.publisher_id,'verification_state',p.verification_state,'trust_tier',p.trust_tier)
        ),
        'package', jsonb_build_object('app_id',p.app_id,'version',p.version,'package_digest_sha256',p.package_digest_sha256),
        'decision','acceptable',
        'scores',jsonb_build_object('keyword',round(p.keyword_score::numeric,6),'embedding_cosine',round(p.vector_score::numeric,6),'hybrid',p.final_score),
        'coverage',p.coverage_rows,
        'required_permissions',(SELECT COALESCE(jsonb_agg(jsonb_build_object(
          'interface',rp.interface_name,'necessity',rc.necessity,'grant',rp.grant_mode,
          'scope_digest_sha256',rp.scope_digest_sha256,'summary',rp.summary
        ) ORDER BY rp.interface_name),'[]'::jsonb) FROM registry.release_permission rp JOIN registry.release_capability rc USING(release_id,interface_name) WHERE rp.release_id=p.release_id),
        'compatibility',jsonb_build_object(
          'profiles',(SELECT jsonb_agg(profile ORDER BY profile) FROM registry.release_profile WHERE release_id=p.release_id),
          'platforms',(SELECT jsonb_agg(jsonb_build_object('os',os,'arch',arch,'profile',profile) ORDER BY os,arch,profile) FROM registry.release_platform WHERE release_id=p.release_id)
        ),
        'source',jsonb_build_object('visibility',p.source_visibility,'license_spdx',p.license_spdx,'source_digest_sha256',p.source_digest_sha256),
        'verification',jsonb_build_object('status','verified','revocation','not-revoked','manifest_digest_sha256',p.manifest_digest_sha256,'sbom_digest_sha256',p.sbom_digest_sha256,'provenance_digest_sha256',p.provenance_digest_sha256),
        'explanation',jsonb_build_object(
          'summary','Candidate passed every hard filter; ranking used PostgreSQL full-text coverage plus the exact model-version pgvector cosine score.',
          'evidence_fields',jsonb_build_array('/app/display_name','/app/summary','/search_metadata/tags','/search_metadata/capability_labels','/compatibility/platforms','/permissions','/verification/status','/publication/state'),
          'embedding_input_digest_sha256',(SELECT content_digest_sha256 FROM registry.release_embedding WHERE release_id=p.release_id AND model_id=embedding_model_id AND model_version=embedding_model_version)
        )
      ) ORDER BY p.final_score DESC,p.app_id,p.version,p.package_digest_sha256),'[]'::jsonb) FROM page p) AS recommendations,
      (SELECT jsonb_build_object(
        'request_digest_sha256',request_digest,'model_id',embedding_model_id,'model_version',embedding_model_version,
        'score',p.final_score,'app_id',p.app_id,'version',p.version,'package_digest_sha256',p.package_digest_sha256
      ) FROM page p ORDER BY p.final_score,p.app_id DESC,p.version DESC,p.package_digest_sha256 DESC LIMIT 1) AS last_cursor,
      (SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'app_id',c.app_id,'version',c.version,'package_digest_sha256',c.package_digest_sha256,
        'decision','insufficient-match','scores',jsonb_build_object('keyword',round(c.keyword_score::numeric,6),'embedding_cosine',round(c.vector_score::numeric,6),'hybrid',c.final_score),
        'coverage',c.coverage_rows
      ) ORDER BY c.final_score DESC,c.app_id,c.version,c.package_digest_sha256),'[]'::jsonb) FROM considered c) AS considered_rows
  )
  SELECT CASE WHEN a.page_count > 0 THEN
    jsonb_build_object(
      'schema_version','vibapp.registry-route.experimental.2026-08-24.1','status','experimental-product-hold','route','recommendation',
      'request_id','registry.' || substr(request_digest::text,1,24),'need_id',request_jsonb#>>'{need,need_id}',
      'recommendations',a.recommendations,'refinement',NULL,
      'retrieval',jsonb_build_object(
        'mode','hard-filter-then-fulltext-plus-pgvector',
        'embedding',jsonb_build_object('status','live','provider','postgres-pgvector','model',embedding_model_id,'model_version',embedding_model_version,'dimensions',384),
        'eligible_candidates',a.eligible_count,'rejected_candidates',jsonb_array_length(rejected),
        'thresholds',jsonb_build_object('final',0.50,'requirement_semantic',0.55,'requirement_lexical',0.30),
        'pagination',jsonb_build_object('next_cursor',CASE WHEN a.has_more THEN registry.cursor_encode(a.last_cursor) ELSE NULL END,'stable_order','score-desc-app-version-digest-asc')
      ),
      'rejected',rejected,
      'codeagent_handoff',jsonb_build_object('created',false,'permitted',false,'reason','An acceptable existing application must be offered before development.')
    )
  ELSE
    jsonb_build_object(
      'schema_version','vibapp.registry-route.experimental.2026-08-24.1','status','experimental-product-hold','route','refinement',
      'request_id','registry.' || substr(request_digest::text,1,24),'need_id',request_jsonb#>>'{need,need_id}',
      'recommendations','[]'::jsonb,
      'refinement',jsonb_build_object('reason_code','no-acceptable-semantic-match','message','Candidates passed hard filters but none met must-have coverage and the stable rerank threshold.'),
      'retrieval',jsonb_build_object(
        'mode','hard-filter-then-fulltext-plus-pgvector',
        'embedding',jsonb_build_object('status','live','provider','postgres-pgvector','model',embedding_model_id,'model_version',embedding_model_version,'dimensions',384),
        'eligible_candidates',a.eligible_count,'rejected_candidates',jsonb_array_length(rejected),
        'thresholds',jsonb_build_object('final',0.50,'requirement_semantic',0.55,'requirement_lexical',0.30),
        'pagination',jsonb_build_object('next_cursor',NULL,'stable_order','score-desc-app-version-digest-asc')
      ),
      'rejected',rejected,'considered',a.considered_rows,
      'codeagent_handoff',jsonb_build_object('created',false,'permitted',false,'reason','Registry only recommends or requests refinement.')
    ) END INTO response
  FROM aggregate_result a;
  RETURN response;
END;
$function$;

CREATE OR REPLACE VIEW registry.public_release_facts_v1 AS
SELECT
  v.record_id, v.app_id, v.version, v.kind, v.display_name, v.summary,
  v.package_digest_sha256, v.manifest_digest_sha256, v.package_format,
  v.component_contract, v.wasi_version, v.wit_world, v.source_visibility,
  v.license_spdx, v.source_digest_sha256, v.sbom_digest_sha256,
  v.provenance_digest_sha256, v.tags, v.capability_labels,
  p.publisher_id, p.verification_state AS publisher_verification_state,
  p.trust_tier, verification.status AS artifact_verification_status,
  publication.state AS publication_state, publication.visibility AS registry_visibility,
  EXISTS (SELECT 1 FROM registry.revocation_event r WHERE r.release_id=v.release_id AND r.effective_at <= statement_timestamp()) AS revoked
FROM registry.app_version v
JOIN registry.app a ON a.app_id=v.app_id
JOIN registry.publisher p ON p.publisher_id=a.publisher_id
LEFT JOIN registry.release_verification verification ON verification.release_id=v.release_id
JOIN registry.publication publication ON publication.release_id=v.release_id
WHERE publication.visibility='public';

REVOKE ALL ON ALL TABLES IN SCHEMA registry FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA registry FROM PUBLIC;
REVOKE USAGE ON SCHEMA registry FROM PUBLIC;

COMMIT;
