"""Keep OAuth authorization codes out of HTTP access logs."""
import logging
import re


class OAuthAccessFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        record.msg = re.sub(r"(/api/build_f/tools/oauth/callback)\?[^\s\"]+", r"\1?[redacted]", message)
        record.args = ()
        return True


def install(logger_name):
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, OAuthAccessFilter) for f in logger.filters):
        logger.addFilter(OAuthAccessFilter())
