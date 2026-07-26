/**
 * Cloudflare Worker for hoard.bfportal.gg
 *
 * - GET /            -> render the SDK index table from versions.json (in R2)
 * - GET /index.html  -> 301 redirect to / (canonical root)
 * - GET /<key>       -> stream the matching object from R2 (the SDK zips),
 *                       with HTTP Range support for resumable large downloads
 */

export interface Env {
  BUCKET: R2Bucket;
}

interface VersionEntry {
  version: string;
  key: string;
  fileSize: number;
  lastModified: string;
}

interface Mapping {
  versions: VersionEntry[];
}

const MAPPING_KEY = "versions.json";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatSize(bytes: number): string {
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  return `${(bytes / 1024 ** 2).toFixed(2)} MB`;
}

/** Strip the directory prefix so the row shows just the archive name. */
function basename(key: string): string {
  const parts = key.split("/");
  return parts[parts.length - 1] || key;
}

/** Compare dotted versions numerically so 1.10.0 sorts above 1.9.0. */
function compareVersions(a: string, b: string): number {
  const left = a.split(".");
  const right = b.split(".");
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const x = Number(left[i] ?? 0);
    const y = Number(right[i] ?? 0);
    if (Number.isNaN(x) || Number.isNaN(y)) return 0;
    if (x !== y) return x - y;
  }
  return 0;
}

/**
 * Newest first. lastModified is a fixed "YYYY-MM-DD HH:MM:SS" layout, so a
 * plain string compare is already chronological; equal stamps fall back to
 * the version number.
 */
function newestFirst(versions: VersionEntry[]): VersionEntry[] {
  return [...versions].sort((a, b) => {
    const byTime = (b.lastModified ?? "").localeCompare(a.lastModified ?? "");
    if (byTime !== 0) return byTime;
    return compareVersions(b.version ?? "", a.version ?? "");
  });
}

/** Signed size difference, e.g. "+1.06 GB" / "-742.19 MB". */
function formatDelta(bytes: number): string {
  if (bytes === 0) return "no change";
  const sign = bytes > 0 ? "+" : "−";
  return `${sign}${formatSize(Math.abs(bytes))}`;
}

