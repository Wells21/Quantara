# list_nasdaq_dukascopy_instruments.py

from dukascopy_python import instruments


KEYWORDS = [
    "GBPJPY",
    "GBP_JPY",
    "GBP/JPY",
    "POUND",
    "STERLING",
    "YEN",
    "JPY",
]


def instrument_to_text(obj):
    """
    Safely convert Dukascopy instrument object to readable text.
    Different versions of dukascopy_python may expose instruments slightly differently.
    """
    for attr in ("value", "name", "instrument"):
        if hasattr(obj, attr):
            try:
                return str(getattr(obj, attr))
            except Exception:
                pass

    return str(obj)


def main():
    print("Scanning dukascopy_python.instruments for NASDAQ-related instruments...\n")

    matches = []

    for name in dir(instruments):
        if not name.startswith("INSTRUMENT_"):
            continue

        obj = getattr(instruments, name)
        text = instrument_to_text(obj)

        combined = f"{name} {text}".upper()

        if any(keyword.upper() in combined for keyword in KEYWORDS):
            matches.append((name, text))

    if not matches:
        print("No NASDAQ-related instruments found.")
        return

    for name, text in matches:
        print(f"{name} -> {text}")

    print(f"\nTotal matches: {len(matches)}")


if __name__ == "__main__":
    main()

