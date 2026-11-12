"""Command line: apigate init | create-admin | add-key | import-keys | refresh-models | set-limit | load-limits | models | grant | status | backup | serve"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from .config import Settings
from .crypto import redactor


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(redactor)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # httpx logs full request URLs at INFO


def _read_secret(prompt: str, stdin: bool) -> str:
    if stdin:
        return sys.stdin.readline().strip()
    return getpass.getpass(prompt)


async def _with_gateway(settings: Settings, fn):
    from .core import Gateway
    gw = Gateway(settings)
    await gw.start(background=False)
    try:
        return await fn(gw)
    finally:
        await gw.stop()


def _bind_uds(path: str, mode: int):
    """Create a unix listening socket with exact permissions (owner+group by default, never world)."""
    import os
    import socket
    import stat
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink() or p.exists():
        if p.is_symlink() or not stat.S_ISSOCK(p.lstat().st_mode):
            raise SystemExit(f"{path} exists and is not a socket - refusing to remove it")
        p.unlink()                                   # stale socket from a crashed run
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        s.bind(str(p))
    finally:
        os.umask(old)
    os.chmod(p, mode)
    s.listen(1024)
    return s


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="apigate", description=__doc__)
    p.add_argument("--config", help="TOML config file (or APIGATE_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the data dir, database and (if absent) master key")
    a = sub.add_parser("create-admin", help="create an administrator account")
    a.add_argument("username")
    a = sub.add_parser("add-key", help="add one provider API key")
    a.add_argument("provider")
    a.add_argument("--label", default="")
    a.add_argument("--stdin", action="store_true", help="read the key from stdin instead of prompting")
    a.add_argument("--no-verify", action="store_true")
    a = sub.add_parser("import-keys", help="import 'label:key' lines (one per line) from a file")
    a.add_argument("provider")
    a.add_argument("file")
    a.add_argument("--no-verify", action="store_true")
    a = sub.add_parser("refresh-models", help="re-discover models on all keys")
    a.add_argument("--provider")
    a = sub.add_parser("set-limit", help="add a limit rule, e.g. set-limit gemini 'gemini-*-flash' --rpm 5 --tpm 250000 --rpd 20")
    a.add_argument("provider")
    a.add_argument("pattern")
    a.add_argument("--key-id", type=int)
    a.add_argument("--scope", choices=("model", "key"), default="model")
    for m in ("rps", "rpm", "rph", "rpd", "rpw", "tps", "tpm", "tph", "tpd", "tpw", "conc"):
        a.add_argument(f"--{m}", type=int)
    a = sub.add_parser("load-limits", help="apply a TOML file of [[rule]] limit presets (idempotent), e.g. presets/gemini-free.toml")
    a.add_argument("file")
    a = sub.add_parser("models", help="list discovered models of a provider and whether any key can currently serve them")
    a.add_argument("provider", nargs="?")
    a = sub.add_parser("grant", help="add credits to a user")
    a.add_argument("username")
    a.add_argument("credits", type=float)
    sub.add_parser("status", help="print providers, keys and model counts")
    a = sub.add_parser("serve", help="run the HTTP server")
    a.add_argument("--host")
    a.add_argument("--port", type=int)
    a.add_argument("--uds", metavar="PATH", help="listen on a unix socket instead of TCP (mode --uds-mode); nginx on the same host talks to it")
    a.add_argument("--uds-mode", default="660", help="octal permissions of the unix socket (default 660)")
    a.add_argument("--fd", type=int, metavar="N", action="append", help="serve on an already-open listening socket inherited as file descriptor N (systemd socket activation: 3); repeat for several sockets (e.g. --fd 3 --fd 4)")
    a.add_argument("--health-port", type=int, metavar="PORT", help="with --uds/--fd: additionally listen on 127.0.0.1:PORT (for container health checks)")
    a.add_argument("--graceful-timeout", type=int, default=120, help="seconds in-flight requests/streams may take to finish on shutdown (default 120)")
    a = sub.add_parser("backup", help="consistent online backup of the database into <data_dir>/backups")
    a.add_argument("--keep", type=int, default=14, help="how many backups to keep")

    args = p.parse_args(argv)
    settings = Settings.load(args.config)
    _setup_logging(settings.log_level)

    if args.cmd == "init":
        async def f(gw):
            print(f"data dir : {gw.settings.data_path.resolve()}")
            print(f"database : {gw.settings.db_path.resolve()}")
            kf = gw.settings.data_path / "master.key"
            if kf.exists():
                print(f"master key: {kf.resolve()}  <- BACK THIS UP; without it stored provider keys are unrecoverable.")
                print("            On a server prefer APIGATE_MASTER_KEY from a secret store and delete the file.")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "create-admin":
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat   : "):
            print("passwords differ", file=sys.stderr)
            return 1

        async def f(gw):
            u = await gw.auth.create_user(args.username, pw, "admin")
            await gw.db.execute("UPDATE users SET unlimited=1 WHERE id=?", (u["id"],))
            await gw.db.audit(None, "admin_create_cli", args.username)
            print(f"admin '{args.username}' created (unlimited credits)")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "add-key":
        secret = _read_secret(f"{args.provider} API key: ", args.stdin)

        async def f(gw):
            out = await gw.add_provider_key(args.provider, args.label or "cli", secret, verify=not args.no_verify)
            print(f"added key #{out['id']} to '{out['provider']}' {out['fingerprint']} models={out['models']}")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "import-keys":
        async def f(gw):
            ok = bad = 0
            with open(args.file, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    label, secret = line.split(":", 1)
                    try:
                        await gw.add_provider_key(args.provider, label.strip(), secret.strip(), verify=not args.no_verify)
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        bad += 1
                        print(f"line {n} ({label.strip()}): {getattr(e, 'message', e)}", file=sys.stderr)
            print(f"imported {ok}, failed {bad}. Now delete {args.file} - the plaintext keys are in it.")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "refresh-models":
        async def f(gw):
            for prov, s in (await gw.refresh_models(args.provider)).items():
                print(f"{prov}: keys ok={s['keys_ok']} failed={s['keys_failed']} models={s['models']}")
                for e in s["errors"]:
                    print("   ", e)
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "set-limit":
        limits = {m: getattr(args, m) for m in ("rps", "rpm", "rph", "rpd", "rpw", "tps", "tpm", "tph", "tpd", "tpw", "conc") if getattr(args, m) is not None}

        async def f(gw):
            rid = await gw.set_limit_rule(None, args.provider, args.key_id, args.pattern, args.scope, limits)
            print(f"rule #{rid} saved")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "load-limits":
        import tomllib
        with open(args.file, "rb") as fh:
            doc = tomllib.load(fh)
        metrics = ("rps", "rpm", "rph", "rpd", "rpw", "tps", "tpm", "tph", "tpd", "tpw", "conc")

        async def f(gw):
            import json as _json
            existing = {(r["provider"], r["key_id"], r["pattern"], r["scope"]): r["id"] for r in await gw.db.fetchall("SELECT * FROM limit_rules")}
            for r in doc.get("rule", []):
                prov = gw.resolve_provider(r["provider"])
                scope = r.get("scope", "model")
                lim = {m: r[m] for m in metrics if m in r}
                rid = existing.get((prov, None, r["pattern"], scope))
                rid = await gw.set_limit_rule(rid, prov, None, r["pattern"], scope, lim, r.get("note", ""))
                print(f"rule #{rid}: {prov} {r['pattern']} {lim}")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "models":
        async def f(gw):
            now = gw.clock()
            prov = gw.resolve_provider(args.provider) if args.provider else None
            for e in sorted(gw.registry.entries.values(), key=lambda e: (e.provider, e.id)):
                if prov and e.provider != prov:
                    continue
                ok = gw._model_servable(e.provider, e.id, now)
                lim = ""
                pool = gw.pools.get(e.provider)
                if pool and pool.keys:
                    k0 = next(iter(pool.keys.values()))
                    lim = " ".join(f"{m}={v}" for m, (v, _) in gw.engine.effective(e.provider, k0.id, e.id).items())
                print(f"{'SERVED ' if ok else 'hidden '} {e.public_id:<52} {e.kind:<10} {lim}")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "grant":
        from .credits import to_uc

        async def f(gw):
            u = await gw.auth.get_user_by_name(args.username)
            if not u:
                print("no such user", file=sys.stderr)
                return 1
            bal = await gw.credits.adjust(u["id"], to_uc(args.credits), None, "cli grant")
            print(f"{args.username}: balance {bal / 1_000_000:.6f} credits")
        return asyncio.run(_with_gateway(settings, f)) or 0

    if args.cmd == "status":
        async def f(gw):
            for p_ in gw.status()["providers"]:
                print(f"{p_['name']:<11} {p_['kind']:<9} models={p_['models']:<4} strategy={p_['effective_strategy']:<12} keys={len(p_['keys'])}")
                for k in p_["keys"]:
                    print(f"   #{k['id']:<3} {k['label']:<20} {k['fingerprint']:<8} {k['status']:<12} enabled={k['enabled']} models={k['models']}")
        asyncio.run(_with_gateway(settings, f))
        return 0

    if args.cmd == "backup":
        import sqlite3
        import time as _t
        src = settings.db_path
        dest_dir = settings.data_path / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"gate-{_t.strftime('%Y%m%d-%H%M%S')}.db"
        a_, b_ = sqlite3.connect(src), sqlite3.connect(dest)
        with b_:
            a_.backup(b_)
        a_.close(); b_.close()
        dest.chmod(0o600)
        for old_ in sorted(dest_dir.glob("gate-*.db"))[:-max(1, args.keep)]:
            old_.unlink()
        print(f"backup written: {dest}  (the master key is NOT included - keep it separately)")
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .core import Gateway
        from .server import create_app

        app = create_app(Gateway(settings))
        log = logging.getLogger("apigate")
        # one process on purpose: limit counters live in memory (see README "Scaling")
        common = dict(proxy_headers=False, server_header=False, access_log=False, log_config=None, timeout_graceful_shutdown=args.graceful_timeout)
        if args.fd or args.uds:
            import socket
            socks = [socket.socket(fileno=n) for n in args.fd] if args.fd else [_bind_uds(args.uds, int(args.uds_mode, 8))]
            if args.health_port:
                hs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                hs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                hs.bind(("127.0.0.1", args.health_port))
                hs.listen(128)
                socks.append(hs)
            # fd= only makes uvicorn print "running on socket"; the sockets themselves are passed to run()
            server = uvicorn.Server(uvicorn.Config(app, fd=socks[0].fileno(), **common))
            server.run(sockets=socks)
            return 0
        host, port = args.host or settings.host, args.port or settings.port
        if host not in ("127.0.0.1", "localhost", "::1") and not settings.cookie_secure:
            log.warning("listening on %s without cookie_secure: put TLS in front and set APIGATE_COOKIE_SECURE=true", host)
        uvicorn.run(app, host=host, port=port, workers=1, **common)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
