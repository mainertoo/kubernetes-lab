---
name: wikijs-update
description: Create, update, list, fetch, move, or delete pages on the homelab Wiki.js (v3 at wiki.lab.mainertoo.com) via wiki3.py. Use whenever adding or editing wiki documentation (e.g. infrastructure/networking/*, runbooks, architecture pages). Handles auth from SOPS automatically, renders pages server-side, and does idempotent upsert-by-path.
---

# Wiki.js update

**All wiki work goes through `wiki3.py`** (in this skill's directory), against Wiki.js v3 at `https://wiki.lab.mainertoo.com` (namespace `wiki`). 

**v2 was retired on 2026-11-11** (PR #2161). Its content is preserved on `mainertoo/wiki` branch `main` (frozen), in the CNPG backups at `s3://volsync/cnpg/wiki-js-cnpg-db`, and in the off-repo `.wkbackup` export.

## Commands
```bash
SKILL=~/.claude/skills/wikijs-update
python3 $SKILL/wiki3.py list [--filter SUBSTR]                 # "<uuid>  <path> | <title>"
python3 $SKILL/wiki3.py get (--id UUID | --path PATH) [--meta]  # markdown, or metadata JSON
python3 $SKILL/wiki3.py upsert --path infrastructure/networking/foo --title "Foo" \
        --file ~/path/foo.md --tags network,dns --desc "one-line description"
cat foo.md | python3 $SKILL/wiki3.py upsert --path .../foo --title "Foo" --content-stdin
python3 $SKILL/wiki3.py move (--id UUID | --path PATH) --to infrastructure/networking/new-path
python3 $SKILL/wiki3.py delete (--id UUID | --path PATH)
python3 $SKILL/wiki3.py render (--id UUID | --path PATH)        # re-render from source
```

## Workflow
1. **Find the right page and path first.** Run `wiki3.py list --filter <term>` and match the EXISTING path scheme; do not invent a parallel section. The site uses `infrastructure/networking/` (with the trailing "-ing"); there is no `infrastructure/network/`.
2. Write the page body and upsert it.
   - Upsert is idempotent by path: an existing path is updated, a new path is created.
   - If `--tags`/`--desc` are omitted on an update, the page keeps its current values.
   - Paths are lowercased.
3. Each upsert re-renders the page on the server. v3 doesn't render markdown on save, because the browser editor normally sends the HTML. If a page ever shows blank, run `wiki3.py render`.
4. The edit reaches git within about 5 minutes (see below). There is no separate publish step and no local copy to keep.

## Auth (automatic; secrets never printed)
All secrets come from `apps/base/wiki/wiki-secret.sops.yaml` in `~/kubernetes-lab`, read with `sops`. Override the repo location with `$KLAB_REPO`.
- **Reads** use the API key `CLAUDE_API_KEY` (Administrators group, expires 2029-11-06).
- **Writes** need a user: v3 refuses API keys for save, move, delete, render and storage actions. The script logs in as `wiki-admin` (`ADMIN_EMAIL`/`ADMIN_PASS`) and uses that session, so edits are authored by "Administrator".

## Backup / git mirror
v3 pushes everything to the **PRIVATE** repo `mainertoo/wiki`, branch **`v3`**, in **push-only** mode every 5 minutes. Each page is a markdown file with YAML frontmatter, and the folder tree mirrors page paths. The mirror is the backup.
- **Push-only means the wiki always wins.** Editing `.md` files on the `v3` branch does NOT flow back into the wiki, and the next push overwrites those edits. Always edit through this tool or the UI.
- **v3 force-pushes.** Never point it at `main`, which is v2's branch.
- **`v3` is the repo's default branch.** The MkDocs guides site and the Spark `wiki-vault-mirror.sh` both read it. `main` is the frozen v2 archive.
- ⚠️ **Never commit to `v3` by hand,** not even files the wiki doesn't own: Wiki.js keeps its own history and force-pushes it, deleting them on its next push (verified 2026-11-11). The MkDocs config lives in kubernetes-lab `apps/base/mkdocs/site-config/`.
- `kubernetes-lab/docs` is a separate, curated, PUBLIC set of runbooks. It is not kept in sync with the wiki.

## v3 content rules
- **Links** are rooted paths with no locale: `/apps/media/plex`, not `/en/apps/media/plex`.
- **Uploaded files** are referenced as `/<file-name>`. v3 renders them as `/_files/<file-name>`. Imported file names use `-`, not `_`.
- **Heading anchors** are lowercase. Punctuation, `_` and emoji are dropped, runs of spaces or dashes collapse to one `-`, and there is no `h-` prefix. Example: `## Foo — bar_baz (2)` → `#foo-barbaz-2`. When linking to a section, check the target with `get` or the rendered page.
- Pages are markdown, and mermaid code fences render as diagrams.

## Conventions for this wiki
- End content pages with a `← Back to **[Section](/path)**` link, matching sibling pages.
- Section index pages (e.g. `infrastructure/networking`) list their child pages under a "Pages in this section" heading.
- Existing top-level areas:
  - `infrastructure/{hardware,networking,external-access}`
  - `backup-and-recovery`, `operations/common-incidents`
  - `apps/*`, `smart-home/*`, `ai-stack/*`
  - `house/*`, `vps`, `docs`, `archive`
