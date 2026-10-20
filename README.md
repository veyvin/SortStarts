# SortStarts

A GitHub Actions workflow that organizes your starred repositories into GitHub Star Lists, automatically and daily.

## How it works

- Runs daily (cron) or on manual trigger.
- Fetches your starred repos and existing Star Lists.
- Skips repos already present in any list.
- Classifies new repos with GitHub Models (AI) into your existing list categories.
- Creates new lists when needed (up to GitHub's 32-list cap) and assigns repos.

## Setup

1. Create a **classic** Personal Access Token with the `user` scope (fine-grained tokens cannot manage Star Lists).
   - https://github.com/settings/tokens → "Generate new token (classic)" → select `user`.
2. Add it as a repository secret named `STARS_TOKEN`:
   - Repo → Settings → Secrets and variables → Actions → New repository secret.
3. (Optional) Adjust the model via the `MODEL` env in the workflow (default `openai/gpt-4o-mini`).
4. Run manually: Actions tab → "Organize Starred Repos" → "Run workflow".

## Notes

- The workflow uses the default `GITHUB_TOKEN` for GitHub Models (requires `models: read`, already set).
- Already-organized stars are never re-processed.
- List membership is the source of truth for "organized" status.