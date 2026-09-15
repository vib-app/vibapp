import {
  classifyInvalidBootstrapError,
  reportInvalidBootstrap,
} from './invalid-bootstrap-audit.mjs';
import {
  verifyDerivationAttestation,
  verifyDerivationFileSet,
  verifyGuestLaunchOutput,
  verifyLaunchControl,
  verifyLaunchEnvelope,
} from './runtime-integrity.mjs';
import { bindForegroundWorker, foregroundBinding, foregroundFields } from './foreground-session.mjs';

function normalizeKind(kind) {
  if (!kind || typeof kind.tag !== 'string') throw new Error('malformed-output:node-kind');
  return { [kind.tag]: kind.val ?? {} };
}

function normalizeSurface(surface) {
  const value = {
    ...surface,
    view: {
      ...surface.view,
      nodes: surface.view.nodes.map(node => ({ ...node, kind: normalizeKind(node.kind) })),
    },
  };
  return JSON.parse(JSON.stringify(value, (_, item) => typeof item === 'bigint' ? (Number.isSafeInteger(Number(item)) ? Number(item) : item.toString()) : item));
}

async function runLaunch(port, boundControl, message) {
  const request = message?.request;
  const responseControl = {
    schema_version: 'vibapp.preview-control.experimental-v1',
    user_id: message?.control?.user_id || 'invalid-user-id',
    app_id: message?.control?.app_id || 'invalid.app',
    artifact_digest: message?.control?.artifact_digest || '0'.repeat(64),
    generation_id: message?.control?.generation_id || 'invalid-generation-id',
    session_id: message?.control?.session_id || 'invalid-session-id',
    view_revision: message?.control?.view_revision || 1,
    event_id: message?.control?.event_id || 'invalid-event-id',
    channel_id: boundControl.channel_id,
    nonce: boundControl.nonce,
    request_id: message?.control?.request_id || 'invalid-request-id',
    sequence: 2,
    kind: 'launch-result',
  };
  try {
    verifyLaunchControl(message?.control, request, {
      channel_id: boundControl.channel_id,
      nonce: boundControl.nonce,
      sequence: 1,
      kind: 'launch',
    });
    await verifyLaunchEnvelope(request);
    const values = new Map();
    for (const descriptor of request.binding.files) {
      const url = new URL(descriptor.path, self.location.origin);
      if (url.origin !== self.location.origin || !url.pathname.startsWith('/launcher/components/')) {
        throw new Error('integrity-failure:derivation-file-origin');
      }
      const response = await fetch(url, { cache: 'no-store', credentials: 'omit', redirect: 'error' });
      if (!response.ok) throw new Error('integrity-failure:derivation-file-unavailable');
      values.set(descriptor.path, await response.arrayBuffer());
    }
    await verifyDerivationFileSet(request.binding, values);
    const attestationUrl = new URL(request.binding.attestation.artifact.path, self.location.origin);
    if (attestationUrl.origin !== self.location.origin || !attestationUrl.pathname.startsWith('/launcher/components/')) {
      throw new Error('integrity-failure:attestation-origin');
    }
    const attestationResponse = await fetch(attestationUrl, { cache: 'no-store', credentials: 'omit', redirect: 'error' });
    if (!attestationResponse.ok) throw new Error('integrity-failure:attestation-unavailable');
    await verifyDerivationAttestation(request.binding, await attestationResponse.arrayBuffer());
    const entryUrl = new URL(request.entry_path, self.location.origin);
    if (entryUrl.origin !== self.location.origin || entryUrl.pathname !== request.binding.entry.path) {
      throw new Error('integrity-failure:entry-origin');
    }
    const component = await import(entryUrl.href);
    if (!component.guest || typeof component.guest.describe !== 'function' || typeof component.guest.handleEvent !== 'function') {
      throw new Error('malformed-output:guest-export');
    }
    const descriptor = component.guest.describe();
    const output = component.guest.handleEvent({
      eventId: 'web-event-' + request.session,
      idempotencyKey: 'web-idempotency-' + request.session,
      cancellation: 'web-cancellation-' + request.session,
      generation: 'web-generation-' + request.binding.canonical_component.sha256.slice(0, 24),
      profile: 'web-preview',
      deadlineMonotonicMs: BigInt(Math.floor(performance.now() + 2_000)),
    }, {
      tag: 'launcher',
      val: {
        tag: 'launch',
        val: {
          entrypoint: 'main',
          session: request.session,
          surface: 'surface-main',
          route: 'home',
          reason: 'deep-link',
        },
      },
    });
    const verified = verifyGuestLaunchOutput(request, descriptor, output);
    let currentSurface = verified.surface;
    const runtimeBinding = foregroundBinding(request, currentSurface);
    bindForegroundWorker(port, runtimeBinding, message => {
      let launcherEvent;
      if (message.operation === 'refresh') {
        launcherEvent = { tag: 'open', val: { entrypoint: 'main', session: request.session, surface: runtimeBinding.surface, route: runtimeBinding.route, reason: 'restore' } };
      } else {
        const actions = currentSurface.view.nodes.flatMap(node => node.kind.tag === 'button' && !node.kind.val.disabled ? [node.kind.val.action]
          : node.kind.tag === 'confirmation' ? [node.kind.val.confirmAction, node.kind.val.cancelAction] : []);
        if (!actions.includes(message.action) || !Array.isArray(message.fields) || message.fields.length > 64) throw new Error('foreground-action-not-advertised');
        const definitions = currentSurface.view.nodes.filter(node => node.kind.tag === 'field').map(node => node.kind.val);
        const fields = foregroundFields(definitions, message.fields);
        launcherEvent = { tag: 'action', val: { session: request.session, surface: runtimeBinding.surface, route: runtimeBinding.route, action: message.action, fields } };
      }
      const update = component.guest.handleEvent({
        eventId: message.event_id, idempotencyKey: message.event_id, cancellation: message.event_id,
        generation: runtimeBinding.generation, profile: 'web-preview', deadlineMonotonicMs: BigInt(Math.floor(performance.now() + 2000)),
      }, { tag: 'launcher', val: launcherEvent });
      if (Array.isArray(update?.surfaces) && update.surfaces.length === 0 && message.operation === 'refresh') {
        return { surface: normalizeSurface(currentSurface), runtime_binding: runtimeBinding };
      }
      const checked = verifyGuestLaunchOutput(request, descriptor, update);
      if (checked.surface.surface !== runtimeBinding.surface || checked.surface.route !== runtimeBinding.route) throw new Error('foreground-surface-binding-mismatch');
      currentSurface = checked.surface;
      return { surface: normalizeSurface(currentSurface), runtime_binding: runtimeBinding };
    }, () => self.close());
    port.postMessage({
      control: responseControl,
      ok: true,
      result: {
        schema_version: 'vibapp.runtime-launch.experimental.v1',
        runtime_process: 'browser-jco-component-worker',
        package_digest_sha256: request.binding.canonical_package_digest_sha256,
        component_sha256: request.binding.canonical_component.sha256,
        browser_artifact_sha256: request.binding.entry.sha256,
        derivation_binding: {
          derived_from_sha256: request.binding.derived_from_sha256,
          binding_payload_sha256: request.binding.attestation.binding_payload_sha256,
          verification_state: request.binding.attestation.verification_state,
          canonical_component_transformation_proven: true,
          stage0_activation_eligible: false,
        },
        descriptor: {
          id: descriptor.id,
          display_name: descriptor.displayName,
          version: descriptor.version,
          kind: descriptor.kind,
        },
        isolation: {
          separate_process: false,
          dedicated_worker: true,
          ambient_wasi_linked: false,
          dom_available: false,
          install_authority: false,
          background_reliability: 'foreground-only',
        },
        capabilities: [
          { interface: 'clock', availability: 'brokered', simulated: false },
          { interface: 'host-info', availability: 'brokered', simulated: false },
          { interface: 'kv', availability: 'mock', simulated: true },
          { interface: 'log', availability: 'mock', simulated: true },
          { interface: 'settings', availability: 'mock', simulated: true },
        ],
        surface: normalizeSurface(verified.surface),
        runtime_binding: runtimeBinding,
        launcher_context: {
          ecosystem: 'vibapp-client',
          mode: 'isolated-preview',
          installed: false,
          foreground_interactive: true,
          host_chrome_owned: true,
          layout_policy: 'host-responsive',
          size_classes: ['compact', 'regular', 'wide'],
          stage0_activation_eligible: false,
        },
      },
    });
  } catch (error) {
    const reason = classifyInvalidBootstrapError(error);
    if (reason) reportInvalidBootstrap(reason, 'app-worker');
    port.postMessage({ control: responseControl, ok: false, error: error instanceof Error ? error.message : 'internal' });
    port.close();
    self.close();
  }
}

