# Taiwan Day/Week Alignment Scanner

This repository runs the Taiwan full-market scanner in GitHub Actions and publishes the verified result to GitHub Pages.

## Schedule

- 06:10 Asia/Taipei
- 14:10 Asia/Taipei

## Inclusion rule

Every published row must satisfy all of the following on both daily and weekly timeframes:

- Close above EMA200
- `+DI > -DI`
- ADX above 30

The workflow keeps the last successful website when scanning or validation fails.
