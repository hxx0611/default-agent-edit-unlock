# -*- coding: utf-8 -*-
"""Unlock the default agent's edit buttons in the QwenPaw console bundle.

Background (verified against qwenpaw 2.3.0):
  * Backend ``PUT /api/agents/{agentId}`` has NO default-agent guard —
    only delete / toggle / unpin are blocked server-side.
  * The console chunk ``assets/AgentGallery-*.js`` hard-disables exactly
    two edit buttons for ``id === "default"``:
      1. the card quick-action edit button
         ``title:t("common.edit"),disabled:d||n.id==="default"``
      2. the detail-drawer primary edit button
         ``disabled:s.id==="default",onClick:()=>{x(null),c(s)},...``
    Pin, drag handle, enable/disable and delete keep their guards.

Cache busting, three layers (v0.3.0):
  Asset responses carry NO Cache-Control, so browsers apply heuristic
  freshness (10% of Last-Modified age ≈ days for week-old bundles) and
  a normal F5 does not even revalidate. The HTML document IS served
  no-cache/no-store, so it is always fresh — that is the anchor.

  1. v0.2.0: every in-JS reference to the patched gallery chunk gets a
     content-hash query ``?v=<md5[:8]>`` of the PATCHED bytes.
  2. v0.3.0: the same treatment propagates one level up — references
     to each modified non-entry chunk (e.g. the Agents page chunk that
     statically imports the gallery) are busted inside their
     referencers, so a fresh entry never imports a stale page chunk.
  3. v0.3.0: an IMPORT MAP is injected into index.html (before the
     module entry script) remapping the URL of every modified chunk
     (gallery, page, entry) to ``<url>?v=<md5[:8]>`` of its current
     bytes. Import maps apply to the HTML entry script, static imports,
     dynamic imports and modulepreload alike, so ALL loaders converge
     on the same fresh URL — one module instance per chunk, no double
     instantiation of the shared entry chunk, and a browser holding a
     heuristically-fresh STOCK entry is forced onto the patched one
     without a hard refresh. Browsers without import-map support
     (Chrome <89 / Safari <16.4 / Firefox <108) fall back to layers
     1-2; a one-time hard refresh fixes them.

Originals (including precompressed .gz/.br siblings and index.html)
are backed up next to the plugin with a manifest; writes are
atomic and every patch is verified by re-reading, with rollback on
failure. Idempotent: an already-patched tree is detected and left
alone; pip upgrades that ship new hash-named bundles get patched again
on the next startup.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("qwenpaw.default_agent_edit_unlock")

PLUGIN_DIR = Path(__file__).resolve().parent
BACKUP_DIR = PLUGIN_DIR / "backups"
MANIFEST_PATH = BACKUP_DIR / "manifest.json"
KILL_SWITCH_ENV = "QWENPAW_DEFAULT_EDIT_UNLOCK"
IMPORTMAP_ID = "qwenpaw-default-edit-unlock"

# Each spec: (original pattern, replacement, patched-form probe).
# Anchored on the stable i18n key "common.edit" and surrounding
# structure; minified identifiers matched generically so a re-minified
# bundle still matches.
PATCH_SPECS = [
    # 1) card quick-action edit button: keep the outer `d` (loading)
    #    guard, drop the `|| n.id === "default"` part.
    (
        re.compile(
            r'(title:\w+\("common\.edit"\),disabled:)'
            r'(\w+)\|\|\w+\.id==="default"'
        ),
        r"\1\2",
        re.compile(r'title:\w+\("common\.edit"\),disabled:\w+,icon:'),
    ),
    # 2) detail-drawer primary edit button: unconditionally enabled.
    (
        re.compile(
            r'disabled:\w+\.id==="default",'
            r'(onClick:\(\)=>\{\w+\(null\),\w+\(\w+\)\},'
            r'children:\w+\("common\.edit"\))'
        ),
        r"disabled:!1,\1",
        re.compile(
            r'disabled:!1,onClick:\(\)=>\{\w+\(null\),\w+\(\w+\)\},'
            r'children:\w+\("common\.edit"\)'
        ),
    ),
]


def find_console_dir() -> Path:
    """Resolve the console static dir the host actually serves."""
    try:
        from qwenpaw.app import _app  # lazy: only valid inside the host

        candidate = Path(_app._CONSOLE_STATIC_DIR)
        if (candidate / "index.html").exists():
            return candidate
    except Exception:
        pass
    try:
        import qwenpaw

        candidate = Path(qwenpaw.__file__).resolve().parent / "console"
        if (candidate / "index.html").exists():
            return candidate
    except Exception:
        pass
    raise RuntimeError("console static directory not found")


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _transform(src: str):
    """Return (new_src, statuses), one status per spec:
    'applied' | 'already' | 'missing'."""
    out = src
    statuses = []
    for pattern, replacement, probe in PATCH_SPECS:
        new, count = pattern.subn(replacement, out)
        if count:
            out = new
            statuses.append("applied")
        elif probe.search(out):
            statuses.append("already")
        else:
            statuses.append("missing")
    return out, statuses


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("default-agent-edit-unlock: manifest unreadable")
    return {}


def _save_manifest(manifest: dict) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        MANIFEST_PATH,
        json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"),
    )


def _backup_file(path: Path, console_dir: Path, manifest: dict) -> None:
    """Back up path and its .gz/.br siblings once (true originals)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    rel = path.relative_to(console_dir).as_posix()
    for suffix in ("", ".gz", ".br"):
        original = Path(str(path) + suffix)
        if not original.exists():
            continue
        backup = BACKUP_DIR / (original.name + ".orig")
        key = rel + suffix
        if not backup.exists():
            shutil.copy2(original, backup)
        manifest.setdefault(key, backup.name)


