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

function renderHtml(mapping: Mapping): string {
  const versions = newestFirst(mapping.versions ?? []);
  // Sorted newest first, so the top row is the newest build.
  const latestKey = versions.length ? versions[0].key : null;

  const rows = versions
    .map((entry, i) => {
      const key = escapeHtml(entry.key);
      const name = escapeHtml(basename(entry.key));
      const version = escapeHtml(entry.version ?? "—");
      const tag = entry.key === latestKey ? `<span class="tag">LATEST</span>` : "";

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

  return `
    <!DOCTYPE html>
    <html>
    <head>
        <title>bfportal: Hoarding</title>
        <style>
            body { font-family: sans-serif; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            tr:nth-child(even) { background-color: #f2f2f2; }
            a { text-decoration: none; }
            a:hover { text-decoration: underline; }
            .size, .delta { text-align: right; }
            .delta[data-dir="up"] { color: #1f7a33; }
            .delta[data-dir="down"] { color: #b3261e; }
            .delta[data-dir="none"] { opacity: 0.55; }
        </style>
    </head>
    <body>
        <h1>gala like to hoard</h1>
        <table>
            <thead>
                <tr>
                    <th class="ver">Ver</th>
                    <th class="archive">Archive</th>
                    <th class="size">Size</th>
                    <th class="delta" title="change vs the previous release">&Delta; prev</th>
                    <th class="ts">Last Modified (UTC) &darr;</th>
                </tr>
            </thead>
    ${body}
        </table>
    </body>
    </html>
    `;
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
