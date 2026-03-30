import { BatchAction } from "@/apis/raw/subtitles";

export const SUBTITLE_TOOL_ACTIONS: [BatchAction, string][] = [
  ["OCR_fixes", "OCR Fixes"],
  ["common", "Common Fixes"],
  ["remove_HI", "Remove Hearing Impaired"],
  ["remove_tags", "Remove Style Tags"],
  ["fix_uppercase", "Fix Uppercase"],
  ["reverse_rtl", "Reverse RTL"],
];
