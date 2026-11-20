# Taiwan Stock External Scheduler

Vercel Cron triggers the GitHub Actions scanner independently of GitHub's
best-effort scheduler. The function first checks the public snapshot and skips
dispatch when the requested update window is already fresh.

- Morning: 22:10 UTC (06:10 Asia/Taipei)
- Afternoon: 06:10 UTC (14:10 Asia/Taipei)
- Required production secrets: `CRON_SECRET`, `GITHUB_TOKEN`
