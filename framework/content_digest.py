#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""RFC 9530 Content-Digest support for outgoing conformance requests.

The UCP message-signature spec requires a `Content-Digest` header, computed
with SHA-256 over the raw request body bytes, on every request that has a
body. Install `add_content_digest` as an httpx request event hook so every
suite sharing the client sends it.
"""

import base64
import hashlib
import httpx

CONTENT_DIGEST_HEADER = "Content-Digest"


def compute_content_digest(body: bytes) -> str:
  """Return the RFC 9530 `sha-256` Content-Digest value for `body`."""
  digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
  return f"sha-256=:{digest}:"


def add_content_digest(request: httpx.Request) -> None:
  """Set Content-Digest on an outgoing request when it has a body.

  An explicitly provided Content-Digest header is left untouched so negative
  tests can send an invalid digest.
  """
  if CONTENT_DIGEST_HEADER in request.headers:
    return
  body = request.read()
  if body:
    request.headers[CONTENT_DIGEST_HEADER] = compute_content_digest(body)