const THEME_CSS = `
    :root {
        color-scheme: dark;
        --bg: #05070a;
        --bg-glow-a: #0b2417;
        --bg-glow-b: #0a1622;
        --panel: rgba(12, 18, 22, 0.72);
        --line: rgba(184, 255, 47, 0.14);
        --line-soft: rgba(184, 255, 47, 0.06);
        --text: #cfe3d4;
        --text-dim: #6f8479;
        --accent: #b8ff2f;
        --accent-2: #4ad9ff;
        --up: #6ee787;
        --down: #ff6b5e;
        --row-hover: rgba(184, 255, 47, 0.05);
        --scan: rgba(190, 255, 160, 0.028);
        --glow: 0 0 12px rgba(184, 255, 47, 0.45);
    }

    :root[data-theme="light"] {
        color-scheme: light;
        --bg: #eef1e9;
        --bg-glow-a: #dbe6cf;
        --bg-glow-b: #d7e3e8;
        --panel: rgba(255, 255, 255, 0.7);
        --line: rgba(24, 46, 20, 0.16);
        --line-soft: rgba(24, 46, 20, 0.07);
        --text: #1d2a1e;
        --text-dim: #5d6c5c;
        --accent: #3d7a00;
        --accent-2: #00688c;
        --up: #1f7a33;
        --down: #b3261e;
        --row-hover: rgba(61, 122, 0, 0.07);
        --scan: rgba(24, 46, 20, 0.022);
        --glow: 0 0 10px rgba(61, 122, 0, 0.25);
    }

    * { box-sizing: border-box; }

    body {
        margin: 0;
        min-height: 100vh;
        padding: clamp(1.25rem, 4vw, 3.5rem) clamp(0.75rem, 4vw, 3rem);
        background:
            radial-gradient(1200px 600px at 12% -10%, var(--bg-glow-a), transparent 65%),
            radial-gradient(900px 500px at 92% 8%, var(--bg-glow-b), transparent 60%),
            var(--bg);
        color: var(--text);
        font-family: ui-monospace, "JetBrains Mono", "SFMono-Regular", "SF Mono", Menlo, Consolas, monospace;
        font-size: 14px;
        line-height: 1.55;
        letter-spacing: 0.01em;
    }

    /* CRT scanlines + a slow sweep, both purely decorative. */
    body::before, body::after {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        z-index: 9;
    }
    body::before {
        background: repeating-linear-gradient(to bottom, var(--scan) 0 1px, transparent 1px 3px);
        mix-blend-mode: screen;
    }
    body::after {
        background: linear-gradient(to bottom, transparent, var(--scan), transparent);
        height: 45vh;
        inset: auto 0 auto 0;
        animation: sweep 9s linear infinite;
        opacity: 0.9;
    }
    @keyframes sweep {
        from { transform: translateY(-50vh); }
        to   { transform: translateY(150vh); }
    }

    .wrap { max-width: 1080px; margin: 0 auto; position: relative; z-index: 1; }

    header {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-end;
        gap: 1rem 1.5rem;
        padding-bottom: 1.1rem;
        border-bottom: 1px solid var(--line);
    }

    .brand { flex: 1 1 320px; min-width: 0; }

    .eyebrow {
        margin: 0;
        font-size: 0.7rem;
        letter-spacing: 0.34em;
        text-transform: uppercase;
        color: var(--text-dim);
    }

    h1 {
        margin: 0.35rem 0 0;
        font-size: clamp(1.5rem, 5vw, 2.35rem);
        font-weight: 600;
        letter-spacing: -0.01em;
        color: var(--accent);
        text-shadow: var(--glow);
    }

    h1 .cursor {
        display: inline-block;
        width: 0.55em;
        height: 1.05em;
        margin-left: 0.15em;
        vertical-align: -0.15em;
        background: var(--accent);
        box-shadow: var(--glow);
        animation: blink 1.1s steps(1) infinite;
    }
    @keyframes blink { 50% { opacity: 0; } }

    .tagline { margin: 0.45rem 0 0; color: var(--text-dim); font-size: 0.82rem; }

    .stats { display: flex; gap: 1.5rem; flex-wrap: wrap; }
    .stat { min-width: 5.5rem; }
    .stat b {
        display: block;
        font-size: 1.15rem;
        font-weight: 600;
        color: var(--text);
    }
    .stat span {
        font-size: 0.65rem;
        letter-spacing: 0.2em;
        text-transform: uppercase;
        color: var(--text-dim);
    }

    #theme {
        appearance: none;
        cursor: pointer;
        font: inherit;
        font-size: 0.68rem;
        letter-spacing: 0.22em;
        text-transform: uppercase;
        color: var(--text-dim);
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 0.42rem 0.9rem;
        transition: color 0.18s, border-color 0.18s, box-shadow 0.18s;
    }
    #theme:hover, #theme:focus-visible {
        color: var(--accent);
        border-color: var(--accent);
        box-shadow: var(--glow);
        outline: none;
    }

    .panel {
        margin-top: 1.6rem;
        border: 1px solid var(--line);
        background: var(--panel);
        backdrop-filter: blur(3px);
        overflow-x: auto;
    }

    /* Fixed proportions: spare width is spread across all five columns
       instead of pooling into one gap between "archive" and "size". */
    table {
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
        /* Wide enough that "1.3.3.0 LATEST" still fits the ver column. */
        min-width: 800px;
    }
    .ver     { width: 20%; }
    .archive { width: 29%; }
    .size    { width: 13%; }
    .delta   { width: 15%; }
    .ts      { width: 23%; }

    thead th {
        position: sticky;
        top: 0;
        background: var(--panel);
        backdrop-filter: blur(6px);
        text-align: left;
        font-weight: 500;
        font-size: 0.64rem;
        letter-spacing: 0.24em;
        text-transform: uppercase;
        color: var(--text-dim);
        padding: 0.85rem 1rem;
        border-bottom: 1px solid var(--line);
        white-space: nowrap;
    }

    tbody td {
        padding: 0.7rem 1rem;
        border-bottom: 1px solid var(--line-soft);
        vertical-align: middle;
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr { transition: background 0.15s; }
    tbody tr:hover { background: var(--row-hover); }

    .ver {
        position: relative;
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
        color: var(--text);
    }
    .ver::before {
        content: "";
        position: absolute;
        left: 0;
        top: 12%;
        bottom: 12%;
        width: 2px;
        background: var(--accent);
        opacity: 0;
        transition: opacity 0.15s;
    }
    tbody tr:hover .ver::before { opacity: 1; }

    .tag {
        margin-left: 0.55rem;
        font-size: 0.58rem;
        letter-spacing: 0.16em;
        padding: 0.1rem 0.35rem;
        border: 1px solid var(--accent-2);
        color: var(--accent-2);
        border-radius: 2px;
        vertical-align: 0.1em;
    }

    a {
        color: var(--text);
        text-decoration: none;
        border-bottom: 1px dashed var(--line);
        word-break: break-all;
    }
    a:hover, a:focus-visible {
        color: var(--accent);
        border-bottom-color: var(--accent);
        text-shadow: var(--glow);
        outline: none;
    }

    /* Right-aligned so the decimal points line up; headers follow the values.
       The extra right padding keeps them off the next column's content. */
    .size, .delta {
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
        text-align: right;
        padding-right: 1.6rem;
    }
    .delta { color: var(--text-dim); }
    .delta[data-dir="up"] { color: var(--up); }
    .delta[data-dir="down"] { color: var(--down); }
    .delta[data-dir="none"] { opacity: 0.55; }

    .ts { white-space: nowrap; color: var(--text-dim); font-variant-numeric: tabular-nums; }

    .empty { padding: 2.5rem 1rem; text-align: center; color: var(--text-dim); }

    footer {
        margin-top: 1.2rem;
        font-size: 0.68rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--text-dim);
    }

    @media (prefers-reduced-motion: reduce) {
        body::after { animation: none; display: none; }
        h1 .cursor { animation: none; }
    }

    @media (max-width: 560px) {
        .stats { gap: 1.1rem; }
        .stat b { font-size: 1rem; }
    }
`;

