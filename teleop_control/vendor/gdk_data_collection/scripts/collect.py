#!/usr/bin/env python3
"""Record visuomotor episodes from an AgiBot G2 through the GDK only.

Run inside the GDK environment (see env.sh):
    source env.sh
    $GDK_PY scripts/collect.py --task "pick up the cube" --duration 30
    $GDK_PY scripts/collect.py --task "fold towel"            # interactive start/stop
    $GDK_PY scripts/collect.py --config config/default.yaml --episodes 5 --duration 20

Interactive mode: Enter starts an episode, Enter stops it, q + Enter quits.
Ctrl-C always stops cleanly and finalizes the current episode.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.config import config_as_dict, load_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "default.yaml"))
    ap.add_argument("--task", required=True,
                    help="natural-language task label stored with every episode")
    ap.add_argument("--duration", type=float, default=None,
                    help="episode length in seconds; omit for interactive start/stop")
    ap.add_argument("--episodes", type=int, default=1,
                    help="number of episodes to record (with --duration)")
    ap.add_argument("--out", default=None, help="override output_root from config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.out:
        cfg.output_root = args.out
    if not cfg.cameras:
        print("config has no cameras — recording proprio-only episodes")

    # Import after arg parsing so --help works without the GDK env.
    from collector.collector import Collector
    from collector.episode_writer import EpisodeWriter

    out_root = cfg.output_root_path
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"collecting to {out_root} (config: {args.config})")
    print("initializing GDK (DDS discovery ~3 s)...")
    collector = Collector(cfg)

    stop_event = threading.Event()

    def on_sigint(_sig, _frm):
        print("\n[collect] stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, on_sigint)

    def record_one() -> dict:
        stop_event.clear()
        writer = EpisodeWriter(out_root, cfg.cameras, cfg.jpeg_passthrough,
                               cfg.depth_png, args.task, config_as_dict(cfg))
        if args.duration is None:
            threading.Thread(target=_wait_enter, args=(stop_event,), daemon=True).start()
            print("[collect] recording — press Enter to stop")
        meta = collector.run_episode(writer, args.duration, stop_event)
        print(f"[collect] episode dir: {writer.dir}")
        return meta

    try:
        if args.duration is not None:
            for i in range(args.episodes):
                print(f"\n=== episode {i + 1}/{args.episodes} ===")
                record_one()
                if stop_event.is_set():
                    break
        else:
            while True:
                ans = input("\nEnter=start episode, q=quit > ").strip().lower()
                if ans == "q":
                    break
                record_one()
    finally:
        collector.close()
        print("[collect] done")


def _wait_enter(stop_event: threading.Event):
    try:
        input()
    except EOFError:
        return
    stop_event.set()


if __name__ == "__main__":
    main()
