import { readFile, readdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const POLICY_PATH = join(HERE, 'web-activation-policy.proposal-v1.json');
const POLICY_SCHEMA_PATH = join(HERE, 'web-activation-policy.proposal-v1.schema.json');
const VALID_PATH = join(HERE, 'fixtures', 'valid.product-candidate.json');
const FIXTURE_ROOT = join(HERE, 'fixtures');
const SHA256 = /^[0-9a-f]{64}$/;

function equal(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function reject(reason) {
  return Object.freeze({ eligible: false, reason });
}

export function validatePolicyProposal(policy) {
  if (policy?.schema_version !== 'vibapp.web-activation-policy.proposal-v1') throw new Error('web-activation-policy:policy-schema');
  if (policy.document_type !== 'product-only-web-activation-policy-proposal') throw new Error('web-activation-policy:policy-type');
  if (policy.status !== 'locally-executable-awaiting-contract-and-independent-acceptance') throw new Error('web-activation-policy:policy-status');
  if (
    policy.authority?.accepted_stage0_source !== false
    || policy.authority?.formal_web_activation !== false
    || policy.authority?.public_activation !== false
    || policy.authority?.publication !== false
  ) throw new Error('web-activation-policy:authority-boundary');
  if (policy.current_product_assessment?.eligible !== false || !policy.current_product_assessment.blockers?.length) {
    throw new Error('web-activation-policy:current-product-boundary');
  }
  if (!Array.isArray(policy.required_negative_fixture_ids) || new Set(policy.required_negative_fixture_ids).size !== policy.required_negative_fixture_ids.length) {
    throw new Error('web-activation-policy:fixture-inventory');
  }
  return policy;
}

function validatePolicySchema(schema) {
  if (schema?.$schema !== 'https://json-schema.org/draft/2020-12/schema') throw new Error('web-activation-policy:json-schema-draft');
  if (schema.type !== 'object' || schema.additionalProperties !== false) throw new Error('web-activation-policy:json-schema-root');
  if (schema.properties?.schema_version?.const !== 'vibapp.web-activation-policy.proposal-v1') throw new Error('web-activation-policy:json-schema-version');
  if (schema.properties?.document_type?.const !== 'product-only-web-activation-policy-proposal') throw new Error('web-activation-policy:json-schema-type');
  const required = new Set(schema.required || []);
  for (const key of ['authority', 'current_product_assessment', 'derivation_requirements', 'exact_toolchain', 'manifest_requirements', 'required_negative_fixture_ids', 'runtime_requirements']) {
    if (!required.has(key)) throw new Error('web-activation-policy:json-schema-required-' + key);
  }
  return schema;
}

export function evaluateActivationCandidate(policy, candidate) {
  validatePolicyProposal(policy);
  const manifestPolicy = policy.manifest_requirements;
  const manifest = candidate?.manifest;
  if (candidate?.schema_version !== 'vibapp.web-activation-candidate.experimental-v1') return reject('candidate-schema');
  if (
    manifest?.package_format !== manifestPolicy.package_format
    || manifest?.contract !== manifestPolicy.contract
    || manifest?.wasi !== manifestPolicy.wasi
  ) return reject('manifest-contract');
  if (manifest.profile !== manifestPolicy.profile) return reject('manifest-profile');
  if (manifest.platform_os !== manifestPolicy.platform_os || manifest.platform_arch !== manifestPolicy.platform_arch) return reject('manifest-platform');
  if (manifest.artifact_role !== manifestPolicy.artifact_role) return reject('manifest-artifact-role');
  if (!SHA256.test(manifest.canonical_component_sha256 || '')) return reject('manifest-canonical-digest');
  if (manifest.browser_derivation?.format !== 'jco-esm') return reject('manifest-derivation-format');
  if (manifest.browser_derivation?.derived_from_sha256 !== manifest.canonical_component_sha256) return reject('manifest-derived-digest');
  if (manifest.browser_derivation?.capabilities_complete !== true) return reject('manifest-capabilities');
  if (manifest.browser_derivation?.live_background_hardware_or_network !== false) return reject('manifest-live-authority');

  const tool = candidate.toolchain;
  const expectedTool = policy.exact_toolchain;
  if (tool?.canonical_target !== expectedTool.canonical_component_target || tool?.rust !== expectedTool.canonical_rust) return reject('toolchain-canonical');
  if (tool.package !== expectedTool.derivation_package || tool.version !== expectedTool.derivation_version) return reject('toolchain-jco');
  if (tool.resolution !== expectedTool.resolution || !equal(tool.flags, expectedTool.derivation_flags)) return reject('toolchain-resolution');

  const derivation = candidate.derivation;
  const expectedDerivation = policy.derivation_requirements;
  if (derivation?.source_state !== expectedDerivation.source_state || derivation?.source_verification_authority !== expectedDerivation.source_verification_authority) return reject('derivation-source');
  if (derivation.all_files_content_addressed !== true || derivation.complete_file_inventory !== true || derivation.attestation_content_addressed !== true) return reject('derivation-content-addressing');
  if (derivation.attestation_verified_by_fresh_independent_owner !== true) return reject('derivation-attestation');
  if (derivation.two_clean_derivations_byte_equal !== true) return reject('derivation-reproducibility');
  if (derivation.executable_blob_or_data_urls !== false) return reject('derivation-executable-url');
  if (derivation.fallback_to_unverified_code !== false) return reject('derivation-fallback');

  const runtime = candidate.runtime;
  const expectedRuntime = policy.runtime_requirements;
  if (runtime?.dedicated_worker !== true || runtime.guest_dom !== false) return reject('runtime-worker');
  if (
    runtime.browser_storage !== false
    || runtime.service_worker !== false
    || runtime.credentials !== false
    || runtime.install_or_permission_authority !== false
    || runtime.background_reliability !== expectedRuntime.background_reliability
  ) return reject('runtime-ambient-authority');
  if (runtime.launcher_origin_relation !== expectedRuntime.launcher_origin_relation) return reject('runtime-origin-isolation');
  if (runtime.bootstrap !== expectedRuntime.bootstrap) return reject('runtime-bootstrap');
  if (
    !equal(runtime.csp?.script_src, expectedRuntime.csp.script_src)
    || !equal(runtime.csp?.worker_src, expectedRuntime.csp.worker_src)
    || !equal(runtime.csp?.connect_src, expectedRuntime.csp.connect_src)
    || runtime.csp?.executable_blob_or_data !== false
    || runtime.csp?.static_server_allowlist !== true
    || runtime.csp?.credential_headers_rejected !== true
  ) return reject('runtime-csp');
  const audit = runtime.invalid_bootstrap_audit;
  if (
    audit?.durable !== true
    || audit.healthy !== true
    || audit.payload_storage !== false
    || audit.bounded_size_and_count !== true
    || audit.retention_and_rotation !== true
    || audit.complete_record_corruption !== 'fail-closed'
  ) return reject('runtime-audit');
  if (candidate.verification?.fresh_independent_owner !== true) return reject('independent-verifier');
  if (candidate.verification?.formal_policy_acceptance !== true) return reject('formal-policy-acceptance');
  return Object.freeze({ eligible: true, reason: null });
}

function mutate(base, mutation) {
  const candidate = structuredClone(base);
  const parts = String(mutation?.path || '').split('.');
  if (!parts.length || parts.some(part => !/^[a-z][a-z0-9_]*$/.test(part))) throw new Error('web-activation-policy:fixture-path');
  let target = candidate;
  for (const part of parts.slice(0, -1)) {
    if (!Object.prototype.hasOwnProperty.call(target, part) || !target[part] || typeof target[part] !== 'object') {
      throw new Error('web-activation-policy:fixture-target');
    }
    target = target[part];
  }
  const leaf = parts.at(-1);
  if (!Object.prototype.hasOwnProperty.call(target, leaf)) throw new Error('web-activation-policy:fixture-leaf');
  target[leaf] = mutation.value;
  return candidate;
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

export async function runPolicyFixtures() {
  const [policy, policySchema, valid] = await Promise.all([
    readJson(POLICY_PATH),
    readJson(POLICY_SCHEMA_PATH),
    readJson(VALID_PATH),
  ]);
  validatePolicySchema(policySchema);
  validatePolicyProposal(policy);
  const positive = evaluateActivationCandidate(policy, valid);
  if (!positive.eligible) throw new Error('web-activation-policy:positive:' + positive.reason);
  const fixtureNames = (await readdir(FIXTURE_ROOT)).filter(name => name.endsWith('.invalid.json')).sort();
  const seen = new Set();
  const results = [];
  for (const name of fixtureNames) {
    const fixture = await readJson(join(FIXTURE_ROOT, name));
    if (seen.has(fixture.case_id)) throw new Error('web-activation-policy:duplicate-fixture');
    seen.add(fixture.case_id);
    const result = evaluateActivationCandidate(policy, mutate(valid, fixture.mutation));
    if (result.eligible || result.reason !== fixture.expected_reason) {
      throw new Error('web-activation-policy:fixture-mismatch:' + fixture.case_id + ':' + result.reason);
    }
    results.push({ case_id: fixture.case_id, outcome: 'rejected', reason: result.reason });
  }
  if (!equal([...seen].sort(), [...policy.required_negative_fixture_ids].sort())) {
    throw new Error('web-activation-policy:fixture-inventory-mismatch');
  }
  return {
    schema_version: 'vibapp.web-activation-policy-local-validation.experimental-v1',
    state: 'local-product-proposal-validation-pass',
    positive_cases: 1,
    negative_cases: results.length,
    negative_results: results,
    proposal_schema: 'draft-2020-12-parsed-and-cross-checked',
    current_product_activation_eligible: false,
    formal_stage0_acceptance: 'not-claimed',
    public_activation: 'not-authorized',
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.stdout.write(JSON.stringify(await runPolicyFixtures()) + '\n');
}
