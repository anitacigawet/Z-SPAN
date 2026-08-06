# Reference adapters

Reference adapters prove that an existing deployment can feed the country-neutral contracts without turning its local vocabulary into kernel vocabulary.

`zspan_arizona.py` converts Z-SPAN’s normalized 11-field meeting rows into the Respawn meeting contract. It deliberately does not copy Arizona’s state, county, city, Census-gazetteer, parser-registry, or interface assumptions.

This adapter works on already normalized meeting records. It contains no source endpoints, credentials, or collection recipes.
