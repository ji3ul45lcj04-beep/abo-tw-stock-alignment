# Taiwan Day/Week Alignment Scanner

This repository runs the Taiwan full-market scanner in GitHub Actions and publishes the verified result to GitHub Pages.

## Schedule

- Primary triggers: external Vercel Cron at 06:10 and 14:10 Asia/Taipei
- GitHub backup triggers: 07:50 and 15:50 Asia/Taipei
- Full scans: at most once after 06:00 and once after 14:00; fresh backups exit early

The first successful run publishes the site. Later attempts in the same window
read the generated timestamp and skip the expensive scan when the site is fresh.

## Inclusion rule

Every published row must satisfy all of the following on both daily and weekly timeframes:

- Close above EMA200
- `+DI > -DI`
- ADX above 30

The workflow keeps the last successful website when scanning or validation fails.
