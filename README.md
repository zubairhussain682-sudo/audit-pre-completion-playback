# Test 6B analytical handoff

Temporary evidence repository for the synthetic ZEE5-like OTT viewership project. This repository contains analytical sidecars, not canonical or frozen marts.

Start with [the report](test6b/REPORT.md), [definitions and limitations](test6b/data/14_methods.csv), [validation](test6b/data/11_validation.csv), and the grouped summaries in `test6b/data/06_access_summary.csv` through `10_lang_alloc.csv`.

The complete Excel workbook and reproducible analysis script are in `test6b/`. Every data sheet also has a CSV copy in `test6b/data/` for text-based retrieval. `test6b/manifest.csv` records row counts, column counts, sizes, and SHA-256 checksums. CSVs preserve all exported values without display rounding; blank cells represent missing values. Use the script and workbook for type and semantic context.

The original analysis created no database tables and changed no frozen marts. This subsequent temporary-repository upload is a separate, user-authorized publication step. Statements in the original methods about no commits or pushes describe the original analytical run.

Large CSV files may require downloading and processing with tools; a GitHub connector's ability to read a repository does not guarantee that it loads every row. Before claiming complete review, verify row counts against the manifest. Read summaries for conclusions, and inspect detailed rows only when needed. No credentials or database configuration files are included.
