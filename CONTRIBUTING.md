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
   python3 -m unittest discover -s tests    # standard library only, uses temporary folders
   bash -n install.sh start.sh
   claude plugin validate .                 # if you touched .claude-plugin/ or hooks/
   python3 demo.py                          # look at the dashboard with made-up data
   ```
   Check the dashboard loads with no errors in the browser console, in both languages and both themes. CI runs the same tests on Python 3.8 to 3.13, Linux, macOS and Windows, plus the installer in a throwaway HOME.
5. Pull request to `develop` with what changes, how it was tested and, if it touches the dashboard, a screenshot without personal data (take it from `demo.py`).

## Releases

When `develop` is stable: pull request from `develop` to `main`, merge, and tag `vX.Y.Z` with a GitHub release summarising the changes.

## Prices

If you change `prices.json`, link the official Anthropic pricing page you used in the PR.

## Translations

Dashboard texts live in the `I18N` dictionary inside `tracker.py` (`en` and `es`), and server-side texts use `tr(en, es)`. Add both languages in the same change.
