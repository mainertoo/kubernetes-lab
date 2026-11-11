"""
MkDocs build hook: publish Wiki.js-uploaded images automatically.

Wiki.js stores uploaded images at the REPO ROOT and links them with an absolute
path (e.g. `![](/screenshot_x.png)`). MkDocs only publishes files inside its
docs_dir, so those root images would 404 on the built site. This hook copies any
root-level asset that a guides page references by absolute path into the built
site at the same path — so the Wiki.js default behaviour "just works" and nobody
has to move files by hand.

Authors: just upload + insert the image in Wiki.js as usual. No path edits needed.
(Relative refs like `img/foo.png` next to a page also work natively — those don't
need this hook.)

Safe by design: only assets actually referenced by an absolute `/...` link are
copied, so unrelated/older root images are never exposed on the public site. Any
error is swallowed so a hook problem can never break the build.
"""
import glob
import os
import re
import shutil

# Markdown `](/foo.png)` and HTML `src="/foo.png"` absolute-path image refs.
_PATTERNS = [
    re.compile(r"]\(\s*/([^)\s]+\.(?:png|jpe?g|gif|webp|svg))\s*\)", re.IGNORECASE),
    re.compile(r"""src\s*=\s*["']/([^"'\s]+\.(?:png|jpe?g|gif|webp|svg))["']""", re.IGNORECASE),
]


def on_post_build(config, **kwargs):
    try:
        site_dir = config["site_dir"]
        docs_dir = config["docs_dir"]
        # Repo root = two levels above docs_dir (repo/guides/docs -> repo/). The
        # config file itself lives in a ConfigMap, outside the repo checkout.
        repo_root = os.path.realpath(os.path.join(docs_dir, "..", ".."))

        referenced = set()
        for md in glob.glob(os.path.join(docs_dir, "**", "*.md"), recursive=True):
            try:
                with open(md, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            for pat in _PATTERNS:
                for m in pat.finditer(text):
                    referenced.add(m.group(1))

        copied = 0
        for rel in referenced:
            src = os.path.normpath(os.path.join(repo_root, rel))
            # Stay inside the repo; ignore anything mkdocs already published.
            if not src.startswith(repo_root) or not os.path.isfile(src):
                continue
            dst = os.path.join(site_dir, rel)
            if os.path.exists(dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        if copied:
            print(f"[stage_assets] published {copied} root-uploaded image(s) to the site")
    except Exception as exc:  # never break the build over an asset copy
        print(f"[stage_assets] skipped (non-fatal): {exc}")
