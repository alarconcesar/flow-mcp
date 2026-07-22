"""JavaScript templates for Google Flow API calls injected into Playwright pages.

All JS snippets are exported as factory functions that accept the dynamic
parameters (URLs, tokens, payloads) and return the complete JS string.
"""

from __future__ import annotations

import json
from typing import Any


# ── Project creation ──────────────────────────────────────────────────────

def create_project_js(timestamp: str) -> str:
    """Inject ``fetch()`` to create a Flow project via tRPC.

    Stores the project ID in ``window.__st = {pid: \"...\"}`` or
    ``window.__st = {error: \"...\"}`` on failure.
    """
    return f"""(async() => {{
    try {{
        const r = await fetch(
            'https://labs.google/fx/api/trpc/project.createProject',
            {{
                method: 'POST',
                headers: {{'content-type': 'application/json'}},
                body: JSON.stringify({{
                    "json": {{
                        "projectTitle": "g_{timestamp}",
                        "toolName": "TOOL_NAME_UNSPECIFIED"
                    }}
                }}),
                credentials: 'include'
            }}
        );
        const p = JSON.parse(await r.text());
        window.__st = {{pid: p.result.data.json.result.projectId}};
    }} catch(e) {{
        window.__st = {{error: e.toString()}};
    }}
}})();"""


# ── Reference image upload ────────────────────────────────────────────────

def upload_image_js(bearer: str, body: str) -> str:
    """Inject ``fetch()`` to upload a reference image.

    Stores the response in ``window.__up``.
    """
    return f"""(async() => {{
    try {{
        const r = await fetch(
            'https://aisandbox-pa.googleapis.com/v1/flow/uploadImage',
            {{
                method: 'POST',
                headers: {{
                    'Authorization': 'Bearer {bearer}',
                    'Content-Type': 'application/json;charset=UTF-8'
                }},
                body: {body}
            }}
        );
        window.__up = await r.json();
    }} catch(e) {{
        window.__up = {{error: e.toString()}};
    }}
}})();"""


# ── Batch image generation ────────────────────────────────────────────────

def generate_images_js(api_url: str, bearer: str, body_json: str) -> str:
    """Inject ``fetch()`` to call ``batchGenerateImages``.

    Handles:
    - HTTP 401 → stores ``HTTP_401:...`` for token refresh
    - HTTP 429 → stores ``HTTP_429:...`` for backoff
    - HTTP 403 → stores ``HTTP_403:...`` (no retry)
    - HTTP 5xx → stores ``HTTP_5xx:...`` for retry
    - Other errors → stores ``ERR:...``

    Success stores the raw response text in ``window.__gen``.
    """
    return f"""(async() => {{
    try {{
        const r = await fetch({json.dumps(api_url)}, {{
            method: 'POST',
            headers: {{
                'Authorization': 'Bearer {bearer}',
                'Content-Type': 'application/json;charset=UTF-8'
            }},
            body: {body_json}
        }});
        if (r.status === 401) {{
            window.__gen = 'HTTP_401:' + (await r.text());
        }} else if (r.status === 429) {{
            window.__gen = 'HTTP_429:' + (await r.text());
        }} else if (r.status === 403) {{
            window.__gen = 'HTTP_403:' + (await r.text());
        }} else if (r.status >= 500) {{
            window.__gen = 'HTTP_5XX:' + r.status + ':' + (await r.text());
        }} else {{
            window.__gen = await r.text();
        }}
    }} catch(e) {{
        window.__gen = 'ERR:' + e;
    }}
}})();"""


# ── Image upscale ─────────────────────────────────────────────────────────

def upscale_image_js(bearer: str, body: str) -> str:
    """Inject ``fetch()`` to upscale an image via ``upsampleImage``.

    Stores the base64-encoded image in ``window.__up_img``,
    or ``HTTP_<status>`` / ``ERR:...`` on failure.
    """
    return f"""(async() => {{
    try {{
        const r = await fetch(
            'https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage',
            {{
                method: 'POST',
                headers: {{
                    'Authorization': 'Bearer {bearer}',
                    'Content-Type': 'application/json;charset=UTF-8'
                }},
                body: {body}
            }}
        );
        if (r.ok) {{
            const data = await r.json();
            window.__up_img = data.encodedImage || null;
        }} else {{
            window.__up_img = 'HTTP_' + r.status;
        }}
    }} catch(e) {{
        window.__up_img = 'ERR:' + e;
    }}
}})();"""


# ─── Download image via browser fetch → data URL ──────────────────────────

DOWNLOAD_IMAGE_JS: str = """async (u) => {
    const r = await fetch(u);
    const b = await r.blob();
    return await new Promise(r => {
        const d = new FileReader();
        d.onload = () => r(d.result);
        d.readAsDataURL(b);
    });
}"""


# ─── Check auth session ───────────────────────────────────────────────────

def check_session_js(api_url: str) -> str:
    """Inject ``fetch()`` to check if the user is authenticated.

    Returns ``data.user.email`` if authed, or ``null`` otherwise.
    """
    return f"""
    async () => {{
        try {{
            const r = await fetch('{api_url}', {{
                credentials: 'include'
            }});
            const data = await r.json();
            if (data && data.user && data.user.email) {{
                return data.user.email;
            }}
            return null;
        }} catch(e) {{
            return null;
        }}
    }}
"""


# ─── Credit balance ───────────────────────────────────────────────────────

