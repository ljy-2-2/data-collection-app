#!/usr/bin/env python3
from FusionDataCollector.collector import main

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print('采集未启动：' + str(exc))
        raise SystemExit(1)
