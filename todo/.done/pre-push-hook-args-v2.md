The scaffold-generated `.git/hooks/pre-push` hook passes `"$@"` to `rlsbl pre-push-check`:

```bash
exec rlsbl pre-push-check "$@"
```

Git passes the remote name and URL as arguments to pre-push hooks. Since rlsbl uses strictcli, these extra positional arguments cause a parse error ("unknown argument 'origin'").

The fix: the scaffold template should generate `exec rlsbl pre-push-check` without `"$@"`. Existing hooks should be updated by `rlsbl scaffold --update`.

Found in: codehome, go-toml-edit (fixed manually). Previously also in howmuchleft, migrable (fixed manually).