def _sync_compressed(path: Path) -> None:
    """Keep precompressed siblings consistent with the written file.

    The current host compresses responses on the fly and never serves
    these siblings, but a future version might; stale copies must not
    undo the patch. .gz regenerated with stdlib; .br regenerated when
    brotli is available, otherwise parked in backups (reversible).
    """
    data = path.read_bytes()
    gz_path = Path(str(path) + ".gz")
    if gz_path.exists():
        _atomic_write(
            gz_path,
            gzip.compress(data, compresslevel=9, mtime=0),
            mode=gz_path.stat().st_mode & 0o777,
        )
    br_path = Path(str(path) + ".br")
    if br_path.exists():
        try:
            import brotli  # optional dependency

            _atomic_write(
                br_path,
                brotli.compress(data),
                mode=br_path.stat().st_mode & 0o777,
            )
        except ImportError:
            parked = BACKUP_DIR / (br_path.name + ".parked")
            if not parked.exists():
                shutil.copy2(br_path, parked)
            br_path.unlink()
            logger.info(
                "default-agent-edit-unlock: parked stale %s in backups "
                "(brotli unavailable; host does not serve .br siblings)",
                br_path.name,
            )


def _find_referencers(console_dir: Path, name: str) -> list:
    """Files whose bytes contain the chunk name (imports, vite dep
    maps, modulepreload links). Byte-level scan: fast, no decode."""
    needle = name.encode("utf-8")
    candidates = [console_dir / "index.html"]
    candidates += sorted((console_dir / "assets").glob("*.js"))
    refs = []
    for path in candidates:
        if path.name == name or not path.is_file():
            continue
        try:
            if needle in path.read_bytes():
                refs.append(path)
        except OSError:
            continue
    return refs


def _bust_refs(data: bytes, name: str, query: str) -> bytes:
    """Append/refresh the content-hash query on every reference."""
    pattern = re.compile(re.escape(name.encode("utf-8")) + rb"(\?v=[0-9a-f]{8})?")
    return pattern.sub(name.encode("utf-8") + query.encode("utf-8"), data)


