\set ON_ERROR_STOP on

DO $test$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector') THEN
    RAISE EXCEPTION 'pgvector extension missing';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='registry' AND p.proname='search_v1') THEN
    RAISE EXCEPTION 'search_v1 missing';
  END IF;
END;
$test$;

INSERT INTO registry.embedding_model(
  model_id, model_version, dimensions, provider_name,
  model_artifact_digest_sha256, active
) VALUES (
  'text-embedding-ada-002','localai-2026-08-24',384,'integration-fixture',
  repeat('1',64)::registry.sha256_hex,true
);

CREATE TEMP TABLE test_values AS
SELECT
  concat_ws(E'\n','Focus Board','Plan focused work sessions with a local task list and progress view.','{focus,productivity,task-list,专注,任务}'::text,'{foreground-ui,local-state,no-network}'::text) AS search_document,
  to_jsonb(ARRAY[1.0::double precision] || array_fill(0.0::double precision,ARRAY[383])) AS fixture_vector;

DO $test$
DECLARE
  record jsonb;
  first_result jsonb;
  replay_result jsonb;
  bad_record jsonb;
  second_record jsonb;
  content_digest text;
  package_digest text := repeat('2',64);
BEGIN
  SELECT encode(digest(convert_to(search_document,'UTF8'),'sha256'),'hex') INTO content_digest FROM test_values;
  SELECT jsonb_build_object(
    'schema_version','vibapp.registry-release-upsert.experimental.2026-08-24.1',
    'record_id','registry.integration.focus.1',
    'app',jsonb_build_object(
      'id','ai.vibapp.integration.focus','version','1.0.0','kind','ui',
      'display_name','Focus Board','summary','Plan focused work sessions with a local task list and progress view.',
      'package_digest_sha256',package_digest
    ),
    'publisher',jsonb_build_object('publisher_id','publisher.integration','display_name','Integration Publisher'),
    'contract',jsonb_build_object(
      'package_format','vibapp.package.experimental-v0','component_contract','vibapp:experimental-v0@0.0.1',
      'wasi','0.2','wit_world','ui-only-reference','manifest_digest_sha256',repeat('3',64)
    ),
    'compatibility',jsonb_build_object(
      'profiles',jsonb_build_array('desktop'),
      'platforms',jsonb_build_array(jsonb_build_object('os','macos','arch','aarch64','profile','desktop'))
    ),
    'capabilities',jsonb_build_array(
      jsonb_build_object('interface','vibapp:experimental-v0/clock@0.0.1','necessity','required','grant','automatic','scope_digest_sha256',repeat('a',64),'summary','Clock access.'),
      jsonb_build_object('interface','vibapp:experimental-v0/host-info@0.0.1','necessity','required','grant','automatic','scope_digest_sha256',repeat('b',64),'summary','Host profile access.'),
      jsonb_build_object('interface','vibapp:experimental-v0/kv@0.0.1','necessity','required','grant','user','scope_digest_sha256',repeat('c',64),'summary','Local state.'),
      jsonb_build_object('interface','vibapp:experimental-v0/log@0.0.1','necessity','required','grant','automatic','scope_digest_sha256',repeat('d',64),'summary','Bounded logs.'),
      jsonb_build_object('interface','vibapp:experimental-v0/settings@0.0.1','necessity','required','grant','user','scope_digest_sha256',repeat('e',64),'summary','Typed settings.')
    ),
    'source',jsonb_build_object(
      'visibility','public','license_spdx','Apache-2.0','source_digest_sha256',repeat('4',64),
      'sbom_digest_sha256',repeat('5',64),'provenance_digest_sha256',repeat('6',64)
    ),
    'search_metadata',jsonb_build_object(
      'tags',jsonb_build_array('focus','productivity','task-list','专注','任务'),
      'capability_labels',jsonb_build_array('foreground-ui','local-state','no-network')
    ),
    'embedding',jsonb_build_object(
      'model_id','text-embedding-ada-002','model_version','localai-2026-08-24','dimensions',384,
      'content_digest_sha256',content_digest,'vector',(SELECT fixture_vector FROM test_values)
    )
  ) INTO record;

  first_result := registry.upsert_release_v1(record,'upsert.integration.focus.1','ingestor.integration');
  IF first_result->>'deduplicated' <> 'false' THEN RAISE EXCEPTION 'first upsert unexpectedly deduplicated'; END IF;
  replay_result := registry.upsert_release_v1(record,'upsert.integration.focus.1','ingestor.integration');
  IF replay_result->>'deduplicated' <> 'true' THEN RAISE EXCEPTION 'replay was not deduplicated'; END IF;
  IF (SELECT count(*) FROM registry.app_version) <> 1 OR (SELECT count(*) FROM registry.audit_event WHERE operation='release-upsert') <> 1 THEN
    RAISE EXCEPTION 'idempotent upsert created duplicate state';
  END IF;

  second_record := jsonb_set(record,'{record_id}','"registry.integration.focus.2"'::jsonb);
  second_record := jsonb_set(second_record,'{app,id}','"ai.vibapp.integration.focus-two"'::jsonb);
  second_record := jsonb_set(second_record,'{app,package_digest_sha256}',to_jsonb(repeat('f',64)));
  PERFORM registry.upsert_release_v1(second_record,'upsert.integration.focus.2','ingestor.integration');

  bad_record := jsonb_set(record,'{app,summary}','"changed immutable summary"'::jsonb);
  BEGIN
    PERFORM registry.upsert_release_v1(bad_record,'upsert.integration.focus.1','ingestor.integration');
    RAISE EXCEPTION 'different payload reused idempotency key without rejection';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  bad_record := jsonb_set(record,'{capabilities}','[]'::jsonb);
  BEGIN
    PERFORM registry.upsert_release_v1(bad_record,'upsert.integration.dirty.1','ingestor.integration');
    RAISE EXCEPTION 'dirty WIT/capability mismatch was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  bad_record := jsonb_set(record,'{embedding,vector}',to_jsonb(array_fill(0.0::double precision,ARRAY[384])));
  BEGIN
    PERFORM registry.upsert_release_v1(bad_record,'upsert.integration.zero-vector.1','ingestor.integration');
    RAISE EXCEPTION 'zero-norm embedding was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END;
