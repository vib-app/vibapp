BEGIN;

CREATE TABLE registry.embedding_model (
  model_id registry.identifier NOT NULL,
  model_version registry.identifier NOT NULL,
  dimensions integer NOT NULL CHECK (dimensions = 384),
  provider_name varchar(200) NOT NULL CHECK (registry.is_clean_text(provider_name, 1, 200)),
  model_artifact_digest_sha256 registry.sha256_hex NOT NULL,
  active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  retired_at timestamptz,
  PRIMARY KEY (model_id, model_version),
  CHECK ((active AND retired_at IS NULL) OR NOT active)
);

CREATE UNIQUE INDEX embedding_model_one_active_version
  ON registry.embedding_model (model_id) WHERE active;

CREATE TABLE registry.publisher (
  publisher_id registry.identifier PRIMARY KEY,
  display_name varchar(200) NOT NULL CHECK (registry.is_clean_text(display_name, 1, 200)),
  verification_state registry.publisher_verification NOT NULL DEFAULT 'unverified',
  trust_tier registry.trust_tier NOT NULL DEFAULT 'unknown',
  authority_evidence_digest_sha256 registry.sha256_hex,
  created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  CHECK (
    (verification_state = 'verified' AND trust_tier IN ('verified', 'first-party') AND authority_evidence_digest_sha256 IS NOT NULL)
    OR verification_state <> 'verified'
  )
);

