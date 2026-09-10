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

"""Shared constants for the conformance framework.

Kept in a dependency-free module so both `base_test` and the servers it
starts can reference them without introducing a circular import.
"""

# Fallback UCP release the suite asserts against when the merchant's
# conformance input does not declare a `ucp_version`.
DEFAULT_UCP_VERSION = "2026-04-08"
