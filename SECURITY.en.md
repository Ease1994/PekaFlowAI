# Security

[中文](SECURITY.md) · [English](SECURITY.en.md)

Do not report security bugs in public Issues, Discussions, or Pull Requests.

## How to report

1. Use the repo **Security → Report a vulnerability** (private Advisory).
2. Describe impact, steps to reproduce, and the risk as you see it. Include a PoC if you have one.

We will confirm as soon as we can. Please keep details private until a fix is out.

## When you deploy

- Set `BOOTSTRAP_ADMIN_PASSWORD` in production. Do not keep the demo password `admin123`.
- Do not commit a filled `.env`, `data/.secrets.json`, or JWT / AES keys.
- API docs at `/docs` are off by default. Do not expose them to the public internet.
- An empty release manifest is not packed as everything; a step with no file list is refused.
