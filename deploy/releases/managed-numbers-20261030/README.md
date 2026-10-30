# Managed-number paid allowance candidate

Artifact-only incoming release, based on server HEAD d9cc7ca. It does not replace dirty server source or configuration. Runtime source is canonical main 8ab232a; only the two reviewed number paths differ from the running ba0623 image. The image preserves its 212 base layers and passes 86 offline checks plus application import. No provider calls were made.

Requires the source-verified migration 20270690001024 and subscription-managed-number bridge before the image switch. Apply only after rechecking current image IDs, complete active Compose configuration, preserved dirty state, no active audio calls and explicit release approval. Verify both service images, eighteen source hashes, health and unchanged non-image configuration afterward. Public signup and catalog acceptance remain separate.
