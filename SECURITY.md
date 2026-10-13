# Security

This bot places orders with exchange API keys and exposes a password-protected dashboard. Please report anything
that could leak a key, Telegram token or dashboard session, bypass the live switch (`BINANCE_ENV=live` +
`LIVE_TRADING_CONFIRMED`), let a Telegram or dashboard control *increase* risk, skip a discipline rule or send an
order that was not intended **privately** through
[GitHub security advisories](https://github.com/thanatchon36/advantage-player/security/advisories/new), not as a
public issue.

Keys are read only from the environment or `.env`, which is git-ignored. Never paste keys, secrets or account
identifiers into issues, PRs or logs you share.
