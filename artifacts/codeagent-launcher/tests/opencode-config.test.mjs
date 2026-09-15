import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import provider, { MODEL, SOURCE_SYSTEM_PROMPT, RELAY_POLICY, configuration, environment } from '../docker/opencode-provider.mjs';

test('model and request route are fixed independently of workload selection', () => {
  assert.equal(provider.route, '/v1/chat/completions');
  for (const model of [MODEL, `vibapp/${MODEL}`]) assert.deepEqual(provider.args(model), ['run', '--pure', '--format', 'json', '--model', `vibapp/${MODEL}`, '--agent', 'build', '--dir', '/work', '--title', 'VibApp admitted source task']);
  for (const model of ['', 'qwen3-4b', `other/${MODEL}`, `${MODEL}\n`]) assert.throws(() => provider.args(model));
});
test('only a trusted boolean enables continuation of the job-local session', () => {
  assert.deepEqual(provider.args(MODEL, false), provider.args(MODEL));
  assert.deepEqual(provider.args(MODEL, true), [...provider.args(MODEL), '--continue']);
  for (const invalid of ['session-host', 'true', 1, {}, null]) assert.throws(() => provider.args(MODEL, invalid));
  assert.ok(!provider.args(MODEL, true).some(arg => ['--session', '--fork', '--attach'].includes(arg)));
  const entry = fs.readFileSync(new URL('../docker/entry.mjs', import.meta.url), 'utf8');
  assert.match(entry, /provider\.args\(input\.model, repairRound > 0\)/);
  assert.match(entry, /author\(\(provider \? '' : input\.prompt\)/);
  assert.doesNotMatch(entry, /input\.(?:session|continueSession)/);
});
test('image declares explicit upstream JSON and downstream SSE compatibility', () => {
  assert.deepEqual(provider.relayPolicy, RELAY_POLICY);
  assert.deepEqual(RELAY_POLICY, { mode: 'localai-native-json-to-sse-v1', upstream_stream: false, downstream_stream: true, reason: 'fixed-localai-stream-tool-truncation', tool_names: 'preserve-native-sdk-case-repair' });
});
test('synthetic config has fixed context, no integrations or unconstrained provider', () => {
  const config = configuration();
  assert.deepEqual(config.enabled_providers, ['vibapp']);
  assert.deepEqual(Object.keys(config.provider), ['vibapp']);
  assert.equal(config.provider.vibapp.options.baseURL, 'http://127.0.0.1:8787/v1');
  assert.deepEqual(config.provider.vibapp.models[MODEL].limit, { context: 32768, output: 16384 });
  assert.equal(config.share, 'disabled'); assert.equal(config.autoupdate, false);
  assert.deepEqual(config.plugin, []); assert.deepEqual(config.mcp, {});
  assert.equal(config.lsp, false); assert.equal(config.formatter, false);
  assert.equal(config.permission['*'], 'deny'); assert.equal(config.permission.edit, 'allow');
  assert.equal(config.permission.bash, 'deny');
  assert.equal(config.permission.grep, 'deny');
});
test('image-bound system prompt replaces generic tool and compile instructions', () => {
  assert.deepEqual(configuration().agent, { build: { prompt: SOURCE_SYSTEM_PROMPT } });
  assert.match(SOURCE_SYSTEM_PROMPT, /exact lowercase names: read, glob, write, edit, todowrite/);
  assert.match(SOURCE_SYSTEM_PROMPT, /grep tool is unavailable/);
  assert.match(SOURCE_SYSTEM_PROMPT, /deliberately has NO Rust compiler/);
  assert.match(SOURCE_SYSTEM_PROMPT, /wit_bindgen::generate!/);
  assert.match(SOURCE_SYSTEM_PROMPT, /below 180 lines of source/);
  assert.match(SOURCE_SYSTEM_PROMPT, /contracts\/rust-support\.md/);
  assert.match(SOURCE_SYSTEM_PROMPT, /immutable source\/src\/vibapp_support\.rs/);
  assert.match(SOURCE_SYSTEM_PROMPT, /FIRST read contracts\/rust-support\.md/);
  assert.match(SOURCE_SYSTEM_PROMPT, /exact supplied WIT remains authoritative/);
  assert.match(SOURCE_SYSTEM_PROMPT, /For other task worlds read their exact relevant WIT/);
  assert.match(SOURCE_SYSTEM_PROMPT, /read offset\/limit for bounded sections/);
  assert.match(SOURCE_SYSTEM_PROMPT, /Do not compile, test, lint, install/);
  assert.match(SOURCE_SYSTEM_PROMPT, /start background processes, access the network/);
  assert.match(SOURCE_SYSTEM_PROMPT, /never edit, remove, chmod or recreate/);
  assert.doesNotMatch(SOURCE_SYSTEM_PROMPT, /\bTask\b|\bBash\b|MUST run the lint/);
});
test('environment is isolated and contains neither inherited keys nor credentials', () => {
  process.env.OPENCODE_API_KEY = 'synthetic-do-not-inherit';
  const env = environment();
  assert.equal(env.OPENCODE_API_KEY, undefined);
  assert.equal(env.HOME, '/tmp/opencode/home');
  assert.equal(env.OPENCODE_DISABLE_DEFAULT_PLUGINS, 'true');
  assert.equal(env.OPENCODE_DISABLE_MODELS_FETCH, 'true');
  assert.equal(env.OPENCODE_DISABLE_CLAUDE_CODE, 'true');
  assert.equal(env.OPENCODE_DISABLE_PROJECT_CONFIG, 'true');
  assert.equal(env.OPENCODE_PURE, 'true');
  assert.equal(env.OTEL_SDK_DISABLED, 'true');
  assert.deepEqual(JSON.parse(env.OPENCODE_CONFIG_CONTENT), configuration());
  assert.ok(!JSON.stringify(env).includes('synthetic-do-not-inherit'));
  delete process.env.OPENCODE_API_KEY;
});
test('hidden reasoning and arbitrary events never become provider traces', () => {
  for (const event of [{ type: 'reasoning', part: { type: 'reasoning', text: 'private' } }, { type: 'text', part: { type: 'reasoning', text: 'private' } }, { type: 'error', error: { message: 'arbitrary' } }]) assert.equal(provider.trace(event), null);
  assert.equal(provider.trace({ type: 'text', part: { type: 'text', text: 'x'.repeat(4000) } }).text.length, 3000);
  assert.equal(provider.trace({ type: 'tool_use', part: { type: 'tool', state: { status: 'completed', output: 'bounded evidence' } } }).exit_code, 0);
});
test('provider hook cannot be selected through the start request', () => {
  const entry = fs.readFileSync(new URL('../docker/entry.mjs', import.meta.url), 'utf8');
  assert.match(entry, /fs\.existsSync\(providerModule\)/);
  assert.doesNotMatch(entry, /input\.provider/);
  assert.match(entry, /uid: 1000, gid: 1000/);
});
