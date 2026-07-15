from apscheduler.schedulers.background import BackgroundScheduler
import logging

logger = logging.getLogger(__name__)

class VantageScheduler:
    """
    Manages the fetch schedule for all loaded modules.
    Each module defines its own interval.
    """

    def __init__(self, modules):
        self.scheduler = BackgroundScheduler()
        self.modules = modules

        for mod in self.modules:
            self.scheduler.add_job(
                func=self._run_module,
                trigger='interval',
                seconds=mod.interval,
                args=[mod],
                id=mod.name,
                replace_existing=True
            )

    def _run_module(self, mod):
        """Fetch data and check alert threshold for a module."""
        try:
            mod.fetch()
            if mod.check_alert():
                self._fire_alert(mod)
        except Exception as e:
            logger.error(f"[{mod.name}] Error during fetch: {e}")

    def _fire_alert(self, mod):
        """
        Called when check_alert() returns True.
        Sunrise/sunset handles its own ntfy push internally.
        This is the hook for any future cross-module alert logic.
        """
        logger.info(f"[ALERT] {mod.name} threshold crossed")

    def start(self):
        """Start the scheduler and immediately run all modules once."""
        self.scheduler.start()
        print(f"Scheduler started with {len(self.modules)} module(s)")

        # Run every module immediately so the dashboard has data right away
        # rather than waiting for the first interval to fire
        for mod in self.modules:
            print(f"  Initial fetch: {mod.name}...")
            self._run_module(mod)

    def stop(self):
        self.scheduler.shutdown()