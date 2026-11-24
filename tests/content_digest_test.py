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

"""Unit tests for the Content-Digest helper (no server required)."""

from absl.testing import absltest
from framework.content_digest import add_content_digest, compute_content_digest
import httpx


class ContentDigestTest(absltest.TestCase):
  """Tests for RFC 9530 Content-Digest generation."""

  def test_matches_rfc_9530_sha256_example(self):
    """Matches the RFC 9530 sha-256 example."""
    # RFC 9530 section 6.3 example body.
    self.assertEqual(
      compute_content_digest(b'{"hello": "world"}\n'),
      "sha-256=:RK/0qy18MlBSVnWgjwz6lZEWjP/lF5HF9bvEF8FabDg=:",
    )

  def test_hook_digests_raw_json_body_bytes(self):
    """Digests the exact bytes httpx serializes."""
    request = httpx.Request("POST", "http://test/x", json={"a": 1})
    add_content_digest(request)
    self.assertEqual(
      request.headers["Content-Digest"], compute_content_digest(request.content)
    )

  def test_hook_skips_requests_without_body(self):
    """Leaves bodiless requests untouched."""
    request = httpx.Request("GET", "http://test/x")
    add_content_digest(request)
    self.assertNotIn("Content-Digest", request.headers)

  def test_hook_skips_bodiless_post(self):
    """Leaves a POST with no body untouched."""
    request = httpx.Request("POST", "http://test/x")
    add_content_digest(request)
    self.assertNotIn("Content-Digest", request.headers)

  def test_hook_digests_non_ascii_utf8_json_body(self):
    """Digests the UTF-8 bytes of a non-ASCII JSON body."""
    request = httpx.Request(
      "POST", "http://test/x", json={"name": "Café ☕ 日本"}
    )
    add_content_digest(request)
    self.assertEqual(
      request.headers["Content-Digest"],
      compute_content_digest(request.content),
    )
    self.assertIn("Café".encode(), request.content)

  def test_hook_keeps_existing_digest(self):
    """Does not overwrite a caller-supplied digest (case-insensitive)."""
    request = httpx.Request(
      "POST",
      "http://test/x",
      json={"a": 1},
      headers={"content-digest": "sha-256=:bad=:"},
    )
    add_content_digest(request)
    self.assertEqual(request.headers["Content-Digest"], "sha-256=:bad=:")

  def test_client_event_hook_sends_header(self):
    """Sends the header on the wire through a client hook."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
      seen.update(request.headers)
      return httpx.Response(200)

    with httpx.Client(
      transport=httpx.MockTransport(handler),
      event_hooks={"request": [add_content_digest]},
    ) as client:
      client.post("http://test/x", json={"a": 1})
    self.assertEqual(
      seen["content-digest"],
      compute_content_digest(b'{"a":1}'),
    )


if __name__ == "__main__":
  absltest.main()
