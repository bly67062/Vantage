class BaseModule:
    """
    Every Vantage tracker module inherits from this.
    Defines the contract that app.py expects.
    """

    # Display name shown on the dashboard card
    name = "Base Module"
    
    # How often to run in seconds (3600 = 1 hour)
    interval = 3600

    def fetch(self):
        """
        Fetch data from the source (API, web, etc.)
        Store results internally on self.
        """
        raise NotImplementedError

    def status(self):
        """
        Return current state as a dict for the dashboard card.
        Must always return a dict.
        """
        raise NotImplementedError

    def check_alert(self):
        """
        Return True if conditions warrant an alert, False otherwise.
        """
        return False