$test$;

SELECT registry.set_publisher_trust_v1(
  'publisher.integration','verified','verified',repeat('7',64)::registry.sha256_hex
);
SELECT registry.record_verification_v1(
  repeat('2',64)::registry.sha256_hex,'evidence.integration.1','independent-verification',
  'verifier.integration',repeat('8',64)::registry.sha256_hex,'verified',statement_timestamp(),'{}'::jsonb
);
SELECT registry.grant_authority_v1(
  repeat('2',64)::registry.sha256_hex,'authority.remote-process.integration','remote-process',
  'publisher.integration',repeat('0',64)::registry.sha256_hex,statement_timestamp(),NULL
);
DO $test$
BEGIN
  BEGIN
    PERFORM registry.publish_release_v1(repeat('2',64)::registry.sha256_hex,repeat('b',64)::registry.sha256_hex);
    RAISE EXCEPTION 'remote-process authority incorrectly implied public publication authority';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END;
$test$;
SELECT registry.grant_authority_v1(
  repeat('2',64)::registry.sha256_hex,'authority.public-list.integration','public-list',
  'publisher.integration',repeat('9',64)::registry.sha256_hex,statement_timestamp(),NULL
);
SELECT registry.grant_authority_v1(
  repeat('2',64)::registry.sha256_hex,'authority.public-install.integration','public-install',
  'publisher.integration',repeat('a',64)::registry.sha256_hex,statement_timestamp(),NULL
);
DO $test$
BEGIN
  BEGIN
    PERFORM registry.publish_release_v1(repeat('2',64)::registry.sha256_hex,repeat('b',64)::registry.sha256_hex);
    RAISE EXCEPTION 'missing archive was accepted';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  IF EXISTS (SELECT 1 FROM registry.publication WHERE state='published') THEN
    RAISE EXCEPTION 'failed archive gate partially published a release';
  END IF;
  BEGIN
    UPDATE registry.publication SET state='published', visibility='public', moderation_evidence_digest_sha256=repeat('b',64);
    RAISE EXCEPTION 'direct table write bypassed archive gate';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
