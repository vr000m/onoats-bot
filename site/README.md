# onoats.dev

Static landing page served by Cloudflare Workers static assets. No build step.

- Content: `public/` (hand-written HTML/CSS, no JS). Images are copies of
  `assets/brand` and `assets/screenshots`; refresh them when those change.
- Preview: `python3 -m http.server 8799 -d public`
- Deploy: `npx wrangler deploy` from this directory, with `CLOUDFLARE_API_TOKEN`
  and `CLOUDFLARE_ACCOUNT_ID` set (Workers Scripts edit + zone Workers Routes
  edit for the `onoats.dev` custom domain).
