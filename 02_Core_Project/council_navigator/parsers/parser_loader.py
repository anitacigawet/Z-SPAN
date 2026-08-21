"""
Parser Loader Utility
Dynamically loads and executes city-specific calendar parsers
"""
import os
import json
import importlib.util
import sys
from typing import Dict, List, Optional, Any

# Path to the parsers directory
PARSERS_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(PARSERS_DIR, 'parser_index.json')

def load_parser_index() -> Dict[str, Any]:
    """Load the parser index JSON file.

    Returns {} when the optional runtime routing file is absent. Parser source
    code is public, but a deployment may omit its local routing and health
    configuration. A present-but-corrupt index still raises loudly.
    """
    if not os.path.exists(INDEX_FILE):
        return {}
    with open(INDEX_FILE, 'r') as f:
        return json.load(f)

def routing_index_unavailable() -> bool:
    """Return whether this deployment omits local parser routing metadata.

    Active parser implementations are public; `parser_index.json` is
    deployment-specific routing and health state. Cached and published data
    can still be served when that local file is absent.
    """
    return not os.path.exists(INDEX_FILE)

def get_parser_for_city(city_name: str) -> Optional[Any]:
    """
    Load and return the parser module for a given city
    
    Args:
        city_name: Name of the city (e.g., "Phoenix", "Mesa")
    
    Returns:
        The loaded parser module, or None if not found
    """
    index = load_parser_index()
    
    if city_name not in index:
        print(f"City '{city_name}' not found in parser index")
        return None
    
    city_info = index[city_name]
    
    if city_info['status'] == 'failed':
        print(f"Parser for '{city_name}' failed during generation: {city_info.get('error', 'Unknown error')}")
        return None
    
    parser_file = city_info.get('parser_file')
    if not parser_file:
        print(f"No parser file specified for '{city_name}'")
        return None
    
    parser_path = os.path.join(PARSERS_DIR, parser_file)
    
    if not os.path.exists(parser_path):
        print(f"Parser file not found: {parser_path}")
        return None
    
    # Dynamically load the module
    spec = importlib.util.spec_from_file_location(f"parser_{city_name}", parser_path)
    if spec is None or spec.loader is None:
        print(f"Failed to load parser spec for '{city_name}'")
        return None
    
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    
    return module

def scrape_city_calendar(city_name: str) -> List[Dict[str, Any]]:
    """
    Scrape calendar data for a given city
    
    Args:
        city_name: Name of the city (e.g., "Phoenix", "Mesa")
    
    Returns:
        List of meeting dictionaries, or empty list if scraping fails
    """
    try:
        parser = get_parser_for_city(city_name)
        if parser is None:
            return []
        
        # Get the calendar URL from the index
        index = load_parser_index()
        calendar_url = index[city_name]['calendar_url']
        
        # Call the scrape_calendar function — wrapped so EVERY parser is a
        # considerate guest of the source site: per-host pacing on custom/self-hosted
        # hosts + one neutral static UA (never a Z-SPAN-repping bot UA), enforced by
        # the harness so no parser (incl. volunteer-written ones) can omit it.
        # See DECISIONS.md § scraping-etiquette + polite_http.py.
        if hasattr(parser, 'scrape_calendar'):
            from polite_http import polite_requests
            with polite_requests():
                meetings = parser.scrape_calendar(calendar_url)
            return meetings if meetings else []
        else:
            print(f"Parser for '{city_name}' does not have a scrape_calendar function")
            return []
    
    except Exception as e:
        print(f"Error scraping calendar for '{city_name}': {str(e)}")
        import traceback
        traceback.print_exc()
        return []

def get_available_cities() -> List[str]:
    """Get list of all cities with available parsers"""
    index = load_parser_index()
    return [city for city, info in index.items() if info['status'] == 'success']

def get_city_info(city_name: str) -> Optional[Dict[str, Any]]:
    """Get metadata about a city's parser"""
    index = load_parser_index()
    return index.get(city_name)
