import requests
from requests.auth import HTTPBasicAuth

class JiraProvider:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip('/')
        self.auth = HTTPBasicAuth(username, password)

    def search_issues(self, jql, fields, max_results=1000):
        url = f"{self.base_url}/rest/api/2/search"
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": max_results
        }
        try:
            response = requests.get(url, auth=self.auth, params=params, timeout=30)
            response.raise_for_status()
            return response.json().get("issues", []), None
        except requests.exceptions.RequestException as e:
            return None, str(e)
