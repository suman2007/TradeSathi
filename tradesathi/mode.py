"""Which mode the app runs in.

owner  - ONE person's private tool: holdings / buy list / paper trades are stored (local files or Google Sheets)
         and a background engine analyses them. Use it on your own computer or a password-protected deployment.
public - for a URL shared with everyone: nothing is stored on the server. Each visitor's lists live only in their own
         browser tab (download / upload to keep them), so visitors can never see each other's shares.

Decided by (first match wins): environment variable TRADESATHI_MODE, the `mode` app secret, then automatically:
Streamlit Community Cloud (code mounted under /mount/src) -> public, anything else -> owner.
Defaulting to public on the cloud means a forgotten setting can never expose a private portfolio."""
import os


def _configured():
    value = os.environ.get("TRADESATHI_MODE")
    if not value:
        try:
            import streamlit as st
            value = st.secrets.get("mode")
        except Exception:               # no secrets file (local run)
            value = None
    return str(value).strip().lower() if value else None


def is_public():
    value = _configured()
    if value in ("public", "owner"):
        return value == "public"
    return os.path.isdir("/mount/src")
