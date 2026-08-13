#!/usr/bin/env python3
"""
Fill a CodeChecker server's SQLAlchemy compiled-statement caches.

Each distinct COUNT of --file / --checker-name / --checker-msg values makes
process_report_filter() emit a different number of ILIKE terms, i.e. a
structurally different SQL statement, i.e. a new compiled-cache key. The
engine cache is per product and per API handler process, and CodeChecker
never sets `query_cache_size` (SQLAlchemy default: 500, evicting at 750).

Patterns are non-matching wildcards: they force the ILIKE path while keeping
result sets empty, so the server does the compile work and little else.

    ./fill_query_cache.py --url localhost:8001/Default -n 200

Watch the server's API handler processes grow, e.g.:
    watch -n5 'ps -o pid,rss,cmd -C python3 | grep CodeChecker'
"""

import argparse
import itertools
import subprocess

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="localhost:8001/Default2",
                help="product URL (default: localhost:8001/Default2)")
ap.add_argument("-n", "--count", type=int, default=200,
                help="number of unique filters to issue (max 336)")
ap.add_argument("--run-glob", default="*", help="run name glob (default: *)")
args = ap.parse_args()

shapes = itertools.product(range(1, 8), range(0, 6), range(0, 4),
                           ("off", "on"))

for i, (n_f, n_c, n_m, uniq) in enumerate(
        itertools.islice(shapes, args.count), start=1):
    cmd = ["CodeChecker", "cmd", "results", args.run_glob,
           "--url", args.url, "-o", "json", "--uniqueing", uniq]
    for flag, count, tag in (("--file", n_f, "f"),
                             ("--checker-name", n_c, "c"),
                             ("--checker-msg", n_m, "m")):
        if count:
            cmd += [flag] + [f"*nomatch_{tag}{j}*" for j in range(count)]
    rc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False).returncode
    print(f"{i}/{args.count}  files={n_f} checkers={n_c} msgs={n_m} "
          f"uniqueing={uniq}  {'ok' if rc == 0 else 'FAILED'}", flush=True)