END;
$test$;

-- Synthetic service-issued receipts; these do not claim real GitHub uploads.
DO $test$
DECLARE v registry.app_version%ROWTYPE; receipt jsonb; invalid jsonb;
BEGIN
  FOR v IN SELECT * FROM registry.app_version LOOP
    receipt := jsonb_build_object('organization','vib-app',
      'repository','app-' || encode(digest(convert_to('publisher.integration' || E'\n' || v.app_id::text,'UTF8'),'sha256'),'hex'),
      'repository_id',12345,'commit_sha',repeat('a',40),
      'source_digest_sha256',v.source_digest_sha256,'package_digest_sha256',v.package_digest_sha256);
    -- Exercise the shared public repository and legacy receipt together.
    IF v.package_digest_sha256 = repeat('2',64) THEN
      receipt := jsonb_set(receipt, '{repository}', '"sources"'::jsonb);
    END IF;
    FOR invalid IN SELECT jsonb_set(receipt,'{organization}','"other-org"')
      UNION ALL SELECT jsonb_set(receipt,'{source_digest_sha256}',to_jsonb(repeat('0',64)))
      UNION ALL SELECT jsonb_set(receipt,'{package_digest_sha256}',to_jsonb(repeat('0',64)))
      UNION ALL SELECT jsonb_set(receipt,'{repository}',to_jsonb('app-' || repeat('0',64)))
      UNION ALL SELECT jsonb_set(receipt,'{repository}','"packages"'::jsonb)
      UNION ALL SELECT jsonb_set(receipt,'{commit_sha}','null')
    LOOP
      BEGIN
        PERFORM registry.record_github_source_archive_v1(v.package_digest_sha256,invalid);
        RAISE EXCEPTION 'mismatched archive accepted';
      EXCEPTION WHEN check_violation THEN NULL;
      END;
    END LOOP;
    PERFORM registry.record_github_source_archive_v1(v.package_digest_sha256,receipt);
    PERFORM registry.record_github_source_archive_v1(v.package_digest_sha256,receipt);
    IF NOT EXISTS (SELECT 1 FROM registry.github_source_archive
      WHERE release_id = v.release_id AND repository = receipt->>'repository') THEN
      RAISE EXCEPTION 'archive receipt repository was not preserved';
    END IF;
    BEGIN
      PERFORM registry.record_github_source_archive_v1(v.package_digest_sha256,
        jsonb_set(receipt,'{commit_sha}',to_jsonb(repeat('b',40))));
      RAISE EXCEPTION 'conflicting archive replay accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
  END LOOP;
END;
$test$;
SELECT registry.publish_release_v1(repeat('2',64)::registry.sha256_hex,repeat('b',64)::registry.sha256_hex);
SELECT registry.record_verification_v1(
  repeat('f',64)::registry.sha256_hex,'evidence.integration.2','independent-verification',
  'verifier.integration',repeat('d',64)::registry.sha256_hex,'verified',statement_timestamp(),'{}'::jsonb
);
SELECT registry.grant_authority_v1(
  repeat('f',64)::registry.sha256_hex,'authority.public-list.integration.2','public-list',
  'publisher.integration',repeat('e',64)::registry.sha256_hex,statement_timestamp(),NULL
);
SELECT registry.grant_authority_v1(
  repeat('f',64)::registry.sha256_hex,'authority.public-install.integration.2','public-install',
  'publisher.integration',repeat('f',64)::registry.sha256_hex,statement_timestamp(),NULL
);
SELECT registry.publish_release_v1(repeat('f',64)::registry.sha256_hex,repeat('1',64)::registry.sha256_hex);

