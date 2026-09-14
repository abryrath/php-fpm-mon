#!/usr/bin/env python3
"""
fpm_mon.py - tiny top-like TUI for watching multiple php-fpm pools at once.

Queries each pool's built-in status page (pm.status_path) directly over its
unix socket using the FastCGI protocol, so it works whether or not nginx is
in front of it. Useful for confirming which pool actually receives traffic
after changing an nginx upstream/routing config -- watch "active" and
"requests" tick up on the pool you expect while you hit it.

Each pool must have `pm.status_path` set in its pool conf (e.g.
`pm.status_path = /status`) and the fpm service reloaded. If a pool has no
status_path configured, this tool will show it as unreachable rather than
guessing one for you.

Usage:
    ./fpm_mon.py                          # auto-discover pools
    ./fpm_mon.py --interval 0.5
    ./fpm_mon.py --config pools.ini       # explicit pool list
    ./fpm_mon.py --pool-dir /etc/php/8.3/fpm/pool.d --socket-dir /run/php

Config file format (INI), only needed if auto-discovery doesn't fit your
layout:

    [www]
    socket = /run/php/php8.3-fpm-www.sock
    status_path = /status

    [api]
    socket = /run/php/php8.3-fpm-api.sock
    status_path = /status
"""
import argparse
import configparser
import curses
import glob
import json
import os
import re
import socket
import struct
import sys
import time

# --- minimal FastCGI client (just enough to hit a status page) ------------

FCGI_VERSION_1 = 1
FCGI_BEGIN_REQUEST = 1
FCGI_PARAMS = 4
FCGI_STDIN = 5
FCGI_STDOUT = 6
FCGI_STDERR = 7
FCGI_END_REQUEST = 3
FCGI_RESPONDER = 1
REQUEST_ID = 1

HEADER_FMT = ">BBHHBx"  # version, type, requestId, contentLength, paddingLength, reserved


def _pack_header(rec_type, content_len):
    return struct.pack(HEADER_FMT, FCGI_VERSION_1, rec_type, REQUEST_ID, content_len, 0)


def _pack_nv(name, value):
    def _len(n):
        return struct.pack(">B", n) if n < 128 else struct.pack(">I", n | 0x80000000)

    name_b = name.encode()
    value_b = str(value).encode()
    return _len(len(name_b)) + _len(len(value_b)) + name_b + value_b


def _pack_params(params):
    body = b"".join(_pack_nv(k, v) for k, v in params.items())
    return _pack_header(FCGI_PARAMS, len(body)) + body + _pack_header(FCGI_PARAMS, 0)


def fcgi_get(sock_path, script_path, query_string="", timeout=1.5):
    """Fetch a status path over a php-fpm unix socket, return raw body text."""
    params = {
        "SCRIPT_FILENAME": script_path,
        "SCRIPT_NAME": script_path,
        "QUERY_STRING": query_string,
        "REQUEST_METHOD": "GET",
        "GATEWAY_INTERFACE": "CGI/1.1",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "SERVER_SOFTWARE": "fpm_mon",
        "CONTENT_LENGTH": "0",
    }

    begin_body = struct.pack(">HB5x", FCGI_RESPONDER, 0)
    payload = (
        _pack_header(FCGI_BEGIN_REQUEST, len(begin_body))
        + begin_body
        + _pack_params(params)
        + _pack_header(FCGI_STDIN, 0)
    )

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(payload)

        out = bytearray()
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:
                _ver, rtype, _reqid, clen, plen = struct.unpack(HEADER_FMT, buf[:8])
                total = 8 + clen + plen
                if len(buf) < total:
                    break
                content = buf[8:8 + clen]
                buf = buf[total:]
                if rtype == FCGI_STDOUT:
                    out += content
                elif rtype == FCGI_END_REQUEST:
                    s.close()
                    return _strip_cgi_headers(bytes(out).decode(errors="replace"))
                elif rtype == FCGI_STDERR:
                    pass
    finally:
        try:
            s.close()
        except OSError:
            pass
    return _strip_cgi_headers(bytes(out).decode(errors="replace"))


def _strip_cgi_headers(text):
    # CGI response is "Header: value\r\n" lines, blank line, then body.
    sep = "\r\n\r\n" if "\r\n\r\n" in text else "\n\n"
    if sep in text:
        return text.split(sep, 1)[1]
    return text


# --- pool discovery ---------------------------------------------------------

POOL_HEADER_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
LISTEN_RE = re.compile(r"^\s*listen\s*=\s*(?P<val>.+?)\s*$")
STATUS_PATH_RE = re.compile(r"^\s*pm\.status_path\s*=\s*(?P<val>.+?)\s*$")


def discover_pools(pool_dirs, socket_dirs, default_status_path):
    """Parse php-fpm pool.d/*.conf files for pool name + listen socket."""
    pools = {}
    conf_files = []
    for d in pool_dirs:
        conf_files.extend(glob.glob(f"{d}/*.conf"))

    for path in sorted(conf_files):
        name = None
        listen = None
        status_path = default_status_path
        try:
            with open(path) as f:
                for line in f:
                    line = line.split(";", 1)[0]  # strip comments
                    m = POOL_HEADER_RE.match(line)
                    if m and m.group("name") != "global":
                        name = m.group("name")
                        continue
                    m = LISTEN_RE.match(line)
                    if m:
                        listen = m.group("val")
                        continue
                    m = STATUS_PATH_RE.match(line)
                    if m:
                        status_path = m.group("val")
        except OSError:
            continue

        if not name or not listen:
            continue
        if listen.startswith("/") or listen.startswith("./"):
            sock_path = listen
        else:
            # TCP listener (host:port) -- not supported by this simple client
            continue
        pools[name] = {"socket": sock_path, "status_path": status_path}

    # Dedupe by resolved real path: /var/run is commonly a symlink to /run
    # (and multiple pool confs could theoretically point at one socket), so
    # comparing raw path strings would let the same socket in twice.
    known_reals = {os.path.realpath(p["socket"]) for p in pools.values()}

    # fall back: any sockets sitting in socket_dirs that weren't matched above
    for d in socket_dirs:
        for sock_path in glob.glob(f"{d}/*.sock"):
            real = os.path.realpath(sock_path)
            if real in known_reals:
                continue
            known_reals.add(real)
            guess_name = sock_path.rsplit("/", 1)[-1]
            pools.setdefault(guess_name, {"socket": sock_path, "status_path": default_status_path})

    return pools


