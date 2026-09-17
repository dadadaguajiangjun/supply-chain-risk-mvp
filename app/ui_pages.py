"""Serve a consistent UI revision across all workspace navigation paths."""
import hashlib
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi.responses import HTMLResponse


STATIC_DIR = Path(__file__).resolve().parent / 'static'
PAGE_PATHS = {'/', '/data-import', '/master-data', '/data-edit', '/purchase-reconciliation'}


def ui_version() -> str:
    # Content-based so in-place updates and restarts cannot reuse an old UI URL.
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.iterdir()):
        if path.is_file() and path.suffix in {'.html', '.js', '.css'}:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def versioned_url(value: str, version: str) -> str:
    parts = urlsplit(html.unescape(value))
    if parts.netloc or parts.scheme:
        return value
    key = 'ui' if parts.path in PAGE_PATHS else 'v' if parts.path.startswith('/static/') else None
    if key is None:
        return value
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query.append((key, version))
    return html.escape(urlunsplit(('', '', parts.path, urlencode(query), parts.fragment)), quote=True)


def workspace_page(filename: str) -> HTMLResponse:
    version = ui_version()
    source = (STATIC_DIR / filename).read_text(encoding='utf-8')
    source = re.sub(r'\b(href|src)="([^"]*)"',
                    lambda m: f'{m[1]}="{versioned_url(m[2], version)}"', source)
    bootstrap = (f'<meta name="workspace-version" content="{version}">'
                 f'<script src="/static/workspace.js?v={version}"></script>')
    source = source.replace('</head>', bootstrap + '</head>', 1)
    return HTMLResponse(source, headers={'X-Workspace-Version': version})
