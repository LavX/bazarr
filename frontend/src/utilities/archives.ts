import { ArchiveExtractResponse } from "@/apis/raw/subtitles";

// Compressed formats the backend can extract subtitle files from (#233).
export const ARCHIVE_EXTENSIONS = [".zip", ".rar", ".7z"] as const;

export function isArchiveFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return ARCHIVE_EXTENSIONS.some((ext) => name.endsWith(ext));
}

// Rebuild a File from the base64 payload the extract endpoint returns, so it
// flows through the existing per-file upload path unchanged.
export function base64ToFile(name: string, base64: string): File {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new File([bytes], name);
}

// Expand a dropped/selected file list: archives are sent to the extractor and
// replaced by their contained subtitle files; plain files pass through. The
// name of any archive that fails to extract is returned in `errors` so the
// caller can surface it without aborting the rest.
export async function expandArchives(
  files: File[],
  extract: (file: File) => Promise<ArchiveExtractResponse>,
): Promise<{ files: File[]; errors: string[] }> {
  const out: File[] = [];
  const errors: string[] = [];
  for (const file of files) {
    if (!isArchiveFile(file)) {
      out.push(file);
      continue;
    }
    try {
      const response = await extract(file);
      for (const entry of response.files) {
        out.push(base64ToFile(entry.name, entry.content));
      }
    } catch {
      errors.push(file.name);
    }
  }
  return { files: out, errors };
}