-- Publication authority alone cannot fabricate upload attestations. These roles
-- exist only in the disposable integration database.
CREATE ROLE archive_gate_publisher NOLOGIN;
GRANT USAGE ON SCHEMA registry TO archive_gate_publisher;
GRANT EXECUTE ON FUNCTION registry.publish_release_v1(registry.sha256_hex,registry.sha256_hex) TO archive_gate_publisher;
SET ROLE archive_gate_publisher;
DO $test$
BEGIN
  BEGIN
    PERFORM registry.record_github_source_archive_v1(repeat('2',64)::registry.sha256_hex,'{}'::jsonb);
    RAISE EXCEPTION 'publisher fabricated source attestation';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM registry.github_source_archive;
    RAISE EXCEPTION 'publisher deleted source attestations';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  -- Normal publication still works through the security-definer function.
  PERFORM registry.publish_release_v1(repeat('2',64)::registry.sha256_hex,repeat('b',64)::registry.sha256_hex);
END;
$test$;
RESET ROLE;
DO $test$
BEGIN
  BEGIN
    DELETE FROM registry.github_source_archive;
    RAISE EXCEPTION 'published archive reference was deleted';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;
END;
$test$;

CREATE TEMP TABLE search_request AS
SELECT jsonb_build_object(
  'need',jsonb_build_object(
    'schema_version','vibapp.need-spec.product-v0.0.1','document_type','need-spec','need_id','need.integration.focus.1',
    'owner',jsonb_build_object('principal_id','user.integration','principal_kind','user'),
    'goal','Plan focused work sessions locally.',
    'requirements',jsonb_build_array(jsonb_build_object(
      'requirement_id','requirement.focus','text','Show a focus task list and progress view.',
      'priority','must-have','acceptance_examples',jsonb_build_array('A task list is visible.')
    )),
    'negative_constraints','[]'::jsonb,
    'platforms',jsonb_build_array(jsonb_build_object('os','macos','arch','aarch64','profile','desktop')),
    'profiles',jsonb_build_array('desktop'),
    'permission_ceiling',jsonb_build_object(
      'allowed_interfaces',jsonb_build_array(
        'vibapp:experimental-v0/clock@0.0.1','vibapp:experimental-v0/host-info@0.0.1',
        'vibapp:experimental-v0/kv@0.0.1','vibapp:experimental-v0/log@0.0.1','vibapp:experimental-v0/settings@0.0.1'
      ),
      'forbidden_interfaces','[]'::jsonb,'maximum_scope_digests','[]'::jsonb
    ),
    'privacy_requirement','local-private','created_at_utc','2026-08-24T00:00:00Z','revision',1
  ),
  'constraints',jsonb_build_object(
    'acceptable_app_kinds',jsonb_build_array('ui','service','hybrid'),'required_interfaces','[]'::jsonb,
    'minimum_publisher_verification','verified','acceptable_publication_states',jsonb_build_array('published'),
    'require_verified_artifact',true,'require_not_revoked',true,'embedding_consent','granted'
  ),
  'limit',1
) AS value;

DO $test$
DECLARE
  request jsonb;
  preflight jsonb;
  result jsonb;
  denied jsonb;
  unrelated jsonb;
  second_page jsonb;
  cursor_token text;
  same_vector public.vector(384) := (ARRAY[1.0::real] || array_fill(0.0::real,ARRAY[383]))::public.vector(384);
  other_vector public.vector(384) := (ARRAY[0.0::real,1.0::real] || array_fill(0.0::real,ARRAY[382]))::public.vector(384);
  same_requirements jsonb := jsonb_build_object('requirement.focus',to_jsonb(ARRAY[1.0::double precision] || array_fill(0.0::double precision,ARRAY[383])));
  other_requirements jsonb := jsonb_build_object('requirement.focus',to_jsonb(ARRAY[0.0::double precision,1.0::double precision] || array_fill(0.0::double precision,ARRAY[382])));
