export const AVATAR_PALETTE_SIZE = 8;

export function getAvatarPaletteIndex(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return hash % AVATAR_PALETTE_SIZE;
}
