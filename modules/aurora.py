from modules.base import BaseModule

class AuroraModule(BaseModule):
    name = "Aurora"
    interval = 900  # check every 15 minutes

    def __init__(self):
        self.kp_value = None
        self.alert_threshold = 4.0  # KP 4+ visible in Indiana

    def fetch(self):
        """Fetch current KP index from NOAA SWPC"""
        import requests
        url = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
        response = requests.get(url, timeout=10)
        data = response.json()
        # Most recent reading is the last item in the list
        latest = data[-1]
        self.kp_value = float(latest['kp_index'])
        print(f"[Aurora] KP index: {self.kp_value}")

    def status(self):
        return {
            "kp_value": self.kp_value,
            "threshold": self.alert_threshold,
            "alert": self.check_alert()
        }

    def check_alert(self):
        if self.kp_value is None:
            return False
        return self.kp_value >= self.alert_threshold