// Sites may serve uploaded static assets without applying _headers. Keep the
// resource restrictions in HTML as well, before any loadable resources.
// frame-ancestors and nosniff still require HTTP headers; do not pretend this
// fallback supplies them. Only same-origin, verified app workers may execute;
// app metadata/links do not authorize arbitrary scripts, frames or external I/O.
export const HOSTED_SHELL_RESOURCE_CSP = "default-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; worker-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";

export function prepareHostedShellHtml(source) {
  let html = source.replace(/<html lang="(en|en-US|zh-CN)">/, '<html lang="$1" data-vibapp-hosted-shell="true">');
  if (!html.includes('data-vibapp-hosted-shell="true"')) throw new Error('Shared GUI HTML transform failed');
  const policy = '<meta http-equiv="Content-Security-Policy" content="' + HOSTED_SHELL_RESOURCE_CSP + '">';
  if (html.includes(policy)) return html;
  const charset = '<meta charset="utf-8">';
  if (!html.includes(charset) || html.includes('http-equiv="Content-Security-Policy"')) {
    throw new Error('Unexpected hosted shell head');
  }
  return html.replace(charset, charset + '\n    ' + policy);
}
