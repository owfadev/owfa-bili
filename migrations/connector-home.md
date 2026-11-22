# Upgrade note: connector home -> machine substrate (bili-cli 0.6.0, package 0.6.0)

Not a kernel staging declaration (the file name does not match `x-to-y.md` on purpose):
the state being moved lives on the **machine**, outside any workspace, so the move cannot
be verified or performed by the workspace-only migration machinery. This is the human
cutover recipe; the connector exposes it through explicit commands, never as a side effect.

## What changed

| | pre-0.4 / connector <= 0.5 | connector 0.6.0 |
|---|---|---|
| state directory | `~/.bili-cli/` (scattered) | `$OWFA_HOME/connectors/bili-cli/homes/<id>/` (`$OWFA_HOME` defaults to `~/.owfa`) |
| resolution | hardcoded | `.owfa/scripts/owfa_home.py` (`.owfa/docs/home.md` ABI), at run time |
| explicit override | none | `BILI_CLI_HOME=/path/to/dir` |
| old path | the only path | loud fallback only (one stderr line), plus `home migrate` |

Requires framework 0.5.2+ (the `.owfa/scripts/owfa_home.py` connector-home ABI this
release rides on; the package `compat` floor is `>= 0.5.2`). Without the helper the
connector keeps working on the loud legacy fallback but `home migrate` fails closed.

Everything inside the directory moves together and stays 0600/0700: `cookies.json`,
`dm-ledger.json`, `dm-device-id`, `api-cache.json`, `api-ban.json`, `api-requests.log`,
`login-qr.png`, `pet.json`, `pet/` (`quota.json`, `replied.json`, `dm-lock.json`,
`plan.json`, `seen.json`, `feed-search-cache.json`, `feedback.json`).

## Cutover (per machine, once — the machine owner does this)

```bash
bili-cli home status            # source=legacy, state_dir=~/.bili-cli, substrate home shown
bili-cli home migrate --dry-run # entries + exact from -> to; changes nothing
bili-cli home migrate           # move (never copy) + bind this workspace root + symlink left behind
bili-cli home status            # source=substrate, legacy_is_symlink=true
bili-cli login                  # session still valid (the credential moved with the directory)
```

Equivalent raw helper command (when the connector is not on PATH):

```bash
python3 .owfa/scripts/owfa_home.py migrate bili-cli --legacy ~/.bili-cli
```

- The legacy path stays as a **symlink** to the new home so old absolute references keep
  resolving; removing that symlink is a human cleanup act, never the kernel's or the
  connector's.
- Do not migrate twice: the second run reports `already_migrated`.
- `home migrate` refuses when `BILI_CLI_HOME` is set, when there is no legacy directory,
  or when the target home already holds state — resolve those by hand.
- Run this from the **workspace root**. In an unbound workspace `home migrate` binds the
  current directory, so starting it from a subdirectory binds that subdirectory as the
  root. `cd` to the workspace root first (or, on that first run, pass
  `--root <workspace>` if you deliberately want another anchor).
- Multiple workspaces: `home migrate` is a one-shot operation — the first run binds the
  workspace it is run from, and once the legacy path is a symlink a second run returns
  `already_migrated` and binds nothing (by design; it never re-targets). Bind another root
  explicitly through the helper:

  ```bash
  python3 .owfa/scripts/owfa_home.py bind bili-cli --root <ws>
  ```

  One home per binding — two workspaces must not drain one spool.
