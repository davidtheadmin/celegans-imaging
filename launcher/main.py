"""
WormScan Launcher — entry point.

Run from the repo root:
    python launcher/main.py

Python adds launcher/ to sys.path[0] automatically, so sibling modules
(config, sync, ui) are imported without any path manipulation.
"""
import logging

import config
import sync as sync_mod
import ui as ui_mod


def main() -> None:
    config.setup_logging()
    log = logging.getLogger(__name__)
    log.info("WormScan Launcher starting")

    settings = config.load()
    status = sync_mod.SyncStatus()
    agent = sync_mod.SyncAgent(settings, status)
    agent.start()

    win = ui_mod.MainWindow(settings, agent, status)
    win.mainloop()

    agent.stop()
    agent.join(timeout=5)
    log.info("WormScan Launcher stopped")


if __name__ == "__main__":
    main()
