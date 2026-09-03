"""Bundle the console into one self-contained HTML page.

The published demo has to be a single file, and the console is written as
native ES modules with no build step. This is the smallest thing that bridges
the two: each module is wrapped in a function that returns its exports, and
every `import { a, b } from './x.js'` becomes a destructuring of that module's
export object. Nothing is minified, renamed or transformed beyond that, so the
bundle is the shipped console, readable, with its comments intact.

The module order below is the dependency order. It is stated rather than
computed because the graph is small and a wrong guess should fail loudly.

Usage:
    python scripts/demo/bundle.py --fixture fixture.json --out demo.html
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "api" / "static"

# Dependency order. A module may only import from modules above it.
ORDER = [
    "copy.js",
    "fmt.js",
    "session.js",
    "dom.js",
    "api.js",
    "sign.js",
    "views/index.js",
    "views/verdict.js",
    "views/acceptance.js",
    "views/chain.js",
    "views/package.js",
    "views/sign.js",
    "views/document.js",
    "views/audit.js",
    "views/evidence.js",
    "views/digest.js",
    "views/monitoring.js",
    "views/spec.js",
    "views/print.js",
    "app.js",
]

IMPORT = re.compile(r"^import \{([^}]*)\} from '([^']+)';\s*$", re.M)
EXPORT_DECL = re.compile(r"^export (async function|function|const|let|class) (\w+)", re.M)
EXPORT_DEFAULT_LINE = re.compile(r"^export default [^{\n][^\n]*;\s*$", re.M)
EXPORT_DEFAULT_BLOCK = re.compile(r"^export default \{.*?\};\s*$", re.M | re.S)


def module_key(path: str) -> str:
    return "__m_" + re.sub(r"[^A-Za-z0-9]", "_", path.removesuffix(".js"))


def resolve(from_module: str, spec: str) -> str:
    base = Path(from_module).parent
    return str((base / spec).resolve().relative_to(Path(".").resolve())).replace("\\", "/")


def transform(path: str, source: str) -> str:
    imports = []
    for names, spec in IMPORT.findall(source):
        target = resolve(path, spec)
        if target not in ORDER:
            raise SystemExit(f"{path} imports {spec}, which is not in ORDER")
        if ORDER.index(target) >= ORDER.index(path):
            raise SystemExit(f"{path} imports {target}, which comes after it in ORDER")
        cleaned = ", ".join(n.strip() for n in names.split(",") if n.strip())
        imports.append(f"  const {{ {cleaned} }} = {module_key(target)};")
    source = IMPORT.sub("", source)

    exported = EXPORT_DECL.findall(source)
    source = EXPORT_DECL.sub(lambda m: f"{m.group(1)} {m.group(2)}", source)
    source = EXPORT_DEFAULT_BLOCK.sub("", source)
    source = EXPORT_DEFAULT_LINE.sub("", source)
    if re.search(r"^export ", source, re.M):
        raise SystemExit(f"{path}: an export form the bundler does not handle")

    names = ", ".join(name for _, name in exported)
    body = "\n".join("  " + line if line.strip() else "" for line in source.splitlines())
    return (
        f"// ---- {path} ----\n"
        f"const {module_key(path)} = (() => {{\n"
        + "\n".join(imports)
        + ("\n" if imports else "")
        + body
        + f"\n  return {{ {names} }};\n"
        f"}})();\n"
    )


def bundle(fixture: dict | None, banner: str) -> str:
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "styles.css").read_text()
    favicon = (STATIC / "favicon.svg").read_text()

    modules = "\n".join(transform(p, (STATIC / p).read_text()) for p in ORDER)

    body_start = html.index("<body>") + len("<body>")
    body_end = html.index("<script type=\"module\"")
    body = html[body_start:body_end]

    parts = [
        # The artifact host adds its own charset meta; a local file server may
        # not, and an em dash decoded as Latin-1 is exactly the kind of small
        # wrongness a validation tool cannot afford on its face.
        '<meta charset="utf-8">',
        "<title>ValKit Console</title>",
        # Percent-encoded, not pasted: the SVG carries double quotes, and an
        # unencoded one closes the href early and leaks the rest as page text.
        f'<link rel="icon" href="data:image/svg+xml,{quote(favicon)}">',
        "<style>", css, "</style>",
    ]
    if fixture is not None:
        fixture_js = json.dumps(fixture).replace("</", "<\\/")
        demo_banner = f'''<div class="demo-banner" role="note">
  <strong>Demo.</strong> {banner}
</div>'''
        parts += [
            "<style>",
            ".demo-banner{background:#fdf3e2;color:#7a4a00;border-bottom:2px solid #7a4a00;"
            "padding:8px 24px;font-size:13px;line-height:1.5}"
            "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]) .demo-banner"
            "{background:#2c2210;color:#e8bd6a;border-color:#e8bd6a}}"
            ":root[data-theme=\"dark\"] .demo-banner{background:#2c2210;color:#e8bd6a;border-color:#e8bd6a}"
            "@media print{.demo-banner{border:1px solid #000}}",
            "</style>",
            demo_banner,
            body,
            f"<script>globalThis.__VALKIT_FIXTURE = {fixture_js};</script>",
            "<script>", (ROOT / "scripts" / "demo" / "fixture.js").read_text(), "</script>",
        ]
    else:
        parts.append(body)

    parts += ["<script>", "'use strict';", modules, "</script>"]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, help="recorded backend responses (JSON)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--banner",
        default=(
            "Every number, document, digest and audit record on this page came from the real "
            "ValKit engine and is served exactly as recorded. Signing, registering and running "
            "change this page’s memory and nothing else: nothing done here is recorded anywhere. "
            "To run the real thing: pip install -e '.[api]' && uvicorn api.main:app"
        ),
    )
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text()) if args.fixture else None
    args.out.write_text(bundle(fixture, args.banner))
    print(f"wrote {args.out} ({args.out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
