# Shopping requirements catalog

A catalog of every RFC 2119 rule on the shopping pages of the specification,
with a stable ID for each, so that the shopping tests can say which rule they
check. It is generated from a pinned commit of the specification with the
requirements extractor from the `.github` repository, plus two options the
extractor doesn't have yet (see [How it was built](#how-it-was-built)).

> The IDs are stable within this repository (see [Stable IDs](#stable-ids)).
> They are not official IDs: those will be issued in the `ucp` repository,
> and every lock entry records its `text_digest` so these IDs can be mapped to
> them.

## Contents

| Path                             | What                                                                                  |
| -------------------------------- | ------------------------------------------------------------------------------------- |
| `catalog/shopping/**/*.json`     | One file per specification page, with every rule on that page                         |
| `catalog/capabilities.json`      | Each capability: extension or not, what it extends, its schema, pages and rule counts |
| `catalog/requirements.lock.json` | Every rule ID ever issued, with the wording it was issued for                         |
| `catalog/history.json`           | The releases read, the rules only older releases state, and rules that left the pages |
| `catalog/extraction_report.json` | The extractor's diagnostics for the run                                               |
| `releases.json`                  | The releases the history is read from, at pinned commits, and where their pages were  |
| `review.json`                    | A person's decisions on 204 rules the automatic pass couldn't settle                  |
| `tools/build_catalog.py`         | Regenerates the `catalog/` folder from the inputs below                               |

## How it was built

- **Specification:** `Universal-Commerce-Protocol/ucp` at `f2626c28`, every
  page under `docs/specification/shopping/`, including `extensions/`.
- **Extractor:** `org-tools/requirements-extractor` from
  `Universal-Commerce-Protocol/.github` PR #76, at `1687b56` (version 0.4.0).
- **Two preview options:**
  - every page gets a capability from its `Capability Name` bullet, or else
    from its file path (`extensions/<page>.md` belongs to
    `dev.ucp.shopping.<page>`; a shared catalog page belongs to both catalog
    capabilities);
  - upper-case RFC 2119 keywords count even when they aren't bold.
- **Extensions:** an extension's parents come from its schema's `$defs` keys
  that name another capability.
- **Testable:** this suite plays the platform and calls a business over REST,
  so a rule is testable when it sits on a transport-neutral or REST page and
  is aimed at the business, as corrected by `review.json`.
- **Review:** decisions are keyed by page and `text_digest`, not by ID, so a
  reworded rule needs a fresh decision: a decision whose wording no longer
  matches a rule fails the build.

Each record keeps the extractor's fields, with `id` holding the stable ID;
everything this repository adds is in its `demo` object, including the
extractor's positional ID as `demo.positional_id`.

## Stable IDs

Each rule's ID, such as `REQ-SHOPPING-CART-0007`, comes from
`catalog/requirements.lock.json`, so it doesn't change when the page around it
does. The prefix comes from the page's capability (`SHOPPING-CATALOG` for the
shared catalog pages), followed by a four-digit number. A lock entry records
the rule's page, its wording and digest, any earlier digests, whether it is
active or retired, and the specification commits it was issued and retired
at.

Each run matches the rules it finds against the lock, page by page:

1. **Same wording:** a rule with the same page and `text_digest` as an entry
   keeps that entry's ID. Copies of one sentence on a page take that
   wording's IDs oldest first, in page order. A retired ID whose wording comes
   back is restored.
2. **Reworded:** a rule left over takes the ID of a still-unmatched active
   entry on the same page whose wording is at least 85% alike, most alike
   first. The entry keeps its old digests in `previous_digests`, and the run
   lists the rule as carried over so a person can confirm it's the same rule.
3. **New:** a rule still left over gets the next number for its prefix.
4. **Retired:** an active entry that matched nothing is marked `retired`. Its
   number is never issued again. A rule only older releases state also has a
   retired ID (see [Release history](#release-history)).

A rule that moves to another page is retired on the old page and gets a new
ID on the new one. If one of two identical sentences on a page is removed, the
copy that stays keeps the older ID, whichever copy was removed.

## Release history

The catalog also records which releases state each rule, so that a server
can be measured against the release it implements. `releases.json` lists the
releases, each at a pinned commit of its release branch: 2026-01-11,
2026-01-23, 2026-04-08 and 2026-08-25. The build reads each one's shopping
pages from a clone of the specification repository. The releases before
2026-08-25 have no shopping folder, so `releases.json` gives each of their
shopping pages its path today; its rules then get the capability and
testability that today's page gives them.

Releases are matched newest first, each against the wording that each rule
has in the next newer version, starting with the pinned specification:

1. the same wording on the same page, then on another page of the same
   capability;
2. the same wording under another list stem, or none, for a wording of at
   least five words (2026-01-11 has no stem for some lists);
3. a wording at least 85% alike, on the same page, then on another page of the
   same capability.

A release's rule that matches nothing is one of two things:

- **Moved out of the shopping pages:** the pinned specification states it on
  another page, such as `embedded-protocol.md`. It isn't in the catalog.
- **Removed:** only older releases state it. It gets a stable ID of its own,
  retired in the lock from the start, and `catalog/history.json` keeps its
  record: wording, level, capability and page.

Each rule's `demo.since` is the first version that states it, and
`demo.releases` says how each release states it: the page and line as the
release names them, the level, the digest, and the wording when it differs.
A removed rule also has `demo.until`, the first version that doesn't state
it. The build lists every match on a reworded wording for a person to check.
If the review finds that the meaning changed, a split in `review.json` makes
the older wording a removed rule of its own:

```json
{
  "splits": [{ "id": "REQ-SHOPPING-…", "before": "2026-08-25", "reason": "…" }]
}
```

One limit: the pinned specification renders the shared REST header rules
(`UCP-Agent` and `Idempotency-Key`) with the `header_requirements` macro,
which the extractor reads as plain text. The catalog has no rule for them on
the checkout, cart, catalog and order REST pages, and the history counts the
older releases' copies as moved out, because the lodging pages still state
them in full.

## Numbers at `f2626c28`

- 510 rules extracted: 395 bold and 115 found through capitals. The review
  found 3 aren't rules, leaving 507.
- 211 are testable by this suite, 98 of them MUST or MUST NOT.

| Capability                        | Rules | Testable | Testable MUST |
| --------------------------------- | ----: | -------: | ------------: |
| `dev.ucp.shopping.checkout`       |   250 |       76 |            35 |
| `dev.ucp.shopping.cart`           |    65 |       21 |             8 |
| `dev.ucp.shopping.catalog.lookup` |    61 |       38 |            16 |
| `dev.ucp.shopping.catalog.search` |    55 |       30 |            11 |
| `dev.ucp.shopping.fulfillment`    |    55 |       34 |            13 |
| `dev.ucp.shopping.order`          |    40 |       24 |            17 |
| `dev.ucp.shopping.buyer_consent`  |    17 |        4 |             3 |
| `dev.ucp.shopping.discount`       |     9 |        7 |             4 |

The 46 rules on the shared catalog pages count under both catalog
capabilities.

This catalog covers rules in the specification pages. Schema field
descriptions hold 86 more upper-case keyword sentences, which aren't
included.

### By release

| Version      | Shopping rules | Testable MUST stated | Pinned testable MUST first stated |
| ------------ | -------------: | -------------------: | --------------------------------: |
| `2026-01-11` |            140 |                   18 |                                12 |
| `2026-01-23` |            150 |                   19 |                                 1 |
| `2026-04-08` |            379 |                   60 |                                44 |
| `2026-08-25` |            512 |                   96 |                                39 |
| draft        |            507 |                   98 |                                 2 |

- **Removed rules:** 22, of which 8 are testable MUST rules that the review
  hasn't looked at yet. Among them are the webhook rules of the first two
  releases: payloads signed as a detached JWT in a `Request-Signature`
  header, which 2026-04-08 replaced with RFC 9421 signatures.
- **Moved out:** 72 statements in older releases: 53 now on
  `embedded-protocol.md`, and 19 header rules the shopping pages now render
  with a macro (see [Release history](#release-history)).

## Regenerate

From the repository root, with a `ucp` checkout at `f2626c28`, a `ucp` clone
that has the commits in `releases.json` (the `release/*` branches of the
`Universal-Commerce-Protocol/ucp` repository), and a `.github` checkout at
`1687b56`:

```bash
uv run --no-project requirements/tools/build_catalog.py \
  --spec-root=<ucp checkout> \
  --spec-commit=f2626c28b8288976eee5bda77f3a9b1ddc0132b1 \
  --releases-repo=<ucp clone> \
  --extractor-dir=<.github checkout>/org-tools/requirements-extractor/scripts
```

The output is deterministic: a second run writes identical bytes. Add
`--check` to write nothing and fail if any output, the lock included, would
change.

Keep `catalog/requirements.lock.json` committed with the catalog. It is the
only record of the IDs issued so far: without it, the next run would issue
every ID again, from scratch.

The `catalog/` folder is excluded from prettier and codespell in
`.pre-commit-config.yaml` so that it stays exactly as the tool writes it.
