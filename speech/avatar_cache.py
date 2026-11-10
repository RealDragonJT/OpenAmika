"""Browser caching for local avatar assets; no model bytes are kept in server memory."""
import hashlib
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, Response


def _identity(stat):
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino


@lru_cache(maxsize=2048)
def _digest(path, identity):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if _identity(path.stat()) != identity:
        raise OSError("Avatar asset changed while hashing")
    return digest.hexdigest()


def _asset_info(path):
    stat = path.stat()
    return stat, _digest(path, _identity(stat))


def versioned_reference(reference, parent, model_dir):
    """Stamp local references only, preserving existing query parameters and fragments."""
    if not isinstance(reference, str) or not reference:
        return reference
    parts = urlsplit(reference)
    if parts.scheme or parts.netloc or parts.path.startswith("/"):
        return reference
    try:
        target = (parent / unquote(parts.path)).resolve()
        if model_dir.resolve() not in target.parents:
            return reference
        _, version = _asset_info(target)
    except (OSError, ValueError):
        return reference
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "v"]
    query.append(("v", version))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _not_modified(request, response):
    # If-None-Match takes precedence, including when it does NOT match.
    tags = request.headers.get("if-none-match")
    if tags is not None:
        return any(tag.strip() == "*" or tag.strip().removeprefix("W/") == response.headers["etag"]
                   for tag in tags.split(","))
    since = request.headers.get("if-modified-since")
    modified = response.headers.get("last-modified")
    if since and modified:
        try:
            return parsedate_to_datetime(since) >= parsedate_to_datetime(modified)
        except (TypeError, ValueError, OverflowError):
            pass
    return False


def _conditional(request, response):
    if _not_modified(request, response):
        return Response(status_code=304, headers={key: value for key, value in response.headers.items()
                                                 if key in ("etag", "last-modified", "cache-control")})
    return response


def model_response(document, request):
    # The generated document also changes when any referenced file changes.
    response = JSONResponse(document, headers={"Cache-Control": "private, no-cache"})
    response.headers["etag"] = '"' + hashlib.sha256(response.body).hexdigest() + '"'
    return _conditional(request, response)


async def asset_response(path: Path, request):
    stat, version = await run_in_threadpool(_asset_info, path)
    # Old/unversioned URLs must revalidate; never cache new bytes forever under an old version.
    policy = "private, max-age=31536000, immutable" if request.query_params.get("v") == version else "private, no-cache"
    response = FileResponse(path, stat_result=stat, headers={"Cache-Control": policy, "ETag": '"' + version + '"'})
    return _conditional(request, response)