// Runs before paint so the stored choice never flashes the wrong theme.
const THEME_BOOT = `
    (function () {
        try {
            var t = localStorage.getItem("hoard-theme");
            if (t === "light") document.documentElement.dataset.theme = "light";
        } catch (e) {}
    })();
`;

const THEME_TOGGLE = `
    (function () {
        var btn = document.getElementById("theme");
        if (!btn) return;
        var root = document.documentElement;
        function label() {
            btn.textContent = root.dataset.theme === "light" ? "daylight" : "phosphor";
        }
        label();
        btn.addEventListener("click", function () {
            var light = root.dataset.theme !== "light";
            if (light) root.dataset.theme = "light";
            else root.removeAttribute("data-theme");
            try { localStorage.setItem("hoard-theme", light ? "light" : "dark"); } catch (e) {}
            label();
        });
    })();
`;

function renderHtml(mapping: Mapping): string {
  const versions = newestFirst(mapping.versions ?? []);
  const totalSize = versions.reduce((sum, e) => sum + (e.fileSize || 0), 0);
  // Sorted newest first, so the top row is the newest build.
  const latestKey = versions.length ? versions[0].key : null;

  const rows = versions
    .map((entry, i) => {
      const key = escapeHtml(entry.key);
      const name = escapeHtml(basename(entry.key));
      const version = escapeHtml(entry.version ?? "—");
      const tag =
        entry.key === latestKey ? `<span class="tag">LATEST</span>` : "";

      // Rows are newest-first, so the previous release is the row below.
      const prev = versions[i + 1];
      let delta = `<td class="delta" data-dir="none">&mdash;</td>`;
      if (prev) {
        const diff = entry.fileSize - prev.fileSize;
        const dir = diff > 0 ? "up" : diff < 0 ? "down" : "none";
        const pct = prev.fileSize
          ? `${diff >= 0 ? "+" : "−"}${Math.abs((diff / prev.fileSize) * 100).toFixed(1)}%`
          : "";
        const title = escapeHtml(
          `${pct} vs ${prev.version ?? basename(prev.key)}`,
        );
        delta = `<td class="delta" data-dir="${dir}" title="${title}">${formatDelta(diff)}</td>`;
      }

      return `
                <tr>
                    <td class="ver">${version}${tag}</td>
                    <td class="archive"><a href="${key}" download>${name}</a></td>
                    <td class="size">${formatSize(entry.fileSize)}</td>
                    ${delta}
                    <td class="ts">${escapeHtml(entry.lastModified)}</td>
                </tr>`;
    })
    .join("");

  const body = rows
    ? `<tbody>${rows}</tbody>`
    : `<tbody><tr><td class="empty" colspan="5">no archives in the hoard</td></tr></tbody>`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>bfportal: Hoarding</title>
    <meta name="description" content="Archived Battlefield Portal SDK builds, mirrored on hoard.bfportal.gg">
    <script>${THEME_BOOT}</script>
    <style>${THEME_CSS}</style>
</head>
<body>
    <div class="wrap">
        <header>
            <div class="brand">
                <p class="eyebrow">hoard.bfportal.gg</p>
                <h1>gala like to hoard<span class="cursor"></span></h1>
                <p class="tagline">every Portal SDK build we caught on the way past. range requests supported &mdash; resume away.</p>
            </div>
            <div class="stats">
                <div class="stat"><b>${versions.length}</b><span>builds</span></div>
                <div class="stat"><b>${formatSize(totalSize)}</b><span>hoarded</span></div>
            </div>
            <button id="theme" type="button" title="toggle theme">phosphor</button>
        </header>

        <div class="panel">
            <table>
                <thead>
                    <tr>
                        <th class="ver">ver</th>
                        <th class="archive">archive</th>
                        <th class="size">size</th>
                        <th class="delta" title="change vs the previous release">&Delta; prev</th>
                        <th class="ts">last modified (utc) &darr;</th>
                    </tr>
                </thead>
${body}
            </table>
        </div>

        <footer>// wachtower is watching</footer>
    </div>
    <script>${THEME_TOGGLE}</script>
</body>
</html>`;
}

/** Build a Content-Range header value from R2's parsed range. */
function contentRange(range: R2Range, size: number): string {
  let start: number;
  let end: number;
  if ("suffix" in range) {
    start = size - range.suffix;
    end = size - 1;
  } else {
    start = range.offset ?? 0;
    end = range.length !== undefined ? start + range.length - 1 : size - 1;
  }
  return `bytes ${start}-${end}/${size}`;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const pathname = decodeURIComponent(url.pathname);

    // Canonicalize /index.html -> /
    if (pathname === "/index.html") {
      return Response.redirect(new URL("/", url).toString(), 301);
    }

    // Index page rendered from the bucket mapping.
    if (pathname === "/") {
      const obj = await env.BUCKET.get(MAPPING_KEY);
      let mapping: Mapping = { versions: [] };
      if (obj) {
        try {
          mapping = (await obj.json()) as Mapping;
        } catch {
          // Malformed mapping -> render empty table rather than 500.
          mapping = { versions: [] };
        }
      }
      return new Response(renderHtml(mapping), {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "public, max-age=60",
        },
      });
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const key = pathname.replace(/^\/+/, "");
    if (!key) {
      return new Response("Not Found", { status: 404 });
    }

    // HEAD: metadata only, no body.
    if (request.method === "HEAD") {
      const head = await env.BUCKET.head(key);
      if (!head) return new Response("Not Found", { status: 404 });
      const headers = new Headers();
      head.writeHttpMetadata(headers);
      headers.set("etag", head.httpEtag);
      headers.set("accept-ranges", "bytes");
      headers.set("content-length", head.size.toString());
      return new Response(null, { headers });
    }

    const object = await env.BUCKET.get(key, {
      range: request.headers,
      onlyIf: request.headers,
    });

    if (object === null) {
      return new Response("Not Found", { status: 404 });
    }

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    headers.set("accept-ranges", "bytes");

    // No body => an onlyIf precondition failed (e.g. If-None-Match matched).
    if (!("body" in object)) {
      return new Response(null, { status: 304, headers });
    }

    const body = object as R2ObjectBody;
    let status = 200;
    if (request.headers.has("range") && body.range) {
      headers.set("content-range", contentRange(body.range, body.size));
      status = 206;
    }

    return new Response(body.body, { status, headers });
  },
};