def load_config(path):
    cp = configparser.ConfigParser()
    cp.read(path)
    pools = {}
    for section in cp.sections():
        pools[section] = {
            "socket": cp[section]["socket"],
            "status_path": cp[section].get("status_path", "/status"),
        }
    return pools


# --- status query ------------------------------------------------------------

FIELDS = ["active processes", "idle processes", "total processes",
          "listen queue", "max listen queue", "max active processes",
          "slow requests", "accepted conn"]

FIELD_ALIASES = {
    "active processes": "active",
    "idle processes": "idle",
    "total processes": "total",
    "listen queue": "queue",
    "max listen queue": "max queue",
    "max active processes": "max active",
    "slow requests": "slow",
    "accepted conn": "requests",
}


def query_pool(name, info):
    try:
        body = fcgi_get(info["socket"], info["status_path"], query_string="json")
        data = json.loads(body)
        return {"name": name, "ok": True, "data": data}
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI
        return {"name": name, "ok": False, "error": str(e)}


# --- curses UI ---------------------------------------------------------------

COLS = ["POOL", "ACTIVE", "IDLE", "TOTAL", "QUEUE", "MAX ACT", "SLOW", "REQS", "STATUS"]
COL_WIDTH = [20, 8, 6, 7, 7, 8, 6, 10, 30]


def draw(stdscr, results, interval, error_pools):
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    title = f"php-fpm pool monitor  (refresh {interval}s, q to quit)"
    stdscr.addnstr(0, 0, title, max_x - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, time.strftime("%Y-%m-%d %H:%M:%S"), max_x - 1)

    header_row = 3
    x = 0
    for col, w in zip(COLS, COL_WIDTH):
        if x >= max_x:
            break
        stdscr.addnstr(header_row, x, col.ljust(w), max_x - x - 1, curses.A_UNDERLINE)
        x += w

    row = header_row + 1
    for r in sorted(results, key=lambda r: r["name"]):
        if row >= max_y - 1:
            break
        x = 0
        attr = curses.A_NORMAL

        if not r["ok"]:
            fields = [r["name"], "-", "-", "-", "-", "-", "-", "-", f"ERROR: {r['error'][:26]}"]
            attr = curses.color_pair(1) if curses.has_colors() else curses.A_DIM
        else:
            d = r["data"]
            active = d.get("active processes", "?")
            fields = [
                r["name"],
                str(active),
                str(d.get("idle processes", "?")),
                str(d.get("total processes", "?")),
                str(d.get("listen queue", "?")),
                str(d.get("max active processes", "?")),
                str(d.get("slow requests", "?")),
                str(d.get("accepted conn", "?")),
                "ok",
            ]
            if isinstance(active, int) and active > 0:
                attr = curses.color_pair(2) if curses.has_colors() else curses.A_BOLD

        for val, w in zip(fields, COL_WIDTH):
            if x >= max_x:
                break
            stdscr.addnstr(row, x, str(val).ljust(w), max_x - x - 1, attr)
            x += w
        row += 1

    if error_pools:
        row += 1
        if row < max_y - 1:
            stdscr.addnstr(
                row, 0,
                "Hint: pools with errors likely need `pm.status_path = /status` set + fpm reloaded.",
                max_x - 1, curses.A_DIM,
            )

    stdscr.refresh()


def run(stdscr, pools, interval):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(int(interval * 1000))
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)

    while True:
        results = [query_pool(name, info) for name, info in pools.items()]
        error_pools = [r["name"] for r in results if not r["ok"]]
        draw(stdscr, results, interval, error_pools)

        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds (default: 1.0)")
    ap.add_argument("--config", help="INI file explicitly listing pools (see --help for format)")
    ap.add_argument("--pool-dir", action="append", default=[],
                     help="glob dir containing pool.d/*.conf files (repeatable, default /etc/php/*/fpm/pool.d)")
    ap.add_argument("--socket-dir", action="append", default=[],
                     help="fallback dir to scan for *.sock files (repeatable, default /var/run/php, /run/php)")
    ap.add_argument("--status-path", default="/status",
                     help="default status path if a pool conf doesn't set pm.status_path (default: /status)")
    ap.add_argument("--once", action="store_true", help="print one snapshot as plain text and exit (no curses)")
    args = ap.parse_args()

    if args.config:
        pools = load_config(args.config)
    else:
        pool_dirs = args.pool_dir or glob.glob("/etc/php/*/fpm/pool.d")
        socket_dirs = args.socket_dir or ["/var/run/php", "/run/php"]
        pools = discover_pools(pool_dirs, socket_dirs, args.status_path)

    if not pools:
        print("No php-fpm pools found. Pass --config, or --pool-dir/--socket-dir explicitly.", file=sys.stderr)
        sys.exit(1)

    if args.once:
        for r in [query_pool(name, info) for name, info in pools.items()]:
            print(r if not r["ok"] else {"name": r["name"], **r["data"]})
        return

    curses.wrapper(run, pools, args.interval)


if __name__ == "__main__":
    main()
