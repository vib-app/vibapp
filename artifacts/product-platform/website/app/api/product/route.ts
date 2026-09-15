import { readStoreCatalog } from '../../../lib/store-catalog.ts';

const REQUEST_VERSION = 'vibapp.web-product-request.experimental-v1';
const RESPONSE_VERSION = 'vibapp.website-product-route.experimental-v1';
const MAX_REQUEST_BYTES = 512 * 1024;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const ALLOWED_COMMANDS = new Set([
  'health',
  'get_state',
  'submit_need',
  'complete_need',
  'get_model_settings',
  'save_model_settings',
  'get_codeagent_settings',
  'save_codeagent_settings',
  'get_network_settings',
  'save_network_settings',
  'submit_development_task',
]);

async function productionReadOnly(command: string): Promise<Response> {
  // Boot-time reads are supported even without a remote service. These are
  // explicit disconnected defaults, never host settings or provider secrets.
  const availability = { configured: false, readOnly: true, unavailableReason: 'remote-service-not-connected' };
  let settings: Record<string, unknown> | null = null;
  if (command === 'get_model_settings') settings = {
    ...availability,
    generation: {
      enabled: false, baseUrl: '', model: '', protocol: 'chat-completions',
      timeoutSeconds: 45, maxOutputTokens: 512, temperature: 0, hasApiKey: false,
    },
    embedding: {
      enabled: false, baseUrl: '', model: '', dimensions: 384,
      timeoutSeconds: 5, hasApiKey: false,
    },
  };
  if (command === 'get_codeagent_settings') settings = {
    ...availability, selectedProvider: '', modelByProvider: {}, providers: [],
  };
  if (command === 'get_network_settings') settings = {
    ...availability,
    p2pEnabled: false, seedVerifiedApps: false, allowUserFileSeeding: false,
    uploadLimitKibPerSecond: 1024, downloadLimitKibPerSecond: 4096,
    cacheLimitMib: 128, maxActiveTransfers: 8,
    rtcEnabled: false, maxActiveChannels: 8,
    turnEnabled: false, turnUrls: [], turnUsername: '', hasTurnCredential: false,
  };
  if (settings) return json(200, {
    schema_version: RESPONSE_VERSION, ok: true, result: settings, error: null,
  });
  if (command === 'health') {
    return json(200, {
      schema_version: RESPONSE_VERSION,
      ok: true,
      result: {
        status: 'ok',
        service: 'vibapp-public-read-only-product-route',
        mutation_authority: false,
      },
      error: null,
    });
  }
  if (command === 'get_state') {
    const catalog = await readStoreCatalog();
    return json(200, {
      schema_version: RESPONSE_VERSION,
      ok: true,
      result: {
        meta: {
          schema_version: 'vibapp.product-bridge-state.experimental-v1',
          runtime_mode: 'public-website-read-only',
          ecosystem_role: 'launcher-appstore-runtime',
          cloud_agent_connected: false,
          local_codeagent_connected: false,
          codeagent_queue_enabled: false,
          codeagent_external_runner_configured: false,
          installation_performed: false,
          mutation_authority: false,
        },
        jobs: [],
        apps: catalog.apps,
        feed_errors: catalog.feed_errors,
      },
      error: null,
    });
  }
  return json(403, {
    schema_version: RESPONSE_VERSION,
    ok: false,
    result: null,
    error: process.env.NEXT_PUBLIC_VIBAPP_HOSTED_SHELL === '1'
      ? '远程服务尚未连接，暂不能分析需求、创建开发任务或保存服务设置。 / Remote service is not connected.'
      : 'authenticated-user-session-required',
  });
}

function json(status: number, value: unknown): Response {
  return Response.json(value, {
    status,
    headers: {
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
    },
  });
}

function exactKeys(value: unknown, expected: string): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value as Record<string, unknown>).sort().join(',') === expected;
}

function backend(): { url: URL; token: string } {
  const rawUrl = process.env.VIBAPP_PRODUCT_BACKEND_URL || '';
  const token = process.env.VIBAPP_PRODUCT_BACKEND_TOKEN || '';
  if (token.length < 32 || token.length > 512 || /\s/.test(token)) {
    throw new Error('backend-unconfigured');
  }
  const url = new URL('/v1/invoke', rawUrl);
  const loopback = url.hostname === '127.0.0.1' || url.hostname === 'localhost' || url.hostname === '::1';
  if (
    (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
    || url.username
    || url.password
    || url.search
    || url.hash
  ) throw new Error('backend-url-invalid');
  return { url, token };
}

export async function POST(request: Request): Promise<Response> {
  try {
    const contentType = request.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase();
    const declared = request.headers.get('content-length');
    if (
      contentType !== 'application/json'
      || !declared
      || !/^\d+$/.test(declared)
      || Number(declared) > MAX_REQUEST_BYTES
    ) return json(400, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'request-size-invalid' });
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_REQUEST_BYTES) {
      return json(400, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'request-limit-exceeded' });
    }
    const value = JSON.parse(new TextDecoder().decode(bytes));
    if (
      !exactKeys(value, 'args,command,schema_version')
      || value.schema_version !== REQUEST_VERSION
      || typeof value.command !== 'string'
      || !ALLOWED_COMMANDS.has(value.command)
      || !value.args
      || typeof value.args !== 'object'
      || Array.isArray(value.args)
    ) return json(400, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'request-invalid' });

    // The public Website has no implemented end-user principal/session binding
    // yet. Never forward shared settings, private jobs, consent, or mutation
    // authority through one server credential. Public discovery reads only the
    // fixed public packages catalog and cannot grant guest execution authority.
    if (process.env.VIBAPP_PREVIEW_LOCAL !== '1') {
      return productionReadOnly(value.command);
    }

    const target = backend();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 75_000);
    let upstream: Response;
    try {
      upstream = await fetch(target.url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': String(bytes.byteLength),
          Authorization: `Bearer ${target.token}`,
        },
        body: bytes,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeout);
    }
    const length = upstream.headers.get('content-length');
    if (length && (!/^\d+$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) {
      return json(502, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'backend-response-limit' });
    }
    const responseBytes = await upstream.arrayBuffer();
    if (responseBytes.byteLength === 0 || responseBytes.byteLength > MAX_RESPONSE_BYTES) {
      return json(502, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'backend-response-limit' });
    }
    const result = JSON.parse(new TextDecoder().decode(responseBytes));
    if (
      !exactKeys(result, 'error,ok,result,schema_version')
      || result.schema_version !== 'vibapp.web-product-response.experimental-v1'
      || typeof result.ok !== 'boolean'
      || (result.error !== null && typeof result.error !== 'string')
    ) return json(502, { schema_version: RESPONSE_VERSION, ok: false, result: null, error: 'backend-response-invalid' });
    return json(result.ok ? 200 : 400, {
      schema_version: RESPONSE_VERSION,
      ok: result.ok,
      result: result.result,
      error: result.error,
    });
  } catch (error) {
    const unavailable = error instanceof Error && error.message === 'backend-unconfigured';
    return json(unavailable ? 503 : 502, {
      schema_version: RESPONSE_VERSION,
      ok: false,
      result: null,
      error: unavailable ? 'backend-unconfigured' : 'backend-unavailable',
    });
  }
}
