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
  `CLOUDFLARE_API_TOKEN` is already set, and gives Wrangler only the token,
  account id, `HOME`, `PATH` and `TMPDIR`, never on a command line).
- Wrangler uploads every file under `public/` except those listed in
  `public/.assetsignore`, which keeps local dotfiles such as `.DS_Store` off the
  site.
- The token needs Workers Scripts edit, plus Workers Routes and DNS edit on the
  `onoats.dev` zone for the custom domain.

## Prompts page

`public/prompts/*.txt` are the source of truth. `public/prompts/index.html`
inlines each one between `<!-- prompt:NAME -->` markers, which must follow that
prompt's `<p class="meta">` paragraph in the same `<li>` (the binding rule is
in the script's docstring). `build_workflow.txt` is the exception: a task for a
coding agent, not a `str.format` template, so the template and untrusted-data
rules skip it; it sits in its own section (`#build`) and links to the seven
prompts. After editing a prompt:

    python3 scripts/build_prompts.py          # rewrite the page
    python3 scripts/build_prompts.py --check  # exit 1 if stale

Both modes also fail when a prompt breaks the untrusted-data rules. A
placeholder that carries transcript text, or text derived from it, goes alone
on its own line between `--- BEGIN UNTRUSTED <LABEL> ---` and
`--- END UNTRUSTED <LABEL> ---`, after the `## Instruction boundary`
paragraph. A new placeholder must be added to `TRUSTED_FIELDS` or
`UNTRUSTED_FIELDS` in the script, and the page must list it.

Both modes also fail when a claim the page makes about the prompts drifts on
either side: the page text changes, or the prompts stop backing it. The claims
live in tables in the script (`FACTS` per prompt, `CAPTION_FACTS` for the
diagram caption, `UNBACKED_WORDS` and `CAPTION_UNBACKED` for words the page
must not use); its module docstring lists the rules. Change the page text and
the table together. The caption numbers prompts by their place in the steps
list, so each of its step-numbered claims has its own entry that follows the
page order, not file names. The coding-agent prompt repeats those claims
(`AGENT_FACTS`) and what each prompt returns (`AGENT_RETURNS`, backed by the
prompt's `FACTS` entry), and must list the prompts in the page's step order
with the placeholders each takes. `AGENT_FACTS` also binds its own rules to the
prompts: the category check against prompt 2's list, the categories prompt 4
skips, the placeholders it asks the user about (every placeholder the prompts
take is asked, computed by the caller or built from onoats's files and earlier
replies), that prompt 5 gets prompt 2's category for each segment, and that
the category is the only model reply taken outside a fence (`MODEL_CHOSEN`).
Its Input section describes the files onoats writes; each of its claims is an
entry in the `AGENT_INPUT` table in `tests/test_site_contract.py`, checked by
recording a session with the recorder's writers and running `onoats convert`
on it, or by resolving the data folder through onoats's own code. Edit the
section and that table together. When you rename a prompt, move its entries
in every `PROMPT_TABLES` table, and its line in `build_workflow.txt`.

The pipeline diagram at the top of the page is hand-drawn inline SVG, in two
copies: a wide one, and a single-column one that replaces it on narrow screens.
A change to the pipeline means editing both, by hand: no check compares the two
copies, or the step numbers drawn in them with the steps list. `check_site.py` checks their `#fragment` links, and `build_prompts.py`
checks the caption.

`site.css` holds everything shared by the pages.

## Site checks

`scripts/check_site.py` owns every site contract the prompts script does not:
shared navigation and footer on every `*.html` page (with `aria-current="page"`
only on the link to the page itself), the copied assets, versioned image names
and `/img/` references, `#fragment` links (same-page and cross-page) against the
ids on their target page, the social card named by `og:image`, the
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
