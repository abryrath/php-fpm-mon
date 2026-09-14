# php-fpm-mon

A tiny `top`-like terminal UI for watching multiple PHP-FPM pools at once.

Useful when you've got several pools behind nginx (e.g. after changing an
upstream/routing config) and want to visually confirm which pool is actually
receiving traffic, without digging through logs.

```
php-fpm pool monitor  (refresh 1.0s, q to quit)
2026-09-14 10:32:01

POOL                ACTIVE  IDLE  TOTAL  QUEUE  MAX ACT  SLOW  REQS   STATUS
api                 1       0     1      0      1        0     395    ok
cp                  1       1     2      0      2        0     452    ok
www                 3       2     5      0      4        0     1820   ok
```

## How it works

Rather than counting worker processes (which stay constant regardless of
traffic and don't tell you much), this speaks the FastCGI protocol directly
to each pool's Unix socket and queries its built-in `pm.status_path`. That
gives you live `active` / `idle` / `requests` counts per pool — numbers that
actually move when nginx sends traffic to that pool — without needing to go
through nginx or install anything extra.

Pools are auto-discovered from `/etc/php/*/fpm/pool.d/*.conf` and
`/var/run/php` / `/run/php`, or you can list them explicitly in a config
file.

## Requirements

- Python 3 (stdlib only — no dependencies)
- Each pool must have `pm.status_path` set in its pool conf, e.g.:

  ```ini
  pm.status_path = /status
  ```

  then reload/restart `php-fpm` for that pool.

## Usage

```sh
./fpm_mon.py                          # auto-discover pools, live TUI
./fpm_mon.py --interval 0.5           # faster refresh
./fpm_mon.py --once                   # one plain-text snapshot, no curses
./fpm_mon.py --config pools.ini       # explicit pool list
./fpm_mon.py --pool-dir /etc/php/8.3/fpm/pool.d --socket-dir /run/php
```

Run `./fpm_mon.py --help` for all options.

### Explicit config file

Only needed if auto-discovery doesn't fit your layout:

```ini
[www]
socket = /run/php/php8.3-fpm-www.sock
status_path = /status

[api]
socket = /run/php/php8.3-fpm-api.sock
status_path = /status
```

## Troubleshooting

A pool showing `ERROR` in the STATUS column usually means it doesn't have
`pm.status_path` configured (or fpm hasn't been reloaded since it was added).

## Contributing

Issues and PRs welcome. This is a small utility maintained on a best-effort
basis and not actively monitored — if something's broken, a PR is more
likely to get merged than an issue is to get a response.

## License

MIT — see [LICENSE](LICENSE).
