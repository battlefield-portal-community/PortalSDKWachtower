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

function formatMB(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(2);
}

function renderHtml(mapping: Mapping): string {
  const rows = (mapping.versions ?? [])
    .map((entry) => {
      const key = escapeHtml(entry.key);
      return `
                <tr>
                    <td><a href="${key}">${key}</a></td>
                    <td>${formatMB(entry.fileSize)}</td>
                    <td>${escapeHtml(entry.lastModified)}</td>
                </tr>
        `;
    })
    .join("");

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
        </style>
    </head>
    <body>
        <h1>gala like to hoard</h1>
        <table>
            <thead>
                <tr>
                    <th>Key</th>
                    <th>Size (MB)</th>
                    <th>Last Modified</th>
                </tr>
            </thead>
            <tbody>
    ${rows}
            </tbody>
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
