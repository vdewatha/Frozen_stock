# Local preview authentication

When `ENVIRONMENT` is not `production` and no `AUTH_*_KEY` values are
configured, the API accepts this fixed demo credential:

`paper-preview-operator-key-7f3b1a9c5d8e2f6a4b0c9d1e3f5a7b9c`

This fallback is intended only for local preview. Configure distinct,
random role keys (each at least 32 characters) for every deployed
environment. Live trading remains disabled by default.