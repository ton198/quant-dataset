# Reproducibility

Preparation is deterministic for a fixed set of input bytes, exclusion bytes,
lookback, and runtime dependencies. Inputs are never modified in place. CSV
outputs use stable role/date/asset ordering, and summary JSON uses sorted keys.

Migration reproducibility is independently checked by comparing SHA256 and
size for every source/target file pair. The receipt states that the migration
was copy-only; immutable records may preserve historical names inside their
contents even though active package, CLI, and documentation terminology is
responsibility-based.

Each layout migration writes a JSON receipt to
`data/provenance/migrations/<YYYY-MM-DD>_<slug>.json`. Receipt fields are
`old_path`, `new_path`, `original_sha256`, `size_bytes`, and `archive_status`,
plus `promoted_copy_of` when applicable. Consumers of archived history resolve
paths through the receipt's mapping; new builds record new paths in their
manifests.
