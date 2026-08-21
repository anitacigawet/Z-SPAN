"""
Z-SPAN Pipeline — Layer 2 of the Z-SPAN architecture
====================================================

Turns ingested council meetings into the V1-RAG-3 broadcast output set:
Whisper transcription (Mac-local node) -> Qdrant indexing (Surface Pro
RAG node) -> `claude -p` Sonnet synthesis -> karaoke sidecars.

Architecture: Work Order Queue (defrag style)
    - Scanner walks recent meetings (default <=30 days old) and enqueues
      rows in the `work_orders` table.
    - Worker daemon processes one work order at a time, slowly, keeping
      every node well inside its envelope.
    - Per-meeting flow: transcribe -> index -> synthesize -> sidecars ->
      save outputs.

Modules:
    scanner.py             -- walks recent meetings, enqueues work orders
    fetcher.py             -- for one WO, dispatches all requested outputs
    qdrant_synthesizer.py  -- Qdrant retrieve + claude -p Sonnet synthesize
    qdrant_quote_extractor.py -- attributed-quote linker pass ([SYMBOLS])
    sidecar_pipeline.py    -- post-extraction stages (align -> karaoke)
    worker.py              -- long-running daemon; defrag pace

Curated synthesis prompts live in: 02_Core_Project/prompts/
"""

__version__ = "0.1.0"
