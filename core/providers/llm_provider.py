import requests

class LLMProvider:
    def __init__(self, api_key, model="deepseek-chat"):
        self.api_key = api_key
        self.model = model
        self.url = "https://api.deepseek.com/chat/completions"

    def call(self, system_prompt, user_prompt):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": False
        }
        try:
            response = requests.post(self.url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content'], None
        except Exception as e:
            return None, str(e)
