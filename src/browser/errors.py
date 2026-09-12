"""Browser failures which can be retried in a fresh tab without losing progress."""

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError


def is_transient_browser_error(error: Exception) -> bool:
    if isinstance(error, (PlaywrightTimeoutError, TimeoutError)):
        return True
    if not isinstance(error, PlaywrightError):
        return False
    message = str(error).lower()
    return any(fragment in message for fragment in (
        "target crashed", "page crashed", "has been closed", "browser closed",
        "connection closed", "connection terminated", "browser disconnected",
        "econnrefused", "econnreset", "etimedout", "socket hang up",
        "net::err_", "ns_error_net_", "ns_error_connection_", "ns_error_abort",
    ))
