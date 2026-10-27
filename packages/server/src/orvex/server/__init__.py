"""Compose the modules; serve the HTTP API, the CLI and the web application."""

from importlib.metadata import version

from orvex.server.identity import load_identity

# The server's distribution is named after the product slug (identity.toml).
__version__ = version(load_identity().slug)