def _refs_current(data: bytes, name: str, query: str) -> bool:
    """True when every occurrence of name already carries query."""
    bare = re.compile(re.escape(name.encode("utf-8")) + rb"(?!\?v=)")
    return bare.search(data) is None and (name + query).encode() in data


def _hash_query(data: bytes) -> str:
    return "?v=" + hashlib.md5(data).hexdigest()[:8]


def _entry_script_name(console_dir: Path):
    """File name of the HTML module entry script (the top of the load
    chain; its URL is remapped via the import map, never via queries
    inside downstream chunks — that would double-instantiate it)."""
    html = (console_dir / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script[^>]*type="module"[^>]*src="([^"]+)"', html)
    if not m:
        m = re.search(r'<script[^>]*src="([^"]+)"[^>]*type="module"', html)
    if not m:
        return None
    return m.group(1).split("?")[0].rsplit("/", 1)[-1]


def _propagate_busts(
    console_dir: Path,
    modified: dict,
    entry_name,
    manifest: dict,
    touched: list,
) -> None:
    """Layer 2: bust references to each modified non-entry chunk inside
    its referencers (gallery -> page chunk -> entry). The entry itself
    is terminal: its importers are handled by the import map, so no
    cyclic rewrites can occur. Already-processed files are protected
    from being rewritten again (their hashes are final)."""
    assets_dir = console_dir / "assets"
    queue = [n for n in modified if n != entry_name]
    processed = set()
    while queue:
        name = queue.pop(0)
        if name in processed or name == entry_name:
            continue
        processed.add(name)
        path = assets_dir / name
        if not path.is_file():
            continue
        query = _hash_query(path.read_bytes())
        referencers = _find_referencers(console_dir, name)
        for ref in referencers:
            if ref.name == "index.html":
                continue  # HTML is handled by the import map phase
            if ref.name in processed:
                continue  # downstream file already finalized
            data = ref.read_bytes()
            if not _refs_current(data, name, query):
                new_data = _bust_refs(data, name, query)
                _backup_file(ref, console_dir, manifest)
                _save_manifest(manifest)
                _atomic_write(ref, new_data, mode=ref.stat().st_mode & 0o777)
                _sync_compressed(ref)
                touched.append(ref)
                modified[ref.name] = True
                if not _refs_current(ref.read_bytes(), name, query):
                    raise RuntimeError(
                        f"post-write verification failed for {name} refs "
                        f"in {ref.name}"
                    )
                logger.info(
                    "default-agent-edit-unlock: cache-busted %s refs in "
                    "%s with %s",
                    name,
                    ref.name,
                    query,
                )
            # Whether or not this ref needed a rewrite, it references a
            # modified chunk, so ITS own upstream refs must be checked
            # too (e.g. v0.2.0 already busted gallery refs inside the
            # page chunk — the page->entry step still has to run).
            if (
                ref.name != entry_name
                and ref.name not in processed
                and ref.name not in queue
            ):
                queue.append(ref.name)


def _desired_importmap(console_dir: Path, manifest: dict, targets: list) -> dict:
    """Map every modified .js asset (backed up in the manifest, plus
    the gallery targets) to its content-hash URL. Only files whose
    bytes we changed need remapping; untouched chunks are correct in
    any cache."""
    names = set()
    for rel in manifest:
        if rel.startswith("assets/") and rel.endswith(".js"):
            names.add(rel[len("assets/"):])
    for g in targets:
        names.add(g.name)
    imports = {}
    for name in sorted(names):
        path = console_dir / "assets" / name
        if not path.is_file():
            continue
        query = _hash_query(path.read_bytes())
        imports["/assets/" + name] = "/assets/" + name + query
    return imports


