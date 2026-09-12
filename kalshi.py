import os
import sys
import time
import base64
import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


BASE_URL = "https://external-api.kalshi.com"
API_PREFIX = "/trade-api/v2"

API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH")


def load_private_key():
    if not PRIVATE_KEY_PATH:
        raise ValueError("KALSHI_PRIVATE_KEY_PATH is not set.")

    with open(PRIVATE_KEY_PATH, "rb") as key_file:
        return serialization.load_pem_private_key(
            key_file.read(),
            password=None
        )


PRIVATE_KEY = None


def create_signature(timestamp, method, path):
    """
    Kalshi signs:
        timestamp + HTTP_METHOD + full API path

    Query parameters must not be included.
    """
    path_without_query = path.split("?")[0]
    message = f"{timestamp}{method.upper()}{path_without_query}".encode()

    signature = PRIVATE_KEY.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )

    return base64.b64encode(signature).decode()


def authenticated_get(endpoint, params=None):
    full_path = API_PREFIX + endpoint
    timestamp = str(int(time.time() * 1000))

    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": create_signature(
            timestamp,
            "GET",
            full_path
        )
    }

    response = requests.get(
        BASE_URL + full_path,
        headers=headers,
        params=params,
        timeout=20
    )

    if not response.ok:
        print("Kalshi API error:")
        print("Status:", response.status_code)
        print("Response:", response.text)
        response.raise_for_status()

    return response.json()


def get_orderbook(ticker):
    # Order-book endpoints are public and don't require authentication.
    url = f"{BASE_URL}{API_PREFIX}/markets/{ticker}/orderbook"

    response = requests.get(url, timeout=20)

    if response.status_code == 404:
        return {"message": "Order book not found."}

    if not response.ok:
        return {
            "message": f"Order-book request failed: {response.status_code}",
            "response": response.text
        }

    return response.json()


def display_orderbook(orderbook):
    book = orderbook.get("orderbook_fp", orderbook.get("orderbook", {}))

    yes_bids = book.get("yes_dollars", book.get("yes", []))
    no_bids = book.get("no_dollars", book.get("no", []))

    print("  Best YES bids:", yes_bids[-5:] if yes_bids else "none")
    print("  Best NO bids: ", no_bids[-5:] if no_bids else "none")

    # A NO bid at n implies a YES ask at 1-n.
    if no_bids:
        try:
            best_no = max(no_bids, key=lambda level: float(level[0]))
            implied_yes_ask = 1 - float(best_no[0])
            print(f"  Implied best YES ask: ${implied_yes_ask:.4f}")
        except (TypeError, ValueError, IndexError):
            pass


def main():
    global PRIVATE_KEY

    if not API_KEY_ID:
        print("Error: KALSHI_API_KEY_ID is not set.")
        sys.exit(1)

    try:
        PRIVATE_KEY = load_private_key()

        data = authenticated_get(
            "/communications/rfqs",
            params={
                "user_filter": "self",
                "limit": 100
            }
        )
    except Exception as error:
        print(f"Failed to retrieve RFQs: {error}")
        sys.exit(1)

    rfqs = data.get("rfqs", [])

    if not rfqs:
        print("No RFQs were returned.")
        return

    # Most recent first.
    rfqs.sort(
        key=lambda rfq: rfq.get("created_ts", ""),
        reverse=True
    )

    print(f"Found {len(rfqs)} RFQs.\n")

    for number, rfq in enumerate(rfqs, start=1):
        ticker = rfq.get("market_ticker")
        legs = rfq.get("mve_selected_legs", [])

        print("=" * 70)
        print(f"{number}. Created: {rfq.get('created_ts')}")
        print(f"   Status:  {rfq.get('status')}")
        print(f"   Ticker:  {ticker}")
        print(f"   RFQ ID:  {rfq.get('id')}")

        if legs:
            print("   Combo legs:")

            for leg in legs:
                print(
                    f"     {leg.get('side', '?').upper():3} "
                    f"{leg.get('market_ticker')}"
                )
        else:
            print("   Not identified as a combo RFQ.")

        if ticker:
            print("\n   Current order book:")
            display_orderbook(get_orderbook(ticker))

        print()


if __name__ == "__main__":
    main() 