CREDIT_BALANCE_JS: str = """async () => {
    try {
        const r = await fetch(
            'https://aisandbox-pa.googleapis.com/v1/flow/creditBalance',
            { credentials: 'include' }
        );
        return await r.json();
    } catch(e) {
        return null;
    }
}"""


# ─── List existing projects (for re-use) ──────────────────────────────────

LIST_PROJECTS_JS: str = """async () => {
    try {
        const r = await fetch(
            'https://labs.google/fx/api/trpc/project.listProjects',
            { credentials: 'include' }
        );
        const data = await r.json();
        if (data && data.result && data.result.data && data.result.data.json) {
            return data.result.data.json.projects || [];
        }
        return [];
    } catch(e) {
        return [];
    }
}"""


# ─── Video generation ─────────────────────────────────────────────────────


def generate_video_js(api_url: str, bearer: str, body_json: str) -> str:
    """Inject ``fetch()`` to kick off async video generation.

    Flow uses an **async** API for video: the response immediately returns
    a media name + status ``MEDIA_GENERATION_STATUS_SCHEDULED``, and the
    caller polls ``batchCheckAsyncVideoGenerationStatus`` until the status
    becomes ``MEDIA_GENERATION_STATUS_SUCCESSFUL`` (or FAILED).

    Stores the raw response in ``window.__vid`` (success) or
    ``HTTP_<status>:...`` / ``ERR:...`` on failure.
    """
    return f"""(async() => {{
    try {{
        const r = await fetch({json.dumps(api_url)}, {{
            method: 'POST',
            headers: {{
                'Authorization': 'Bearer {bearer}',
                'Content-Type': 'application/json;charset=UTF-8'
            }},
            body: {body_json}
        }});
        if (r.status === 401) {{
            window.__vid = 'HTTP_401:' + (await r.text());
        }} else if (r.status === 429) {{
            window.__vid = 'HTTP_429:' + (await r.text());
        }} else if (r.status === 403) {{
            window.__vid = 'HTTP_403:' + (await r.text());
        }} else if (r.status >= 500) {{
            window.__vid = 'HTTP_5XX:' + r.status + ':' + (await r.text());
        }} else {{
            window.__vid = await r.text();
        }}
    }} catch(e) {{
        window.__vid = 'ERR:' + e;
    }}
}})();"""


def check_video_status_js(api_url: str, bearer: str, body_json: str) -> str:
    """Inject ``fetch()`` to poll async video generation status.

    Body is ``{"media": [{"name": "...", "projectId": "..."}]}`` (the
    ``name`` is the UUID returned by ``batchAsyncGenerateVideoText``).

    Response is JSON; ``window.__vid_status`` receives the raw body.
    """
    return f"""(async() => {{
    try {{
        const r = await fetch({json.dumps(api_url)}, {{
            method: 'POST',
            headers: {{
                'Authorization': 'Bearer {bearer}',
                'Content-Type': 'application/json;charset=UTF-8'
            }},
            body: {body_json}
        }});
        if (r.status === 401) {{
            window.__vid_status = 'HTTP_401:' + (await r.text());
        }} else if (r.status === 429) {{
            window.__vid_status = 'HTTP_429:' + (await r.text());
        }} else if (r.status === 403) {{
            window.__vid_status = 'HTTP_403:' + (await r.text());
        }} else if (r.status >= 500) {{
            window.__vid_status = 'HTTP_5XX:' + r.status + ':' + (await r.text());
        }} else {{
            window.__vid_status = await r.text();
        }}
    }} catch(e) {{
        window.__vid_status = 'ERR:' + e;
    }}
}})();"""


def get_video_url_js(name: str) -> str:
    """Inject ``fetch()`` to get a redirect URL for the generated video.

    Flow stores the actual MP4 in its media service. This calls the
    ``media.getMediaUrlRedirect`` tRPC endpoint with
    ``MEDIA_URL_TYPE_DOWNLOAD`` to obtain a redirect to the video file.

    Stored as ``window.__vid_url`` (a string) on success, or
    ``HTTP_<status>`` / ``ERR:...`` on failure.
    """
    return f"""(async() => {{
    try {{
        // tRPC v11 expects a GET with the input in the `input` query
        // param as a JSON-stringified object. mediaUrlType must be the
        // DOWNLOAD type (not THUMBNAIL) to get the full video file URL.
        const input = JSON.stringify({{
            json: {{
                name: {json.dumps(name)},
                mediaUrlType: 'MEDIA_URL_TYPE_DOWNLOAD'
            }}
        }});
        const url = 'https://labs.google/fx/api/trpc/media.getMediaUrlRedirect'
            + '?input=' + encodeURIComponent(input);
        const r = await fetch(url, {{ credentials: 'include' }});
        if (!r.ok) {{
            window.__vid_url = 'HTTP_' + r.status;
            return;
        }}
        const data = await r.json();
        // tRPC v11 wraps the result as {{ result: {{ data: <json> }} }}.
        // Older responses may return the inner object directly.
        const inner = data?.result?.data?.json
                   ?? data?.result?.data
                   ?? data?.json
                   ?? data;
        // The redirect URL lives in `mediaUrlRedirect` (string), or in
        // `url` / `redirectUrl` as a fallback for older shapes.
        window.__vid_url = inner?.mediaUrlRedirect
                        || inner?.url
                        || inner?.redirectUrl
                        || (typeof inner === 'string' ? inner : null)
                        || JSON.stringify(inner);
    }} catch(e) {{
        window.__vid_url = 'ERR:' + e;
    }}
}})();"""