const PORT_BIND_KEYS = ['channel_id', 'nonce', 'port', 'schema_version'].join(',');
let portBound = false;

function bindPreviewPort(event) {
  const binding = event.data;
  if (portBound) {
    reportInvalidBootstrap('replay', 'app-worker');
    return;
  }
  const objectBinding = binding && typeof binding === 'object' && !Array.isArray(binding);
  const exactKeys = objectBinding && Object.keys(binding).sort().join(',') === PORT_BIND_KEYS;
  if (
    !exactKeys
    || binding?.schema_version !== 'vibapp.preview-port-bind.experimental-v1'
    || typeof binding.channel_id !== 'string'
    || typeof binding.nonce !== 'string'
    || !(binding.port instanceof MessagePort)
  ) {
    reportInvalidBootstrap(
      objectBinding && Object.keys(binding).some(key => !PORT_BIND_KEYS.split(',').includes(key)) ? 'extra-authority' : 'tamper',
      'app-worker',
    );
    return;
  }
  portBound = true;
  const port = binding.port;
  let consumed = false;
  port.onmessage = messageEvent => {
    if (consumed) {
      reportInvalidBootstrap('replay', 'app-worker');
      port.close();
      return;
    }
    consumed = true;
    void runLaunch(port, binding, messageEvent.data);
  };
  port.start();
}

self.addEventListener('message', bindPreviewPort);