CREATE TABLE registry.app (
  app_id registry.identifier PRIMARY KEY,
  publisher_id registry.identifier NOT NULL REFERENCES registry.publisher(publisher_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TABLE registry.app_version (
  release_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  record_id registry.identifier NOT NULL UNIQUE,
  app_id registry.identifier NOT NULL REFERENCES registry.app(app_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  version varchar(64) NOT NULL CHECK (registry.is_clean_text(version, 1, 64)),
  kind registry.app_kind NOT NULL,
  display_name varchar(200) NOT NULL CHECK (registry.is_clean_text(display_name, 1, 200)),
  summary varchar(2000) NOT NULL CHECK (registry.is_clean_text(summary, 1, 2000)),
  package_digest_sha256 registry.sha256_hex NOT NULL UNIQUE,
  manifest_digest_sha256 registry.sha256_hex NOT NULL,
  package_format varchar(128) NOT NULL CHECK (package_format = 'vibapp.package.experimental-v0'),
  component_contract varchar(128) NOT NULL CHECK (component_contract = 'vibapp:experimental-v0@0.0.1'),
  wasi_version varchar(16) NOT NULL CHECK (wasi_version = '0.2'),
  wit_world varchar(128) NOT NULL CHECK (wit_world IN ('ui-only-reference', 'service-only-reference', 'hybrid-reference', 'web-preview-reference')),
  source_visibility registry.source_visibility NOT NULL,
  license_spdx varchar(64) NOT NULL CHECK (registry.is_clean_text(license_spdx, 1, 64)),
  source_digest_sha256 registry.sha256_hex NOT NULL,
  sbom_digest_sha256 registry.sha256_hex NOT NULL,
  provenance_digest_sha256 registry.sha256_hex NOT NULL,
  tags text[] NOT NULL DEFAULT '{}',
  capability_labels text[] NOT NULL DEFAULT '{}',
  search_document text NOT NULL,
  search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, search_document)) STORED,
  immutable_record_digest_sha256 registry.sha256_hex NOT NULL,
  created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  UNIQUE (app_id, version),
  CHECK (cardinality(tags) <= 64 AND cardinality(capability_labels) <= 64),
  CHECK (array_position(tags, NULL) IS NULL AND array_position(capability_labels, NULL) IS NULL),
  CHECK (wit_world <> 'ui-only-reference' OR kind = 'ui'),
  CHECK (wit_world <> 'service-only-reference' OR kind = 'service'),
  CHECK (wit_world <> 'hybrid-reference' OR kind = 'hybrid'),
  CHECK (wit_world <> 'web-preview-reference' OR kind = 'ui')
);

CREATE INDEX app_version_search_tsv_idx ON registry.app_version USING gin(search_tsv);
CREATE INDEX app_version_search_trgm_idx ON registry.app_version USING gin(search_document public.gin_trgm_ops);

CREATE TABLE registry.release_profile (
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  profile registry.execution_profile NOT NULL,
  PRIMARY KEY (release_id, profile)
);

CREATE TABLE registry.release_platform (
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  os registry.platform_os NOT NULL,
  arch registry.platform_arch NOT NULL,
  profile registry.execution_profile NOT NULL,
  PRIMARY KEY (release_id, os, arch, profile),
  FOREIGN KEY (release_id, profile) REFERENCES registry.release_profile(release_id, profile) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CHECK ((os = 'browser' AND arch = 'wasm32' AND profile IN ('web-preview', 'web-runtime')) OR os <> 'browser'),
  CHECK ((profile IN ('web-preview', 'web-runtime') AND os = 'browser' AND arch = 'wasm32') OR profile NOT IN ('web-preview', 'web-runtime'))
);

CREATE TABLE registry.release_capability (
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  interface_name varchar(200) NOT NULL CHECK (
    interface_name ~ '^vibapp:experimental-v0/(clock|scheduler|notification|kv|log|host-info|settings|system-metrics|http)@0[.]0[.]1$'
  ),
  necessity registry.capability_necessity NOT NULL,
  PRIMARY KEY (release_id, interface_name)
);

CREATE TABLE registry.release_permission (
  release_id bigint NOT NULL,
  interface_name varchar(200) NOT NULL,
  grant_mode registry.capability_grant NOT NULL,
  scope_digest_sha256 registry.sha256_hex NOT NULL,
  summary varchar(500) NOT NULL CHECK (registry.is_clean_text(summary, 1, 500)),
  PRIMARY KEY (release_id, interface_name),
  FOREIGN KEY (release_id, interface_name) REFERENCES registry.release_capability(release_id, interface_name) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE registry.verification_evidence (
  evidence_id registry.identifier PRIMARY KEY,
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  evidence_kind varchar(64) NOT NULL CHECK (evidence_kind IN ('manifest', 'component', 'provenance', 'sbom', 'build', 'scan', 'independent-verification')),
  verifier_id registry.identifier NOT NULL,
  evidence_digest_sha256 registry.sha256_hex NOT NULL,
  verdict registry.verification_status NOT NULL,
  observed_at timestamptz NOT NULL,
  expires_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object' AND pg_column_size(metadata) <= 65536),
  CHECK (expires_at IS NULL OR expires_at > observed_at),
  UNIQUE (release_id, evidence_kind, verifier_id, evidence_digest_sha256)
);

CREATE TABLE registry.release_verification (
  release_id bigint PRIMARY KEY REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  status registry.verification_status NOT NULL,
  evidence_id registry.identifier NOT NULL REFERENCES registry.verification_evidence(evidence_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  verified_at timestamptz,
  CHECK ((status = 'verified' AND verified_at IS NOT NULL) OR status <> 'verified')
);

CREATE TABLE registry.revocation_event (
  event_id registry.identifier PRIMARY KEY,
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  authority_id registry.identifier NOT NULL,
  reason_code varchar(64) NOT NULL CHECK (registry.is_clean_text(reason_code, 1, 64)),
  reason varchar(500) NOT NULL CHECK (registry.is_clean_text(reason, 1, 500)),
  evidence_digest_sha256 registry.sha256_hex NOT NULL,
  effective_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  UNIQUE (release_id, authority_id, evidence_digest_sha256)
);

CREATE INDEX revocation_event_release_idx ON registry.revocation_event(release_id, effective_at);

CREATE TABLE registry.publication (
  release_id bigint PRIMARY KEY REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  state registry.publication_state NOT NULL DEFAULT 'unpublished',
  visibility registry.source_visibility NOT NULL DEFAULT 'private',
  moderation_evidence_digest_sha256 registry.sha256_hex,
  changed_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  CHECK (state <> 'published' OR (visibility = 'public' AND moderation_evidence_digest_sha256 IS NOT NULL)),
  CHECK (visibility <> 'private' OR state <> 'published')
);

CREATE TABLE registry.authority_grant (
  authority_event_id registry.identifier PRIMARY KEY,
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  authority_kind registry.authority_kind NOT NULL,
  principal_id registry.identifier NOT NULL,
  payload_digest_sha256 registry.sha256_hex NOT NULL,
  granted_at timestamptz NOT NULL,
  expires_at timestamptz,
  revoked_at timestamptz,
  CHECK (expires_at IS NULL OR expires_at > granted_at),
  CHECK (revoked_at IS NULL OR revoked_at >= granted_at),
  UNIQUE (release_id, authority_kind, principal_id, payload_digest_sha256)
);

CREATE UNIQUE INDEX authority_one_active_kind
  ON registry.authority_grant(release_id, authority_kind)
  WHERE revoked_at IS NULL;

CREATE TABLE registry.release_embedding (
  release_id bigint NOT NULL REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  model_id registry.identifier NOT NULL,
  model_version registry.identifier NOT NULL,
  content_digest_sha256 registry.sha256_hex NOT NULL,
  embedding public.vector(384) NOT NULL,
  embedded_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  PRIMARY KEY (release_id, model_id, model_version),
  FOREIGN KEY (model_id, model_version) REFERENCES registry.embedding_model(model_id, model_version) ON UPDATE RESTRICT ON DELETE RESTRICT,
  CHECK (public.vector_dims(embedding) = 384 AND public.vector_norm(embedding) > 0)
);

CREATE INDEX release_embedding_hnsw_cosine_idx
  ON registry.release_embedding USING hnsw (embedding public.vector_cosine_ops);

CREATE TABLE registry.audit_event (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id registry.identifier NOT NULL UNIQUE,
  operation varchar(64) NOT NULL CHECK (registry.is_clean_text(operation, 1, 64)),
  idempotency_key registry.identifier NOT NULL,
  actor_principal registry.identifier NOT NULL,
  request_digest_sha256 registry.sha256_hex NOT NULL,
  outcome registry.audit_outcome NOT NULL,
  subject_app_id registry.identifier,
  subject_release_id bigint,
  result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object' AND pg_column_size(result) <= 65536),
  occurred_at timestamptz NOT NULL DEFAULT statement_timestamp(),
  UNIQUE (operation, idempotency_key),
  FOREIGN KEY (subject_app_id) REFERENCES registry.app(app_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (subject_release_id) REFERENCES registry.app_version(release_id) ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE INDEX audit_event_subject_idx ON registry.audit_event(subject_app_id, subject_release_id, sequence DESC);

COMMIT;