def _install_importmap(
    console_dir: Path, imports: dict, manifest: dict, touched: list
) -> bool:
    """Layer 3: inject/refresh the import map in index.html, before the
    module entry script. Returns True when the HTML was rewritten."""
    html_path = console_dir / "index.html"
    html = html_path.read_text(encoding="utf-8")
    payload = json.dumps(
        {"imports": imports}, sort_keys=True, separators=(",", ":")
    )
    tag = f'<script type="importmap" id="{IMPORTMAP_ID}">{payload}</script>'
    existing = re.search(
        r'<script[^>]*id="' + re.escape(IMPORTMAP_ID) + r'"[^>]*>.*?</script>',
        html,
        re.S,
    )
    if existing:
        if existing.group(0) == tag:
            return False
        new_html = html[: existing.start()] + tag + html[existing.end():]
    else:
        m = re.search(r'<script[^>]*type="module"', html)
        if not m:
            raise RuntimeError("no module entry script found in index.html")
        new_html = html[: m.start()] + tag + "\n    " + html[m.start():]
    _backup_file(html_path, console_dir, manifest)
    _save_manifest(manifest)
    _atomic_write(
        html_path, new_html.encode("utf-8"), mode=html_path.stat().st_mode & 0o777
    )
    _sync_compressed(html_path)
    touched.append(html_path)
    # Verify by re-reading: map present, JSON intact, precedes entry.
    check = html_path.read_text(encoding="utf-8")
    mm = re.search(
        r'<script[^>]*id="' + re.escape(IMPORTMAP_ID) + r'"[^>]*>(.*?)</script>',
        check,
        re.S,
    )
    if not mm or json.loads(mm.group(1)) != {"imports": imports}:
        raise RuntimeError("import map verification failed")
    entry_pos = re.search(r'<script[^>]*type="module"', check)
    if not entry_pos or mm.start() > entry_pos.start():
        raise RuntimeError("import map must precede the module entry script")
    logger.info(
        "default-agent-edit-unlock: import map installed (%d entries)",
        len(imports),
    )
    return True


def _rollback(touched: list, console_dir: Path, manifest: dict) -> None:
    seen = set()
    for path in reversed(touched):
        if path in seen:
            continue
        seen.add(path)
        rel = path.relative_to(console_dir).as_posix()
        backup = BACKUP_DIR / (path.name + ".orig")
        if backup.exists():
            _atomic_write(
                path,
                backup.read_bytes(),
                mode=path.stat().st_mode & 0o777,
            )
            _sync_compressed(path)
        manifest.pop(rel, None)
    _save_manifest(manifest)


