#!/usr/bin/env python3.11
"""
Analyze all parser files to identify required dependencies
"""
import os
import re
from pathlib import Path
from collections import defaultdict

PARSERS_DIR = Path(__file__).parent

def extract_imports(file_path):
    """Extract all import statements from a Python file"""
    imports = set()
    try:
        with open(file_path, 'r') as f:
            content = f.read()
            
            # Match "import module" and "from module import ..."
            import_pattern = r'^(?:from\s+(\S+)|import\s+(\S+))'
            for match in re.finditer(import_pattern, content, re.MULTILINE):
                module = match.group(1) or match.group(2)
                # Get the top-level package name
                top_level = module.split('.')[0]
                imports.add(top_level)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    
    return imports

def main():
    # Standard library modules (don't need to be installed)
    stdlib_modules = {
        'os', 'sys', 're', 'json', 'datetime', 'time', 'urllib', 'http',
        'xml', 'html', 'collections', 'itertools', 'functools', 'pathlib',
        'typing', 'io', 'csv', 'logging', 'argparse', 'subprocess', 'shutil',
        'tempfile', 'hashlib', 'base64', 'uuid', 'random', 'math', 'calendar'
    }
    
    # Track which parsers use which external packages
    parser_deps = defaultdict(set)
    all_external_deps = set()
    
    # Analyze each parser
    parser_files = sorted(PARSERS_DIR.glob('*_parser.py'))
    
    print("=" * 80)
    print("PARSER DEPENDENCY ANALYSIS")
    print("=" * 80)
    print(f"\nAnalyzing {len(parser_files)} parser files...\n")
    
    for parser_file in parser_files:
        imports = extract_imports(parser_file)
        external_deps = imports - stdlib_modules
        
        if external_deps:
            parser_name = parser_file.stem.replace('_parser', '').title()
            parser_deps[parser_name] = external_deps
            all_external_deps.update(external_deps)
    
    # Display results
    print("EXTERNAL DEPENDENCIES REQUIRED:")
    print("-" * 80)
    for dep in sorted(all_external_deps):
        parsers_using = [name for name, deps in parser_deps.items() if dep in deps]
        print(f"\n{dep}")
        print(f"  Used by {len(parsers_using)} parsers: {', '.join(sorted(parsers_using)[:5])}")
        if len(parsers_using) > 5:
            print(f"  ... and {len(parsers_using) - 5} more")
    
    print("\n" + "=" * 80)
    print("INSTALLATION COMMANDS")
    print("=" * 80)
    print("\nsudo pip3 install " + " ".join(sorted(all_external_deps)))
    
    # Generate requirements.txt
    requirements_path = PARSERS_DIR / 'requirements.txt'
    with open(requirements_path, 'w') as f:
        for dep in sorted(all_external_deps):
            f.write(f"{dep}\n")
    
    print(f"\n✅ requirements.txt generated at: {requirements_path}")
    print()

if __name__ == '__main__':
    main()
