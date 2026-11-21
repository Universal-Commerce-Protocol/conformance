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

"""Payment Terms helpers for the conformance tests.

Holds the per-release Payment Terms URLs and a mock agent profile server that
declares the extension, so only the suites that opt in negotiate it.
"""

import json

from fastapi.responses import JSONResponse

from framework.agent_profile_server import AgentProfileServer

PT_CAPABILITY = "dev.ucp.common.payment.terms"


def terms_release(ucp_version: str) -> dict[str, str] | None:
  """Return the Payment Terms URLs for a UCP release, or None.

  Payment Terms arrived at 2026-08-25; no earlier release defines the
  extension, so there is nothing to advertise before it and None is returned.
  The parent checkout specification also moved in that release, so its path is
  carried per release here rather than assumed, the same shape as ap2_release.
  """
  base = f"https://ucp.dev/{ucp_version}"
  if ucp_version == "2026-08-25":
    return {
      "spec": f"{base}/specification/payment/extensions/terms",
      "schema": f"{base}/schemas/common/payment_terms.json",
      "checkout_spec": f"{base}/specification/shopping/checkout",
      "checkout_schema": f"{base}/schemas/shopping/checkout.json",
    }
  return None


class TermsAgentProfileServer(AgentProfileServer):
  """The framework agent profile server plus a payment-terms profile.

  Served on its own path so only tests that opt in negotiate Payment Terms.
  A Business that supports the extension applies the selected term to every
  later total, so advertising it in the default profile would change what
  every other suite negotiates.
  """

  TERMS_PROFILE_PATH = "/profiles/shopping-agent-terms.json"

  def _setup_routes(self) -> None:
    super()._setup_routes()

    @self.app.get(self.TERMS_PROFILE_PATH, response_model=None)
    async def get_terms_profile() -> JSONResponse:
      """Return an agent profile that negotiates the payment-terms extension.

      A release that does not define Payment Terms gets the plain profile
      unchanged, so the route never advertises a capability whose spec and
      schema do not exist at that release.
      """
      content = self._profile_template.replace(
        "{webhook_port}", str(self.webhook_port)
      ).replace("{ucp_version}", self.ucp_version)
      content_dict = json.loads(content)
      terms = terms_release(self.ucp_version)
      if terms is None:
        return JSONResponse(content=content_dict)
      capabilities = content_dict["ucp"].setdefault("capabilities", {})
      # Terms extends checkout and order (terms.md "Discovery"), and the
      # intersection algorithm prunes an extension whose parent is absent
      # (core-concepts.md "Extensions"), so checkout is declared with the path
      # this release uses.
      capabilities["dev.ucp.shopping.checkout"] = [
        {
          "name": "dev.ucp.shopping.checkout",
          "version": self.ucp_version,
          "spec": terms["checkout_spec"],
          "schema": terms["checkout_schema"],
        }
      ]
      capabilities[PT_CAPABILITY] = [
        {
          "version": self.ucp_version,
          "extends": [
            "dev.ucp.shopping.checkout",
            "dev.ucp.shopping.order",
          ],
          "spec": terms["spec"],
          "schema": terms["schema"],
        }
      ]
      return JSONResponse(content=content_dict)
