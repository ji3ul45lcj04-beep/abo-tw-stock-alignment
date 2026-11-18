# Taiwan Day/Week Alignment Scanner

This repository runs the Taiwan full-market scanner in GitHub Actions and publishes the verified result to GitHub Pages.

## Schedule

- Morning attempts: 06:10, 06:30, 06:50 Asia/Taipei
- Afternoon attempts: 14:10, 14:30, 14:50 Asia/Taipei

The first successful run publishes the site. Later attempts in the same window
read the generated timestamp and skip the expensive scan when the site is fresh.

## Inclusion rule

Every published row must satisfy all of the following on both daily and weekly timeframes:

- Close above EMA200
- `+DI > -DI`
- ADX above 30

The workflow keeps the last successful website when scanning or validation fails.
