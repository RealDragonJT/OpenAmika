"""Keep OAuth authorization codes out of HTTP access logs."""
import logging
import re

_CALLBACK_QUERY = re.compile(r"(/api/build_f/tools/oauth/callback)\?[^\s\"]+")


def _redact(value):
    return _CALLBACK_QUERY.sub(r"\1?[redacted]", value) if isinstance(value, str) else value


class OAuthAccessFilter(logging.Filter):
    """Redacts in place: Uvicorn's access formatter unpacks record.args into its five request fields, so the
    arguments keep their shape and only the callback's query string is replaced."""

    def filter(self, record):
        record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact(arg) for key, arg in record.args.items()}
        return True


def install(logger_name):
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, OAuthAccessFilter) for f in logger.filters):
        logger.addFilter(OAuthAccessFilter())
