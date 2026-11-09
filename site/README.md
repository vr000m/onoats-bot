# onoats.dev

Static landing page served by Cloudflare Workers static assets. The published
site has no runtime build step or JavaScript.

- Content: `public/` (hand-written HTML/CSS, no JS).
- Toolchain: Node 24 (`.node-version`), npm 11.19 (`packageManager`), and
  [`just`](https://github.com/casey/just). `just deps` installs the exact
  `package-lock.json` graph with lifecycle scripts disabled through a
  deny-by-default environment that passes only the process settings npm needs.
- Commands (run from `site/`):
  `just dev` (plain static preview), `just preview` (`wrangler dev`, serves
  `_headers` and the 404 page like Cloudflare), `just prompts` (regenerate the
  prompts page), `just check` (run `build_prompts.py --check` and
  `check_site.py`), `just dry-run` (validate the Worker config without
  publishing), `just deploy` (publish; reads `~/.secrets/cf.env` unless
  `CLOUDFLARE_API_TOKEN` is already set).
- Wrangler uploads every file under `public/` except those listed in
  `public/.assetsignore`, which keeps local dotfiles such as `.DS_Store` off the
  site.
- The token needs Workers Scripts edit, plus Workers Routes and DNS edit on the
  `onoats.dev` zone for the custom domain.

## Prompts page

`public/prompts/*.txt` are the source of truth. `public/prompts/index.html`
inlines each one between `<!-- prompt:NAME -->` markers. After editing a prompt:

    python3 scripts/build_prompts.py          # rewrite the page
    python3 scripts/build_prompts.py --check  # exit 1 if stale

Both modes also fail when a prompt breaks the untrusted-data rules. A
placeholder that carries transcript text, or text derived from it, goes alone
on its own line between `--- BEGIN UNTRUSTED <LABEL> ---` and
`--- END UNTRUSTED <LABEL> ---`, after the `## Instruction boundary`
paragraph. A new placeholder must be added to `TRUSTED_FIELDS` or
`UNTRUSTED_FIELDS` in the script, and the page must list it.

`site.css` holds everything shared by the pages.

## Site checks

`scripts/check_site.py` owns every site contract the prompts script does not:
shared navigation and footer on every `*.html` page (with `aria-current="page"`
only on the link to the page itself), the copied assets, versioned image names
and `/img/` references, the social card named by `og:image`, the
`public/.assetsignore` rule, and the `_headers` rules (security headers and the
Content-Security-Policy in `/*`, immutable caching only in `/img/*`). The exact
values live in its module docstring and constants, not here.

## Images

Every file under `public/img/` has a release version in its filename because
Cloudflare serves that directory with a one-year immutable cache policy.
`scripts/check_site.py` rejects unversioned filenames and references.

Screenshots are quantized with `pngquant --quality=80-95 --strip` before they are
committed (roughly 85% smaller, no visible change). Do the same to a new one.

The mark, favicon and two published screenshots are byte-for-byte copies of
their declared sources under `../assets/brand` and `../assets/screenshots`.
`just check` verifies them. Copy a new release snapshot under a new versioned
name instead of replacing a cached file.

The `service-toolbar-*-0.9.0.png` screenshots on `/local-models/` are copies from
`docs/images/` in `vr000m/pipecat-local-tts-server`; the number is that
project's version when they were copied, and `check_site.py` cannot compare them
with a source in this repository.

`scripts/render_social_card.swift` is the source owner for the 1200x630 social
preview. It reads `../assets/brand/onoats-mark.svg`, renders exactly 1200x630
pixels (independent of the display's scale) and quantizes the result with
`pngquant` (`brew install pngquant`), about 40 KB instead of 700 KB, because link
previews skip large images. Run these commands from `site/` after changing its
copy or design:

    swift scripts/render_social_card.swift
    swift scripts/render_social_card.swift --check

A published card is cached as immutable, so bump the version in its filename when
the artwork changes. Change the name in the renderer's `outputURL` and in both `og:image`
and `twitter:image` in `public/index.html` in the same commit;
`scripts/check_site.py` fails when they disagree.
