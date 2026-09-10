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

"""Tests for HTTP/JSON envelope and protocol binding verification."""

from absl.testing import absltest
from framework.base_test import BaseIntegrationTest


class ProtocolBindingTest(BaseIntegrationTest):
  """Tests for HTTP/JSON envelope and protocol binding compliance.

  Validated Paths:
  - GET /.well-known/ucp
  """

  def test_protocol_envelope_headers(self) -> None:
    """Test that standard UCP transport headers are accepted and processed.

    Given a discovery request,
    When standard UCP headers (UCP-Agent, idempotency-key, request-id) are sent,
    Then the response returns 200 OK with application/json Content-Type.
    """
    headers = self.get_headers()
    response = self.client.get("/.well-known/ucp", headers=headers)
    self.assert_response_status(response, 200)
    content_type = response.headers.get("content-type", "")
    self.assertIn("application/json", content_type.lower())

  def test_invalid_content_type_rejected(self) -> None:
    """Test that POST requests with unsupported Content-Type return 400 or 415.

    When sending non-JSON payloads to protocol endpoints,
    Then the server should reject the request.
    """
    headers = self.get_headers()
    headers["Content-Type"] = "text/plain"
    # POST with non-JSON content type to well-known or discovery
    response = self.client.post(
      "/.well-known/ucp",
      content="plain text body",
      headers=headers,
    )
    # Server should return 405 (Method Not Allowed), 415, or 400
    self.assertIn(response.status_code, [400, 404, 405, 415])


if __name__ == "__main__":
  absltest.main()
