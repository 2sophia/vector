/** Validazione/normalizzazione lato client per slug e chiavi property. */

// Campi di sistema del payload Qdrant + slug: una property custom non può usarli
// (deve restare allineato a RESERVED_PROP_KEYS nel backend).
export const RESERVED_PROP_KEYS = new Set<string>([
  "job_id",
  "file_id",
  "vector_store_id",
  "filename",
  "chunk_index",
  "text",
  "headings",
  "page_numbers",
  "sophia_directory_slug",
]);

/**
 * Normalizzazione "soft" per input live: converte spazi/illegali ma NON taglia
 * i separatori di bordo, così l'utente può continuare a digitare.
 */
export function softSlug(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s_-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-");
}

export function softPropKey(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s_-]/g, "")
    .replace(/[\s-]+/g, "_")
    .replace(/_+/g, "_");
}
