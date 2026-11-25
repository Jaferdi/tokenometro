# Contributing

## Branches

- `main`: released versions. It only receives merges from `develop` when cutting a release, each one tagged (`v0.1.0`, `v0.2.0`…).
- `develop`: integration. Finished work lands here.
- `feature/<topic>`, `fix/<topic>`, `docs/<topic>`: one branch per change, created from `develop`.

Neither `main` nor `develop` accept direct pushes: everything goes through a pull request.

## Workflow

1. `git switch develop && git pull`
2. `git switch -c feature/<topic>`
3. Small commits, in English, saying what changes and why.
4. Before opening the PR:
   ```bash
   python3 -m py_compile tracker.py statusline.py
   bash -n install.sh start.sh
   python3 -c "import json; json.load(open('prices.json')); json.load(open('clients.example.json'))"
   ```
   then open the dashboard and check it loads with no errors in the browser console, in both languages and both themes.
5. Pull request to `develop` with what changes, how it was tested and, if it touches the dashboard, a screenshot without personal data.

## Releases

When `develop` is stable: pull request from `develop` to `main`, merge, and tag `vX.Y.Z` with a GitHub release summarising the changes.

## Prices

If you change `prices.json`, link the official Anthropic pricing page you used in the PR.

## Translations

Dashboard texts live in the `I18N` dictionary inside `tracker.py` (`en` and `es`), and server-side texts use `tr(en, es)`. Add both languages in the same change.
