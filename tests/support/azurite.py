"""Requests to Azurite, Azure Storage's emulator, signed with its development account (#127).

The account and its key are Microsoft's documented development credentials: public and
disposable, never a secret. Only what the tests need: create a container, list its blobs, put a
blob.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import urllib.request
import xml.etree.ElementTree as ElementTree

import pytest

VARIABLE = "STORE_TEST_AZURE_CONNECTION_STRING"
VERSION = "2021-08-06"
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def connection_string() -> str:
    """Azurite's connection string, from CI (required there), or skip."""
    value = os.environ.get(VARIABLE, "")
    if not value:
        if os.environ.get("CI"):
            pytest.fail(f"{VARIABLE} is required in CI")
        pytest.skip("no Azurite configured for this run")
    return value


def _parts(connection: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in connection.strip(";").split(";"))


def request(
    connection: str,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> bytes:
    """A SharedKey-signed request to `<BlobEndpoint>/<path>`."""
    parts = _parts(connection)
    account = parts["AccountName"]
    key = base64.b64decode(parts["AccountKey"])
    endpoint = parts["BlobEndpoint"].rstrip("/")
    query = query or {}
    sent = {
        "x-ms-date": dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "x-ms-version": VERSION,
        **(headers or {}),
    }
    content_type = "application/octet-stream"
    canonical_headers = "".join(
        f"{name}:{value}\n" for name, value in sorted(sent.items()) if name.startswith("x-ms-")
    )
    # The emulator's resource names the account twice (once in the path).
    resource = f"/{account}/{account}/{path}" + "".join(
        f"\n{name}:{value}" for name, value in sorted(query.items())
    )
    length = str(len(body)) if body else ""
    # VERB, Content-Encoding, -Language, -Length, -MD5, -Type, Date, If-Modified-Since,
    # If-Match, If-None-Match, If-Unmodified-Since, Range.
    fields = [method, "", "", length, "", content_type, "", "", "", "", "", ""]
    to_sign = "\n".join(fields) + "\n" + canonical_headers + resource
    signature = base64.b64encode(hmac.new(key, to_sign.encode(), hashlib.sha256).digest())
    url = f"{endpoint}/{path}" + (
        "?" + "&".join(f"{name}={value}" for name, value in query.items()) if query else ""
    )
    prepared = urllib.request.Request(
        url,
        method=method,
        data=body,
        headers={
            **sent,
            "Authorization": f"SharedKey {account}:{signature.decode()}",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        },
    )
    with _HTTP.open(prepared, timeout=30) as response:
        content: bytes = response.read()
        return content


def create_container(connection: str, container: str) -> None:
    request(connection, "PUT", container, {"restype": "container"})


def blobs(connection: str, container: str) -> list[str]:
    listed = ElementTree.fromstring(
        request(connection, "GET", container, {"comp": "list", "restype": "container"})
    )
    return [element.text or "" for element in listed.iter("Name")]


def put_blob(connection: str, container: str, name: str, body: bytes) -> None:
    request(
        connection, "PUT", f"{container}/{name}", body=body, headers={"x-ms-blob-type": "BlockBlob"}
    )
