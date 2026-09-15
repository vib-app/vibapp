// OpenCode v1 image-only hook. No host configuration, plugins or credentials.
import fs from 'node:fs';

export const MODEL = 'qwen3.8-27b-uncensored-mtp-q4';
export const RELAY_POLICY = {
  mode: 'localai-native-json-to-sse-v1', upstream_stream: false, downstream_stream: true,
  reason: 'fixed-localai-stream-tool-truncation', tool_names: 'preserve-native-sdk-case-repair',
};
export const SOURCE_SYSTEM_PROMPT = [
  'You are the source-authoring OpenCode agent for one admitted VibApp task. The user supplies the complete task and immutable contract bindings.',
  'Use the offered native function tools by their exact lowercase names: read, glob, write, edit, todowrite. Supply complete JSON arguments matching each offered schema. Do not print tool-call markup as ordinary response text. The grep tool is unavailable in this contained image; use bounded read with offset/limit for the supplied known file paths.',
  'For a UI task FIRST read contracts/rust-support.md for the exact Guest/main/API recipe, then briefly read source/Cargo.toml and reuse the immutable source/src/vibapp_support.rs as the guide shows. Read only relevant sections of source/wit/contract.wit when a signature or type is missing from the guide; the exact supplied WIT remains authoritative. For other task worlds read their exact relevant WIT contracts. Use read offset/limit for bounded sections, not repeated whole-file reads. Physically write or edit application Rust source only below /work/source/. A code block or success claim is not a source delivery.',
  'The supplied Cargo.toml and all contract files are immutable: never edit, remove, chmod or recreate them. Follow the task world, imports, permission ceiling and source policy exactly.',
  'The authoring container deliberately has NO Rust compiler or Cargo cache. Do not search for them: the separate Builder supplies them. UI support and its short guide are already supplied within /work; never rewrite the allocator or guess its API. Begin writing source/src/lib.rs with the write tool after reading the guide. Do not explore the container filesystem or request a shell.',
  'Keep each write/edit tool call below 180 lines of source. Split the application into src/lib.rs plus small Rust modules (for example memory.rs, state.rs, view.rs). Author each module in a separate native tool call, or append a small section with edit. A giant single-file write can be cut off and none of it will execute. All code and tool JSON must be complete; use concise code instead of large comment banners.',
  'Rust binding recipe for a UI task: wit_bindgen::generate!({ path: "wit", world: "ui-only-reference" }); generates imports under vibapp::experimental_v0 and the exported trait under exports::vibapp::experimental_v0::guest::Guest. Substitute the exact task world for other app kinds. Implement all six Guest functions for your app type and call export!(YourAppType). WIT kebab-case maps to Rust snake_case, records/enums/variants to PascalCase types/cases. The generate macro creates bindings during the separate build; there is no generated .rs file to locate now. Use the exact supplied WIT for signatures and records.',
  'Do not delegate or invoke unavailable tools. Do not compile, test, lint, install, fetch dependencies, run generated code, start background processes, access the network, verify, sign, publish or install an app. Only read/write/edit/glob tools inside /work are needed to author source.',
  'A separate offline Builder checks the source. If its bounded feedback is returned in this same task, repair source only without changing the original requirements or contracts. Finish with a brief factual summary only after the source files exist.',
  'Be concise. Use native file tools to make edits, not a long discussion of edits you might make. Once source and its inventory are complete, finish so the separate Builder can check it. Generic host clock import retention is not a request for timer behavior in every application.',
].join('\n');
export function selectedModel(value) {
  if (value !== MODEL && value !== `vibapp/${MODEL}`) throw new Error('unsupported fixed LocalAI model');
  return `vibapp/${MODEL}`;
}
export function configuration() {
  return {
    model: `vibapp/${MODEL}`, small_model: `vibapp/${MODEL}`,
    enabled_providers: ['vibapp'], plugin: [], mcp: {}, instructions: [],
    // Supported v1 agent prompt replacement, not an appended lower-priority
    // instruction competing with the generic CLI's compile/delegation prompt.
    agent: { build: { prompt: SOURCE_SYSTEM_PROMPT } },
    share: 'disabled', autoupdate: false, lsp: false, formatter: false,
    compaction: { auto: false, prune: false },
    permission: { '*': 'deny', read: 'allow', glob: 'allow', grep: 'deny', list: 'allow', edit: 'allow', bash: 'deny', todowrite: 'allow' },
    provider: {
      vibapp: {
        npm: '@ai-sdk/openai-compatible', name: 'VibApp fixed LocalAI relay',
        options: { baseURL: 'http://127.0.0.1:8787/v1' },
        models: { [MODEL]: { name: 'Qwen3.8 27B MTP (fixed LAN)', limit: { context: 32768, output: 16384 } } },
      },
    },
  };
}
export function environment() {
  return {
    PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/tmp/opencode/home',
    XDG_CONFIG_HOME: '/tmp/opencode/config', XDG_CACHE_HOME: '/tmp/opencode/cache',
    XDG_DATA_HOME: '/tmp/opencode/data', XDG_STATE_HOME: '/tmp/opencode/state',
    TMPDIR: '/tmp/opencode/tmp', LANG: 'C.UTF-8', TZ: 'UTC',
    OPENCODE_CONFIG_CONTENT: JSON.stringify(configuration()),
    OPENCODE_CONFIG_DIR: '/tmp/opencode/config/opencode',
    OPENCODE_DISABLE_PROJECT_CONFIG: 'true', OPENCODE_PURE: 'true',
    OPENCODE_DISABLE_AUTOUPDATE: 'true', OPENCODE_DISABLE_DEFAULT_PLUGINS: 'true',
    OPENCODE_DISABLE_LSP_DOWNLOAD: 'true', OPENCODE_DISABLE_AUTOCOMPACT: 'true',
    OPENCODE_DISABLE_PRUNE: 'true', OPENCODE_DISABLE_TERMINAL_TITLE: 'true',
    OPENCODE_DISABLE_CLAUDE_CODE: 'true', OPENCODE_DISABLE_CLAUDE_CODE_PROMPT: 'true',
    OPENCODE_DISABLE_CLAUDE_CODE_SKILLS: 'true', OPENCODE_DISABLE_MODELS_FETCH: 'true',
    OPENCODE_AUTO_SHARE: 'false', OPENCODE_ENABLE_EXA: 'false', OPENCODE_ENABLE_PARALLEL: 'false',
    DO_NOT_TRACK: '1', OTEL_SDK_DISABLED: 'true',
  };
}
export default {
  relayPolicy: RELAY_POLICY,
  cli: '/usr/local/bin/opencode', bundle: '/usr/local/lib/node_modules/opencode-ai',
  route: '/v1/chat/completions',
  prepare(model) {
    selectedModel(model);
    for (const name of ['', '/home', '/config', '/config/opencode']) {
      // Root-owned config/HOME cannot acquire workload-supplied rules/plugins
      // before a compiler repair starts a fresh CLI process in this same job.
      fs.mkdirSync('/tmp/opencode' + name, { mode: 0o755 });
    }
    fs.writeFileSync('/tmp/opencode/config/opencode/.gitignore', 'node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore', { mode: 0o444, flag: 'wx' });
    for (const name of ['/cache', '/data', '/state', '/tmp']) {
      const directory = '/tmp/opencode' + name;
      fs.mkdirSync(directory, { mode: 0o700 }); fs.chownSync(directory, 1000, 1000);
    }
  },
  args(model, continueSession = false) {
    if (typeof continueSession !== 'boolean') throw new Error('invalid session continuation');
    // One root session exists in this job's isolated XDG data. Only trusted
    // compiler feedback may continue it; no host or workload session is accepted.
    return ['run', '--pure', '--format', 'json', '--model', selectedModel(model), '--agent', 'build', '--dir', '/work', '--title', 'VibApp admitted source task', ...(continueSession ? ['--continue'] : [])];
  },
  env: environment,
  trace(event) {
    // Deliberately exclude reasoning events/content and raw logs, even privately.
    if (event.type === 'text' && event.part?.type === 'text')
      return { item_type: 'agent_message', exit_code: null, text: String(event.part.text || '').slice(-3000) };
    if (event.type === 'tool_use' && event.part?.type === 'tool' && ['completed', 'error'].includes(event.part.state?.status))
      return { item_type: 'command_execution', exit_code: event.part.state.status === 'completed' ? 0 : 1, text: String(event.part.state.output || event.part.state.error || '').slice(-3000) };
    return null;
  },
};
