import requests

API_KEY = "YOUR_API_KEY"

url = "https://api.the-odds-api.com/v4/sports"

try:
    response = requests.get(
        url,
        params={"apiKey": API_KEY, "all": "true"},
        timeout=20,
    )

    print("Status:", response.status_code)
    print(response.text[:500])

except Exception:
    import traceback
    traceback.print_exc()