# onoats.dev

Static landing page served by Cloudflare Workers static assets. No build step.

- Content: `public/` (hand-written HTML/CSS, no JS). Images are copies of
  `assets/brand` and `assets/screenshots`; refresh them when those change.
- Commands (run from `site/`, needs [`just`](https://github.com/casey/just)):
  `just dev` (plain static preview), `just preview` (`wrangler dev`, serves
  `_headers` and the 404 page like Cloudflare), `just prompts` / `just check`
  (regenerate / verify the prompts page), `just dry-run` (validate the Worker
  config without publishing), `just deploy` (publish; reads
  `~/.secrets/cf.env` unless `CLOUDFLARE_API_TOKEN` is already set).
- The token needs Workers Scripts edit, plus Workers Routes and DNS edit on the
  `onoats.dev` zone for the custom domain.

## Prompts page

`public/prompts/*.txt` are the source of truth. `public/prompts/index.html`
inlines each one between `<!-- prompt:NAME -->` markers. After editing a prompt:

    python3 site/scripts/build_prompts.py          # rewrite the page
    python3 site/scripts/build_prompts.py --check  # exit 1 if stale

`site.css` holds everything shared by the home page, `/prompts/` and `404.html`.