def install(console_dir: Path | None = None) -> dict:
    """Apply patch + cache busting idempotently. Returns a report."""
    kill = os.environ.get(KILL_SWITCH_ENV, "1").strip().lower()
    if kill in {"0", "false", "off", "no"}:
        restored = restore()
        logger.info(
            "default-agent-edit-unlock: kill switch active, restored %s",
            restored,
        )
        return {"kill_switch": True, "restored": restored}

    console_dir = Path(console_dir) if console_dir else find_console_dir()
    assets_dir = console_dir / "assets"
    targets = sorted(assets_dir.glob("AgentGallery-*.js"))
    report = {"console_dir": str(console_dir), "files": []}
    if not targets:
        logger.warning(
            "default-agent-edit-unlock: no AgentGallery-*.js under %s",
            assets_dir,
        )
        return report

    manifest = _load_manifest()
    touched = []
    modified = {}
    try:
        for js_path in targets:
            src = js_path.read_text(encoding="utf-8")
            patched, statuses = _transform(src)
            entry = {"file": js_path.name, "specs": statuses}
            if "missing" in statuses:
                entry["state"] = (
                    "partial-match-skipped"
                    if "applied" in statuses or "already" in statuses
                    else "pattern-drift"
                )
                logger.warning(
                    "default-agent-edit-unlock: edit-button pattern missing "
                    "in %s (%s); left untouched",
                    js_path.name,
                    statuses,
                )
                report["files"].append(entry)
                continue

            patched_bytes = patched.encode("utf-8")
            query = _hash_query(patched_bytes)
            entry["query"] = query
            gallery_dirty = "applied" in statuses

            referencers = _find_referencers(console_dir, js_path.name)
            ref_plan = []
            for ref in referencers:
                if ref.name == "index.html":
                    # HTML refs (import-map keys) are owned by layer 3;
                    # busting the bare map KEY here would corrupt it.
                    continue
                data = ref.read_bytes()
                if _refs_current(data, js_path.name, query):
                    continue
                ref_plan.append((ref, _bust_refs(data, js_path.name, query)))
            entry["referencers"] = [r.name for r in referencers]
            modified[js_path.name] = True

            if not gallery_dirty and not ref_plan:
                entry["state"] = "already-patched"
                report["files"].append(entry)
                continue

            # Back up true originals before any write.
            _backup_file(js_path, console_dir, manifest)
            for ref, _ in ref_plan:
                _backup_file(ref, console_dir, manifest)
            _save_manifest(manifest)

            if gallery_dirty:
                _atomic_write(
                    js_path,
                    patched_bytes,
                    mode=js_path.stat().st_mode & 0o777,
                )
                _sync_compressed(js_path)
                touched.append(js_path)
            for ref, new_data in ref_plan:
                _atomic_write(
                    ref, new_data, mode=ref.stat().st_mode & 0o777
                )
                _sync_compressed(ref)
                touched.append(ref)
                modified[ref.name] = True

            # Verify by re-reading everything we touched.
            check = js_path.read_text(encoding="utf-8")
            ok = all(
                not pattern.search(check) for pattern, _, _ in PATCH_SPECS
            )
            for ref, _ in ref_plan:
                if not _refs_current(
                    ref.read_bytes(), js_path.name, query
                ):
                    ok = False
            if not ok:
                raise RuntimeError("post-write verification failed")
            entry["verified"] = True
            entry["state"] = "patched"
            logger.info(
                "default-agent-edit-unlock: patched %s (edit unlocked, "
                "refs cache-busted with %s)",
                js_path.name,
                query,
            )
            report["files"].append(entry)

        # Layer 2: propagate busts up the load chain (page -> entry).
        entry_name = _entry_script_name(console_dir)
        _propagate_busts(console_dir, modified, entry_name, manifest, touched)

        # Layer 3: import map in the (always-fresh) HTML document.
        imports = _desired_importmap(console_dir, manifest, targets)
        if imports:
            written = _install_importmap(
                console_dir, imports, manifest, touched
            )
            report["importmap"] = {
                "entries": len(imports),
                "written": written,
                "mapping": imports,
            }
        report["state"] = "ok"
    except Exception as exc:
        report["state"] = "rolled-back"
        report["error"] = str(exc)
        logger.error(
            "default-agent-edit-unlock: %s; rolling back %s",
            exc,
            [p.name for p in touched],
        )
        _rollback(touched, console_dir, manifest)
    return report


def restore(console_dir: Path | None = None) -> list:
    """Restore every backed-up original. Returns restored rel paths."""
    if not BACKUP_DIR.is_dir():
        return []
    try:
        console_dir = (
            Path(console_dir) if console_dir else find_console_dir()
        )
    except Exception as exc:
        logger.warning(
            "default-agent-edit-unlock: cannot resolve console dir for "
            "restore: %s",
            exc,
        )
        return []
    manifest = _load_manifest()
    # Legacy v0.1.0 backups (gallery only, no manifest entry).
    for backup in sorted(BACKUP_DIR.glob("AgentGallery-*.orig")):
        rel = "assets/" + backup.name[: -len(".orig")]
        manifest.setdefault(rel, backup.name)
    restored = []
    for rel, backup_name in sorted(manifest.items()):
        backup = BACKUP_DIR / backup_name
        target = console_dir / rel
        if not backup.exists() or not target.parent.is_dir():
            continue
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        _atomic_write(target, backup.read_bytes(), mode=mode)
        restored.append(rel)
        logger.info("default-agent-edit-unlock: restored stock %s", rel)
    for parked in sorted(BACKUP_DIR.glob("*.parked")):
        parked.unlink()
    return restored