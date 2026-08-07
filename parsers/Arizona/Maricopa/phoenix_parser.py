"""Phoenix — Legistar OData meeting parser."""
from legistar_odata_helper import scrape_legistar_odata


def scrape_calendar(calendar_url=None):
    """Return Phoenix meeting rows from Legistar's structured OData feed."""
    # Calendar.aspx exports can return HTML; OData is keyed by jurisdiction
    # slug and provides structured event records directly.
    return scrape_legistar_odata(jurisdiction="phoenix", city_name="Phoenix")


if __name__ == "__main__":
    import json
    meetings = scrape_calendar()
    print(f"Fetched {len(meetings)} Phoenix meetings via Legistar OData")
    for m in meetings[:3]:
        print(json.dumps(m, indent=2, default=str))
