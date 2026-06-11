"""
WormScan Launcher — entry point.

Run from the repo root:
    python launcher/main.py

Python adds launcher/ to sys.path[0] automatically, so sibling modules
(config, sync, ui, analysis) are imported without any path manipulation.
"""
import logging

import config
import sync as sync_mod
import ui as ui_mod
from analysis.motility import MotilityAgent, MotilityStatus
from analysis.crawling import CrawlingAgent, CrawlingStatus
from analysis.counting_agent import CountingAgent, CountingStatus


def main() -> None:
    config.setup_logging()
    log = logging.getLogger(__name__)
    log.info("WormScan Launcher starting")

    settings = config.load()

    status = sync_mod.SyncStatus()
    agent = sync_mod.SyncAgent(settings, status)
    agent.start()

    motility_status = MotilityStatus()
    motility_agent = MotilityAgent(settings, motility_status)
    motility_agent.start()

    crawling_status = CrawlingStatus()
    crawling_agent = CrawlingAgent(settings, crawling_status)
    crawling_agent.start()

    counting_status = CountingStatus()
    counting_agent = CountingAgent(settings, counting_status)
    counting_agent.start()

    win = ui_mod.MainWindow(
        settings, agent, status,
        motility_agent, motility_status,
        crawling_agent, crawling_status,
        counting_agent, counting_status,
    )
    win.mainloop()

    agent.stop()
    agent.join(timeout=5)
    motility_agent.stop()
    motility_agent.join(timeout=5)
    crawling_agent.stop()
    crawling_agent.join(timeout=5)
    counting_agent.stop()
    counting_agent.join(timeout=5)
    log.info("WormScan Launcher stopped")


if __name__ == "__main__":
    main()
