# PortalSDKWachtower

Watches the Battlefield Portal SDK for new versions, pings Discord, uploads the
new SDK to an R2 bucket, and updates the `versions.json` mapping. The index page
at [hoard.bfportal.gg](https://hoard.bfportal.gg) is served by a Cloudflare Worker
that renders that mapping and streams the SDK downloads from R2.

## Deploying the Cloudflare Worker (from local)

The Worker lives in [`cf-worker/`](cf-worker/).

### Prerequisites

- Node.js 18+ and npm
- A Cloudflare API token with these permissions (see token notes below):
  - Account · **Workers Scripts**: Edit
  - Account · **Account Settings**: Read
  - Zone (`bfportal.gg`) · **Workers Routes**: Edit
  - Zone (`bfportal.gg`) · **DNS**: Edit  *(required because the Worker uses a custom domain)*

### Steps

```sh
cd cf-worker

# 1. Install dependencies
npm install

# 2. Authenticate. Either log in interactively...
npx wrangler login
#    ...or export a scoped API token (preferred for CI / headless):
export CLOUDFLARE_API_TOKEN=<your-token>

# 3. Test locally against the real bucket before shipping
npx wrangler dev --remote
#    Then open the printed URL: '/' should render the table and a
#    '/SDKs/PortalSDK-vX.Y.Z.W.zip' link should download.

# 4. Deploy
npx wrangler deploy
```

`npm run dev` / `npm run deploy` / `npm run typecheck` are also wired in
`package.json`.

### Configuration

Edit [`cf-worker/wrangler.toml`](cf-worker/wrangler.toml):

- `bucket_name` — the R2 bucket holding the SDK zips and `versions.json`.
- `routes` — the custom domain (`hoard.bfportal.gg`). On first deploy this
  registers the Worker on that hostname; a Worker route and an R2 custom domain
  **cannot coexist on the same hostname**, so remove the R2 custom domain from
  `hoard.bfportal.gg` first.

The Worker expects a `versions.json` object in the bucket:

```json
{
  "versions": [
    { "version": "1.1.3.0", "key": "SDKs/PortalSDK-v1.1.3.0.zip",
      "fileSize": 6528632708, "lastModified": "2025-12-09 22:37:47" }
  ]
}
```

If it's absent the Worker renders an empty table. The watchtower appends to it
automatically on each new release.

### CI deploy

Pushes to `main` that touch `cf-worker/**` deploy the Worker automatically via
[`.github/workflows/deploy-worker.yml`](.github/workflows/deploy-worker.yml).
Set a `CLOUDFLARE_API_TOKEN` repo secret with the permissions listed above.

## Running the watchtower

The watchtower is the [`app`](src/app/) package. Copy `.env.example` to `.env`,
fill in the Discord webhook and `R2_*` credentials (S3 access key / secret for
the bucket), then run it either in Docker:

```sh
docker compose up --build
```

or locally with [uv](https://docs.astral.sh/uv/):

```sh
uv run app
```
