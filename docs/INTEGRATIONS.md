# M7 Integrations

Forge currently supports connecting a GitHub personal access token and a Vercel access token from
`/settings/integrations`. OAuth authorization and callback flows are not implemented; the current
settings flow accepts tokens directly. Connected metadata is listed at
`GET /api/v1/integrations`; connect with `POST /api/v1/integrations/{kind}/connect` and remove
with `DELETE /api/v1/integrations/{id}`.

Tokens are encrypted at rest with the existing per-user envelope encryption. API responses expose
only service kind, account label, creation time, and the final four token characters. The Deployer
gets those non-secret account descriptors and integration IDs; it never receives token contents.
Tokens are decrypted inside the provider adapter immediately before an external API request. Adapter
errors omit provider response bodies and authorization headers.

External tool risks are enforced by the shared tool pipeline. Repository creation, default-branch
updates, Vercel project creation, and deployment are `irreversible` and always require approval.
Feature-branch operations and pull requests are `reversible_external`. Vercel deployment also
requires a passing Reviewer result. The approval UI displays the selected account and exact tool
payload. `http_check` accepts public HTTPS URLs, pins the resolved public IP for TLS requests, and
reports whether the response status is 200.

Adapter tests replace the HTTP transport with local fixtures. They do not contact GitHub or Vercel.
Netlify, Cloudflare Pages, and Render currently have protocol-compatible placeholder adapters only.