BEGIN
  SELECT value INTO request FROM search_request;
  preflight := registry.search_preflight_v1(request,'text-embedding-ada-002','localai-2026-08-24');
  IF preflight->>'needs_embedding' <> 'true' THEN RAISE EXCEPTION 'eligible preflight did not request embedding'; END IF;
  result := registry.search_v1(request,'text-embedding-ada-002','localai-2026-08-24',same_vector,same_requirements,NULL);
  IF result->>'route' <> 'recommendation' OR result#>>'{codeagent_handoff,created}' <> 'false'
     OR result#>>'{recommendations,0,app,id}' <> 'ai.vibapp.integration.focus' THEN
    RAISE EXCEPTION 'matching vector did not produce compatible recommendation: %',result;
  END IF;
  cursor_token := result#>>'{retrieval,pagination,next_cursor}';
  IF cursor_token IS NULL THEN RAISE EXCEPTION 'first bounded page omitted its next cursor'; END IF;
  second_page := registry.search_v1(request,'text-embedding-ada-002','localai-2026-08-24',same_vector,same_requirements,cursor_token);
  IF second_page#>>'{recommendations,0,app,id}' <> 'ai.vibapp.integration.focus-two'
     OR second_page#>>'{retrieval,pagination,next_cursor}' IS NOT NULL THEN
    RAISE EXCEPTION 'stable keyset pagination failed: %',second_page;
  END IF;
  unrelated := jsonb_set(request,'{need,goal}','"Design a printable vegetarian recipe book."'::jsonb);
  unrelated := jsonb_set(unrelated,'{need,requirements,0,text}','"Create recipes with ingredient quantities and printable cooking steps."'::jsonb);
  result := registry.search_v1(unrelated,'text-embedding-ada-002','localai-2026-08-24',other_vector,other_requirements,NULL);
  IF result->>'route' <> 'refinement' OR result#>>'{codeagent_handoff,created}' <> 'false' THEN
    RAISE EXCEPTION 'orthogonal semantic vector should refine and must never create CodeAgent handoff: %',result;
  END IF;

  denied := jsonb_set(request,'{need,permission_ceiling,allowed_interfaces}','[]'::jsonb);
  preflight := registry.search_preflight_v1(denied,'text-embedding-ada-002','localai-2026-08-24');
  IF preflight->>'needs_embedding' <> 'false' OR preflight#>>'{response,refinement,reason_code}' <> 'no-hard-filter-match'
     OR preflight#>>'{response,retrieval,embedding,status}' <> 'not-needed' THEN
    RAISE EXCEPTION 'hard-filter preflight did not stop before embedding: %',preflight;
  END IF;

  PERFORM registry.revoke_release_v1(
    repeat('2',64)::registry.sha256_hex,'revocation.integration.1','security.integration',
    'malware','Synthetic integration revocation.',repeat('c',64)::registry.sha256_hex,statement_timestamp()
  );
  result := registry.search_v1(request,'text-embedding-ada-002','localai-2026-08-24',same_vector,same_requirements,NULL);
  IF result#>>'{recommendations,0,app,id}' <> 'ai.vibapp.integration.focus-two'
     OR NOT (result->'rejected' @> jsonb_build_array(jsonb_build_object('app_id','ai.vibapp.integration.focus'))) THEN
    RAISE EXCEPTION 'revoked release was reintroduced or its safe rejection disappeared: %',result;
  END IF;
  IF (SELECT count(*) FROM registry.revocation_event) <> 1 OR (
    SELECT p.state FROM registry.publication p JOIN registry.app_version v USING(release_id)
    WHERE v.package_digest_sha256=repeat('2',64)
  ) <> 'withdrawn' THEN
    RAISE EXCEPTION 'revocation audit/publication state was not preserved';
  END IF;
END;
$test$;

DO $test$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.role_table_grants
     WHERE table_schema='registry' AND grantee='PUBLIC'
  ) THEN RAISE EXCEPTION 'PUBLIC retained registry table privileges'; END IF;
END;
$test$;

SELECT 'registry-store integration PASS' AS result;
